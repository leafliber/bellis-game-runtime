"""录制时的实时运行：拉起实时核，帧经定序帧源进入插件与录制，声音按块录下；停止时按顺序收尾。

录制工具（`uv run record`）用它；只读运行入口同时录制时复用。设计见
`docs/modules/offline-tools.md` 的“录制工具”。不发任何输入：内核的 `CoreControl` 是 `NoControl`，
不给实时核测试设备，也不发心跳。
"""

import asyncio
import contextlib
import dataclasses
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from core_protocol.models import CaptureState
from game_runtime.adapter import Adapter
from game_runtime.audio_ring import AudioRingTornError
from game_runtime.core_link import (
    AudioConfig,
    CaptureConfig,
    CoreLink,
    CoreLinkOptions,
    StopResult,
)
from game_runtime.frame_ring import RingFrame
from game_runtime.frames import DEFAULT_BACKLOG, AdmissionPolicy, SequencedFrameSource
from game_runtime.kernel import FRAMES_READ, Kernel, State
from game_runtime.recording.layout import (
    LIVE_DIR,
    AdmissionInfo,
    GameInfo,
    RecordingMeta,
    Resolution,
    ToolInfo,
    recordings_root,
)
from game_runtime.recording.pipeline import (
    NoControl,
    adapter_entries,
    perception_catalog,
    perception_entries,
    readers,
)
from game_runtime.recording.replay import CatalogFactory
from game_runtime.recording.results import ResultsHub
from game_runtime.recording.writer import RecordingWriter, WriterOptions

logger = logging.getLogger(__name__)

DRAIN_TIMEOUT: Final = 30.0
"""停止时等读者处理完已接纳的帧、剧情记录处理完积压的更新的时限（秒）。"""
AUDIO_POLL_INTERVAL: Final = 0.02
"""录声音时看一次音频环的间隔（秒），每次读到其间的全部块。不短于 Windows 上事件循环的时钟精度，
理由见 `game_runtime.frames.sequenced.DEFAULT_POLL_INTERVAL`。"""
_ALL_BLOCKS: Final = 2**63 - 1
"""当作时长交给 `read_recent`：读到已录的那一块或音频环里最旧的块为止。"""


def capture_config(adapter: Adapter, max_size: tuple[int, int] | None = None) -> CaptureConfig:
    """按适配包的窗口识别规则采集，帧环按期望分辨率（或 `max_size`）定大小。"""
    width, height = max_size or (adapter.resolution.width, adapter.resolution.height)
    return CaptureConfig(target=adapter.window_rule(), max_width=width, max_height=height)


def audio_config(adapter: Adapter) -> AudioConfig:
    """按同一条窗口识别规则采游戏进程的声音，格式取缺省。"""
    return AudioConfig(target=adapter.window_rule())


@dataclass(frozen=True, slots=True)
class LiveOptions:
    link: CoreLinkOptions
    """实时核的配置；`capture` 与 `audio` 以下面两项代替。"""
    adapter: Adapter
    adapter_sha256: str
    capture: CaptureConfig
    audio: AudioConfig | None
    data_dir: Path
    tool: ToolInfo
    games_dir: Path | None = None
    name: str | None = None
    policy: AdmissionPolicy = dataclasses.field(default_factory=AdmissionPolicy)
    backlog: int = DEFAULT_BACKLOG
    perception: bool = True
    """同时运行感知插件（`perception_entries`），把它们的输出写进录制的 `live/`；为假时只录制。"""
    plugins: CatalogFactory | None = None
    """按 `ResultsHub` 给出插件目录，缺省为 `perception_catalog`；测试换成不要模型文件的 OCR 与
    配音检测。"""
    writer: WriterOptions = dataclasses.field(default_factory=WriterOptions)
    drain_timeout: float = DRAIN_TIMEOUT


@dataclass(frozen=True, slots=True)
class SessionStatus:
    capture: CaptureState | None
    """画面的采集状态；还没收到 `core.health` 为 None。"""
    audio: CaptureState | None
    core_running: bool
    admitted: int
    ring_latest: int
    """帧环最新的帧号。"""
    overflows: int
    pending: int
    """在途的编码。"""
    frames: int
    keyframes: int
    frame_bytes: int
    audio_blocks: int
    audio_gaps: int
    first_capture_ns: int | None
    last_capture_ns: int | None

    @property
    def duration(self) -> float:
        """第一帧到最后一帧的采集时刻之差（秒）。"""
        if self.first_capture_ns is None or self.last_capture_ns is None:
            return 0.0
        return (self.last_capture_ns - self.first_capture_ns) / 1e9


@dataclass(frozen=True, slots=True)
class SessionResult:
    meta: RecordingMeta
    path: Path
    core: StopResult
    drained: bool
    """读者在时限内处理完了已接纳的帧。"""


class LiveSession:
    """一次录制：`start`，按需读 `status`，`stop` 收尾。只在事件循环的线程里使用。"""

    def __init__(self, options: LiveOptions) -> None:
        self.options = options
        link_options = dataclasses.replace(
            options.link, capture=options.capture, audio=options.audio, test_device=None
        )
        self.link = CoreLink(link_options)
        self._frames: SequencedFrameSource | None = None
        self._kernel: Kernel | None = None
        self._writer: RecordingWriter | None = None
        self._audio_task: asyncio.Task[None] | None = None
        self._readers = 0
        self._hub = ResultsHub()

    @property
    def path(self) -> Path:
        """录制目录。"""
        return self._require_writer().path

    @property
    def frames(self) -> SequencedFrameSource:
        if self._frames is None:
            raise RuntimeError("还没有开始")
        return self._frames

    @property
    def audio_task(self) -> asyncio.Task[None] | None:
        """录声音的任务；没有音频为 None。它异常结束即录制出错。"""
        return self._audio_task

    def _require_writer(self) -> RecordingWriter:
        if self._writer is None:
            raise RuntimeError("还没有开始")
        return self._writer

    async def start(self) -> None:
        """拉起实时核并挂接两个环、激活插件、建录制目录，然后开始接纳与录声音。

        实时核起不来抛 `CoreStartError`；插件没有全部激活抛 RuntimeError，此时不建录制目录。
        """
        options = self.options
        await self.link.start()
        try:
            ring = self.link.frames
            if ring is None:
                raise RuntimeError("配置了采集，实时核起来后却没有帧环")
            frames = SequencedFrameSource(
                ring, policy=options.policy, backlog=options.backlog, sink=self._sink
            )
            self._frames = frames
            game = options.adapter.id
            root = recordings_root(options.data_dir, game)
            writer = RecordingWriter.create(
                root,
                game=GameInfo(
                    id=game,
                    name=options.adapter.name,
                    version=options.adapter.version,
                    adapter_sha256=options.adapter_sha256,
                    resolution=Resolution(
                        width=options.adapter.resolution.width,
                        height=options.adapter.resolution.height,
                    ),
                ),
                name=options.name,
                admission=AdmissionInfo(
                    max_rate=options.policy.max_rate,
                    idle_interval=options.policy.idle_interval,
                    backlog=options.backlog,
                ),
                audio_format=(
                    None
                    if options.audio is None
                    else (options.audio.sample_rate, options.audio.channels)
                ),
                tool=options.tool,
                options=options.writer,
            )
            self._writer = writer
            if options.audio is not None:
                # 先于插件开始录声音：录下的块从配音检测读到的第一块之前就开始。
                self._audio_task = asyncio.get_running_loop().create_task(
                    self._record_audio(writer), name="录声音"
                )
            if options.perception:
                entries = perception_entries(
                    options.adapter,
                    data_dir=options.data_dir,
                    results_dir=writer.path / LIVE_DIR,
                    games_dir=options.games_dir,
                )
            else:
                entries = adapter_entries(game, games_dir=options.games_dir)
            self._readers = readers(entries, FRAMES_READ)
            kernel = Kernel(
                catalog=(options.plugins or perception_catalog)(self._hub),
                core=NoControl(),
                frames=frames,
                audio=self.link.audio_reader,
            )
            self._kernel = kernel
            kernel.add(entries)
            await kernel.settled()
            inactive = [s for s in kernel.snapshot() if s.state is not State.ACTIVE]
            if inactive:
                reasons = "，".join(
                    f"{s.id}：{s.state.value}" + "".join(f"；{r.message}" for r in s.reasons)
                    for s in inactive
                )
                raise RuntimeError(f"插件没有全部激活：{reasons}")
            frames.start()
        except BaseException:
            await self._abort()
            raise

    async def _sink(self, frame: RingFrame) -> None:
        await self._require_writer().write_frame(frame)

    async def _record_audio(self, writer: RecordingWriter) -> None:
        """每 `AUDIO_POLL_INTERVAL` 读一次音频环里比已录的更新的全部块，按块号追加；最新的一块撕裂时
        下一轮再读。"""
        while True:
            ring = self.link.audio
            if ring is not None and ring.latest_block_number() > writer.last_block:
                try:
                    read = ring.read_recent(_ALL_BLOCKS, after=writer.last_block)
                except AudioRingTornError:
                    read = None
                if read is not None and read.blocks:
                    writer.write_audio(read)
            await asyncio.sleep(AUDIO_POLL_INTERVAL)

    async def _abort(self) -> None:
        """启动失败时收拾：停内核、停录声音、停实时核；已建的录制目录还没有任何帧，删掉它。"""
        if self._kernel is not None:
            await self._kernel.shutdown()
        if self._audio_task is not None:
            self._audio_task.cancel()
            await asyncio.wait((self._audio_task,))
        await self.link.stop()
        writer = self._writer
        if writer is not None:
            await writer.finish(stop_reason="error", state="failed")
            if writer.meta.frames.count == 0:
                await asyncio.to_thread(shutil.rmtree, writer.path, True)

    def status(self) -> SessionStatus:
        health = self.link.last_health
        frames = self.frames
        stats = frames.stats
        writer = self._require_writer()
        meta = writer.meta
        audio = meta.audio
        return SessionStatus(
            capture=None if health is None else health.capture,
            audio=None if health is None else health.audio,
            core_running=self.link.running,
            admitted=stats.admitted,
            ring_latest=stats.latest_ring_frame,
            overflows=stats.overflows,
            pending=writer.pending,
            frames=meta.frames.count,
            keyframes=meta.frames.keyframes,
            frame_bytes=meta.frames.bytes,
            audio_blocks=0 if audio is None else audio.blocks,
            audio_gaps=0 if audio is None else audio.gaps,
            first_capture_ns=meta.frames.first_capture_ns,
            last_capture_ns=meta.frames.last_capture_ns,
        )

    async def stop(self, reason: str) -> SessionResult:
        """按顺序收尾：关闭定序帧源 → 等读者处理完已接纳的帧、剧情记录处理完积压的更新（各至多
        `drain_timeout`）→ 停插件内核 → 停录声音 → 按 R-06 停实时核 → 写完在途的编码、索引
        与元数据。"""
        frames = self.frames
        writer = self._require_writer()
        await frames.close()
        drained = True
        timeout = self.options.drain_timeout
        try:
            await asyncio.wait_for(frames.wait_ended(self._readers), timeout)
            drained = await self._hub.drain(timeout)
        except TimeoutError:
            drained = False
        if not drained:
            logger.warning("%g 秒内插件没有处理完已接纳的帧，录制时的实时输出不完整", timeout)
        if self._kernel is not None:
            await self._kernel.shutdown()
        audio_failure: BaseException | None = None
        if self._audio_task is not None:
            self._audio_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait((self._audio_task,))
            if not self._audio_task.cancelled():
                audio_failure = self._audio_task.exception()
        core = await self.link.stop()
        failure = frames.failure or audio_failure
        if failure is not None:
            logger.error("录制出错：%s", failure)
        meta = await writer.finish(
            stop_reason="error" if failure is not None else reason,
            reader_overflows=frames.stats.overflows,
            state="failed" if failure is not None else None,
        )
        return SessionResult(meta, writer.path, core, drained)
