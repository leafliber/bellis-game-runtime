"""按完整句的采集时间核对阅读和配音进度。"""

from collections.abc import Callable
from typing import Final

from game_runtime.adapter.schema import DialogueBox, Pacing
from game_runtime.narrative import Completeness, NarrativeRecord, NarrativeService, compare_key
from game_runtime.scene import SceneUpdate
from game_runtime.voice_activity import VoiceActivityService, VoiceState

DEFAULT_PACING: Final = Pacing(
    min_action_interval=0.2,
    read_base=0.5,
    read_per_char=0.02,
    reread_base=0.5,
    reread_per_char=0.02,
    voice_unknown_wait=2.0,
    voice_busy_wait=8.0,
)

type LineRevision = tuple[int, int, int, int | None]


def line_revision(record: NarrativeRecord) -> LineRevision:
    return record.session, record.occurrence, record.revision, record.complete_frame


class ReadingWaitError(RuntimeError):
    """等待当前句完整、阅读时间或配音证据。"""


class ReadingPacing:
    def __init__(
        self,
        params: Pacing,
        narrative: Callable[[], NarrativeService | None],
        voice: Callable[[], VoiceActivityService | None],
    ) -> None:
        self.params = params
        self._narrative = narrative
        self._voice = voice
        self._source: NarrativeService | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._complete: tuple[LineRevision, int] | None = None
        self._seen: dict[str, int] = {}

    def close(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
        self._unsubscribe = None
        self._source = None
        self._complete = None
        self._seen.clear()

    def follow(self) -> None:
        latest = self._narrative()
        if latest is self._source:
            return
        self.close()
        if latest is not None:
            for record in latest.records():
                self._observe(record)
            self._unsubscribe = latest.subscribe(self._observe)
            self._source = latest

    def _observe(self, record: NarrativeRecord) -> None:
        if record.cutscene is not None or record.completeness is not Completeness.COMPLETE:
            return
        if record.closed:
            key = compare_key(record.text)
            if key:
                self._seen.setdefault(key, record.scope)
        elif record.complete_frame is not None and record.complete_frame == record.last_frame:
            self._complete = line_revision(record), record.last_ns

    def require_ready(self, update: SceneUpdate, box: DialogueBox, now_ns: int) -> NarrativeRecord:
        self.follow()
        source = self._source
        if source is None:
            raise ReadingWaitError("dialogue-narrative-unavailable")
        if source.processed_frame < update.frame.frame_number:
            raise ReadingWaitError("dialogue-awaiting-record")
        record = source.latest
        if (
            record is None
            or record.screen != box.screen
            or record.closed
            or record.cutscene is not None
            or record.completeness is not Completeness.COMPLETE
            or record.complete_frame is None
            or record.complete_frame > update.frame.frame_number
            or not compare_key(record.text)
        ):
            raise ReadingWaitError("dialogue-awaiting-complete")
        key = line_revision(record)
        if self._complete is None or self._complete[0] != key:
            if record.last_ns > update.frame.capture_ns:
                raise ReadingWaitError("dialogue-awaiting-record")
            # 晚订阅时从已经读到的采集时刻保守计时，随后沿用这一修订的起点。
            self._complete = key, record.last_ns
        complete_ns = self._complete[1]
        if complete_ns > update.frame.capture_ns:
            raise ReadingWaitError("dialogue-awaiting-record")
        elapsed = now_ns - complete_ns
        first_scope = self._seen.get(compare_key(record.text))
        rereading = first_scope is not None and first_scope < record.scope
        base = self.params.reread_base if rereading else self.params.read_base
        per_char = self.params.reread_per_char if rereading else self.params.read_per_char
        chars = sum(not char.isspace() for char in record.text)
        if elapsed < round((base + chars * per_char) * 1e9):
            raise ReadingWaitError("dialogue-reading")
        voice = self._voice()
        status = None if voice is None else voice.status
        if status is not None and status.state is VoiceState.BUSY:
            wait, reason = self.params.voice_busy_wait, "dialogue-voice-busy"
        elif (
            status is not None
            and status.state is VoiceState.IDLE
            and status.processed_ns is not None
            and status.processed_ns >= update.frame.capture_ns
        ):
            return record
        else:
            wait, reason = self.params.voice_unknown_wait, "dialogue-voice-unknown"
        if elapsed < round(wait * 1e9):
            raise ReadingWaitError(reason)
        return record
