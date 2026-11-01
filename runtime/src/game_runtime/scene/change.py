"""变化与稳定检测，与游戏无关：降采样的格子图逐帧比较，时刻取帧的采集时刻；持续整屏的动态是播片。

规则见 `docs/modules/perception.md` 的“变化与稳定”。
"""

from dataclasses import dataclass
from enum import StrEnum

import cv2
import numpy as np
from numpy.typing import NDArray

from game_runtime.frame_ring import RingFrame
from game_runtime.frame_ring.ring import BYTES_PER_PIXEL

type Bgra = NDArray[np.uint8]
"""BGRA 像素，形状为（高，宽，4），行可以带填充。"""


def bgra_view(frame: RingFrame) -> Bgra:
    """帧的像素看作（高，宽，4）的数组，不拷贝；行尾的填充不在视图里。"""
    rows = np.frombuffer(frame.pixels, np.uint8, count=frame.row_stride * frame.height)
    rows = rows.reshape(frame.height, frame.row_stride)
    return rows[:, : frame.width * BYTES_PER_PIXEL].reshape(frame.height, frame.width, 4)


class Stability(StrEnum):
    STABLE = "stable"
    """已稳定：稳定时长不短于 `stable_after`。"""
    CHANGING = "changing"
    """变化中：逐字显示、计时条走动、局部动画。"""
    TRANSITION = "transition"
    """过场：变化比例不低于 `transition_fraction`，即切换画面、淡入淡出、整屏的动画。"""


@dataclass(frozen=True, slots=True)
class ChangeParams:
    grid_width: int = 64
    """格子图的列数；行数按宽高比取整，至少 1。"""
    cell_threshold: int = 8
    """格子的任一通道之差超过它即算变化（满量程 255）。"""
    stable_after_ns: int = 250_000_000
    """稳定时长不短于它即已稳定。"""
    transition_fraction: float = 0.5
    """未稳定时变化比例不低于它即过场。"""
    motion_fraction: float = 0.1
    """变化比例不低于它的帧为动帧。"""
    playing_gap_ns: int = 500_000_000
    """相邻两个动帧相隔不超过它即连成一段，隔得更远即另起一段。"""
    playing_after_ns: int = 2_000_000_000
    """当前一段动帧从第一个到最近一个覆盖不短于它，即在播片中。"""
    playing_end_ns: int = 1_500_000_000
    """在播片中时连续这么久没有动帧，即不再在播片中。"""

    def __post_init__(self) -> None:
        if self.grid_width < 1:
            raise ValueError(f"grid_width 须为正数：{self.grid_width}")
        if not 0 <= self.cell_threshold < 255:
            raise ValueError(f"cell_threshold 须在 0 到 254 之间：{self.cell_threshold}")
        if self.stable_after_ns < 1:
            raise ValueError(f"stable_after_ns 须为正数：{self.stable_after_ns}")
        if not 0 < self.transition_fraction <= 1:
            raise ValueError(f"transition_fraction 须在 (0, 1] 内：{self.transition_fraction}")
        if not 0 < self.motion_fraction <= 1:
            raise ValueError(f"motion_fraction 须在 (0, 1] 内：{self.motion_fraction}")
        if self.playing_gap_ns < 1:
            raise ValueError(f"playing_gap_ns 须为正数：{self.playing_gap_ns}")
        if self.playing_after_ns < 1:
            raise ValueError(f"playing_after_ns 须为正数：{self.playing_after_ns}")
        if self.playing_end_ns < 1:
            raise ValueError(f"playing_end_ns 须为正数：{self.playing_end_ns}")


@dataclass(frozen=True, slots=True)
class Change:
    stability: Stability
    stable_ns: int
    """稳定时长：本帧的采集时刻减最近一次有变化的帧的采集时刻，本帧有变化时为 0。"""
    changed_fraction: float
    """与上一处理帧相比变化的格子比例；没有可比的上一帧时为 1。"""
    playing: bool
    """本帧在播片中。"""
    playing_since_ns: int | None
    """在播片中时，这段播片的开始时刻：这一段动帧的第一个动帧的采集时刻。"""
    playing_ended_ns: int | None
    """本帧起不再在播片中时，刚结束的播片的结束时刻：最后一个动帧之后那一帧的采集时刻。"""


class ChangeDetector:
    """逐帧更新的变化与稳定检测。结果只取决于依次处理的帧，不读墙钟。不是线程安全的。"""

    def __init__(self, params: ChangeParams | None = None) -> None:
        self.params = params or ChangeParams()
        self._previous: NDArray[np.int16] | None = None
        self._previous_size: tuple[int, int] | None = None
        self._last_change_ns = 0
        self._playing = _PlayingDetector(self.params)

    def update(self, bgra: Bgra, capture_ns: int) -> Change:
        grid = self._grid(bgra)
        size = (bgra.shape[1], bgra.shape[0])
        previous = self._previous
        if previous is None or self._previous_size != size:
            changed = 1.0  # 第一帧与尺寸变化后的第一帧没有可比的上一帧
        else:
            diff = np.abs(grid - previous).max(axis=2)
            changed = float(np.count_nonzero(diff > self.params.cell_threshold) / diff.size)
        self._previous = grid
        self._previous_size = size
        if changed > 0:
            self._last_change_ns = capture_ns
        stable_ns = max(0, capture_ns - self._last_change_ns)
        if stable_ns >= self.params.stable_after_ns:
            stability = Stability.STABLE
        elif changed >= self.params.transition_fraction:
            stability = Stability.TRANSITION
        else:
            stability = Stability.CHANGING
        since, ended = self._playing.update(capture_ns, changed >= self.params.motion_fraction)
        return Change(stability, stable_ns, changed, since is not None, since, ended)

    def _grid(self, bgra: Bgra) -> NDArray[np.int16]:
        """降采样成格子图，每格是所覆盖像素的平均 BGR；帧比格子图还小时按帧的尺寸。"""
        height, width = bgra.shape[:2]
        columns = min(self.params.grid_width, width)
        rows = min(max(1, round(self.params.grid_width * height / width)), height)
        small = cv2.resize(bgra, (columns, rows), interpolation=cv2.INTER_AREA)
        return np.asarray(small, dtype=np.uint8)[:, :, :3].astype(np.int16)


class _PlayingDetector:
    """播片：相邻相隔不超过 `playing_gap_ns` 的动帧连成一段，当前这一段从第一个到最近一个动帧覆盖
    不短于 `playing_after_ns` 时进入；此后连续 `playing_end_ns` 没有动帧即结束，之后的动帧另起
    一段。只按采集时刻。"""

    def __init__(self, params: ChangeParams) -> None:
        self.params = params
        self._running = False
        """有当前这一段：处理过动帧，播片结束之后还没有新的动帧。"""
        self._first = 0
        """当前这一段的第一个动帧的采集时刻；在播片中时即开始时刻。"""
        self._last = 0
        """当前这一段最近一个动帧的采集时刻。"""
        self._playing = False
        self._after_last: int | None = None
        """在播片中时最后一个动帧之后那一帧的采集时刻；最后一帧就是动帧时为 None。"""

    def update(self, capture_ns: int, motion: bool) -> tuple[int | None, int | None]:
        """处理一帧，返回（在播片中时的开始时刻，本帧结束了播片时的结束时刻）。"""
        params = self.params
        if self._playing:
            if motion:
                self._last, self._after_last = capture_ns, None
                return self._first, None
            if self._after_last is None:
                self._after_last = capture_ns
            if capture_ns - self._last < params.playing_end_ns:
                return self._first, None
            self._playing = self._running = False  # 结束之后的动帧另起一段
            return None, self._after_last
        if not motion:
            return None, None
        if not self._running or capture_ns - self._last > params.playing_gap_ns:
            self._running, self._first = True, capture_ns
        self._last = capture_ns
        if capture_ns - self._first < params.playing_after_ns:
            return None, None
        self._playing, self._after_last = True, None
        return self._first, None
