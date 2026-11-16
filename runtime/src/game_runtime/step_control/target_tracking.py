"""把原帧已存在的文字控件关联到新帧。匹配结果只描述控件，权限仍由调用方核对。"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from game_runtime.adapter import PixelRect
from game_runtime.ocr import OcrLine, OcrResult
from game_runtime.scene.evidence import (
    CapturedScene,
    EvidenceFailure,
    MissingEvidence,
    check_interval,
    contains,
    screen_identity,
)
from game_runtime.scene.signatures import Verdict
from game_runtime.step_control.contracts import TextTarget
from game_runtime.step_control.risk import ActionRefusedError, text_key, verify_ocr


@dataclass(frozen=True, slots=True)
class SceneSample:
    capture: CapturedScene
    ocr: OcrResult | None = None


@dataclass(frozen=True, slots=True)
class TrackedText:
    """文本与几何关联，调用方须独立取得未覆盖的证据后才能决定是否提交。"""

    capture: CapturedScene
    line: OcrLine
    missing: frozenset[MissingEvidence] = field(
        init=False,
        default=frozenset(
            {
                MissingEvidence.MODAL_STATE,
                MissingEvidence.REGION_VISIBILITY,
                MissingEvidence.CONTROL_AVAILABILITY,
            }
        ),
    )


def check_text_read(capture: CapturedScene, read: OcrResult) -> bool:
    """核对识别的原帧及识别区域，含其内每个文字框。"""
    try:
        verify_ocr(capture.update, read)
    except ActionRefusedError:
        return False
    return all(contains(read.rect, line.box) and 0 <= line.confidence <= 1 for line in read.lines)


def track_text(
    origin: CapturedScene,
    target: TextTarget,
    samples: Sequence[SceneSample],
    *,
    now_ns: int,
    max_age_ns: int,
    max_gap_ns: int,
    min_confidence: float,
    min_overlap: float,
) -> TrackedText | EvidenceFailure:
    """在完整的采集区间内逐帧核对文字、控件组与相对几何。"""
    if not 0 < min_confidence <= 1 or not 0 < min_overlap <= 1:
        raise ValueError("置信度与重合度门槛须在 (0, 1] 内")
    key = text_key(target.text or "")
    if not key or target.phrase is not None:
        return EvidenceFailure.UNSUPPORTED
    failure = check_interval(
        [sample.capture for sample in samples],
        now_ns=now_ns,
        max_age_ns=max_age_ns,
        max_gap_ns=max_gap_ns,
    )
    if failure is not None:
        return failure
    first = samples[0].capture
    if (
        first.epoch is not origin.epoch
        or first.sequence != origin.sequence
        or first.update.frame is not origin.update.frame
        or first.update.observation_scope is not origin.update.observation_scope
    ):
        return EvidenceFailure.INVALIDATED

    baseline = screen_identity(origin)
    previous: OcrResult | None = None
    line: OcrLine | None = None
    for sample in samples:
        read = sample.ocr
        if read is None:
            return EvidenceFailure.UNSUPPORTED
        if not check_text_read(sample.capture, read) or screen_identity(sample.capture) != baseline:
            return EvidenceFailure.INVALIDATED
        matches = tuple(item for item in read.lines if text_key(item.text) == key)
        if not matches:
            return EvidenceFailure.NO_MATCH
        if len(matches) != 1:
            return EvidenceFailure.INVALIDATED
        if any(item.confidence < min_confidence for item in read.lines):
            return EvidenceFailure.INSUFFICIENT
        if previous is None:
            if not _is_origin_group(origin, target, read):
                return EvidenceFailure.UNSUPPORTED
        elif not _same_group(previous, read, min_overlap):
            return EvidenceFailure.INVALIDATED
        previous = read
        line = matches[0]

    return (
        TrackedText(samples[-1].capture, line) if line is not None else EvidenceFailure.INSUFFICIENT
    )


def _is_origin_group(origin: CapturedScene, target: TextTarget, read: OcrResult) -> bool:
    state = origin.update.state
    if len(read.lines) < 2 and not (state.verdict is Verdict.SCREEN and state.matched):
        return False
    if target.roi is not None:
        return target.roi == read.roi
    if target.point is None:
        return False
    x, y = target.point
    return read.rect.left <= x < read.rect.right and read.rect.top <= y < read.rect.bottom


def _same_group(before: OcrResult, after: OcrResult, min_overlap: float) -> bool:
    if (
        before.roi != after.roi
        or len(before.lines) != len(after.lines)
        or _overlap(before.rect, after.rect) < min_overlap
    ):
        return False
    return all(
        text_key(first.text) == text_key(second.text)
        and _overlap(_relative(first.box, before.rect), _relative(second.box, after.rect))
        >= min_overlap
        for first, second in zip(before.lines, after.lines, strict=True)
    )


type _Rect = tuple[float, float, float, float]


def _relative(box: PixelRect, group: PixelRect) -> _Rect:
    width, height = group.right - group.left, group.bottom - group.top
    return (
        (box.left - group.left) / width,
        (box.top - group.top) / height,
        (box.right - group.left) / width,
        (box.bottom - group.top) / height,
    )


def _overlap(before: _Rect, after: _Rect) -> float:
    left, top = max(before[0], after[0]), max(before[1], after[1])
    right, bottom = min(before[2], after[2]), min(before[3], after[3])
    intersection = max(0, right - left) * max(0, bottom - top)
    previous_area = (before[2] - before[0]) * (before[3] - before[1])
    current_area = (after[2] - after[0]) * (after[3] - after[1])
    return intersection / (previous_area + current_area - intersection)
