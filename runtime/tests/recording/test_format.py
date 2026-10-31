"""样本格式：写入方与读取方的往返（解出的像素逐一等于原帧）、关键帧与差分的规则、CRC 的校验、
声音的块与空缺、收尾不留半截文件、写入出错，以及回放帧源与回放音频源。

帧取自合成游戏；经真实实时核的录制与回放见 `runtime/tests/core_link/test_recording.py`。
"""

import asyncio
import dataclasses
import json
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from game_runtime.audio_ring import AudioBlock, AudioRead
from game_runtime.frame_ring import RingFrame
from game_runtime.recording import (
    Recording,
    RecordingError,
    RecordingWriter,
    ReplayAudio,
    ReplayFrameSource,
    ReplayUnsupportedError,
    codec,
    mark_line,
)
from game_runtime.recording import writer as writer_module
from game_runtime.recording.codec import packed_pixels
from game_runtime.recording.layout import FRAMES_INDEX, LIVE_DIR, read_meta, write_meta
from game_runtime.recording.results import VOICE_FEED_FILE
from game_runtime.voice_activity import FeedEnd, FeedInterrupt, FeedSkip, UnknownReason

MS = 1_000_000

type NewWriter = Callable[..., RecordingWriter]


def leftovers(directory: Path) -> list[Path]:
    """写了一半的临时文件。"""
    return [path for path in directory.rglob("*") if path.name.endswith(".tmp")]


def write_all(writer: RecordingWriter, frames: list[RingFrame], **finish: object) -> None:
    async def scenario() -> None:
        for frame in frames:
            await writer.write_frame(frame)
        await writer.finish(stop_reason="seconds", **finish)  # pyright: ignore[reportArgumentType]

    asyncio.run(scenario())


def test_frames_round_trip_pixel_for_pixel(new_writer: NewWriter, frames: list[RingFrame]) -> None:
    first, *rest = frames
    # 与上一帧相同、只改了变换版本的帧：`same`，像素对象也是同一个。
    moved = dataclasses.replace(
        rest[0],
        frame_number=rest[0].frame_number + 1,
        capture_ns=55 * MS,
        transform_version=2,
        pixels=rest[0].pixels,
    )
    # 行尾带填充的帧：录下去掉填充的像素。
    padded_rows = np.frombuffer(rest[1].pixels, np.uint8).reshape(rest[1].height, -1)
    padded = np.pad(padded_rows, ((0, 0), (0, 8))).tobytes()
    stride = dataclasses.replace(
        rest[1],
        frame_number=moved.frame_number + 1,
        capture_ns=60 * MS,
        row_stride=rest[1].row_stride + 8,
        pixels=padded,
    )
    # 换了尺寸：关键帧。
    small = RingFrame(stride.frame_number + 1, 70 * MS, 2, 320, 180, 1280, bytes(range(256)) * 900)
    sequence = [
        first,
        rest[0],
        moved,
        stride,
        small,
        *[
            dataclasses.replace(
                f, frame_number=small.frame_number + i + 1, capture_ns=100 * MS + f.capture_ns
            )
            for i, f in enumerate(rest[2:])
        ],
    ]
    writer = new_writer(keyframe_interval=0.3, threads=2)
    write_all(writer, sequence)

    recording = Recording.open(writer.path)
    assert recording.complete
    assert recording.frame_numbers == tuple(f.frame_number for f in sequence)
    encodings = [entry.encoding for entry in recording.frames]
    # 菜单换成对话：变化的像素超过一半，写成关键帧；逐字显示多了几个字：差分。
    assert encodings[:5] == ["png", "png", "same", "delta", "png"]
    assert "delta" in encodings[5:]
    assert encodings.count("png") >= 3  # 每 0.3 秒一个定时关键帧
    for index, original in enumerate(sequence):
        replayed = recording.frame(index)
        assert replayed.pixels == packed_pixels(original)
        assert replayed.row_stride == original.width * 4
        assert (replayed.capture_ns, replayed.transform_version) == (
            original.capture_ns,
            original.transform_version,
        )
    # 依次读时 same 与上一帧共用像素对象。
    fresh = Recording.open(writer.path)
    assert fresh.frame(1).pixels is fresh.frame(2).pixels
    meta = read_meta(writer.path)
    assert meta.state == "complete"
    assert meta.stop_reason == "seconds"
    assert meta.frames.count == len(sequence)
    assert meta.frames.keyframes == encodings.count("png")
    assert meta.frames.first_capture_ns == 0
    assert leftovers(writer.path) == []
    # 关键帧本身就是能直接打开的 PNG。
    key = recording.frames[0].file
    assert key is not None
    assert (writer.path / key).read_bytes().startswith(b"\x89PNG")


def test_a_big_change_is_written_as_a_keyframe(new_writer: NewWriter) -> None:
    black = RingFrame(1, 0, 1, 64, 32, 256, bytes(64 * 32 * 4))
    white = RingFrame(2, MS, 1, 64, 32, 256, bytes([255]) * (64 * 32 * 4))
    corner = bytearray(white.pixels)
    corner[:16] = bytes(16)
    small_change = RingFrame(3, 2 * MS, 1, 64, 32, 256, bytes(corner))
    writer = new_writer()
    write_all(writer, [black, white, small_change])
    recording = Recording.open(writer.path)
    assert [e.encoding for e in recording.frames] == ["png", "png", "delta"]
    assert recording.frame(2).pixels == bytes(corner)


def test_corrupt_or_missing_pixels_are_reported(
    new_writer: NewWriter, frames: list[RingFrame]
) -> None:
    writer = new_writer()
    write_all(writer, frames[:4])
    recording = Recording.open(writer.path)
    delta = next(e for e in recording.frames if e.encoding == "delta")
    assert delta.file is not None
    path = writer.path / delta.file
    data = bytearray(path.read_bytes())
    data[-1] ^= 0xFF
    path.write_bytes(bytes(data))
    index = recording.index_of(delta.frame_number)
    with pytest.raises(RecordingError):
        Recording.open(writer.path).frame(index)
    path.unlink()
    with pytest.raises(RecordingError, match="读不出"):
        Recording.open(writer.path).frame(index)
    with pytest.raises(KeyError):
        recording.index_of(2)


def test_finish_writes_every_pending_frame_and_leaves_no_partial_files(
    new_writer: NewWriter, frames: list[RingFrame], monkeypatch: pytest.MonkeyPatch
) -> None:
    encode = codec.encode_delta

    def slow(pixels: bytes, previous: bytes) -> bytes | None:
        time.sleep(0.05)
        return encode(pixels, previous)

    monkeypatch.setattr(writer_module, "encode_delta", slow)

    # 逐字显示中的几帧都是差分：编码慢，收尾时还在途。
    frames = frames[:8]

    async def scenario() -> RecordingWriter:
        writer = new_writer(threads=1)
        for frame in frames:
            await writer.write_frame(frame)
        assert writer.pending > 0
        meta = await writer.finish(stop_reason="enter")
        assert meta.frames.count == len(frames)
        assert writer.pending == 0
        # 收尾之后再调用只返回元数据。
        assert await writer.finish(stop_reason="again") == meta
        with pytest.raises(RuntimeError, match="已收尾"):
            await writer.write_frame(frames[0])
        return writer

    writer = asyncio.run(scenario())
    assert leftovers(writer.path) == []
    recording = Recording.open(writer.path)
    assert [recording.frame(i).pixels for i in range(len(frames))] == [f.pixels for f in frames]


def test_a_failing_encode_marks_the_recording_failed(
    new_writer: NewWriter, frames: list[RingFrame], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(pixels: bytes, previous: bytes) -> bytes | None:
        raise OSError("磁盘满了")

    monkeypatch.setattr(writer_module, "encode_delta", broken)

    async def write(writer: RecordingWriter) -> None:
        for frame in frames:
            await writer.write_frame(frame)

    async def scenario() -> RecordingWriter:
        writer = new_writer(threads=1)
        with pytest.raises(OSError, match="磁盘满了"):
            await write(writer)
        meta = await writer.finish(stop_reason="error")
        assert meta.state == "failed"
        return writer

    writer = asyncio.run(scenario())
    recording = Recording.open(writer.path)
    assert not recording.complete
    assert [e.encoding for e in recording.frames] == ["png"]  # 出错的那一帧之前的
    assert leftovers(writer.path) == []


def test_an_interrupted_recording_opens_without_its_partial_last_line(
    new_writer: NewWriter, frames: list[RingFrame]
) -> None:
    writer = new_writer()
    write_all(writer, frames[:3])
    # 像异常中断那样：元数据停在 recording，索引的最后一行只写了一半。
    write_meta(writer.path, read_meta(writer.path).model_copy(update={"state": "recording"}))
    index = writer.path / FRAMES_INDEX
    with index.open("a", encoding="utf-8") as file:
        file.write('{"frame_number": 9')
    recording = Recording.open(writer.path)
    assert recording.meta.state == "recording"
    assert len(recording.frames) == 3
    with pytest.raises(RecordingError, match="不完整"):
        asyncio.run(_replay(recording))


async def _replay(recording: Recording) -> None:
    from game_runtime.recording import replay

    await replay(recording, data_dir=recording.path)


def tone_blocks(numbers: list[int], channels: int = 2) -> list[AudioBlock]:
    blocks: list[AudioBlock] = []
    for number in numbers:
        samples = np.full(480 * channels, number / 1000, np.float32)
        blocks.append(AudioBlock(number, number * 10 * MS, 480, number == 1, samples.tobytes()))
    return blocks


def test_audio_blocks_round_trip_with_gaps_and_replay_in_order(new_writer: NewWriter) -> None:
    blocks = tone_blocks([1, 2, 3, 6, 7, *range(8, 300)])
    writer = new_writer(audio=(48_000, 2))
    writer.write_audio(AudioRead(48_000, 2, tuple(blocks[:4])))
    # 读到已写过的块不重复写。
    writer.write_audio(AudioRead(48_000, 2, tuple(blocks[2:])))
    with pytest.raises(ValueError, match="音频格式"):
        writer.write_audio(AudioRead(16_000, 1, tuple(blocks[:1])))
    meta = asyncio.run(writer.finish(stop_reason="enter"))
    assert meta.audio is not None
    assert (meta.audio.blocks, meta.audio.gaps) == (len(blocks), 1)
    recording = Recording.open(writer.path)
    assert recording.blocks(0, len(blocks)) == blocks
    assert recording.audio_end_ns() == blocks[-1].capture_ns + 10 * MS

    # 录制时 voice-activity@1 的喂入标记。
    marks = [FeedSkip(0, 2), FeedInterrupt(7, UnknownReason.STALLED, 99), FeedEnd(250)]
    (writer.path / LIVE_DIR / VOICE_FEED_FILE).write_text(
        "".join(mark_line(mark) + "\n" for mark in marks), encoding="utf-8"
    )

    async def scenario() -> None:
        audio = ReplayAudio(recording)
        audio.start()
        assert list(audio.voice_marks()) == marks
        with pytest.raises(ReplayUnsupportedError):
            audio.read_recent(10**9)
        got: list[AudioBlock] = []
        after = 0
        while (read := await audio.next_blocks(after)) is not None:
            assert (read.sample_rate, read.channels) == (48_000, 2)
            assert len(read.blocks) <= 100
            got.extend(read.blocks)
            after = read.blocks[-1].block_number
        assert got == blocks
        await asyncio.wait_for(audio.wait_ended(1), 1)

    asyncio.run(scenario())


def test_the_replay_frame_source_gives_every_reader_the_recorded_sequence(
    new_writer: NewWriter, frames: list[RingFrame]
) -> None:
    writer = new_writer()
    write_all(writer, frames)
    recording = Recording.open(writer.path)

    async def scenario() -> None:
        source = ReplayFrameSource(recording)
        source.start()

        async def read() -> list[RingFrame]:
            got: list[RingFrame] = []
            after = 0
            while (frame := await source.next_frame(after)) is not None:
                got.append(frame)
                after = frame.frame_number
            return got

        first, second = await asyncio.gather(read(), read())
        assert [f.pixels for f in first] == [f.pixels for f in frames]
        assert first == second
        assert source.position == source.total == len(frames)
        await asyncio.wait_for(source.wait_ended(2), 1)
        # 帧号有空缺：给出帧号大于 after 的第一帧。
        middle = await source.next_frame(frames[3].frame_number - 1)
        assert middle is not None
        assert middle.frame_number == frames[3].frame_number

    asyncio.run(scenario())


def test_metadata_and_index_are_utf8_json_lines(
    new_writer: NewWriter, frames: list[RingFrame]
) -> None:
    writer = new_writer(name="first-play")
    write_all(writer, frames[:2])
    assert writer.path.name.endswith("Z-first-play")
    lines = (writer.path / FRAMES_INDEX).read_bytes().split(b"\n")
    assert lines[-1] == b""
    entry = json.loads(lines[0])
    assert set(entry) == {
        "frame_number",
        "capture_ns",
        "transform_version",
        "width",
        "height",
        "encoding",
        "file",
        "crc32",
    }
    meta = json.loads((writer.path / "recording.json").read_text(encoding="utf-8"))
    assert meta["format"] == 1
    assert meta["game"]["id"] == "synthetic-game"
    assert meta["started_at"].endswith("Z")
    # 同一秒里再建一段同名的录制：目录名加序号。
    again = new_writer(name="first-play")
    assert again.path.name != writer.path.name
    asyncio.run(again.finish(stop_reason="enter"))
