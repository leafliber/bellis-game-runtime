"""从同一张已签名的新鲜帧选择唯一最新的自动存档。"""

import asyncio
from collections.abc import Sequence
from datetime import datetime

from game_runtime.adapter import Adapter, ScreenType, Signature
from game_runtime.adapter.schema import AutoSaveRecovery, AutoSaveSlot
from game_runtime.ocr import OcrResult, OcrService
from game_runtime.operator import Clock
from game_runtime.scene import SceneService, SceneUpdate, ScreenSource, Stability, Verdict
from game_runtime.step_control.risk import ActionRefusedError, Located, text_key, verify_ocr

_MIN_CONFIDENCE = 0.8
_SAVE_LOAD_ONLY = frozenset({ScreenType.SAVE_LOAD})


def _require_menu(
    update: SceneUpdate, signature: Signature, now_ns: int, max_frame_age_ns: int
) -> None:
    frame, state = update.frame, update.state
    if not 0 <= now_ns - frame.capture_ns <= max_frame_age_ns:
        raise ActionRefusedError("recovery-frame-stale")
    if (
        state.frame_number != frame.frame_number
        or state.capture_ns != frame.capture_ns
        or state.transform_version != frame.transform_version
        or (state.width, state.height) != (frame.width, frame.height)
        or state.verdict is not Verdict.SCREEN
        or state.stability is not Stability.STABLE
        or state.playing
        or state.screen != signature.screen
        or signature.id not in state.matched
        or update.screen_state.classification.source is not ScreenSource.SIGNATURE
        or update.screen_state.restrictions != _SAVE_LOAD_ONLY
    ):
        raise ActionRefusedError("recovery-menu-unverified")


async def newest_auto_save(
    adapter: Adapter,
    scene: SceneService,
    ocr: OcrService,
    update: SceneUpdate,
    recovery: AutoSaveRecovery,
    *,
    clock: Clock,
    max_frame_age_ns: int,
) -> tuple[AutoSaveSlot, Located]:
    """读取当前观察范围内的一张帧，返回槽位及其定位证据。"""
    if recovery not in adapter.recoveries.values():
        raise ActionRefusedError("recovery-undeclared")
    menu = next(
        (sig for sig in adapter.signatures if sig.id == recovery.open.success_signature), None
    )
    if (
        menu is None
        or not menu.calibrated
        or adapter.generic_type(menu.screen) is not ScreenType.SAVE_LOAD
    ):
        raise ActionRefusedError("recovery-menu-unverified")
    latest = scene.latest
    if latest is None or latest.observation_scope is not update.observation_scope:
        raise ActionRefusedError("scene-changed")
    now = clock.now_ns()
    _require_menu(update, menu, now, max_frame_age_ns)
    if latest is not update:
        _require_menu(latest, menu, now, max_frame_age_ns)

    frame = update.frame
    names = tuple(slot.label_roi for slot in recovery.slots)
    reads = tuple(await asyncio.gather(*(ocr.recognize(frame, name) for name in names)))
    for name, result in zip(names, reads, strict=True):
        verify_ocr(update, result)
        if result.roi != name or result.rect != adapter.roi(name).to_pixels(
            frame.width, frame.height
        ):
            raise ActionRefusedError("stale-ocr")
    current = scene.with_ocr(update, reads)
    if current is None:
        raise ActionRefusedError("scene-changed")
    _require_menu(current, menu, clock.now_ns(), max_frame_age_ns)
    if any(
        line not in current.screen_state.text_targets for result in reads for line in result.lines
    ):
        raise ActionRefusedError("recovery-slot-ocr-unverified")
    slot = _unique_latest(recovery, reads)
    box = adapter.roi(slot.click_roi).to_pixels(frame.width, frame.height)
    if box.left >= box.right or box.top >= box.bottom:
        raise ActionRefusedError("recovery-slot-unlocatable")
    return slot, Located(((box.left + box.right) // 2, (box.top + box.bottom) // 2), reads=reads)


def _unique_latest(recovery: AutoSaveRecovery, reads: Sequence[OcrResult]) -> AutoSaveSlot:
    fmt = recovery.timestamp_pattern
    empty = None if recovery.empty_text is None else text_key(recovery.empty_text)
    candidates: list[tuple[datetime, AutoSaveSlot]] = []
    for slot, result in zip(recovery.slots, reads, strict=True):
        if not result.lines or any(
            not _MIN_CONFIDENCE <= line.confidence <= 1.0 for line in result.lines
        ):
            raise ActionRefusedError("recovery-slot-ocr-unverified")
        label = text_key(slot.label)
        observed = text_key(result.text)
        if not observed.startswith(label):
            raise ActionRefusedError("recovery-slot-label-mismatch")
        stamp = observed[len(label) :]
        if stamp == empty:
            continue
        try:
            at = datetime.strptime(stamp, fmt)
        except ValueError as error:
            raise ActionRefusedError("recovery-time-unverified") from error
        if text_key(at.strftime(fmt)) != stamp:
            raise ActionRefusedError("recovery-time-unverified")
        candidates.append((at, slot))
    if not candidates:
        raise ActionRefusedError("recovery-no-autosave")
    latest = max(at for at, _ in candidates)
    selected = [slot for at, slot in candidates if at == latest]
    if len(selected) != 1:
        raise ActionRefusedError("recovery-autosave-tied")
    (slot,) = selected
    return slot
