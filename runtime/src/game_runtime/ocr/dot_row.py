"""从完整对白裁片量测单行省略号的几何证据。"""

from dataclasses import dataclass

import cv2
import numpy as np

from game_runtime.adapter import PixelRect
from game_runtime.ocr.engine import Image


@dataclass(frozen=True, slots=True)
class DotRow:
    dots: tuple[PixelRect, ...]

    @property
    def count(self) -> int:
        return len(self.dots)

    @property
    def box(self) -> PixelRect:
        return PixelRect(
            self.dots[0].left,
            min(dot.top for dot in self.dots),
            self.dots[-1].right,
            max(dot.bottom for dot in self.dots),
        )

    def translated(self, x: int, y: int) -> "DotRow":
        return DotRow(
            tuple(
                PixelRect(dot.left + x, dot.top + y, dot.right + x, dot.bottom + y)
                for dot in self.dots
            )
        )


def measure_dot_row(image: Image) -> DotRow | None:
    """每个亮区都须属于同一紧凑、等距、边界完整的水平点列。"""
    bright = cv2.inRange(image, (185, 185, 185), (255, 255, 255))
    count, _, components, _ = cv2.connectedComponentsWithStats(bright, connectivity=8)
    if count < 4:  # 连通区计数含背景，省略号至少含三个点。
        return None
    regions = components[1:]
    regions = regions[regions[:, cv2.CC_STAT_LEFT].argsort()]
    x, y, width, height, area = regions.T
    min_width, min_height = int(width.min()), int(height.min())
    max_width, max_height = int(width.max()), int(height.max())
    if (
        min(min_width, min_height) < 2
        or np.any((width > 2 * height) | (height > 2 * width) | (2 * area < width * height))
        or max_width - min_width > max(1, min_width / 3)
        or max_height - min_height > max(1, min_height / 3)
        or area.max() > 2 * area.min()
    ):
        return None
    center_y = y + height / 2
    gaps = np.diff(x + width / 2)
    pitch = float(np.median(gaps))
    if (
        float(center_y.max() - center_y.min()) > max(1, min_height / 4)
        or pitch < 2 * max_width
        or pitch > 6 * min_width
        or np.any(np.abs(gaps - pitch) > max(1, pitch / 8))
    ):
        return None
    image_height, image_width = image.shape[:2]
    if (
        x[0] < pitch
        or image_width - int(x[-1] + width[-1]) < pitch
        or y.min() < max_height
        or image_height - int((y + height).max()) < max_height
    ):
        return None
    # 较暗的字边可紧邻其亮区。裁片其它位置有疑似字迹时不认点列。
    ink = cv2.inRange(image, (0, 0, 0), (164, 164, 164)) == 0
    radius = max(1, min_height // 3)
    halo = cv2.dilate(bright, np.ones((2 * radius + 1, 2 * radius + 1), np.uint8))
    if np.any(ink & (halo == 0)):
        return None
    return DotRow(
        tuple(
            PixelRect(int(left), int(top), int(left + w), int(top + h))
            for left, top, w, h in zip(x, y, width, height, strict=True)
        )
    )
