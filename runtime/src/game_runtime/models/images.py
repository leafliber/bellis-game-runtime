"""客户区的取图、缩小与坐标还原。"""

import base64
import math
from dataclasses import dataclass, field
from fractions import Fraction

import cv2
import numpy as np

from game_runtime.adapter import NormalizedRect, PixelRect
from game_runtime.frame_ring import RingFrame
from game_runtime.frame_ring.ring import BYTES_PER_PIXEL
from game_runtime.frames import check_frame
from game_runtime.models.config import Coordinates, ImageSpec
from game_runtime.models.errors import ModelCallError, ModelFailure


@dataclass(frozen=True, slots=True)
class ModelImage:
    frame: RingFrame
    crop: PixelRect | None = None


@dataclass(frozen=True, slots=True)
class ImageTransform:
    frame_number: int
    transform_version: int
    crop: PixelRect
    width: int
    height: int
    coordinates: Coordinates

    def client_point(self, x: float, y: float) -> tuple[int, int]:
        """模型给的图像坐标换算为客户区的像素点。"""
        left, top, right, bottom = self.crop
        dx = self._relative(x, self.width, right - left, edge=False)
        dy = self._relative(y, self.height, bottom - top, edge=False)
        return min(left + math.floor(dx), right - 1), min(top + math.floor(dy), bottom - 1)

    def client_rect(self, left: float, top: float, right: float, bottom: float) -> PixelRect:
        """模型给的图像矩形换算为覆盖它的半开客户区矩形。"""
        x, y, crop_right, crop_bottom = self.crop
        dx1 = self._relative(left, self.width, crop_right - x, edge=True)
        dy1 = self._relative(top, self.height, crop_bottom - y, edge=True)
        dx2 = self._relative(right, self.width, crop_right - x, edge=True)
        dy2 = self._relative(bottom, self.height, crop_bottom - y, edge=True)
        if not (dx1 < dx2 and dy1 < dy2):
            raise ValueError("模型给出的图像矩形为空或边顺序不合法")
        return PixelRect(
            x + math.floor(dx1), y + math.floor(dy1), x + math.ceil(dx2), y + math.ceil(dy2)
        )

    def _relative(self, value: float, sent_size: int, crop_size: int, *, edge: bool) -> Fraction:
        limit = 1 if self.coordinates == "normalized" else sent_size
        inside = (
            0 <= value <= limit if edge or self.coordinates == "normalized" else 0 <= value < limit
        )
        if isinstance(value, bool) or not inside:
            raise ValueError("模型给出的坐标在图像范围之外")
        return Fraction(str(value)) * crop_size / limit


@dataclass(frozen=True, slots=True)
class PreparedKeyframe:
    """关键帧用途本机准备的 PNG 与帧引用。"""

    transform: ImageTransform
    capture_ns: int
    _owner: object = field(repr=False)
    _spec: ImageSpec = field(repr=False)
    _roi: NormalizedRect | None = field(repr=False)
    _url: str = field(repr=False)

    def url_for(self, owner: object, spec: ImageSpec, roi: NormalizedRect | None) -> str:
        if self._owner is not owner or (self._spec, self._roi) != (spec, roi):
            raise ModelCallError(ModelFailure.INVALID_REQUEST)
        return self._url


def encode_image(
    image: ModelImage, spec: ImageSpec, roi: NormalizedRect | None
) -> tuple[str, ImageTransform]:
    frame = image.frame
    check_frame(frame)
    bounds = (
        roi.to_pixels(frame.width, frame.height)
        if roi is not None
        else PixelRect(0, 0, frame.width, frame.height)
    )
    crop = image.crop if image.crop is not None else bounds
    if (
        any(type(edge) is not int for edge in crop)
        or not bounds.left <= crop.left < crop.right <= bounds.right
        or not bounds.top <= crop.top < crop.bottom <= bounds.bottom
    ):
        raise ModelCallError(ModelFailure.INVALID_REQUEST)

    bgra = np.ndarray(
        (frame.height, frame.width, BYTES_PER_PIXEL),
        dtype=np.uint8,
        buffer=frame.pixels,
        strides=(frame.row_stride, BYTES_PER_PIXEL, 1),
    )
    region = bgra[crop.top : crop.bottom, crop.left : crop.right]
    old_h, old_w = region.shape[:2]
    scale = min(1.0, spec.max_long_edge / max(old_w, old_h))
    width, height = max(1, round(old_w * scale)), max(1, round(old_h * scale))
    if (width, height) != (old_w, old_h):
        region = cv2.resize(region, (width, height), interpolation=cv2.INTER_AREA)
    bgr = cv2.cvtColor(region, cv2.COLOR_BGRA2BGR)
    ok, png = cv2.imencode(".png", bgr)
    if not ok:
        raise ModelCallError(ModelFailure.INVALID_REQUEST)
    url = "data:image/png;base64," + base64.b64encode(png.tobytes()).decode("ascii")
    return url, ImageTransform(
        frame.frame_number, frame.transform_version, crop, width, height, spec.coordinates
    )
