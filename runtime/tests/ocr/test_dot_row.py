"""用合成像素核对完整点列、框与弃权条件。"""

from typing import Literal

import numpy as np
import pytest

from game_runtime.adapter import PixelRect
from game_runtime.ocr.dot_row import measure_dot_row
from game_runtime.ocr.engine import Image


def dot_image(count: int, *, left: int = 26) -> Image:
    image = np.full((64, 340, 3), 32, np.uint8)
    for index in range(count):
        x = left + index * 14
        image[18:22, x : x + 3 + index % 2] = 235
    return image


@pytest.mark.parametrize("count", [3, 7, 17])
def test_measure_all_dots_and_their_physical_boxes(count: int) -> None:
    row = measure_dot_row(dot_image(count))
    assert row is not None
    assert row.count == count
    assert row.dots[0] == PixelRect(26, 18, 29, 22)
    assert row.box == PixelRect(26, 18, 26 + (count - 1) * 14 + 3 + (count - 1) % 2, 22)
    assert row.translated(50, 10).dots[0] == PixelRect(76, 28, 79, 32)


@pytest.mark.parametrize("fault", ["cutoff", "speck", "separator", "text", "dim-text", "off-row"])
def test_refuse_ambiguous_or_incomplete_text(
    fault: Literal["cutoff", "speck", "separator", "text", "dim-text", "off-row"],
) -> None:
    image = dot_image(6, left=0 if fault == "cutoff" else 26)
    match fault:
        case "speck":
            image[43, 257] = 220
        case "separator":
            image[38:40, 58:220] = 230
        case "text":
            image[15:38, 177:181] = 225
            image[25:28, 169:189] = 225
        case "dim-text":
            image[20:31, 170:174] = 170
        case "off-row":
            image[28:32, 146:149] = 220
        case "cutoff":
            pass
    assert measure_dot_row(image) is None
