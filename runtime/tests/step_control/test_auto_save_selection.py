"""用合成的画面和 OCR 回执核对只读自动存档选择。"""

import asyncio
from dataclasses import replace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from game_runtime.adapter import GAMES_DIR, Adapter, ScreenType, load_adapter
from game_runtime.adapter.schema import AutoSaveSlot
from game_runtime.frame_ring import RingFrame
from game_runtime.ocr import OcrLine, OcrResult, OcrService
from game_runtime.operator import Clock
from game_runtime.scene import (
    SceneHub,
    SceneState,
    SceneUpdate,
    ScreenClassification,
    ScreenSource,
    ScreenState,
    Stability,
    Verdict,
)
from game_runtime.step_control.recovery import newest_auto_save
from game_runtime.step_control.risk import ActionRefusedError, Located

_OLDER = "2143-03-02 01:04:05"
_NEWER = "2143-03-02 01:05:06"
_EMPTY = "Empty"
_AGE = 100_000_000


@pytest.fixture
def adapter() -> Adapter:
    raw = load_adapter(GAMES_DIR / "synthetic-game").model_dump(mode="json")
    raw["screens"].append({"id": "archive", "type": ScreenType.SAVE_LOAD.value, "title": "Archive"})
    raw["signatures"].append(
        {
            "id": "archive-mark",
            "screen": "archive",
            "texts": [{"roi": "popup-text", "text": "Archive"}],
        }
    )
    raw["restarts"] = {}
    raw["recoveries"] = {
        "bad-ending": {
            "open": {"roi": "ending-retry", "text": "Restore", "success_signature": "archive-mark"},
            "success_signature": "dialogue",
            "timestamp_format": "%Y-%m-%d %H:%M:%S",
            "empty_text": _EMPTY,
            "slots": [
                {"label": "One:", "label_roi": "menu-start", "click_roi": "shop-list"},
                {"label": "Two:", "label_roi": "menu-shop", "click_roi": "shop-leave"},
                {"label": "Three:", "label_roi": "menu-help", "click_roi": "choice-prompt"},
            ],
        }
    }
    return Adapter.model_validate(raw)


@pytest.fixture
def scene() -> SceneHub:
    hub = SceneHub(problems=(), uncalibrated=())
    frame = RingFrame(7, 1_000_000_000, 1, 160, 90, 640, bytes(57_600))
    state = SceneState(
        frame.frame_number,
        frame.capture_ns,
        frame.transform_version,
        frame.width,
        frame.height,
        Stability.STABLE,
        250_000_000,
        0,
        False,
        None,
        None,
        Verdict.SCREEN,
        "archive",
        ("archive-mark",),
        (),
        None,
    )
    hub.publish(
        SceneUpdate(
            frame,
            state,
            ScreenState(ScreenClassification(ScreenType.SAVE_LOAD, ScreenSource.SIGNATURE, 1)),
        )
    )
    return hub


def _scan(
    adapter: Adapter,
    scene: SceneHub,
    values: tuple[str, ...],
    *,
    fault: str = "",
    received: list[tuple[RingFrame, str]] | None = None,
) -> tuple[AutoSaveSlot, Located]:
    update = scene.latest
    assert update is not None
    recovery = adapter.recoveries["bad-ending"]
    clock = Mock(spec=Clock)
    clock.now_ns.return_value = update.frame.capture_ns + 1
    if fault == "old-frame":
        clock.now_ns.return_value += _AGE
    slots = dict(zip((slot.label_roi for slot in recovery.slots), recovery.slots, strict=True))
    stamps = dict(zip(slots, values, strict=True))

    async def recognize(frame: RingFrame, name: str) -> OcrResult:
        if received is not None:
            received.append((frame, name))
        rect = adapter.roi(name).to_pixels(frame.width, frame.height)
        first = name == recovery.slots[0].label_roi
        if first and fault == "scene-changed":
            scene.invalidate()
        if first and fault == "expired-ocr":
            clock.now_ns.return_value += _AGE
        label = "Wrong:" if first and fault == "wrong-label" else slots[name].label
        confidence = 0.4 if first and fault == "low-confidence" else 0.95
        number = frame.frame_number + 1 if first and fault == "other-frame" else frame.frame_number
        lines = (OcrLine(f"{label} {stamps[name]}", rect, confidence),)
        return OcrResult(number, frame.transform_version, name, rect, lines)

    ocr = Mock(spec=OcrService)
    ocr.recognize.side_effect = recognize
    if fault == "unsigned":
        scene.publish(replace(update, state=replace(update.state, matched=())))
        update = scene.latest
        assert update is not None
    if fault == "missing-slot":
        recovery = recovery.model_copy(update={"slots": recovery.slots[1:]})
    return asyncio.run(
        newest_auto_save(adapter, scene, ocr, update, recovery, clock=clock, max_frame_age_ns=_AGE)
    )


def test_reads_every_slot_on_the_declared_frame(adapter: Adapter, scene: SceneHub) -> None:
    received: list[tuple[RingFrame, str]] = []
    picked, proof = _scan(adapter, scene, (_EMPTY, _OLDER, _NEWER), received=received)
    update = scene.latest
    assert update is not None
    slots = adapter.recoveries["bad-ending"].slots
    assert picked == slots[-1]
    assert received == [(update.frame, slot.label_roi) for slot in slots]
    assert [read.roi for read in proof.reads] == [slot.label_roi for slot in slots]


@pytest.mark.parametrize(
    ("values", "error"),
    [
        ((_OLDER, _NEWER, _NEWER), "recovery-autosave-tied"),
        ((_EMPTY, _EMPTY, _EMPTY), "recovery-no-autosave"),
        ((_NEWER, _OLDER, "2143-03-02"), "recovery-time-unverified"),
        ((_NEWER, _OLDER, "2143-03-02 01:04:5"), "recovery-time-unverified"),
    ],
)
def test_requires_a_verified_unique_latest(
    adapter: Adapter, scene: SceneHub, values: tuple[str, ...], error: str
) -> None:
    with pytest.raises(ActionRefusedError, match=error):
        _scan(adapter, scene, values)


@pytest.mark.parametrize(
    ("fault", "error"),
    [
        ("unsigned", "recovery-menu-unverified"),
        ("old-frame", "recovery-frame-stale"),
        ("expired-ocr", "recovery-frame-stale"),
        ("scene-changed", "scene-changed"),
        ("other-frame", "stale-ocr"),
        ("wrong-label", "recovery-slot-label-mismatch"),
        ("low-confidence", "recovery-slot-ocr-unverified"),
        ("missing-slot", "recovery-undeclared"),
    ],
)
def test_rejects_unverified_evidence(
    adapter: Adapter, scene: SceneHub, fault: str, error: str
) -> None:
    with pytest.raises(ActionRefusedError, match=error):
        _scan(adapter, scene, (_OLDER, _NEWER, _EMPTY), fault=fault)


@pytest.mark.parametrize("fmt", ["%Y-%m-%d", "%y-%m-%d %H:%M:%S"])
def test_adapter_requires_complete_dates_and_times(adapter: Adapter, fmt: str) -> None:
    raw = adapter.model_dump(mode="json")
    raw["recoveries"]["bad-ending"]["timestamp_format"] = fmt
    with pytest.raises(ValidationError, match="须包含完整日期与时间"):
        Adapter.model_validate(raw)
