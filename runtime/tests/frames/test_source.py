"""帧源：内存帧源按顺序逐帧给出，工作线程被取消时先做完。定序帧源见 `test_sequenced.py`。"""

import asyncio
import threading

import pytest

from game_runtime._threads import in_thread
from game_runtime.frame_ring import RingFrame
from game_runtime.frames import MemoryFrameSource, check_frame


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
