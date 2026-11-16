"""让真实文件调用等在工作线程，核对控制循环、取消和录制索引的一致性。"""

import asyncio
import threading
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import cast

import pytest

from game_runtime.adapter import Adapter
from game_runtime.audio_ring import AudioBlock, AudioRead
from game_runtime.audio_ring.fake import FakeAudioOutput
from game_runtime.core_link import CoreLinkOptions
from game_runtime.frame_ring import RingFrame
from game_runtime.recording import Recording, RecordingMeta, RecordingWriter
from game_runtime.recording.layout import GameInfo, ToolInfo, read_meta
from game_runtime.recording.session import LiveOptions, LiveSession, capture_config

type NewWriter = Callable[..., RecordingWriter]


class BlockedFileCall:
    """在真正的 write、flush 或 close 之前阻塞。用事件确认调用已到达，避免用 sleep 猜时序。"""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        writer: RecordingWriter,
        file: str,
        operation: str,
        *,
        error: OSError | None = None,
    ) -> None:
        self.entered = asyncio.Event()
        self.release = threading.Event()
        loop = asyncio.get_running_loop()
        target: object = getattr(writer, file)
        original = cast("Callable[..., object]", getattr(target, operation))

        def blocked(*args: object) -> object:
            loop.call_soon_threadsafe(self.entered.set)
            if not self.release.wait(5):
                raise TimeoutError("事件循环没有在文件调用阻塞时继续运行")
            if error is not None:
                raise error
            return original(*args)

        monkeypatch.setattr(target, operation, blocked)


async def cancel_write_and_finish(
    writer: RecordingWriter, write: Coroutine[object, object, None], blocked: BlockedFileCall
) -> RecordingMeta:
    task = asyncio.create_task(write)
    finish: asyncio.Task[RecordingMeta] | None = None
    try:
        await asyncio.wait_for(blocked.entered.wait(), 4)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish = asyncio.create_task(writer.finish(stop_reason="interrupt"))
        await asyncio.sleep(0)
        assert not finish.done()
    finally:
        blocked.release.set()
        task.cancel()
        (outcome,) = await asyncio.gather(task, return_exceptions=True)
        meta = await (finish if finish is not None else writer.finish(stop_reason="error"))
    assert isinstance(outcome, asyncio.CancelledError)
    return meta


@pytest.mark.parametrize("operation", ["write", "flush"])
def test_frame_index_io_yields_and_is_not_repeated_after_cancellation(
    new_writer: NewWriter, frames: list[RingFrame], monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    async def scenario() -> None:
        writer = new_writer(threads=1)
        blocked = BlockedFileCall(monkeypatch, writer, "_frames_index", operation)
        submitted: list[RingFrame] = []

        async def feed() -> None:
            for frame in frames[:4]:
                submitted.append(frame)
                await writer.write_frame(frame)

        meta = await cancel_write_and_finish(writer, feed(), blocked)
        assert meta.state == "complete"
        recording = Recording.open(writer.path)
        assert meta.frames.count == len(submitted) > 0
        assert recording.frame_numbers == tuple(frame.frame_number for frame in submitted)
        assert [recording.frame(i).pixels for i in range(len(submitted))] == [
            frame.pixels for frame in submitted
        ]
        assert writer.pending == 0

    asyncio.run(scenario())


def test_frame_index_failure_during_cancellation_cannot_skip_to_a_later_frame(
    new_writer: NewWriter, frames: list[RingFrame], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        writer = new_writer(threads=1)
        error = OSError("帧索引写入失败")
        blocked = BlockedFileCall(monkeypatch, writer, "_frames_index", "write", error=error)

        async def feed() -> None:
            for frame in frames[:4]:
                await writer.write_frame(frame)

        meta = await cancel_write_and_finish(writer, feed(), blocked)
        assert writer.failure is error
        assert writer.pending == 0
        assert meta.state == "failed"
        assert Recording.open(writer.path).frames == ()

    asyncio.run(scenario())


@pytest.mark.parametrize("file", ["_audio_samples", "_audio_index"])
@pytest.mark.parametrize("operation", ["write", "flush"])
def test_live_audio_file_io_yields_and_commits_before_finish(
    new_writer: NewWriter,
    synthetic: Adapter,
    root: Path,
    game: GameInfo,
    monkeypatch: pytest.MonkeyPatch,
    file: str,
    operation: str,
) -> None:
    async def scenario() -> None:
        writer = new_writer(audio=(48_000, 2))
        blocked = BlockedFileCall(monkeypatch, writer, file, operation)
        session = LiveSession(
            LiveOptions(
                link=CoreLinkOptions(Path("unused")),
                adapter=synthetic,
                adapter_sha256=game.adapter_sha256,
                capture=capture_config(synthetic),
                audio=None,
                data_dir=root,
                tool=ToolInfo(commit=None, dirty=None),
            )
        )
        block = AudioBlock(1, 10_000_000, 1, False, bytes(8))
        with FakeAudioOutput.create(48_000, 2, block_capacity=480, block_count=4) as ring:
            ring.play(block.samples, capture_ns=block.capture_ns)
            monkeypatch.setattr(session.link, "_audio", ring)
            # 走 record 与 observe 共用的轮询调用，只有音频环替身，无需启动实时核。
            poll = session._record_audio(writer)  # pyright: ignore[reportPrivateUsage]
            meta = await cancel_write_and_finish(writer, poll, blocked)
        assert meta.state == "complete"
        assert meta.audio is not None
        assert meta.audio.blocks == writer.last_block == 1
        assert Recording.open(writer.path).blocks(0, 1) == [block]

    asyncio.run(scenario())


def test_partial_audio_write_latches_the_error_and_rejects_later_blocks(
    new_writer: NewWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = new_writer(audio=(48_000, 2))
    blocks = tuple(AudioBlock(i, 10_000_000 * i, 1, False, bytes([i]) * 8) for i in range(1, 5))
    writer.write_audio(AudioRead(48_000, 2, blocks[:1]))
    index = writer._audio_index  # pyright: ignore[reportPrivateUsage]
    assert index is not None
    original = index.write
    error = OSError("音频索引写入失败")
    writes = 0

    def fail_second(line: str) -> int:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise error
        return original(line)

    monkeypatch.setattr(index, "write", fail_second)
    with pytest.raises(OSError, match="音频索引写入失败"):
        writer.write_audio(AudioRead(48_000, 2, blocks[1:3]))
    assert writer.failure is error
    assert writer.last_block == 1
    assert writer.meta.audio is not None
    assert (writer.meta.audio.blocks, writer.meta.audio.bytes) == (1, len(blocks[0].samples))
    with pytest.raises(OSError, match="音频索引写入失败"):
        writer.write_audio(AudioRead(48_000, 2, blocks[1:]))
    assert writes == 2
    meta = asyncio.run(writer.finish(stop_reason="enter", state="complete"))
    assert meta.state == read_meta(writer.path).state == "failed"
    assert Recording.open(writer.path).blocks(0, 1) == [blocks[0]]


def test_close_yields_and_cancelled_finish_still_commits_once(
    new_writer: NewWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        writer = new_writer()
        blocked = BlockedFileCall(monkeypatch, writer, "_frames_index", "close")
        finish = asyncio.create_task(writer.finish(stop_reason="interrupt"))
        try:
            await asyncio.wait_for(blocked.entered.wait(), 4)
            finish.cancel()
            await asyncio.sleep(0)
            assert not finish.done()
        finally:
            blocked.release.set()
            (outcome,) = await asyncio.gather(finish, return_exceptions=True)
        assert isinstance(outcome, asyncio.CancelledError)
        meta = await writer.finish(stop_reason="enter")
        assert meta == read_meta(writer.path)
        assert (meta.state, meta.stop_reason) == ("complete", "interrupt")

    asyncio.run(scenario())
