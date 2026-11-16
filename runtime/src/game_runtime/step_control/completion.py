"""在独立的新帧上累积完成条件的视觉证据。任务授权和完成事件由控制器负责。"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from game_runtime.adapter import PixelRect
from game_runtime.ocr import OcrLine, OcrResult
from game_runtime.scene.evidence import (
    CapturedScene,
    EvidenceFailure,
    MissingEvidence,
    check_interval,
    same_crop,
    screen_identity,
)
from game_runtime.scene.signatures import Verdict
from game_runtime.scene.state import ScreenSource
from game_runtime.step_control.contracts import EffectKind, ExpectedEffect, Goal
from game_runtime.step_control.risk import text_key
from game_runtime.step_control.target_tracking import SceneSample, check_text_read


@dataclass(frozen=True, slots=True)
class CompletionProof:
    """给出匹配的完成条件证据，调用方另外核对未覆盖的画面状态及任务授权。"""

    generation: int
    capture: CapturedScene
    missing: frozenset[MissingEvidence] = field(
        init=False,
        default=frozenset({MissingEvidence.MODAL_STATE, MissingEvidence.REGION_VISIBILITY}),
    )


def prove_completion(
    goal: Goal,
    samples: Sequence[SceneSample],
    *,
    goal_started_ns: int,
    now_ns: int,
    max_age_ns: int,
    max_gap_ns: int,
    stable_for_ns: int,
    text_rect: PixelRect | None = None,
    min_text_confidence: float | None = None,
) -> CompletionProof | EvidenceFailure:
    """核对同一目标代次的一段新鲜帧。文字条件使用由适配包解析的证据区域。"""
    if stable_for_ns < 1:
        raise ValueError("证据稳定时长须为正")
    expected = goal.completion
    if expected is None or expected.kind in (
        EffectKind.CHANGED,
        EffectKind.UNCHANGED,
        EffectKind.TEXT_DISAPPEARED,
    ):
        # 文字缺席需独立的可见性和可靠否定证据。空 OCR 结果不能代替它。
        return EvidenceFailure.UNSUPPORTED
    if now_ns < goal_started_ns or (goal.deadline_ns is not None and now_ns >= goal.deadline_ns):
        return EvidenceFailure.INVALIDATED
    frames = [sample.capture for sample in samples]
    failure = check_interval(frames, now_ns=now_ns, max_age_ns=max_age_ns, max_gap_ns=max_gap_ns)
    if failure is not None:
        return failure

    first, last = frames[0], frames[-1]
    start = first.update.frame.capture_ns
    if start < goal_started_ns:
        return EvidenceFailure.INVALIDATED
    if last.update.frame.capture_ns - start < stable_for_ns:
        return EvidenceFailure.INSUFFICIENT
    baseline = screen_identity(first)
    if any(screen_identity(frame) != baseline for frame in frames[1:]):
        return EvidenceFailure.INVALIDATED

    if expected.kind is EffectKind.TEXT_APPEARED:
        if text_rect is None or min_text_confidence is None:
            return EvidenceFailure.UNSUPPORTED
        if not 0 < min_text_confidence <= 1:
            raise ValueError("文字置信度门槛须在 (0, 1] 内")
        failure = _visible_text(samples, expected, text_rect, min_text_confidence)
        if failure is not None:
            return failure
    elif not all(_signed_match(frame, expected) for frame in frames):
        return EvidenceFailure.NO_MATCH
    return CompletionProof(goal.generation, last)


def _signed_match(frame: CapturedScene, expected: ExpectedEffect) -> bool:
    state = frame.update.state
    if state.verdict is not Verdict.SCREEN or not state.screen or not state.matched:
        return False
    if expected.kind is EffectKind.SIGNATURE:
        return expected.signature in state.matched
    classification = frame.update.screen_state.baseline
    return (
        classification.source is ScreenSource.SIGNATURE
        and classification.screen_type is expected.screen_type
    )


def _visible_text(
    samples: Sequence[SceneSample],
    expected: ExpectedEffect,
    rect: PixelRect,
    confidence: float,
) -> EvidenceFailure | None:
    key = text_key(expected.text or "")
    if not key:
        return EvidenceFailure.UNSUPPORTED
    source: CapturedScene | None = None
    previous: OcrLine | None = None
    for sample in samples:
        read = sample.ocr
        if read is None:
            if source is None or not same_crop(source, sample.capture, rect):
                return EvidenceFailure.INSUFFICIENT
            continue
        if (
            read.roi != expected.roi
            or read.rect != rect
            or not check_text_read(sample.capture, read)
        ):
            return EvidenceFailure.INVALIDATED
        line = _unique_line(read, key, confidence)
        if isinstance(line, EvidenceFailure):
            return line
        if previous is not None and (
            text_key(line.text) != text_key(previous.text) or line.box != previous.box
        ):
            return EvidenceFailure.INVALIDATED
        previous = line
        source = sample.capture
    return None


def _unique_line(read: OcrResult, key: str, confidence: float) -> OcrLine | EvidenceFailure:
    matches = tuple(line for line in read.lines if key in text_key(line.text))
    if not matches:
        return EvidenceFailure.INSUFFICIENT if not read.lines else EvidenceFailure.NO_MATCH
    if len(matches) != 1:
        return EvidenceFailure.INVALIDATED
    line = matches[0]
    return line if line.confidence >= confidence else EvidenceFailure.INSUFFICIENT
