"""GR-38 的采集连续性和原图变化证据。像素差分只说明哪些像素发生了变化。"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise

import cv2
import numpy as np

from game_runtime.adapter import PixelRect, ScreenType
from game_runtime.frames import check_frame
from game_runtime.scene.analyzer import SceneUpdate
from game_runtime.scene.change import Stability, bgra_view
from game_runtime.scene.signatures import Verdict


class EvidenceFailure(StrEnum):
    INSUFFICIENT = "insufficient"
    INVALIDATED = "invalidated"
    NO_MATCH = "no-match"
    UNSUPPORTED = "unsupported"


class MissingEvidence(StrEnum):
    MODAL_STATE = "modal-state"
    REGION_VISIBILITY = "region-visibility"
    CONTROL_AVAILABILITY = "control-availability"


@dataclass(frozen=True, slots=True)
class CapturedScene:
    """保留原始判帧，由帧源所有者给出连续性标记与接纳序号。"""

    update: SceneUpdate
    epoch: object
    sequence: int
    """已判帧接纳序号。定序帧源可跳过未采样的帧环槽，帧号本身不连续。"""

    def __post_init__(self) -> None:
        frame = self.update.frame
        state = self.update.state
        check_frame(frame)
        if self.sequence < 1 or self.epoch is None:
            raise ValueError("已判帧须有来源标记和正的接纳序号")
        if (
            state.frame_number,
            state.capture_ns,
            state.transform_version,
            state.width,
            state.height,
        ) != (
            frame.frame_number,
            frame.capture_ns,
            frame.transform_version,
            frame.width,
            frame.height,
        ):
            raise ValueError("判别结果与采集帧不符")

    @property
    def usable(self) -> bool:
        state = self.update.state
        return (
            state.stability is not Stability.TRANSITION
            and not state.playing
            and not self.update.screen_state.uncertain_signature
            and state.verdict in (Verdict.SCREEN, Verdict.NONE)
        )


def contains(outer: PixelRect, inner: PixelRect) -> bool:
    return (
        outer.left <= inner.left < inner.right <= outer.right
        and outer.top <= inner.top < inner.bottom <= outer.bottom
    )


def screen_identity(
    capture: CapturedScene,
) -> tuple[Verdict, str | None, frozenset[str], frozenset[ScreenType]]:
    """一个采集区间内须保持一致的判别结果和限制。"""
    update = capture.update
    state = update.state
    return state.verdict, state.screen, frozenset(state.matched), update.screen_state.restrictions


def _same_geometry(before: CapturedScene, after: CapturedScene) -> bool:
    first, second = before.update.frame, after.update.frame
    return before.epoch is after.epoch and (
        first.transform_version,
        first.width,
        first.height,
        first.row_stride,
    ) == (second.transform_version, second.width, second.height, second.row_stride)


def check_interval(
    frames: Sequence[CapturedScene], *, now_ns: int, max_age_ns: int, max_gap_ns: int
) -> EvidenceFailure | None:
    """核对连续的已判帧和末帧帧龄。旧的原观察可随策略调用在途而变陈旧。"""
    if max_age_ns < 0 or max_gap_ns < 1:
        raise ValueError("帧龄上限须非负，帧间隔上限须为正")
    if len(frames) < 2:
        return EvidenceFailure.INSUFFICIENT
    age_ns = now_ns - frames[-1].update.frame.capture_ns
    if age_ns < 0:
        return EvidenceFailure.INSUFFICIENT
    if age_ns > max_age_ns or any(not item.usable for item in frames):
        return EvidenceFailure.INVALIDATED
    for before, after in pairwise(frames):
        first, second = before.update.frame, after.update.frame
        if (
            not _same_geometry(before, after)
            or after.sequence != before.sequence + 1
            or second.frame_number <= first.frame_number
            or not 0 < second.capture_ns - first.capture_ns <= max_gap_ns
        ):
            return EvidenceFailure.INVALIDATED
    return None


def same_crop(before: CapturedScene, after: CapturedScene, rect: PixelRect) -> bool:
    """比较同坐标的可见像素。调用方另行核对两帧之间的连续性。"""
    frame = before.update.frame
    if not _same_geometry(before, after) or not contains(
        PixelRect(0, 0, frame.width, frame.height), rect
    ):
        return False
    rows = slice(rect.top, rect.bottom)
    columns = slice(rect.left, rect.right)
    return bool(
        np.array_equal(
            bgra_view(frame)[rows, columns], bgra_view(after.update.frame)[rows, columns]
        )
    )


def changed_regions(
    before: CapturedScene, after: CapturedScene, *, max_regions: int
) -> tuple[PixelRect, ...] | EvidenceFailure:
    """按原图找相连的差异，返回矩形包围框。超出数量上限时交给调用方保守处理。"""
    if max_regions < 1:
        raise ValueError("变化区域数量上限须为正")
    if not _same_geometry(before, after):
        return EvidenceFailure.INVALIDATED
    first, second = bgra_view(before.update.frame), bgra_view(after.update.frame)
    changed = np.any(first != second, axis=2).astype(np.uint8)
    count, _, stats, _ = cv2.connectedComponentsWithStats(changed, connectivity=8)
    if count - 1 > max_regions:
        return EvidenceFailure.INSUFFICIENT
    return tuple(sorted(_bounds(row) for row in stats[1:]))


def _bounds(row: np.ndarray[tuple[int], np.dtype[np.int32]]) -> PixelRect:
    left, top, width, height, _ = map(int, row)
    return PixelRect(left, top, left + width, top + height)
