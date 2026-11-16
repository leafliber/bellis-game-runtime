"""监督录制消费者在有界队列中交接复制件，并把录制中断交给宿主。"""

import asyncio
import threading
from collections.abc import Callable

import pytest

from game_runtime.audio_ring import AudioBlock, AudioRead
from game_runtime.frame_ring import RingFrame
from game_runtime.recording import Recording, RecordingWriter
from game_runtime.recording import consumer as consumer_module
from game_runtime.recording import writer as writer_module
from game_runtime.recording.consumer import RecordingConsumer
from game_runtime.recording.retention import QuotaKeeper, directory_size

type NewWriter = Callable[..., RecordingWriter]


def consumer_for(writer: RecordingWriter, *, capacity: int = 4) -> RecordingConsumer:
    return RecordingConsumer(writer, QuotaKeeper(writer.path.parent, 10**9), capacity=capacity)


def audio(number: int) -> AudioRead:
    block = AudioBlock(number, number * 10_000_000, 1, False, bytes([number]) * 8)
    return AudioRead(48_000, 2, (block,))


def test_consumer_drains_admitted_copies_in_order(
    new_writer: NewWriter, frames: list[RingFrame]
) -> None:
    async def run() -> None:
        writer = new_writer(audio=(48_000, 2))
        consumer = consumer_for(writer)
        for number, frame in enumerate(frames[:2], start=1):
            await consumer.accept_frame(frame)
            assert consumer.offer_audio(audio(number))
        consumer.close_input()
        await consumer.accept_frame(frames[2])
        assert not consumer.offer_audio(audio(3))
        meta = await consumer.finish(stop_reason="enter", reader_overflows=2)
        recording = Recording.open(writer.path)
        assert (meta.state, meta.stop_reason, meta.reader_overflows) == ("complete", "enter", 2)
        assert recording.frame_numbers == tuple(frame.frame_number for frame in frames[:2])
        assert recording.blocks(0, 2) == [*audio(1).blocks, *audio(2).blocks]
        assert recording.frame(1).pixels == frames[1].pixels
        assert await consumer.finish(stop_reason="again") == meta

    asyncio.run(run())


def test_full_queue_reports_error_without_waiting_for_disk(
    new_writer: NewWriter, frames: list[RingFrame], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        writer = new_writer(audio=(48_000, 2), threads=1)
        consumer = consumer_for(writer, capacity=1)
        stream = writer._frames_index  # pyright: ignore[reportPrivateUsage]
        flush = stream.flush
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def blocked() -> None:
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(6):
                raise TimeoutError("帧索引 flush 未获放行")
            flush()

        monkeypatch.setattr(stream, "flush", blocked)
        waiter = asyncio.create_task(consumer.wait_interruption())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        try:
            await consumer.accept_frame(frames[0])
            await asyncio.wait_for(entered.wait(), 4)
            await consumer.accept_frame(frames[1])
            assert not consumer.offer_audio(audio(1))
            assert await asyncio.wait_for(consumer.wait_interruption(), 1) == "error"
            await consumer.accept_frame(frames[2])
            finish = asyncio.create_task(consumer.finish(stop_reason="enter"))
            await asyncio.sleep(0)
            try:
                for _ in range(2):
                    finish.cancel()
                    await asyncio.wait((finish,), timeout=0.05)
                    assert not finish.done()
            finally:
                release.set()
                (outcome,) = await asyncio.gather(finish, return_exceptions=True)
            assert isinstance(outcome, asyncio.CancelledError)
        finally:
            release.set()
            meta = await consumer.finish(stop_reason="enter")
        assert (meta.state, meta.stop_reason) == ("failed", "error")
        recording = Recording.open(writer.path)
        assert recording.frame_numbers == tuple(frame.frame_number for frame in frames[:2])
        assert recording.audio == ()

    asyncio.run(run())


def test_encoder_failure_is_observed_even_without_another_frame(
    new_writer: NewWriter, frames: list[RingFrame], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        writer = new_writer(audio=(48_000, 2))
        consumer = consumer_for(writer)
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()

        def fail_encode(_pixels: bytes, _width: int, _height: int) -> bytes:
            loop.call_soon_threadsafe(started.set)
            if not release.wait(6):
                raise TimeoutError("关键帧编码未获放行")
            raise OSError("无法写入关键帧")

        monkeypatch.setattr(writer_module, "encode_png", fail_encode)
        try:
            await consumer.accept_frame(frames[0])
            assert consumer.offer_audio(audio(1))
            await asyncio.wait_for(started.wait(), 4)
            release.set()
            assert await asyncio.wait_for(consumer.wait_interruption(), 4) == "error"
            assert not consumer.offer_audio(audio(2))
        finally:
            release.set()
            meta = await consumer.finish(stop_reason="enter")
        assert (meta.state, meta.stop_reason, meta.frames.count) == ("failed", "error", 0)
        assert meta.audio is not None
        assert meta.audio.blocks == 0

    asyncio.run(run())


def test_audio_write_failure_interrupts_recording(
    new_writer: NewWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_flush() -> None:
        raise OSError("无法刷新音频")

    async def run() -> None:
        writer = new_writer(audio=(48_000, 2))
        consumer = consumer_for(writer)
        stream = writer._audio_samples  # pyright: ignore[reportPrivateUsage]
        assert stream is not None
        monkeypatch.setattr(stream, "flush", fail_flush)
        try:
            assert consumer.offer_audio(audio(1))
            assert await asyncio.wait_for(consumer.wait_interruption(), 4) == "error"
            assert not consumer.offer_audio(audio(2))
        finally:
            meta = await consumer.finish(stop_reason="enter")
        assert (meta.state, meta.stop_reason) == ("failed", "error")
        assert meta.audio is not None
        assert meta.audio.blocks == 0

    asyncio.run(run())


def test_quota_stops_admission_and_keeps_the_current_recording(
    new_writer: NewWriter, frames: list[RingFrame], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(consumer_module, "QUOTA_INTERVAL", 0.01)

    async def run() -> None:
        oldest = new_writer(name="old")
        await oldest.finish(stop_reason="enter")
        writer = new_writer(name="current")
        budget = directory_size(oldest.path) + directory_size(writer.path)
        keeper = QuotaKeeper(writer.path.parent, budget)
        consumer = RecordingConsumer(writer, keeper, capacity=4)
        try:
            await consumer.accept_frame(frames[0])
            assert await asyncio.wait_for(consumer.wait_interruption(), 4) == "quota"
            await consumer.accept_frame(frames[1])
            assert not consumer.offer_audio(audio(1))
        finally:
            meta = await consumer.finish(stop_reason="enter")
        assert (meta.state, meta.stop_reason, meta.frames.count) == ("complete", "quota", 1)
        assert writer.path.exists()
        assert not oldest.path.exists()
        assert [item.path for item in keeper.deleted] == [oldest.path]

    asyncio.run(run())
