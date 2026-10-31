"""引擎的前后处理，不用模型文件：检测输入尺寸、DB 后处理、同行合并、加宽、排序、裁出、
识别输入、CTC 解码、字表读取，以及用假模型串起的整条识别路径。
"""

from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from game_runtime.adapter import PixelRect
from game_runtime.ocr import EngineOptions, ModelLoadError, OcrEngine
from game_runtime.ocr.engine import (
    Quad,
    crop_quad,
    ctc_decode,
    db_boxes,
    det_input_size,
    merge_rows,
    pad_horizontal,
    read_dictionary,
    rec_input,
    sort_quads,
)


def rect(left: float, top: float, right: float, bottom: float) -> Quad:
    return np.array([[left, top], [right, top], [right, bottom], [left, bottom]], np.float32)


def bounds(quad: Quad) -> tuple[float, float, float, float]:
    xs, ys = quad[:, 0], quad[:, 1]
    return float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())


# 检测


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        pytest.param((170, 1120), (160, 1120), id="ROI at native size, rounded to 32"),
        pytest.param((40, 200), (64, 320), id="short side below 64 is enlarged"),
        pytest.param((1000, 8000), (512, 4000), id="long side above 4000 is reduced"),
        pytest.param((5, 5), (64, 64), id="tiny"),
    ],
)
def test_detector_input_size(size: tuple[int, int], expected: tuple[int, int]) -> None:
    assert det_input_size(*size, limit_side=64, max_side=4000) == expected


def test_db_finds_each_region_and_expands_it() -> None:
    prob = np.zeros((100, 300), np.float32)
    prob[20:40, 10:110] = 0.9
    prob[60:80, 150:290] = 0.95

    boxes = db_boxes(prob, thresh=0.3, box_thresh=0.6, unclip_ratio=1.5, max_candidates=1000)

    found = sorted((bounds(quad), score) for quad, score in boxes)
    assert len(found) == 2
    for (left, top, right, bottom), score in found:
        assert score > 0.85
        assert right > left
        assert bottom > top
    (left, top, right, bottom), _ = found[0]
    # 最小外接矩形约 99×19（轮廓的像素中心），外扩 面积 × 1.5 ÷ 周长。
    w, h = 99.0, 19.0
    d = w * h * 1.5 / (2 * (w + h))
    assert left == pytest.approx(10 - d, abs=1.0)
    assert right == pytest.approx(109 + d, abs=1.0)
    assert top == pytest.approx(20 - d, abs=1.0)
    assert bottom == pytest.approx(39 + d, abs=1.0)


def test_db_drops_faint_and_tiny_regions() -> None:
    prob = np.zeros((100, 300), np.float32)
    prob[20:40, 10:110] = 0.4  # 过了二值化阈值，平均概率不到 0.6
    prob[60:62, 150:152] = 0.99  # 太小

    assert db_boxes(prob, thresh=0.3, box_thresh=0.6, unclip_ratio=1.5, max_candidates=1000) == []


def test_boxes_sort_top_to_bottom_then_left_to_right() -> None:
    a, b, c = rect(200, 12, 300, 40), rect(10, 15, 100, 40), rect(10, 60, 100, 90)

    ordered = sort_quads([c, a, b])

    assert [bounds(q)[0:2] for q in ordered] == [(10, 15), (200, 12), (10, 60)]


def test_a_line_split_at_punctuation_is_merged() -> None:
    first, second = rect(160, 1017, 367, 1088), rect(408, 1011, 1007, 1094)

    assert [bounds(q) for q in merge_rows([second, first], 1.0)] == [(160, 1011, 1007, 1094)]


def test_a_trailing_period_box_joins_its_line() -> None:
    main, period = rect(160, 1014, 1000, 1089), rect(996, 1063, 1009, 1075)

    assert [bounds(q) for q in merge_rows([main, period], 1.0)] == [(160, 1014, 1009, 1089)]


def test_separate_lines_and_distant_items_stay_apart() -> None:
    line_one, line_two = rect(80, 500, 600, 540), rect(80, 548, 500, 588)
    name, price = rect(100, 150, 200, 190), rect(1000, 150, 1100, 190)

    merged = merge_rows([line_one, line_two, name, price], 1.0)

    assert sorted(bounds(q) for q in merged) == sorted(
        bounds(q) for q in (line_one, line_two, name, price)
    )


def test_tilted_boxes_are_not_merged() -> None:
    tilted = np.array([[0, 0], [100, 30], [95, 50], [-5, 20]], np.float32)
    flat = rect(102, 30, 200, 60)

    merged = merge_rows([tilted, flat], 1.0)

    assert len(merged) == 2
    assert any(np.array_equal(q, tilted) for q in merged)


def test_padding_widens_horizontal_boxes_within_the_image() -> None:
    assert bounds(pad_horizontal(rect(10, 0, 100, 40), 0.25, 105)) == (0, 0, 105, 40)
    assert bounds(pad_horizontal(rect(50, 0, 60, 8), 0.25, 200)) == (48, 0, 62, 8)


# 识别


def test_axis_aligned_crop_is_the_slice() -> None:
    image = np.arange(60 * 80 * 3, dtype=np.uint32).reshape(60, 80, 3).astype(np.uint8)

    crop = crop_quad(image, rect(10, 5, 50, 25))

    assert crop is not None
    assert crop.shape == (20, 40, 3)
    assert np.array_equal(crop, image[5:25, 10:50])


def test_tall_crops_are_rotated_and_empty_ones_dropped() -> None:
    image = np.zeros((100, 100, 3), np.uint8)

    tall = crop_quad(image, rect(10, 10, 20, 60))
    assert tall is not None
    assert tall.shape[:2] == (10, 50)
    assert crop_quad(image, rect(10, 10, 10.5, 60)) is None


def test_recognizer_input_pads_to_the_widest_line() -> None:
    narrow = np.full((24, 24, 3), 255, np.uint8)
    wide = np.full((48, 960, 3), 255, np.uint8)

    batch = rec_input([narrow, wide])

    assert batch.shape == (2, 3, 48, 960)
    assert batch.dtype == np.float32
    # 白色归一为 1，补的是 0。
    assert batch[0, :, :, :48].min() == pytest.approx(1.0)
    assert np.all(batch[0, :, :, 48:] == 0)
    assert rec_input([narrow]).shape == (1, 3, 48, 320)


def one_hot(indices: list[list[int]], classes: int, value: float = 0.9) -> NDArray[np.float32]:
    probs = np.full((len(indices), len(indices[0]), classes), (1 - value) / (classes - 1))
    for row, sequence in enumerate(indices):
        for step, index in enumerate(sequence):
            probs[row, step, index] = value
    return probs.astype(np.float32)


def test_ctc_collapses_repeats_and_drops_blanks() -> None:
    characters = ["blank", "雨", "停", "了", " "]
    probs = one_hot([[1, 1, 0, 2, 2, 0, 2, 3], [0, 0, 0, 0, 0, 0, 0, 0]], len(characters))

    decoded = ctc_decode(probs, characters)

    assert decoded[0][0] == "雨停停了"
    assert decoded[0][1] == pytest.approx(0.9)
    assert decoded[1] == ("", 0.0)


def test_the_dictionary_splits_only_on_newlines(tmp_path: Path) -> None:
    path = tmp_path / "dict.txt"
    path.write_bytes("一\r\n \n\x85\n二\n".encode())

    assert read_dictionary(path) == ["一", " ", "\x85", "二"]


# 用假模型串起整条路径


class FakeDetector:
    """深色像素即文字：概率图在输入的深色处为 1。"""

    output_classes = None

    def run(self, batch: NDArray[np.float32]) -> NDArray[np.float32]:
        dark = batch[0, 0] < 0  # 归一化后低于均值即深色
        return dark.astype(np.float32)[np.newaxis, np.newaxis]


class FakeRecognizerModel:
    """每行都读成“甲乙”，置信度 0.9；第二个参数给 0.3 时都读成低置信度。"""

    def __init__(self, classes: int, value: float = 0.9) -> None:
        self.output_classes = classes
        self._value = value

    def run(self, batch: NDArray[np.float32]) -> NDArray[np.float32]:
        sequence = [1, 1, 0, 2]
        return one_hot([sequence] * batch.shape[0], self.output_classes or 0, self._value)


DICTIONARY = ["甲", "乙", "丙"]


def white_with_dark_bars(bars: list[tuple[int, int, int, int]]) -> NDArray[np.uint8]:
    image = np.full((200, 600, 3), 255, np.uint8)
    for left, top, right, bottom in bars:
        image[top:bottom, left:right] = 0
    return image


def test_the_pipeline_reports_lines_in_image_coordinates() -> None:
    engine = OcrEngine(
        FakeDetector(),
        FakeRecognizerModel(len(DICTIONARY) + 2),
        DICTIONARY,
        EngineOptions(),
        model="fake",
    )
    image = white_with_dark_bars([(40, 120, 300, 150), (40, 30, 500, 60)])

    lines = engine.recognize(image)

    assert [line.text for line in lines] == ["甲乙", "甲乙"]
    first, second = (line.box for line in lines)
    # 上面那条先报；识别框是检测框本身（外扩过），不含识别前的加宽。
    assert first.top < 30 < 60 < first.bottom
    assert first.left < 40
    assert first.right > 500
    assert first.right < 500 + 30
    assert second.top < 120 < 150 < second.bottom
    assert all(isinstance(value, int) for value in first)
    assert lines[0].confidence == pytest.approx(0.9)


def test_low_confidence_lines_are_dropped() -> None:
    engine = OcrEngine(
        FakeDetector(),
        FakeRecognizerModel(len(DICTIONARY) + 2, value=0.3),
        DICTIONARY,
        EngineOptions(min_confidence=0.5),
        model="fake",
    )

    assert engine.recognize(white_with_dark_bars([(40, 30, 500, 60)])) == []


def test_a_blank_image_has_no_lines() -> None:
    engine = OcrEngine(
        FakeDetector(),
        FakeRecognizerModel(len(DICTIONARY) + 2),
        DICTIONARY,
        EngineOptions(),
        model="fake",
    )

    assert engine.recognize(white_with_dark_bars([])) == []


def test_a_dictionary_that_does_not_fit_the_model_is_rejected() -> None:
    with pytest.raises(ModelLoadError, match="字表有 3 个字，识别模型有 10 类"):
        OcrEngine(
            FakeDetector(), FakeRecognizerModel(10), DICTIONARY, EngineOptions(), model="fake"
        )


def test_recognized_boxes_are_pixel_rects() -> None:
    engine = OcrEngine(
        FakeDetector(),
        FakeRecognizerModel(len(DICTIONARY) + 2),
        DICTIONARY,
        EngineOptions(),
        model="fake",
    )

    (only,) = engine.recognize(white_with_dark_bars([(40, 30, 500, 60)]))

    assert isinstance(only.box, PixelRect)
