"""帧源：内存帧源按顺序逐帧给出，实时帧源读帧环给出最新的一帧，工作线程被取消时先做完。

实时帧源读的帧环由只用于测试的假显示面写入（它就是一个帧环）；经真实实时核的联调见
`runtime/tests/core_link/test_scene.py`。
"""

import asyncio
import threading

import pytest

from game_runtime._threads import in_thread
from game_runtime.frame_ring import FrameRingError, RingFrame
from game_runtime.frame_ring.fake import FakeDisplay
from game_runtime.frames import MemoryFrameSource, RingFrameSource, check_frame


def frame(number: int, *, width: int = 4, height: int = 2, value: int = 0) -> RingFrame:
    return RingFrame(
        frame_number=number,
        capture_ns=number * 1_000_000,
        transform_version=1,
        width=width,
        height=height,
        row_stride=width * 4,
        pixels=bytes([value]) * (width * 4 * height),
    )


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


# 内存帧源


def test_memory_source_gives_every_frame_in_order_to_each_reader() -> None:
    async def scenario() -> None:
        source = MemoryFrameSource()
        for number in (1, 2, 5):
            source.push(frame(number))

        async def read_all() -> list[int]:
            numbers: list[int] = []
            after = 0
            while (got := await source.next_frame(after)) is not None:
                numbers.append(got.frame_number)
                after = got.frame_number
            return numbers

        readers = [asyncio.create_task(read_all()) for _ in range(2)]
        await settle()
        assert not any(reader.done() for reader in readers)  # 读完已有的帧后等新帧
        source.push(frame(7))
        source.close()
        assert [await reader for reader in readers] == [[1, 2, 5, 7], [1, 2, 5, 7]]
        # 跳过的帧号不影响：给出帧号大于 after 的第一帧。
        got = await source.next_frame(3)
        assert got is not None
        assert got.frame_number == 5
        assert await source.next_frame(7) is None

    asyncio.run(scenario())


def test_memory_source_rejects_bad_frames() -> None:
    source = MemoryFrameSource()
    source.push(frame(2))

    with pytest.raises(ValueError, match="帧号须递增"):
        source.push(frame(2))
    with pytest.raises(ValueError, match="帧号须为正数"):
        check_frame(frame(0))
    bad_stride = RingFrame(3, 0, 1, 4, 2, 15, bytes(32))
    with pytest.raises(ValueError, match="行跨度 15 小于宽 4 × 4"):
        source.push(bad_stride)
    short = RingFrame(3, 0, 1, 4, 2, 16, bytes(31))
    with pytest.raises(ValueError, match="像素只有 31 字节"):
        source.push(short)
    source.close()
    with pytest.raises(RuntimeError, match="已关闭"):
        source.push(frame(3))
    assert [f.frame_number for f in source.frames] == [2]


# 实时帧源


def test_ring_source_waits_for_a_frame_then_gives_the_latest() -> None:
    async def scenario() -> None:
        with FakeDisplay.create(4, 2) as display:
            source = RingFrameSource(display, poll_interval=0.001)
            waiting = asyncio.create_task(source.next_frame(0))
            await asyncio.sleep(0.02)
            assert not waiting.done()

            display.present(bytes([7]) * 32, 4, 2, capture_ns=123, transform_version=4)
            first = await asyncio.wait_for(waiting, 5)
            assert first is not None
            assert (first.frame_number, first.capture_ns, first.transform_version) == (1, 123, 4)
            assert first.pixels == bytes([7]) * 32

            # 读者处理不过来时跳过中间的帧，拿到最新的。
            for value in (1, 2, 3):
                display.present(bytes([value]) * 32, 4, 2)
            latest = await asyncio.wait_for(source.next_frame(first.frame_number), 5)
            assert latest is not None
            assert latest.frame_number == 4
            assert latest.pixels == bytes([3]) * 32

    asyncio.run(scenario())


class TornDisplay(FakeDisplay):
    """测试用：把槽的 seqlock 序号改成奇数，像是写方正在写这一槽。"""

    def bump_seq(self, slot: int, by: int) -> None:
        self._open_views().slots[slot].seq += by


def test_ring_source_retries_torn_reads() -> None:
    async def scenario() -> None:
        with TornDisplay.create(4, 2, slot_count=2) as display:
            source = RingFrameSource(display, poll_interval=0.001)
            display.present(bytes(32), 4, 2)
            display.bump_seq(0, 1)
            reading = asyncio.create_task(source.next_frame(0))
            await asyncio.sleep(0.05)
            assert not reading.done()  # 每次尝试都撕裂，稍后再读

            display.bump_seq(0, 1)
            got = await asyncio.wait_for(reading, 5)
            assert got is not None
            assert got.frame_number == 1

    asyncio.run(scenario())


def test_ring_source_fails_once_the_ring_is_closed() -> None:
    async def scenario() -> None:
        display = FakeDisplay.create(4, 2)
        source = RingFrameSource(display)
        display.close()
        with pytest.raises(FrameRingError, match="已关闭"):
            await source.next_frame(0)

    asyncio.run(scenario())
    with FakeDisplay.create(4, 2) as display, pytest.raises(ValueError, match="poll_interval"):
        RingFrameSource(display, poll_interval=0)


# 工作线程


def test_cancelled_thread_work_finishes_before_the_cancellation_propagates() -> None:
    async def scenario() -> None:
        started = threading.Event()
        release = threading.Event()
        finished: list[str] = []

        def work() -> str:
            started.set()
            release.wait(5)
            finished.append("做完")
            return "结果"

        task = asyncio.create_task(in_thread(work))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()  # 还在等工作线程
        task.cancel()  # 再次取消也一样等
        await asyncio.sleep(0.05)
        assert not task.done()

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished == ["做完"]
        assert await in_thread(lambda: 42) == 42

    asyncio.run(scenario())
