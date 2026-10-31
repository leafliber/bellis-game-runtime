"""M0 回放：回放帧源与回放音频源，以及把一段录制喂给与录制时相同的一组插件的入口。

设计见 `docs/modules/offline-tools.md` 的“M0 回放”。两个源都不读墙钟，读者要多快就给多快；帧号、
采集时刻、变换版本与像素都与实时运行时读者拿到的相同。
"""

import asyncio
import bisect
import functools
import hashlib
import logging
import shutil
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from game_runtime._threads import in_thread
from game_runtime.adapter import ADAPTER_FILE, GAMES_DIR, AdapterError, load_adapter
from game_runtime.audio_ring import AudioRead
from game_runtime.frame_ring import RingFrame
from game_runtime.kernel import AUDIO_READ, FRAMES_READ, Catalog, Kernel, State
from game_runtime.recording.layout import LIVE_DIR, REPLAY_DIR
from game_runtime.recording.pipeline import (
    NoControl,
    perception_catalog,
    perception_entries,
    readers,
)
from game_runtime.recording.reader import Recording, RecordingError
from game_runtime.recording.results import (
    VOICE_FEED_FILE,
    Comparison,
    ResultsHub,
    compare_dirs,
    read_marks,
)
from game_runtime.voice_activity import FeedMark

logger = logging.getLogger(__name__)

BLOCKS_PER_READ: Final = 100
"""`ReplayAudio.next_blocks` 每次至多给出的块数。"""
STALL_TIMEOUT: Final = 60.0
"""回放多久没有进展即报错（秒）。"""


class _Ends:
    """读到结束的次数，以及等它达到某个数。"""

    def __init__(self) -> None:
        self.count = 0
        self._waiters: set[asyncio.Future[None]] = set()

    def add(self) -> None:
        self.count += 1
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(None)

    async def wait(self, count: int) -> None:
        while self.count < count:
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.add(waiter)
            try:
                await waiter
            finally:
                self._waiters.discard(waiter)


class ReplayFrameSource:
    """回放帧源：按帧索引的顺序逐帧解码给出，读完即结束。解码在工作线程里，校验 CRC。

    `start` 之前不给帧：插件内核按顺序激活，先激活的读者不能在订阅结果的插件激活之前就把帧读走。
    `gate` 在给出每一帧之前等待：回放给帧比实时快，宿主以此等处理得慢的下游跟上。
    """

    def __init__(
        self, recording: Recording, *, gate: Callable[[], Awaitable[None]] | None = None
    ) -> None:
        self._recording = recording
        self._gate = gate
        self._started = asyncio.Event()
        self._numbers = recording.frame_numbers
        self._ends = _Ends()
        self.position = 0
        """已给出的帧数：给出过的最靠后一帧的位置加一。"""

    @property
    def total(self) -> int:
        return len(self._numbers)

    @property
    def ended(self) -> int:
        """读到结束的次数。"""
        return self._ends.count

    def start(self) -> None:
        """开始给帧。"""
        self._started.set()

    async def next_frame(self, after: int) -> RingFrame | None:
        await self._started.wait()
        index = bisect.bisect_right(self._numbers, after)
        if index >= len(self._numbers):
            self._ends.add()
            return None
        if self._gate is not None:
            await self._gate()
        frame = await in_thread(functools.partial(self._recording.frame, index))
        self.position = max(self.position, index + 1)
        return frame

    async def wait_ended(self, count: int) -> None:
        await self._ends.wait(count)


class ReplayUnsupportedError(RuntimeError):
    """回放没有“现在”：`read_recent` 不支持。"""


class ReplayAudio:
    """回放音频源：`next_blocks` 按块号给出录下的块，读完即结束；`voice_marks` 给出录制时
    `voice-activity@1` 的喂入标记（`live/voice-feed.jsonl`），满足 `RecordedAudio`。"""

    def __init__(self, recording: Recording) -> None:
        audio = recording.meta.audio
        self._recording = recording
        self._format = None if audio is None else (audio.sample_rate, audio.channels)
        self._numbers = recording.block_numbers
        self._marks = tuple(read_marks(recording.path / LIVE_DIR / VOICE_FEED_FILE))
        self._ends = _Ends()
        self._started = asyncio.Event()
        self.position = 0

    def start(self) -> None:
        """开始给块；之前不给，理由同 `ReplayFrameSource.start`。"""
        self._started.set()

    def voice_marks(self) -> Sequence[FeedMark]:
        return self._marks

    @property
    def ended(self) -> int:
        return self._ends.count

    def read_recent(self, duration_ns: int, *, after: int = 0) -> AudioRead | None:
        raise ReplayUnsupportedError("回放中不支持 read_recent，按块读用 next_blocks")

    async def next_blocks(self, after: int) -> AudioRead | None:
        await self._started.wait()
        start = bisect.bisect_right(self._numbers, after)
        if self._format is None or start >= len(self._numbers):
            self._ends.add()
            return None
        stop = min(start + BLOCKS_PER_READ, len(self._numbers))
        blocks = await in_thread(functools.partial(self._recording.blocks, start, stop))
        self.position = max(self.position, stop)
        return AudioRead(self._format[0], self._format[1], tuple(blocks))

    async def wait_ended(self, count: int) -> None:
        await self._ends.wait(count)


def adapter_sha256(game: str, games_dir: Path | None = None) -> str:
    """`games/<id>/adapter.toml` 的 SHA-256。"""
    return hashlib.sha256(((games_dir or GAMES_DIR) / game / ADAPTER_FILE).read_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class ReplayResult:
    output: Path
    """输出目录。"""
    frames: int
    blocks: int
    comparisons: dict[str, Comparison]
    """各结果文件与录制时的实时输出的比对；录制里没有实时输出时为空。"""
    adapter_changed: bool
    """适配包与录制时不同。"""


type Progress = Callable[[int, int], None]
"""回放进度：已给出的帧数与总帧数。"""
type CatalogFactory = Callable[[ResultsHub], Catalog]
"""按 `ResultsHub` 给出插件目录，如 `perception_catalog`。"""


async def replay(
    recording: Recording,
    *,
    data_dir: Path,
    output: Path | None = None,
    games_dir: Path | None = None,
    stall_timeout: float = STALL_TIMEOUT,
    progress: Progress | None = None,
    progress_interval: float = 2.0,
    plugins: CatalogFactory | None = None,
) -> ReplayResult:
    """在录制上跑与录制时相同的一组插件，输出写进 `output`（缺省为录制目录的 `replay/`，每次先
    清空），再与 `live/` 里的实时输出比对。`plugins` 缺省为 `perception_catalog`。

    读帧与读音频的插件都读到结束后，等剧情记录处理完积压的更新（`ResultsHub.drain`），再停内核。
    剧情记录的 SQLite 在输出目录里，与实时运行的剧情记录分开。
    """
    if not recording.complete:
        raise RecordingError(f"{recording.path} 不完整（{recording.meta.state}），不能回放")
    game = recording.meta.game.id
    changed = adapter_sha256(game, games_dir) != recording.meta.game.adapter_sha256
    if changed:
        logger.warning("适配包 %s 与录制时不同，签名与剧情记录的结果可能本来就不同", game)
    try:
        adapter = load_adapter((games_dir or GAMES_DIR) / game)
    except AdapterError as error:
        raise RecordingError(f"读不出录制的适配包：{error}") from None
    out = output or recording.path / REPLAY_DIR
    if output is None:
        await asyncio.to_thread(shutil.rmtree, out, True)
    hub = ResultsHub()
    frames = ReplayFrameSource(recording, gate=hub.pace)
    audio = ReplayAudio(recording)
    entries = perception_entries(adapter, data_dir=data_dir, results_dir=out, games_dir=games_dir)
    kernel = Kernel(
        catalog=(plugins or perception_catalog)(hub), core=NoControl(), frames=frames, audio=audio
    )
    try:
        kernel.add(entries)
        await kernel.settled()
        problems = [
            f"{status.id}：{status.state.value}" + "".join(f"；{r.message}" for r in status.reasons)
            for status in kernel.snapshot()
            if status.state is not State.ACTIVE
        ]
        if problems:
            raise RecordingError("回放的插件没有全部激活：" + "，".join(problems))
        frames.start()
        audio.start()
        ended = asyncio.gather(
            frames.wait_ended(readers(entries, FRAMES_READ)),
            audio.wait_ended(readers(entries, AUDIO_READ)),
        )
        try:
            last = (-1, -1)
            last_change = time.monotonic()
            while True:
                try:
                    await asyncio.wait_for(asyncio.shield(ended), progress_interval)
                    break
                except TimeoutError:
                    pass
                now = (frames.position, audio.position)
                if progress is not None:
                    progress(frames.position, frames.total)
                if now != last:
                    last, last_change = now, time.monotonic()
                elif time.monotonic() - last_change > stall_timeout:
                    raise RecordingError(f"回放 {stall_timeout:g} 秒没有进展")
        finally:
            if not ended.done():
                ended.cancel()
                await asyncio.wait((ended,))
        if progress is not None:
            progress(frames.position, frames.total)
        if not await hub.drain(stall_timeout):
            raise RecordingError(f"剧情记录 {stall_timeout:g} 秒没有处理完积压的更新")
    finally:
        await kernel.shutdown()
    comparisons = compare_dirs(recording.path / LIVE_DIR, out)
    return ReplayResult(out, frames.total, len(recording.audio), comparisons, changed)
