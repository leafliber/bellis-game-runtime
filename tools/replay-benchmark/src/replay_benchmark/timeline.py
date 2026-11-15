"""将录制的采集间隔移到本次实时核使用的单调时钟。"""

import asyncio
import time
from itertools import pairwise

from game_runtime.frame_ring.fake import FakeDisplay
from game_runtime.recording.layout import frame_file
from game_runtime.recording.reader import Recording, RecordingError
from game_runtime.scene import SceneUpdate
from replay_benchmark.score import ObservedFrame, Trace


class Timeline:
    def __init__(self, recording: Recording, trace: Trace) -> None:
        if not recording.complete or not recording.frames:
            raise RecordingError("离线基准需要包含画面的完整录制")
        times = tuple(frame.capture_ns for frame in recording.frames)
        if any(right <= left for left, right in pairwise(times)):
            raise RecordingError("离线基准要求录制的采集时刻严格递增")
        root = recording.path.resolve()
        for frame in recording.frames:
            if frame.file is not None and (
                frame.file != frame_file(frame.frame_number, frame.encoding)
                or not (root / frame.file).resolve().is_relative_to(root)
            ):
                raise RecordingError(f"录制帧 {frame.frame_number} 的像素路径不合样本格式")
        self.recording = recording
        self.trace = trace
        self.width = max(frame.width for frame in recording.frames)
        self.height = max(frame.height for frame in recording.frames)
        self._presented: dict[int, int] = {}

    def observe(self, update: SceneUpdate) -> None:
        frame = update.frame
        original = self._presented.get(frame.capture_ns)
        if original is not None:
            self.trace.frames[frame.frame_number] = ObservedFrame(
                original, frame.capture_ns, time.perf_counter_ns(), update.state.stability.value
            )

    async def play(self, display: FakeDisplay) -> None:
        recording = self.recording
        first = recording.frames[0].capture_ns
        origin = time.perf_counter_ns()
        for index in range(len(recording.frames)):
            frame = await asyncio.to_thread(recording.frame, index)
            capture_ns = origin + frame.capture_ns - first
            remaining = capture_ns - time.perf_counter_ns()
            if remaining > 0:
                await asyncio.sleep(remaining / 1e9)
            self._presented[capture_ns] = frame.frame_number
            display.present(
                frame.pixels,
                frame.width,
                frame.height,
                row_stride=frame.row_stride,
                capture_ns=capture_ns,
                transform_version=frame.transform_version,
            )
            self.trace.presented += 1
