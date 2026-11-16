"""在真实实时核和假采集源上验证录制磁盘阻塞时仍能关读环并停核。"""

import asyncio
import sys
import threading
from pathlib import Path
from typing import IO, cast

import pytest

from game_runtime.audio_ring.fake import FakeAudioOutput
from game_runtime.core_link import AudioConfig, CaptureConfig, CoreLink, CoreLinkOptions, StopStage
from game_runtime.frame_ring.fake import FakeDisplay
from game_runtime.frames import SequencedFrameSource
from game_runtime.recording import Recording, RecordingWriter, WriterOptions
from game_runtime.recording.consumer import RecordingConsumer
from game_runtime.recording.layout import AdmissionInfo, GameInfo, Resolution, ToolInfo, read_meta
from game_runtime.recording.retention import QuotaKeeper

WIDTH, HEIGHT = 32, 24
RATE, CHANNELS = 48_000, 2


@pytest.mark.parametrize("media", ["frame", "audio"])
def test_capture_and_core_stop_before_recording_flush_is_released(
    core_exe: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, media: str
) -> None:
    async def run() -> None:
        root = Path("\\\\?\\" + str(tmp_path.resolve())) if sys.platform == "win32" else tmp_path
        writer = RecordingWriter.create(
            root,
            game=GameInfo(
                id="synthetic-game",
                name="合成游戏",
                version="0.1.0",
                adapter_sha256="0" * 64,
                resolution=Resolution(width=WIDTH, height=HEIGHT),
            ),
            name=None,
            admission=AdmissionInfo(max_rate=10.0, idle_interval=0.25, backlog=32),
            audio_format=(RATE, CHANNELS),
            tool=ToolInfo(commit=None, dirty=None),
            options=WriterOptions(threads=1),
        )
        consumer = RecordingConsumer(writer, QuotaKeeper(root, 10**9), capacity=4)
        file = "_frames_index" if media == "frame" else "_audio_samples"
        stream = cast("IO[str] | IO[bytes] | None", getattr(writer, file))
        assert stream is not None
        flush = stream.flush
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def blocked() -> None:
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(10):
                raise TimeoutError("录制文件的 flush 未获放行")
            flush()

        monkeypatch.setattr(stream, "flush", blocked)
        with (
            FakeDisplay.create(WIDTH, HEIGHT, slot_count=2) as display,
            FakeAudioOutput.create(RATE, CHANNELS, block_capacity=16, block_count=4) as output,
        ):
            core = CoreLink(
                CoreLinkOptions(
                    core_exe,
                    capture=CaptureConfig(
                        target=display.target, max_width=WIDTH, max_height=HEIGHT
                    ),
                    audio=AudioConfig(
                        target=output.target,
                        sample_rate=RATE,
                        channels=CHANNELS,
                        block_capacity=16,
                        block_count=4,
                    ),
                )
            )
            frames: SequencedFrameSource | None = None
            try:
                await core.start()
                ring, sound = core.frames, core.audio
                assert ring is not None
                assert sound is not None
                frames = SequencedFrameSource(ring, sink=consumer.accept_frame)
                frames.start()
                after = 0
                if media == "frame":
                    display.present(bytes(4 * WIDTH * HEIGHT), WIDTH, HEIGHT)
                    first = await asyncio.wait_for(frames.next_frame(0), 4)
                    assert first is not None
                    after = first.frame_number
                else:
                    output.play(bytes(8))
                    async with asyncio.timeout(4):
                        while sound.latest_block_number() == 0:
                            await asyncio.sleep(0.02)
                    read = sound.read_recent(1_000_000_000)
                    assert read is not None
                    assert read.blocks
                    assert consumer.offer_audio(read)
                await asyncio.wait_for(entered.wait(), 4)
                # 即使录制仍在同步 flush，帧泵仍能交接下一帧并关闭。
                display.present(bytes([255]) * (4 * WIDTH * HEIGHT), WIDTH, HEIGHT)
                final_frame = await asyncio.wait_for(frames.next_frame(after), 4)
                assert final_frame is not None
                await asyncio.wait_for(frames.close(), 2)
                consumer.close_input()
                stopped = await asyncio.wait_for(core.stop(), 3)
                assert stopped.stage is StopStage.SHUTDOWN
                assert ring.closed
                assert sound.closed
                assert not release.is_set()
                assert read_meta(writer.path).state == "recording"
            finally:
                release.set()
                if frames is not None:
                    await frames.close()
                if core.frames is not None or core.audio is not None or core.running:
                    await core.stop()
                meta = await consumer.finish(stop_reason="seconds")
        assert meta.state == "complete"
        recording = Recording.open(writer.path)
        assert len(recording.frames) == (2 if media == "frame" else 1)
        assert len(recording.audio) == (1 if media == "audio" else 0)

    asyncio.run(run())
