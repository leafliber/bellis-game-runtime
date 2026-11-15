"""用合成 BGRA 验证裁剪、行填充和坐标还原。"""

import base64

import cv2
import numpy as np
import pytest

from game_runtime.adapter import NormalizedRect, PixelRect
from game_runtime.frame_ring import RingFrame
from game_runtime.models import ImageTransform, ModelCallError, ModelFailure, ModelImage
from game_runtime.models.config import ImageSpec
from game_runtime.models.images import encode_image


def test_crop_precedes_resize_and_only_cropped_bgr_is_sent() -> None:
    rows = np.full((6, 45), 255, dtype=np.uint8)
    pixels = rows[:, :40].reshape((6, 10, 4))
    pixels[2:5, 2:9] = [20, 80, 140, 9]
    frame = RingFrame(7, 0, 42, 10, 6, 45, rows.tobytes())
    crop = PixelRect(2, 2, 9, 5)
    url, transform = encode_image(
        ModelImage(frame, crop), ImageSpec(max_long_edge=4), NormalizedRect(0.1, 0.0, 0.9, 1.0)
    )
    assert url.startswith("data:image/png;base64,")
    png = base64.b64decode(url.split(",", 1)[1], validate=True)
    decoded = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_UNCHANGED)
    assert decoded is not None
    assert decoded.shape == (2, 4, 3)
    assert np.all(decoded == [20, 80, 140])

    assert transform.frame_number == 7
    assert transform.transform_version == 42
    assert transform.crop == crop
    assert transform.client_point(3, 1) == (7, 3)
    assert transform.client_rect(1.25, 0.5, 4, 2) == PixelRect(4, 2, 9, 5)
    with pytest.raises(ValueError, match="范围"):
        transform.client_point(4, 1)
    with pytest.raises(ValueError, match="矩形"):
        transform.client_rect(2, 0, 2, 1)


def test_normalized_edges_and_no_upscale() -> None:
    frame = RingFrame(1, 0, 1, 10, 4, 40, bytes(160))
    _, transform = encode_image(
        ModelImage(frame), ImageSpec(coordinates="normalized"), NormalizedRect(0.3, 0.25, 0.8, 0.75)
    )
    assert (transform.width, transform.height) == (5, 2)
    assert transform.client_point(0, 0) == (3, 1)
    assert transform.client_point(0.5, 0.5) == (5, 2)
    assert transform.client_point(1, 1) == (7, 2)
    assert transform.client_rect(0, 0, 1, 1) == PixelRect(3, 1, 8, 3)
    for bad in (-0.01, 1.01, float("nan"), float("inf"), True):
        with pytest.raises(ValueError, match="范围"):
            transform.client_point(bad, 0)


def test_decimal_coordinate_edges_use_exact_scaling() -> None:
    crop = PixelRect(10, 20, 110, 120)
    normalized = ImageTransform(1, 1, crop, 40, 40, "normalized")
    assert normalized.client_point(0.58, 0.58) == (68, 78)
    assert normalized.client_rect(0.0, 0.0, 0.14, 0.14) == PixelRect(10, 20, 24, 34)
    assert normalized.client_point(0.5799999999999998, 0.5801) == (67, 78)
    assert normalized.client_rect(0.0, 0.0, 0.1399, 0.1400000000000001) == PixelRect(10, 20, 24, 35)

    pixels = ImageTransform(1, 1, crop, 100, 50, "pixels")
    assert pixels.client_point(58.0, 29.0) == (68, 78)
    assert pixels.client_point(57.9999, 29.0001) == (67, 78)
    assert pixels.client_rect(0.0, 0.0, 14.0, 7.0001) == PixelRect(10, 20, 24, 35)


@pytest.mark.parametrize("crop", [None, PixelRect(0, 0, 4, 4), PixelRect(2, 2, 2, 4)])
def test_collapsed_roi_and_crop_outside_roi_are_rejected(crop: PixelRect | None) -> None:
    frame = RingFrame(1, 0, 1, 4, 4, 16, bytes(64))
    roi = (
        NormalizedRect(0.01, 0.0, 0.05, 1.0) if crop is None else NormalizedRect(0.5, 0.5, 1.0, 1.0)
    )
    with pytest.raises(ModelCallError) as failure:
        encode_image(ModelImage(frame, crop), ImageSpec(), roi)
    assert failure.value.reason is ModelFailure.INVALID_REQUEST
