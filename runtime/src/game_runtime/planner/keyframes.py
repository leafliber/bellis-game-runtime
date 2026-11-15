"""按采集时刻保留播片关键帧。缓存只持有关键帧用途已处理的图像。"""

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from game_runtime.models import ModelCallError, ModelClient, ModelImage, PreparedKeyframe
from game_runtime.narrative import Cutscene, NarrativeRecord
from game_runtime.scene import SceneUpdate
from game_runtime.step_control.contracts import ObservationReceipt

_MAX_IMAGES = 8


@dataclass(eq=False, slots=True)
class _Clip:
    since: int
    interval: int
    requested_frame: tuple[int, int, int] | None = None
    index: int | None = None
    sampled_at: int | None = None
    ended: bool = False
    wait_subtitles: bool = False
    frames: list[PreparedKeyframe] = field(default_factory=list[PreparedKeyframe])
    subtitles: dict[int, NarrativeRecord] = field(default_factory=dict[int, NarrativeRecord])


@dataclass(frozen=True, slots=True)
class Clip:
    frames: tuple[PreparedKeyframe, ...]
    subtitles: tuple[NarrativeRecord, ...]


class CutsceneFrames:
    def __init__(
        self, model: ModelClient, interval_ns: int, max_subtitles: int, wake: Callable[[], None]
    ) -> None:
        self._model = model
        self._interval = interval_ns
        self._max_subtitles = max_subtitles
        self._wake = wake
        self._clips: deque[_Clip] = deque(maxlen=2)
        self._task: asyncio.Task[None] | None = None
        self._sampling: _Clip | None = None
        self._in_use: _Clip | None = None

    def on_frame(self, update: SceneUpdate, *, wait_subtitles: bool) -> None:
        since = update.state.playing_since_ns if update.state.playing else None
        clip = self._clips[-1] if self._clips else None
        if clip is not None and not clip.ended and since != clip.since:
            clip.ended = True
            clip.wait_subtitles = wait_subtitles
            self._wake()
        if since is None:
            return
        if clip is None or clip.since != since or clip.requested_frame is not None:
            clip = _Clip(since, self._interval)
            self._clips.append(clip)
        if clip.ended or self._task is not None:
            return
        frame = update.frame
        if clip.sampled_at is not None and frame.capture_ns - clip.sampled_at < clip.interval:
            return
        if len(clip.frames) == _MAX_IMAGES:
            clip.frames = clip.frames[::2]
            clip.interval *= 2
        clip.sampled_at = frame.capture_ns
        self._prepare(clip, ModelImage(frame))

    def request(self, update: SceneUpdate) -> bool:
        if self._clips or self._task is not None or self._in_use is not None:
            return False
        frame = update.frame
        clip = _Clip(
            frame.capture_ns,
            self._interval,
            requested_frame=(frame.frame_number, frame.transform_version, frame.capture_ns),
            ended=True,
        )
        self._clips.append(clip)
        self._prepare(clip, ModelImage(frame))
        return True

    def on_observation(self, receipt: ObservationReceipt) -> bool:
        """作废匹配的快路径帧。返回是否还须取消在途调用。"""
        identity = (receipt.frame_number, receipt.transform_version, receipt.capture_ns)
        for clip in self._clips:
            if clip.requested_frame == identity:
                self._clips.remove(clip)
                if self._sampling is clip and self._task is not None:
                    self._task.cancel()
                break
        return self._in_use is not None and self._in_use.requested_frame == identity

    def _prepare(self, clip: _Clip, image: ModelImage) -> None:
        self._sampling = clip
        self._task = asyncio.create_task(self._sample(clip, image))
        self._task.add_done_callback(self._sample_done)

    def on_record(self, record: NarrativeRecord) -> None:
        if record.cutscene is None:
            return
        for clip in reversed(self._clips):
            if clip.requested_frame is not None or record.first_ns < clip.since:
                continue
            if clip.index is None or clip.index == record.cutscene:
                clip.subtitles[record.occurrence] = record
                if len(clip.subtitles) > self._max_subtitles:
                    del clip.subtitles[min(clip.subtitles)]
            return

    def on_cutscene(self, cutscene: Cutscene) -> None:
        clip = next(
            (
                item
                for item in self._clips
                if item.requested_frame is None and item.since == cutscene.first_ns
            ),
            None,
        )
        if clip is None:
            return
        clip.index = cutscene.index
        if not cutscene.closed:
            return
        captured = set(cutscene.sentences)
        clip.subtitles = {
            index: line for index, line in clip.subtitles.items() if index in captured
        }
        clip.ended = True
        clip.wait_subtitles = False
        self._wake()

    def narrative_unavailable(self) -> None:
        for clip in self._clips:
            clip.wait_subtitles = False
        if self._clips:
            self._wake()

    def take(self) -> Clip | None:
        if self._task is not None:
            return None
        while self._clips:
            clip = self._clips[0]
            if not clip.ended or clip.wait_subtitles:
                return None
            if clip.frames:
                self._in_use = clip
                return Clip(
                    tuple(clip.frames),
                    tuple(clip.subtitles[k] for k in sorted(clip.subtitles)),
                )
            self._clips.popleft()
        return None

    def release(self, *, retain: bool) -> None:
        clip, self._in_use = self._in_use, None
        if not retain and clip is not None and clip in self._clips:
            self._clips.remove(clip)

    def discard(self) -> None:
        self._clips.clear()
        if self._task is not None:
            self._task.cancel()

    async def close(self) -> None:
        task = self._task
        self.discard()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def _sample(self, clip: _Clip, raw: ModelImage) -> None:
        try:
            frame = await self._model.prepare_keyframe(raw)
            if clip in self._clips:
                clip.frames.append(frame)
        except ModelCallError:
            pass

    def _sample_done(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()
        self._task = None
        self._sampling = None
        self._wake()
