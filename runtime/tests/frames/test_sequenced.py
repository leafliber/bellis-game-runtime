"""定序帧源：接纳的规则、每个读者都按顺序逐帧读到、积压的上限、接收方让接纳慢下来、关闭与出错。

帧环由只用于测试的假显示面写入（它就是一个帧环）。经真实实时核的联调见
`runtime/tests/core_link/test_scene.py` 与 `test_recording.py`。
"""

import asyncio
import time
from collections.abc import Callable

import pytest

from game_runtime.frame_ring import FrameRingError, RingFrame
from game_runtime.frame_ring.fake import FakeDisplay
from game_runtime.frames import (
    AdmissionPolicy,
    FrameBacklogError,
    SequencedFrameSource,
)

WAIT = 5.0
MS = 1_000_000
FAST = AdmissionPolicy(max_rate=1000.0, idle_interval=10.0)
"""读帧几乎不限速；内容不变的帧 10 秒内不再接纳。"""


def pixels(value: int) -> bytes:
    return bytes([value]) * 32


async def until[T](probe: Callable[[], T | None], timeout: float = WAIT) -> T:
    deadline = time.monotonic() + timeout
    while (value := probe()) is None:
        assert time.monotonic() < deadline, "等待的条件没有成立"
        await asyncio.sleep(0.001)
    return value


async def show(
    display: FakeDisplay,
    source: SequencedFrameSource,
    value: int,
    *,
    capture_ns: int,
    transform_version: int = 1,
) -> int:
    """写一帧，等泵读过它（读不读都算），返回它的帧号。"""
    reads = source.stats.reads
    number = display.present(
        pixels(value), 4, 2, capture_ns=capture_ns, transform_version=transform_version
    )
    await until(lambda: True if source.stats.reads > reads else None)
    return number


async def read_all(source: SequencedFrameSource, after: int = 0) -> list[RingFrame]:
    frames: list[RingFrame] = []
    while (frame := await source.next_frame(after)) is not None:
        frames.append(frame)
        after = frame.frame_number
    return frames


def test_admits_changes_and_gives_every_admitted_frame_to_each_reader() -> None:
    async def scenario() -> None:
        with FakeDisplay.create(4, 2) as display:
            source = SequencedFrameSource(display, policy=FAST, poll_interval=0.001)
            readers = [asyncio.create_task(read_all(source)) for _ in range(2)]
            source.start()
            first = await show(display, source, 1, capture_ns=1 * MS)
            await show(display, source, 1, capture_ns=2 * MS)  # 内容没变：不接纳
            changed = await show(display, source, 2, capture_ns=3 * MS)
            moved = await show(display, source, 2, capture_ns=4 * MS, transform_version=2)
            await source.close()
            got = [await reader for reader in readers]
            for frames in got:
                assert [f.frame_number for f in frames] == [first, changed, moved]
                assert [f.pixels for f in frames] == [pixels(1), pixels(2), pixels(2)]
                # 只改变换版本、像素相同：与上一接纳帧共用同一个对象。
                assert frames[2].pixels is frames[1].pixels
                assert frames[2].transform_version == 2
            assert got[0] == got[1]
            assert source.stats.admitted == 3
            assert source.stats.reads == 4
            assert source.stats.ended == 2
            assert source.failure is None

    asyncio.run(scenario())


def test_unchanged_frames_are_admitted_once_the_idle_interval_has_passed() -> None:
    async def scenario() -> None:
        with FakeDisplay.create(4, 2) as display:
            policy = AdmissionPolicy(max_rate=1000.0, idle_interval=0.25)
            source = SequencedFrameSource(display, policy=policy, poll_interval=0.001)
            source.start()
            numbers = [
                await show(display, source, 7, capture_ns=at * MS) for at in (0, 100, 200, 300, 550)
            ]
            await source.close()
            frames = await read_all(source)
            # 按采集时刻：0 接纳；300 距 0 不短于 250 ms；550 距 300 不短于 250 ms。
            assert [f.frame_number for f in frames] == [numbers[0], numbers[3], numbers[4]]
            assert all(f.pixels is frames[0].pixels for f in frames)

    asyncio.run(scenario())


def test_reads_and_admissions_stay_within_max_rate() -> None:
    async def scenario() -> None:
        with FakeDisplay.create(4, 2) as display:
            policy = AdmissionPolicy(max_rate=20.0, idle_interval=0.0)
            source = SequencedFrameSource(display, policy=policy, poll_interval=0.001)
            source.start()
            started = time.monotonic()
            value = 0
            while time.monotonic() - started < 0.5:
                value = (value + 1) % 256
                display.present(pixels(value), 4, 2)
                await asyncio.sleep(0.002)
            elapsed = time.monotonic() - started
            await source.close()
            stats = source.stats
            assert stats.admitted == stats.reads
            assert stats.reads <= elapsed * 20 + 2
            assert stats.reads >= 3
            assert stats.latest_ring_frame > 3 * stats.reads  # 两次读帧之间到达的帧不读

    asyncio.run(scenario())


def test_a_reader_that_falls_behind_the_backlog_is_told_and_resumes() -> None:
    async def scenario() -> None:
        with FakeDisplay.create(4, 2) as display:
            source = SequencedFrameSource(display, policy=FAST, backlog=3, poll_interval=0.001)
            source.start()
            numbers = [await show(display, source, v, capture_ns=v * MS) for v in range(1, 6)]
            with pytest.raises(FrameBacklogError) as caught:
                await source.next_frame(0)
            assert caught.value.after == 0
            assert caught.value.resume_after == numbers[1]
            assert source.stats.overflows == 1
            # 读完被移出的最后一帧的读者没有落后。
            resumed = await source.next_frame(caught.value.resume_after)
            assert resumed is not None
            assert resumed.frame_number == numbers[2]
            await source.close()
            assert [f.frame_number for f in await read_all(source, numbers[1])] == numbers[2:]

    asyncio.run(scenario())


def test_a_busy_sink_holds_admission_back_and_sees_every_admitted_frame() -> None:
    async def scenario() -> None:
        gate = asyncio.Event()
        sunk: list[int] = []

        async def sink(frame: RingFrame) -> None:
            sunk.append(frame.frame_number)
            await gate.wait()

        with FakeDisplay.create(4, 2) as display:
            source = SequencedFrameSource(display, policy=FAST, poll_interval=0.001, sink=sink)
            reader = asyncio.create_task(read_all(source))
            source.start()
            first = await show(display, source, 1, capture_ns=MS)
            for value in range(2, 6):
                display.present(pixels(value), 4, 2, capture_ns=value * MS)
            await asyncio.sleep(0.05)
            assert source.stats.reads == 1  # 接收方没收下，泵不读帧环
            gate.set()
            await until(lambda: True if source.stats.reads == 2 else None)
            closing = asyncio.create_task(source.close())
            frames = await reader
            await closing
            assert sunk == [first, first + 4]
            assert [f.frame_number for f in frames] == sunk
            assert frames[-1].pixels == pixels(5)

    asyncio.run(scenario())


def test_close_lets_readers_finish_the_backlog_and_counts_the_ends() -> None:
    async def scenario() -> None:
        with FakeDisplay.create(4, 2) as display:
            source = SequencedFrameSource(display, policy=FAST, poll_interval=0.001)
            source.start()
            numbers = [await show(display, source, v, capture_ns=v * MS) for v in (1, 2)]
            waiting = asyncio.create_task(source.wait_ended(2))
            await source.close()
            assert source.closed
            assert [f.frame_number for f in await read_all(source)] == numbers
            await asyncio.sleep(0.01)
            assert not waiting.done()
            assert await read_all(source, numbers[-1]) == []
            await asyncio.wait_for(waiting, WAIT)
            await source.close()  # 可以重复调用
            with pytest.raises(RuntimeError, match="已经开始或已关闭"):
                source.start()

    asyncio.run(scenario())


def test_a_failing_sink_stops_admission() -> None:
    async def scenario() -> None:
        async def sink(frame: RingFrame) -> None:
            if frame.frame_number == 2:
                raise OSError("磁盘满了")

        with FakeDisplay.create(4, 2) as display:
            source = SequencedFrameSource(display, policy=FAST, poll_interval=0.001, sink=sink)
            source.start()
            for value in (1, 2):
                await show(display, source, value, capture_ns=value * MS)
            done = source.done
            assert done is not None
            await asyncio.wait_for(asyncio.wait((done,)), WAIT)
            assert source.closed
            assert isinstance(source.failure, OSError)
            # 交给读者之后才交给接收方：读者看到了出错的那一帧。
            assert [f.frame_number for f in await read_all(source)] == [1, 2]

    asyncio.run(scenario())


class TornDisplay(FakeDisplay):
    """测试用：把槽的 seqlock 序号改成奇数，像是写方正在写这一槽。"""

    def bump_seq(self, slot: int, by: int) -> None:
        self._open_views().slots[slot].seq += by


def test_torn_reads_are_retried_and_a_closed_ring_stops_admission() -> None:
    async def scenario() -> None:
        with TornDisplay.create(4, 2, slot_count=2) as display:
            source = SequencedFrameSource(display, policy=FAST, poll_interval=0.001)
            source.start()
            display.present(pixels(1), 4, 2)
            display.bump_seq(0, 1)
            await asyncio.sleep(0.05)
            assert source.stats.admitted == 0  # 每次都撕裂，下一轮再读
            display.bump_seq(0, 1)
            frame = await asyncio.wait_for(source.next_frame(0), WAIT)
            assert frame is not None
            assert frame.frame_number == 1
            await source.close()

        display = FakeDisplay.create(4, 2)
        source = SequencedFrameSource(display, policy=FAST, poll_interval=0.001)
        display.close()
        source.start()
        assert await asyncio.wait_for(source.next_frame(0), WAIT) is None
        assert isinstance(source.failure, FrameRingError)

    asyncio.run(scenario())


def test_parameters_are_checked() -> None:
    with pytest.raises(ValueError, match="max_rate"):
        AdmissionPolicy(max_rate=0.0)
    with pytest.raises(ValueError, match="idle_interval"):
        AdmissionPolicy(idle_interval=-1.0)
    with FakeDisplay.create(4, 2) as display:
        with pytest.raises(ValueError, match="backlog"):
            SequencedFrameSource(display, backlog=0)
        with pytest.raises(ValueError, match="poll_interval"):
            SequencedFrameSource(display, poll_interval=0)
    assert AdmissionPolicy().read_interval_ns == 100 * MS
