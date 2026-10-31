"""外壳的喂入标记与按录制喂入：实时运行时发布的标记（第一块不是 1、块被覆盖、块号不前进的中断）
连同全部录下的块交回外壳，得到与实时逐条相同的变化与标记，喂到 `FeedEnd` 为止。

实时一侧用内存里的读取方与手动的墙钟，模型用按音量判断的假模型。经录制与回放的联调见
`runtime/tests/core_link/test_recording.py`。
"""

import asyncio
import time
from collections.abc import Callable, Sequence

import numpy as np

from game_runtime.audio_ring import AudioBlock, AudioRead
from game_runtime.voice_activity import (
    FeedEnd,
    FeedInterrupt,
    FeedMark,
    FeedSkip,
    RecordedAudio,
    UnknownReason,
    VoiceActivityDetector,
    VoiceChange,
    VoiceHub,
    VoiceParams,
    VoiceRunner,
    VoiceState,
)
from game_runtime.voice_activity.fake import EnergyModel

RATE = 16_000
BLOCK = 160
BLOCK_NS = 10_000_000
WAIT = 5.0


def block(number: int, loud: bool) -> AudioBlock:
    t = (np.arange(BLOCK) + number * BLOCK) / RATE
    samples = (0.5 * np.sin(2 * np.pi * 300 * t) if loud else np.zeros(BLOCK)).astype(np.float32)
    return AudioBlock(number, number * BLOCK_NS, BLOCK, False, samples.tobytes())


class LiveAudio:
    """音频环的替身：`blocks` 里的块按 `read_recent` 的规则给出。"""

    def __init__(self) -> None:
        self.blocks: list[AudioBlock] = []

    def read_recent(self, duration_ns: int, *, after: int = 0) -> AudioRead | None:
        picked = [b for b in self.blocks if b.block_number > after]
        if duration_ns == 0:
            picked = picked[-1:]
        return AudioRead(RATE, 1, tuple(picked))


class Recorded:
    """录制的声音：全部块与喂入标记。"""

    def __init__(self, blocks: Sequence[AudioBlock], marks: Sequence[FeedMark]) -> None:
        self.blocks = list(blocks)
        self.marks = tuple(marks)
        self.ended = 0

    def read_recent(self, duration_ns: int, *, after: int = 0) -> AudioRead | None:
        raise AssertionError("按录制喂入时不读 read_recent")

    async def next_blocks(self, after: int) -> AudioRead | None:
        picked = [b for b in self.blocks if b.block_number > after][:7]
        if not picked:
            self.ended += 1
            return None
        return AudioRead(RATE, 1, tuple(picked))

    def voice_marks(self) -> Sequence[FeedMark]:
        return self.marks


async def until[T](probe: Callable[[], T | None], timeout: float = WAIT) -> T:
    deadline = time.monotonic() + timeout
    while (value := probe()) is None:
        assert time.monotonic() < deadline, "等待的条件没有成立"
        await asyncio.sleep(0.001)
    return value


def make_runner(
    reader: object, now: Callable[[], int]
) -> tuple[VoiceRunner, list[VoiceChange], list[FeedMark], VoiceHub]:
    detector = VoiceActivityDetector(EnergyModel(), VoiceParams())
    hub = VoiceHub(detector.status)
    changes: list[VoiceChange] = []
    marks: list[FeedMark] = []
    hub.subscribe(changes.append)
    hub.subscribe_feed(marks.append)
    runner = VoiceRunner(
        reader,  # pyright: ignore[reportArgumentType]
        detector,
        hub,
        poll_interval=0.001,
        stall_timeout=0.05,
        now_ns=now,
    )
    return runner, changes, marks, hub


def test_recorded_feeding_repeats_the_live_changes_and_marks() -> None:
    async def scenario() -> None:
        clock = [0]
        live = LiveAudio()
        runner, changes, marks, hub = make_runner(live, lambda: clock[0])
        task = asyncio.create_task(runner.run())
        await asyncio.sleep(0.01)

        # 还没有块：块号不前进的时限从开始等的时刻算。
        clock[0] = 60_000_000
        await until(lambda: True if marks else None)
        assert marks == [FeedInterrupt(0, UnknownReason.STALLED, 60_000_000)]

        # 第一块不是 1（实时运行开始得晚，音频环里更早的块已被覆盖）。
        recorded = [block(n, loud=n <= 20) for n in range(1, 121)]
        live.blocks = recorded[2:80]
        clock[0] = 80 * BLOCK_NS
        await until(lambda: True if hub.fed_block == 80 else None)
        # 读得太慢：81 到 89 已被覆盖。
        live.blocks = recorded[89:110]
        clock[0] = 110 * BLOCK_NS
        await until(lambda: True if hub.fed_block == 110 else None)
        # 块号不前进。
        clock[0] = 110 * BLOCK_NS + 80_000_000
        await until(lambda: True if len(marks) == 4 else None)
        task.cancel()
        await asyncio.wait((task,))
        end = FeedEnd(hub.fed_block)

        assert marks == [
            FeedInterrupt(0, UnknownReason.STALLED, 60_000_000),
            FeedSkip(0, 3),
            FeedSkip(80, 90),
            FeedInterrupt(110, UnknownReason.STALLED, 110 * BLOCK_NS + 80_000_000),
        ]
        states = [change.state for change in changes]
        assert VoiceState.BUSY in states
        assert VoiceState.IDLE in states
        assert any(change.reason is UnknownReason.GAP for change in changes)

        # 录下的块比实时读到的多：开头的 1、2，被覆盖的 81 到 89，结束之后的 111 到 120。
        source = Recorded(recorded, [*marks, end])
        assert isinstance(source, RecordedAudio)
        replay, replay_changes, replay_marks, replay_hub = make_runner(source, _no_clock)
        await asyncio.wait_for(replay.run(), WAIT)
        assert replay_changes == changes
        assert replay_marks == marks
        assert replay_hub.fed_block == end.after
        assert source.ended == 1

    asyncio.run(scenario())


def test_recorded_feeding_without_marks_feeds_every_block_and_stops_at_errors() -> None:
    async def scenario() -> None:
        blocks = [block(n, loud=n <= 20) for n in range(1, 81)]
        everything = Recorded(blocks, [])
        runner, changes, marks, hub = make_runner(everything, _no_clock)
        await asyncio.wait_for(runner.run(), WAIT)
        assert hub.fed_block == 80
        assert marks == []
        assert [change.state for change in changes][-1] is VoiceState.IDLE

        # 实时运行时检测出错：回放在同样的位置喂入同样的中断，之后不再喂。
        failed = Recorded(blocks, [FeedInterrupt(50, UnknownReason.ERROR, 123)])
        runner, changes, marks, hub = make_runner(failed, _no_clock)
        await asyncio.wait_for(runner.run(), WAIT)
        assert hub.fed_block == 50
        assert changes[-1] == VoiceChange(VoiceState.UNKNOWN, 123, 123, UnknownReason.ERROR)
        assert failed.ended == 1

    asyncio.run(scenario())


def _no_clock() -> int:
    raise AssertionError("按录制喂入时不读墙钟")
