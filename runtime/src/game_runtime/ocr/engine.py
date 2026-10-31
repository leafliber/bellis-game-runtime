"""PP-OCRv5 的文字检测与识别，经 ONNX Runtime 在 CPU 上运行；前后处理自写，只用 numpy 与 OpenCV。

流程、参数与另加的两步（同行合并、识别前加宽）见 `docs/modules/perception.md` 的“引擎”；
前后处理自写还是用现成封装、另加的两步是否保留，由 GS-04 定。

DB 的外扩：PaddleOCR 用 pyclipper 把最小外接矩形按 面积 × 比例 ÷ 周长 向外偏移，再取偏移后
多边形的最小外接矩形；矩形偏移后的最小外接矩形就是各边外移同样的距离，所以这里直接算，不需要
pyclipper。
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

import cv2
import numpy as np
from numpy.typing import NDArray

from game_runtime.adapter import PixelRect
from game_runtime.artifacts import verify_artifact
from game_runtime.ocr._onnx import OnnxModel
from game_runtime.ocr.models import ModelSet

type Image = NDArray[np.uint8]
"""H×W×3 的 BGR 图像，uint8。"""
type Quad = NDArray[np.float32]
"""4×2 的四边形：左上、右上、右下、左下，(x, y)。"""

_DET_MEAN: Final = np.array([0.485, 0.456, 0.406], np.float32)
_DET_STD: Final = np.array([0.229, 0.224, 0.225], np.float32)
_DET_ALIGN: Final = 32
_MIN_BOX_SIDE: Final = 3
_SORT_ROW_TOLERANCE: Final = 10
_HORIZONTAL_DEGREES: Final = 5.0
"""上边与水平线的夹角不超过它的框算水平的。"""
_REC_HEIGHT: Final = 48
_REC_BASE_WIDTH: Final = 320
_REC_MAX_WIDTH: Final = 3200
_BLANK: Final = "blank"
"""字表首位的占位：CTC 的空白，序号 0。"""


class ModelLoadError(Exception):
    """模型文件能读出来，但与预期不符（如字表长度与识别模型的类别数不同）。"""


@dataclass(frozen=True)
class EngineOptions:
    threads: int | None = 4
    """ONNX Runtime 的算子内线程数；None 由 ONNX Runtime 决定。缺省随 GS-04 定。"""
    det_limit_side: int = 64
    det_max_side: int = 4000
    """检测输入的长边上限；缺省取 PaddleOCR 的值，随 GS-04 定。"""
    det_thresh: float = 0.3
    det_box_thresh: float = 0.6
    det_unclip_ratio: float = 1.5
    det_max_candidates: int = 1000
    merge_gap_ratio: float | None = 1.0
    """同行合并的间隙上限（行高的倍数）；None 不合并。"""
    rec_pad_ratio: float = 0.25
    """水平框识别前左右各加宽行高的这一倍数；0 不加宽。"""
    rec_batch: int = 6
    min_confidence: float = 0.5
    """识别置信度低于它的行丢弃。"""


@dataclass(frozen=True)
class RecognizedLine:
    """图像中的一行文字；`box` 是图像坐标下的外接矩形（含左上，不含右下）。"""

    text: str
    box: PixelRect
    confidence: float


class Recognizer(Protocol):
    """识别一张 BGR 图像里的全部文字行，按从上到下、从左到右排列。"""

    def recognize(self, image: Image) -> list[RecognizedLine]: ...


class Model(Protocol):
    """一个一输入一输出的模型，即 `OnnxModel`；测试可换成假的。"""

    @property
    def output_classes(self) -> int | None: ...

    def run(self, batch: NDArray[np.float32]) -> NDArray[np.float32]: ...


# 检测


def det_input_size(height: int, width: int, limit_side: int, max_side: int) -> tuple[int, int]:
    """检测模型的输入尺寸（高、宽）：短边不足 `limit_side` 时放大，长边超过 `max_side` 时缩小，
    再各取最近的 32 的倍数，至少 32。"""
    ratio = 1.0
    if min(height, width) < limit_side:
        ratio = limit_side / min(height, width)
    if max(height, width) * ratio > max_side:
        ratio = max_side / max(height, width)
    resized_h = max(round(height * ratio / _DET_ALIGN) * _DET_ALIGN, _DET_ALIGN)
    resized_w = max(round(width * ratio / _DET_ALIGN) * _DET_ALIGN, _DET_ALIGN)
    return resized_h, resized_w


def det_input(image: Image, size: tuple[int, int]) -> NDArray[np.float32]:
    """1×3×H×W 的检测输入。"""
    height, width = size
    resized = cv2.resize(image, (width, height), interpolation=cv2.INTER_LINEAR)
    normalized = (np.asarray(resized, np.float32) / 255.0 - _DET_MEAN) / _DET_STD
    return np.ascontiguousarray(normalized.transpose(2, 0, 1)[np.newaxis], dtype=np.float32)


def _box_points(center: tuple[float, float], size: tuple[float, float], angle: float) -> Quad:
    """旋转矩形的四个角，排成左上、右上、右下、左下（同 PaddleOCR 的 get_mini_boxes）。"""
    points = np.asarray(cv2.boxPoints((center, size, angle)), np.float32)
    by_x: list[list[float]] = sorted(points.tolist(), key=lambda p: p[0])
    first, second = (0, 1) if by_x[1][1] > by_x[0][1] else (1, 0)
    third, fourth = (2, 3) if by_x[3][1] > by_x[2][1] else (3, 2)
    return np.array([by_x[first], by_x[third], by_x[fourth], by_x[second]], np.float32)


def _box_score(prob: NDArray[np.float32], quad: Quad) -> float:
    """四边形内的平均概率。"""
    height, width = prob.shape
    xmin = min(max(math.floor(float(quad[:, 0].min())), 0), width - 1)
    xmax = min(max(math.ceil(float(quad[:, 0].max())), 0), width - 1)
    ymin = min(max(math.floor(float(quad[:, 1].min())), 0), height - 1)
    ymax = min(max(math.ceil(float(quad[:, 1].max())), 0), height - 1)
    mask = np.zeros((ymax - ymin + 1, xmax - xmin + 1), np.uint8)
    shifted = (quad - np.array([xmin, ymin], np.float32)).astype(np.int32)
    cv2.fillPoly(mask, [shifted], (1,))
    region = prob[ymin : ymax + 1, xmin : xmax + 1]
    return float(cv2.mean(region, mask)[0])


def db_boxes(
    prob: NDArray[np.float32],
    *,
    thresh: float,
    box_thresh: float,
    unclip_ratio: float,
    max_candidates: int,
) -> list[tuple[Quad, float]]:
    """DB 后处理：概率图上的文字框（概率图坐标）与得分。"""
    bitmap = (prob > thresh).astype(np.uint8) * 255
    contours, _ = cv2.findContours(bitmap, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    boxes: list[tuple[Quad, float]] = []
    for contour in contours[:max_candidates]:
        (cx, cy), (w, h), angle = cv2.minAreaRect(contour)
        if min(w, h) < _MIN_BOX_SIDE:
            continue
        score = _box_score(prob, _box_points((cx, cy), (w, h), angle))
        if score < box_thresh:
            continue
        distance = w * h * unclip_ratio / (2 * (w + h))
        grown = (w + 2 * distance, h + 2 * distance)
        if min(grown) < _MIN_BOX_SIDE + 2:
            continue
        boxes.append((_box_points((cx, cy), grown, angle), score))
    return boxes


def _distance(a: NDArray[np.float32], b: NDArray[np.float32]) -> float:
    return math.hypot(float(a[0] - b[0]), float(a[1] - b[1]))


def _bounds(quad: Quad) -> tuple[float, float, float, float]:
    """外接矩形：左、上、右、下。"""
    xs, ys = quad[:, 0], quad[:, 1]
    return float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())


def _is_horizontal(quad: Quad) -> bool:
    dx, dy = float(quad[1, 0] - quad[0, 0]), float(quad[1, 1] - quad[0, 1])
    return abs(math.degrees(math.atan2(dy, dx))) <= _HORIZONTAL_DEGREES


def _rect_quad(left: float, top: float, right: float, bottom: float) -> Quad:
    return np.array([[left, top], [right, top], [right, bottom], [left, bottom]], np.float32)


def sort_quads(quads: Sequence[Quad]) -> list[Quad]:
    """按从上到下、从左到右排列；左上角的纵坐标相差不到 10 像素的算同一行（同 PaddleOCR）。"""
    ordered = sorted(quads, key=lambda q: (float(q[0, 1]), float(q[0, 0])))
    for i in range(len(ordered) - 1):
        for j in range(i, -1, -1):
            upper, lower = ordered[j], ordered[j + 1]
            same_row = abs(float(lower[0, 1]) - float(upper[0, 1])) < _SORT_ROW_TOLERANCE
            if same_row and float(lower[0, 0]) < float(upper[0, 0]):
                ordered[j], ordered[j + 1] = lower, upper
            else:
                break
    return ordered


def merge_rows(quads: Sequence[Quad], gap_ratio: float) -> list[Quad]:
    """把同一行上相邻的水平框合并为一个外接矩形。

    纵向重叠不少于较矮者一半的框算同一行；同一行里按横坐标排列，相邻两框的间隙不超过
    `gap_ratio` 乘以两者中较高的那个就合并。不水平的框原样保留。结果按 `sort_quads` 排列。
    DB 常把一行文字在全角标点处断开，或把行末的句号单独检出成小框；分开识别会丢掉标点或认错。
    """
    boxes = [_bounds(quad) for quad in quads if _is_horizontal(quad)]
    others = [quad for quad in quads if not _is_horizontal(quad)]
    rows: list[list[tuple[float, float, float, float]]] = []
    for box in sorted(boxes, key=lambda b: (b[1] + b[3]) / 2):
        for row in rows:
            top = min(b[1] for b in row)
            bottom = max(b[3] for b in row)
            overlap = min(box[3], bottom) - max(box[1], top)
            if overlap >= 0.5 * min(box[3] - box[1], bottom - top):
                row.append(box)
                break
        else:
            rows.append([box])
    merged: list[Quad] = []
    for row in rows:
        row.sort(key=lambda b: b[0])
        left, top, right, bottom = row[0]
        for b_left, b_top, b_right, b_bottom in row[1:]:
            height = max(bottom - top, b_bottom - b_top)
            if b_left - right <= gap_ratio * height:
                top, right, bottom = min(top, b_top), max(right, b_right), max(bottom, b_bottom)
            else:
                merged.append(_rect_quad(left, top, right, bottom))
                left, top, right, bottom = b_left, b_top, b_right, b_bottom
        merged.append(_rect_quad(left, top, right, bottom))
    return sort_quads([*merged, *others])


def pad_horizontal(quad: Quad, ratio: float, width: int) -> Quad:
    """水平框左右各加宽 `ratio` 倍行高，不超出 [0, `width`]；不水平的原样返回。

    DB 常漏掉行末的标点（句号、逗号画在字格的左下角，概率低）；加宽后识别模型能看到它。
    """
    if not _is_horizontal(quad):
        return quad
    left, top, right, bottom = _bounds(quad)
    pad = ratio * (bottom - top)
    return _rect_quad(max(0.0, left - pad), top, min(float(width), right + pad), bottom)


# 识别


def crop_quad(image: Image, quad: Quad) -> Image | None:
    """把四边形透视变换为水平的行图；竖长（高 ≥ 1.5 倍宽）的转 90 度。太小时为 None。"""
    width = int(max(_distance(quad[0], quad[1]), _distance(quad[2], quad[3])))
    height = int(max(_distance(quad[0], quad[3]), _distance(quad[1], quad[2])))
    if width < 1 or height < 1:
        return None
    target = np.array([[0, 0], [width, 0], [width, height], [0, height]], np.float32)
    matrix = cv2.getPerspectiveTransform(quad.astype(np.float32), target)
    crop = np.asarray(
        cv2.warpPerspective(
            image, matrix, (width, height), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
        ),
        np.uint8,
    )
    if height / width >= 1.5:
        crop = np.rot90(crop)
    return np.ascontiguousarray(crop, dtype=np.uint8)


def rec_input(crops: Sequence[Image]) -> NDArray[np.float32]:
    """N×3×48×W 的识别输入：高缩放到 48，宽按比例，补零到这一批里最宽的（至少 320，至多 3200）。"""
    ratios = [crop.shape[1] / crop.shape[0] for crop in crops]
    max_ratio = max(_REC_BASE_WIDTH / _REC_HEIGHT, *ratios)
    batch_width = min(int(_REC_HEIGHT * max_ratio), _REC_MAX_WIDTH)
    batch = np.zeros((len(crops), 3, _REC_HEIGHT, batch_width), np.float32)
    for index, (crop, ratio) in enumerate(zip(crops, ratios, strict=True)):
        width = min(batch_width, math.ceil(_REC_HEIGHT * ratio))
        resized = cv2.resize(crop, (width, _REC_HEIGHT), interpolation=cv2.INTER_LINEAR)
        normalized = (np.asarray(resized, np.float32) / 255.0 - 0.5) / 0.5
        batch[index, :, :, :width] = normalized.transpose(2, 0, 1)
    return batch


def ctc_decode(probs: NDArray[np.float32], characters: Sequence[str]) -> list[tuple[str, float]]:
    """CTC 贪心解码。`probs` 为 N×T×C 的概率，`characters[0]` 是空白。"""
    indices = probs.argmax(axis=2)
    confidences = probs.max(axis=2)
    results: list[tuple[str, float]] = []
    for row, scores in zip(indices, confidences, strict=True):
        keep = row != 0
        keep[1:] &= row[1:] != row[:-1]
        kept: list[int] = row[keep].tolist()
        text = "".join(characters[index] for index in kept)
        confidence = float(scores[keep].mean()) if kept else 0.0
        results.append((text, confidence))
    return results


def read_dictionary(path: Path) -> list[str]:
    """字表：UTF-8，每行一个字；只按换行分，不按其它 Unicode 行分隔符分。"""
    lines = path.read_bytes().decode("utf-8").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line.removesuffix("\r") for line in lines]


class OcrEngine:
    """检测加识别，同步、不是线程安全的；`ocr@1` 在它自己的一个工作线程里调用。"""

    def __init__(
        self,
        det: Model,
        rec: Model,
        dictionary: Sequence[str],
        options: EngineOptions,
        *,
        model: str,
    ) -> None:
        self.model = model
        """模型组的名字。"""
        self.options = options
        self._det = det
        self._rec = rec
        self._characters = [_BLANK, *dictionary, " "]
        classes = rec.output_classes
        if classes is not None and classes != len(self._characters):
            raise ModelLoadError(
                f"字表有 {len(dictionary)} 个字，识别模型有 {classes} 类，应为字数加 2"
            )

    @classmethod
    def load(cls, model_set: ModelSet, data_dir: Path, options: EngineOptions) -> "OcrEngine":
        """按摘要核对本地数据目录里的制品后加载；没有或不符即抛 `ArtifactError`。"""
        det_path = verify_artifact(model_set.det, data_dir)
        rec_path = verify_artifact(model_set.rec, data_dir)
        dictionary = read_dictionary(verify_artifact(model_set.dictionary, data_dir))
        return cls(
            OnnxModel(det_path, options.threads),
            OnnxModel(rec_path, options.threads),
            dictionary,
            options,
            model=model_set.name,
        )

    def detect(self, image: Image) -> list[Quad]:
        """图像中的文字框（图像坐标），按从上到下、从左到右排列。"""
        height, width = image.shape[:2]
        options = self.options
        size = det_input_size(height, width, options.det_limit_side, options.det_max_side)
        prob = self._det.run(det_input(image, size))[0, 0]
        scale = np.array([width / size[1], height / size[0]], np.float32)
        limit = np.array([width, height], np.float32)
        quads: list[Quad] = []
        for quad, _ in db_boxes(
            prob,
            thresh=options.det_thresh,
            box_thresh=options.det_box_thresh,
            unclip_ratio=options.det_unclip_ratio,
            max_candidates=options.det_max_candidates,
        ):
            scaled = np.clip(quad * scale, 0, limit).astype(np.float32)
            if min(_distance(scaled[0], scaled[1]), _distance(scaled[0], scaled[3])) > 3:
                quads.append(scaled)
        if options.merge_gap_ratio is not None:
            return merge_rows(quads, options.merge_gap_ratio)
        return sort_quads(quads)

    def read_lines(self, crops: Sequence[Image]) -> list[tuple[str, float]]:
        """识别若干行图，按输入的顺序给出文字与置信度。"""
        order = sorted(range(len(crops)), key=lambda i: crops[i].shape[1] / crops[i].shape[0])
        results: list[tuple[str, float]] = [("", 0.0)] * len(crops)
        step = max(1, self.options.rec_batch)
        for start in range(0, len(order), step):
            chunk = order[start : start + step]
            probs = self._rec.run(rec_input([crops[i] for i in chunk]))
            for index, result in zip(chunk, ctc_decode(probs, self._characters), strict=True):
                results[index] = result
        return results

    def recognize(self, image: Image) -> list[RecognizedLine]:
        height, width = image.shape[:2]
        pad = self.options.rec_pad_ratio
        # 识别框报检测框本身；加宽只用于裁出给识别模型的行图。
        pairs = [
            (quad, crop_quad(image, pad_horizontal(quad, pad, width) if pad > 0 else quad))
            for quad in self.detect(image)
        ]
        kept = [(quad, crop) for quad, crop in pairs if crop is not None]
        texts = self.read_lines([crop for _, crop in kept])
        lines: list[RecognizedLine] = []
        for (quad, _), (raw, confidence) in zip(kept, texts, strict=True):
            text = raw.strip()
            if not text or confidence < self.options.min_confidence:
                continue
            left, top, right, bottom = _bounds(quad)
            box = PixelRect(
                max(0, math.floor(left)),
                max(0, math.floor(top)),
                min(width, math.ceil(right)),
                min(height, math.ceil(bottom)),
            )
            lines.append(RecognizedLine(text, box, confidence))
        return lines
