"""用合成帧、签名结果与 OCR 框核对 GR-38 的独立证据。"""

from dataclasses import replace

import pytest

from game_runtime.adapter import PixelRect, ScreenType
from game_runtime.frame_ring import RingFrame
from game_runtime.ocr import OcrLine, OcrResult
from game_runtime.scene import (
    ChangeDetector,
    ChangeParams,
    SceneState,
    SceneUpdate,
    ScreenClassification,
    ScreenObservation,
    ScreenSource,
    ScreenState,
    Stability,
    Verdict,
    bgra_view,
)
from game_runtime.scene.evidence import (
    CapturedScene,
    EvidenceFailure,
    MissingEvidence,
    changed_regions,
    check_interval,
    same_crop,
)
from game_runtime.step_control.completion import CompletionProof, prove_completion
from game_runtime.step_control.contracts import EffectKind, ExpectedEffect, Goal, TextTarget
from game_runtime.step_control.target_tracking import SceneSample, TrackedText, track_text

PANEL = PixelRect(9, 10, 51, 43)
TITLE = OcrLine("工作台", PixelRect(12, 12, 29, 18), 0.96)
OPEN = OcrLine("打开", PixelRect(13, 25, 32, 32), 0.97)


def capture(
    epoch: object,
    sequence: int,
    *,
    changes: tuple[tuple[int, int, int], ...] = (),
    transform: int = 1,
    screen: str | None = "workbench",
    capture_ns: int | None = None,
) -> CapturedScene:
    width, height = 80, 60
    stride = width * 4 + 4
    pixels = bytearray(stride * height)
    for row in range(height):
        pixels[row * stride + width * 4] = sequence  # 行填充的变化不算画面变化。
    for x, y, value in changes:
        pixels[y * stride + x * 4] = value
    stamp = sequence * 10 if capture_ns is None else capture_ns
    frame = RingFrame(sequence * 3, stamp, transform, width, height, stride, bytes(pixels))
    state = SceneState(
        frame.frame_number,
        stamp,
        transform,
        width,
        height,
        Stability.CHANGING,
        0,
        0.1,
        False,
        None,
        None,
        Verdict.NONE if screen is None else Verdict.SCREEN,
        screen,
        () if screen is None else (screen,),
        (),
        None,
    )
    flags = (
        ScreenState()
        if screen is None
        else ScreenState(ScreenClassification(ScreenType.MENU, ScreenSource.SIGNATURE, 1.0))
    )
    return CapturedScene(SceneUpdate(frame, state, flags), epoch, sequence)


def sample(
    frame: CapturedScene,
    lines: tuple[OcrLine, ...] = (TITLE, OPEN),
    *,
    rect: PixelRect = PANEL,
    roi: str | None = "panel",
) -> SceneSample:
    identity = frame.update.frame
    return SceneSample(
        frame, OcrResult(identity.frame_number, identity.transform_version, roi, rect, lines)
    )


def track(
    samples: list[SceneSample], target: TextTarget | None = None
) -> TrackedText | EvidenceFailure:
    return track_text(
        samples[0].capture,
        target or TextTarget(text="打开", roi="panel"),
        samples,
        now_ns=31,
        max_age_ns=12,
        max_gap_ns=14,
        min_confidence=0.9,
        min_overlap=0.6,
    )


def complete(
    effect: ExpectedEffect, samples: list[SceneSample], *, started: int = 10, deadline: int = 40
) -> CompletionProof | EvidenceFailure:
    return prove_completion(
        Goal("完成调查", effect, deadline, 7),
        samples,
        goal_started_ns=started,
        now_ns=31,
        max_age_ns=12,
        max_gap_ns=14,
        stable_for_ns=10,
        text_rect=PANEL,
        min_text_confidence=0.9,
    )


def test_original_pixels_reveal_tiny_changes_even_when_grid_is_unchanged() -> None:
    epoch = object()
    first = capture(epoch, 1)
    second = capture(epoch, 2, changes=((1, 1, 3), (2, 2, 3), (74, 49, 3)))
    detector = ChangeDetector(ChangeParams(grid_width=4))
    for item in (first, second):
        result = detector.update(bgra_view(item.update.frame), item.update.frame.capture_ns)
    assert result.changed_fraction == 0
    assert changed_regions(first, second, max_regions=2) == (
        PixelRect(1, 1, 3, 3),
        PixelRect(74, 49, 75, 50),
    )
    assert changed_regions(first, second, max_regions=1) is EvidenceFailure.INSUFFICIENT
    assert same_crop(first, second, PANEL)
    assert not same_crop(first, second, PixelRect(72, 48, 76, 52))
    assert changed_regions(first, capture(epoch, 2), max_regions=1) == ()


def test_continuity_uses_the_admitted_sequence_and_fresh_terminal_frame() -> None:
    epoch = object()
    first, second = capture(epoch, 1), capture(epoch, 2)
    assert first.update.frame.frame_number + 1 < second.update.frame.frame_number
    assert check_interval([first, second], now_ns=22, max_age_ns=2, max_gap_ns=10) is None
    assert (
        check_interval([first, second], now_ns=23, max_age_ns=2, max_gap_ns=10)
        is EvidenceFailure.INVALIDATED
    )


def test_future_terminal_frame_waits_until_the_monotonic_clock_reaches_it() -> None:
    epoch = object()
    first = capture(epoch, 1, capture_ns=100)
    future = capture(epoch, 2, capture_ns=112)
    assert (
        check_interval([first, future], now_ns=111, max_age_ns=5, max_gap_ns=14)
        is EvidenceFailure.INSUFFICIENT
    )
    assert check_interval([first, future], now_ns=112, max_age_ns=5, max_gap_ns=14) is None


@pytest.mark.parametrize("fault", ["epoch", "skip", "transform", "pause", "time-reversed"])
def test_continuity_detects_distinct_capture_failures(fault: str) -> None:
    epoch = object()
    first = capture(epoch, 1)
    after = {
        "epoch": capture(object(), 2),
        "skip": capture(epoch, 3),
        "transform": capture(epoch, 2, transform=2),
        "pause": capture(epoch, 2, capture_ns=27),
        "time-reversed": capture(epoch, 2, capture_ns=9),
    }[fault]
    assert check_interval([first, after], now_ns=30, max_age_ns=21, max_gap_ns=10) is (
        EvidenceFailure.INVALIDATED
    )


def test_scene_evidence_rejects_metadata_from_another_frame() -> None:
    epoch = object()
    first, second = capture(epoch, 1), capture(epoch, 2)
    with pytest.raises(ValueError, match="判别结果与采集帧不符"):
        CapturedScene(replace(first.update, state=second.update.state), epoch, 1)


@pytest.mark.parametrize(
    ("group", "label", "anchor"),
    [
        (PANEL, OPEN, TITLE),
        (
            PixelRect(6, 41, 64, 57),
            OcrLine("打开", PixelRect(32, 44, 47, 51), 0.96),
            OcrLine("工具", PixelRect(9, 44, 22, 51), 0.97),
        ),
    ],
)
def test_track_returns_current_geometry_for_two_layouts_with_old_scope_gone(
    group: PixelRect, label: OcrLine, anchor: OcrLine
) -> None:
    def shift(rect: PixelRect) -> PixelRect:
        return PixelRect(rect.left + 2, rect.top, rect.right + 2, rect.bottom)

    epoch = object()
    first = capture(epoch, 1, changes=((70, 2, 90),))
    second = capture(epoch, 2, changes=((71, 2, 80),))
    last = capture(epoch, 3, changes=((74, 2, 170),))
    moved_label = replace(label, box=shift(label.box))
    moved_anchor = replace(anchor, box=shift(anchor.box))
    samples = [
        sample(first, (anchor, label), rect=group),
        sample(second, (anchor, label), rect=group),
        sample(last, (moved_anchor, moved_label), rect=shift(group)),
    ]
    assert first.update.observation_scope is not last.update.observation_scope
    assert first.update.frame.pixels != last.update.frame.pixels
    result = track(samples)
    assert isinstance(result, TrackedText)
    assert result.capture is last
    assert result.line is moved_label
    assert result.missing == {
        MissingEvidence.MODAL_STATE,
        MissingEvidence.REGION_VISIBILITY,
        MissingEvidence.CONTROL_AVAILABILITY,
    }
    assert samples[0].ocr is not None
    assert samples[0].ocr.lines[1] is label


@pytest.mark.parametrize("missing", [0, 1])
def test_text_must_be_present_from_origin_through_the_last_frame(missing: int) -> None:
    epoch = object()
    samples = [sample(capture(epoch, i + 1)) for i in range(3)]
    samples[missing] = sample(samples[missing].capture, (TITLE,))
    assert track(samples) is EvidenceFailure.NO_MATCH


@pytest.mark.parametrize("fault", ["back-to-same-screen", "duplicate", "old-ocr", "new-panel"])
def test_same_label_does_not_repair_broken_interaction_evidence(fault: str) -> None:
    epoch = object()
    first = sample(capture(epoch, 1))
    second = sample(capture(epoch, 2))
    last = sample(capture(epoch, 3))
    if fault == "back-to-same-screen":
        second = sample(capture(epoch, 2, screen="another-panel"))
    elif fault == "duplicate":
        duplicate = replace(OPEN, box=PixelRect(32, 25, 49, 32))
        second = sample(second.capture, (TITLE, OPEN, duplicate))
    elif fault == "old-ocr":
        last = replace(last, ocr=first.ocr)
    else:
        new_rect = PixelRect(53, 10, 78, 43)
        new_title = replace(TITLE, box=PixelRect(55, 12, 73, 18))
        new_target = replace(OPEN, box=PixelRect(55, 25, 73, 32))
        second = sample(second.capture, (new_title, new_target), rect=new_rect)
    assert track([first, second, last]) is EvidenceFailure.INVALIDATED


def test_coordinate_alone_and_lone_unsigned_label_are_uncovered() -> None:
    epoch = object()
    samples = [sample(capture(epoch, i, screen=None), (OPEN,)) for i in (1, 2, 3)]
    assert track(samples, TextTarget(point=(15, 26))) is EvidenceFailure.UNSUPPORTED
    assert track(samples) is EvidenceFailure.UNSUPPORTED


@pytest.mark.parametrize(
    "effect",
    [
        ExpectedEffect(EffectKind.SIGNATURE, signature="workbench"),
        ExpectedEffect(EffectKind.SCREEN, screen_type=ScreenType.MENU),
        ExpectedEffect(EffectKind.TEXT_APPEARED, text="打开", roi="panel"),
    ],
)
def test_completion_uses_a_typed_condition_on_changing_new_frames(effect: ExpectedEffect) -> None:
    epoch = object()
    first = capture(epoch, 1, changes=((71, 2, 80),))
    last = capture(epoch, 2, changes=((71, 2, 90), (17, 27, 120)))
    assert first.update.observation_scope is not last.update.observation_scope
    assert first.update.frame.pixels != last.update.frame.pixels
    proof = complete(effect, [sample(first), sample(last)])
    assert isinstance(proof, CompletionProof)
    assert proof.generation == 7
    assert proof.capture is last
    assert proof.missing == {MissingEvidence.MODAL_STATE, MissingEvidence.REGION_VISIBILITY}


@pytest.mark.parametrize(("first_ns", "last_ns"), [(20, 32), (1_000_000, 1_000_012)])
def test_future_frames_cannot_complete_the_stability_window(first_ns: int, last_ns: int) -> None:
    epoch = object()
    first = capture(epoch, 1, capture_ns=first_ns)
    last = capture(epoch, 2, capture_ns=last_ns)
    expected = ExpectedEffect(EffectKind.SIGNATURE, signature="workbench")
    assert (
        complete(expected, [SceneSample(first), SceneSample(last)]) is EvidenceFailure.INSUFFICIENT
    )


def test_completion_reuses_only_unchanged_crop_without_rebinding_ocr() -> None:
    epoch = object()
    first = sample(capture(epoch, 1))
    same = SceneSample(capture(epoch, 2, changes=((73, 1, 50),)))
    changed = SceneSample(capture(epoch, 2, changes=((15, 26, 1),)))
    expected = ExpectedEffect(EffectKind.TEXT_APPEARED, "打开", "panel")
    assert complete(expected, [first, same]) == CompletionProof(7, same.capture)
    assert complete(expected, [first, changed]) is EvidenceFailure.INSUFFICIENT
    assert complete(expected, [first, replace(same, ocr=first.ocr)]) is EvidenceFailure.INVALIDATED
    assert first.ocr is not None
    assert first.ocr.frame_number == first.capture.update.frame.frame_number


def test_completion_requires_current_goal_interval_and_signature_source() -> None:
    epoch = object()
    first, last = capture(epoch, 1), capture(epoch, 2)
    expected = ExpectedEffect(EffectKind.SCREEN, screen_type=ScreenType.MENU)
    samples = [SceneSample(first), SceneSample(last)]
    assert complete(expected, samples, started=11) is EvidenceFailure.INVALIDATED
    assert complete(expected, samples, deadline=31) is EvidenceFailure.INVALIDATED
    unknown = [capture(epoch, i, screen=None) for i in (1, 2)]
    observation = ScreenObservation(screen_type=ScreenType.MENU, confidence=1.0)
    observed = [
        SceneSample(
            replace(
                item,
                update=replace(item.update, screen_state=ScreenState(observations=(observation,))),
            )
        )
        for item in unknown
    ]
    assert complete(expected, observed) is EvidenceFailure.NO_MATCH

    without_matches = [
        SceneSample(
            replace(item, update=replace(item.update, state=replace(item.update.state, matched=())))
        )
        for item in (first, last)
    ]
    assert complete(expected, without_matches) is EvidenceFailure.NO_MATCH


def test_completion_cannot_skip_an_uncertified_middle_read() -> None:
    epoch = object()
    first = sample(capture(epoch, 1))
    obscured = SceneSample(capture(epoch, 2, changes=((15, 26, 120),)))
    last = sample(capture(epoch, 3))
    expected = ExpectedEffect(EffectKind.TEXT_APPEARED, "打开", "panel")
    assert same_crop(first.capture, last.capture, PANEL)
    assert complete(expected, [first, obscured, last]) is EvidenceFailure.INSUFFICIENT


@pytest.mark.parametrize(
    "kind", [EffectKind.CHANGED, EffectKind.UNCHANGED, EffectKind.TEXT_DISAPPEARED]
)
def test_uncovered_completion_cannot_treat_empty_ocr_or_motion_as_success(kind: EffectKind) -> None:
    epoch = object()
    effect = ExpectedEffect(kind, "打开", "panel")
    samples = [sample(capture(epoch, i, changes=((70, 2, i),)), ()) for i in (1, 2)]
    assert complete(effect, samples) is EvidenceFailure.UNSUPPORTED
    present = ExpectedEffect(EffectKind.TEXT_APPEARED, "打开", "panel")
    assert complete(present, samples) is EvidenceFailure.INSUFFICIENT
