"""一句的边界、修订号、完整度与剧情范围：按处理的顺序逐个观察，确定性地更新记录。

规则见 `docs/modules/narrative-planner.md` 的“一句的边界”“完整度”与“剧情范围”。这里不读墙钟，
时刻都取观察里帧的采集时刻；同样的观察序列总得到同样的记录。
"""

from dataclasses import dataclass, replace
from typing import Final

from game_runtime.narrative.record import (
    Completeness,
    Discontinuity,
    LowConfidenceLine,
    NarrativeRecord,
    Scope,
    compare_key,
    continues,
)


@dataclass(frozen=True, slots=True)
class Reading:
    """一个 ROI 的识别结果：各行的文字与置信度，按阅读顺序。"""

    roi: str
    lines: tuple[tuple[str, float], ...]

    @property
    def text(self) -> str:
        """各行依次连接的原文。"""
        return "".join(text for text, _ in self.lines)


@dataclass(frozen=True, slots=True)
class BoxFrame:
    """判出对白框画面的一帧，及其名字栏与对白文字的识别结果。"""

    frame_number: int
    capture_ns: int
    screen: str
    text: Reading
    speaker: Reading | None
    """没有名字栏时为 None。"""
    marker: bool
    """继续标记可见。"""


@dataclass(frozen=True, slots=True)
class OtherScreen:
    """判出了画面，但不是对白框画面的一帧。"""

    frame_number: int
    screen: str
    starts_scope: bool
    """画面的通用类型是坏结局或存读档：下一句开始新的剧情范围。"""


@dataclass(frozen=True, slots=True)
class Unjudged:
    """没有判出画面的一帧，或 OCR 出错的一帧：不改变任何记录。"""

    frame_number: int


@dataclass(frozen=True, slots=True)
class Gap:
    """积压超限时丢掉的一段更新。"""

    first_frame: int
    last_frame: int
    count: int


type Observation = BoxFrame | OtherScreen | Unjudged | Gap


@dataclass(frozen=True, slots=True)
class Changes:
    """处理一个观察产生的变化：要通知与写入的记录（按发生的顺序）、新的剧情范围与中断。"""

    records: tuple[NarrativeRecord, ...] = ()
    scope: Scope | None = None
    discontinuity: Discontinuity | None = None

    def __bool__(self) -> bool:
        return bool(self.records) or self.scope is not None or self.discontinuity is not None


NO_CHANGES: Final = Changes()


@dataclass(frozen=True, slots=True)
class TrackerParams:
    complete_frames: int = 3
    """比较文本连续这么多个对白框帧不变……"""
    complete_after_ns: int = 500_000_000
    """……且从最近一次变化起已过这么久（纳秒），才判为完整。"""
    low_confidence: float = 0.8
    """识别置信度低于它的行标出。"""

    def __post_init__(self) -> None:
        if self.complete_frames < 1:
            raise ValueError(f"complete_frames 须至少为 1：{self.complete_frames}")
        if self.complete_after_ns < 0:
            raise ValueError(f"complete_after_ns 不能为负：{self.complete_after_ns}")


@dataclass(slots=True)
class _Open:
    """当前句：记录与判断同一句、完整度要用的状态。"""

    record: NarrativeRecord
    key: str
    speaker_key: str
    same_frames: int
    """比较文本连续不变的对白框帧数，含最近一次变化的那一帧。"""
    changed_ns: int
    """比较文本最近一次变化的帧的采集时刻。"""


class SentenceTracker:
    """逐个观察，维护当前句与本次运行的全部记录。不是线程安全的。"""

    def __init__(self, session: int, params: TrackerParams | None = None) -> None:
        self.session = session
        self.params = params or TrackerParams()
        self._records: list[NarrativeRecord] = []
        self._open: _Open | None = None
        self._scope = 0
        self._scope_after: str | None = None
        self._scopes: list[Scope] = []
        self._discontinuities: list[Discontinuity] = []

    @property
    def records(self) -> tuple[NarrativeRecord, ...]:
        """本次运行的各句的最新状态，按出现编号。"""
        return tuple(self._records)

    @property
    def latest(self) -> NarrativeRecord | None:
        return self._records[-1] if self._records else None

    @property
    def scopes(self) -> tuple[Scope, ...]:
        return tuple(self._scopes)

    @property
    def discontinuities(self) -> tuple[Discontinuity, ...]:
        return tuple(self._discontinuities)

    def observe(self, observation: Observation) -> Changes:
        """处理一个观察，返回产生的变化。"""
        match observation:
            case BoxFrame():
                return self._box(observation)
            case OtherScreen():
                closed = self._close(observation.frame_number)
                if observation.starts_scope:
                    self._scope_after = observation.screen
                return Changes(closed)
            case Unjudged():
                return NO_CHANGES
            case Gap():
                closed = self._close(observation.first_frame)
                gap = Discontinuity(
                    self.session, observation.first_frame, observation.last_frame, observation.count
                )
                self._discontinuities.append(gap)
                return Changes(closed, discontinuity=gap)

    # 对白框

    def _box(self, frame: BoxFrame) -> Changes:
        key = compare_key(frame.text.text)
        speaker_key = "" if frame.speaker is None else compare_key(frame.speaker.text)
        current = self._open
        if not key:
            return Changes(self._close(frame.frame_number))
        if current is not None and self._same_sentence(current, frame.screen, key, speaker_key):
            return self._update(current, frame, key, speaker_key)
        closed = self._close(frame.frame_number)
        scope = self._next_scope(frame.frame_number)
        opened = self._start(frame, key, speaker_key)
        return Changes((*closed, opened), scope=scope)

    @staticmethod
    def _same_sentence(current: _Open, screen: str, key: str, speaker_key: str) -> bool:
        if current.record.screen != screen:
            return False
        if current.speaker_key and speaker_key and current.speaker_key != speaker_key:
            return False
        return continues(current.key, key)

    def _start(self, frame: BoxFrame, key: str, speaker_key: str) -> NarrativeRecord:
        record = NarrativeRecord(
            session=self.session,
            occurrence=len(self._records) + 1,
            scope=self._scope,
            screen=frame.screen,
            speaker=frame.speaker.text if frame.speaker is not None and speaker_key else None,
            text=frame.text.text,
            revision=1,
            completeness=Completeness.PARTIAL,
            first_frame=frame.frame_number,
            first_ns=frame.capture_ns,
            last_frame=frame.frame_number,
            last_ns=frame.capture_ns,
            revision_frame=frame.frame_number,
            complete_frame=None,
            closed_frame=None,
            low_confidence=self._low_confidence(frame),
        )
        current = _Open(record, key, speaker_key, same_frames=1, changed_ns=frame.capture_ns)
        current.record = self._judge(current, frame)
        self._open = current
        self._records.append(current.record)
        return current.record

    def _update(self, current: _Open, frame: BoxFrame, key: str, speaker_key: str) -> Changes:
        record = current.record
        before = record
        if key != current.key:
            record = replace(
                record,
                text=frame.text.text,
                revision=record.revision + 1,
                revision_frame=frame.frame_number,
                completeness=Completeness.PARTIAL,
                complete_frame=None,
                low_confidence=self._low_confidence(frame),
            )
            current.key = key
            current.same_frames = 1
            current.changed_ns = frame.capture_ns
        else:
            current.same_frames += 1
        if not current.speaker_key and speaker_key and frame.speaker is not None:
            record = replace(
                record, speaker=frame.speaker.text, low_confidence=self._low_confidence(frame)
            )
            current.speaker_key = speaker_key
        record = replace(record, last_frame=frame.frame_number, last_ns=frame.capture_ns)
        current.record = record
        current.record = self._judge(current, frame)
        self._records[-1] = current.record
        if _content(current.record) == _content(before):
            return NO_CHANGES
        return Changes((current.record,))

    def _judge(self, current: _Open, frame: BoxFrame) -> NarrativeRecord:
        """按继续标记与连续不变的帧数判断完整。"""
        record = current.record
        if record.completeness is Completeness.COMPLETE:
            return record
        params = self.params
        stable = (
            current.same_frames >= params.complete_frames
            and frame.capture_ns - current.changed_ns >= params.complete_after_ns
        )
        if frame.marker or stable:
            return replace(
                record, completeness=Completeness.COMPLETE, complete_frame=frame.frame_number
            )
        return record

    def _low_confidence(self, frame: BoxFrame) -> tuple[LowConfidenceLine, ...]:
        readings = (frame.speaker, frame.text) if frame.speaker is not None else (frame.text,)
        return tuple(
            LowConfidenceLine(reading.roi, text, confidence)
            for reading in readings
            for text, confidence in reading.lines
            if confidence < self.params.low_confidence
        )

    # 结束与范围

    def _close(self, frame_number: int) -> tuple[NarrativeRecord, ...]:
        """结束当前句：部分的记为缺口，完整的仍是完整。"""
        current = self._open
        if current is None:
            return ()
        self._open = None
        record = current.record
        completeness = (
            Completeness.COMPLETE
            if record.completeness is Completeness.COMPLETE
            else Completeness.GAP
        )
        closed = replace(record, completeness=completeness, closed_frame=frame_number)
        self._records[-1] = closed
        return (closed,)

    def _next_scope(self, frame_number: int) -> Scope | None:
        """新开一句之前：第一句或触发过新范围时，开始新的剧情范围。"""
        if self._scope and self._scope_after is None:
            return None
        self._scope += 1
        scope = Scope(self.session, self._scope, self._scope_after, frame_number)
        self._scope_after = None
        self._scopes.append(scope)
        return scope


def _content(record: NarrativeRecord) -> NarrativeRecord:
    """记录中除最后出现的帧号与时刻之外的部分：它们每帧都变，不单独通知与写入。"""
    return replace(record, last_frame=0, last_ns=0)
