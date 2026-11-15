"""画面签名的判别：像素检查、锚点模板与 OCR 固定文字，恰好一个画面命中才给出画面。

规则见 `docs/modules/perception.md` 的“画面签名的判别”与“固定文字的 OCR 接口”。
"""

import hashlib
import logging
import math
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol
from weakref import ReferenceType, ref

import cv2
import numpy as np

from game_runtime.adapter import Adapter, Anchor, PixelCheck, PixelRect, Resolution, RoiText
from game_runtime.adapter.schema import Signature
from game_runtime.frame_ring import RingFrame
from game_runtime.kernel import ServiceToken
from game_runtime.scene.change import Bgra
from game_runtime.scene.templates import Bgr, TemplateError, is_uniform, load_template

logger = logging.getLogger(__name__)

ASPECT_TOLERANCE: Final = 0.01
"""本帧宽高比与期望的宽高比相差超过这个比例即分辨率不符。"""
TEXT_CACHE_SIZE: Final = 32
"""按 ROI 与像素内容缓存的 OCR 结果的条数。"""


class TextReader(Protocol):
    """签名的固定文字项所用的 OCR 接口，由 `ocr@1` 的服务提供。"""

    def read_lines(self, frame: RingFrame, rect: PixelRect) -> Sequence[str]:
        """识别 `frame` 在 `rect`（客户区物理像素）内的文字，按阅读顺序返回各行。

        在 `scene@1` 的工作线程里同步调用，可以阻塞，须能在多个线程里同时调用；识别不了时抛异常。
        """
        ...


OCR: Final = ServiceToken[TextReader]("ocr", 1)
"""`scene@1` 的可选依赖：只用到它的 `read_lines`。"""


class Outcome(StrEnum):
    """一个签名的判别结果。"""

    MATCH = "match"
    """全部组成项通过。"""
    NO_MATCH = "no-match"
    """有组成项不通过。"""
    UNDETERMINED = "undetermined"
    """没有不通过的组成项，但有不可判的。"""


class Verdict(StrEnum):
    """一帧的签名判别结果。只有 `SCREEN` 给出画面。"""

    SCREEN = "screen"
    """最高档的命中都指向同一个画面。"""
    AMBIGUOUS = "ambiguous"
    """最高档的命中指向多于一个画面。"""
    NONE = "none"
    """没有命中，也没有不可判的签名。"""
    UNDETERMINED = "undetermined"
    """不可判的签名可能改变结果。"""
    DISABLED = "disabled"
    """分辨率不符，本帧不判别签名。"""


@dataclass(frozen=True, slots=True)
class AnchorScore:
    """一个锚点的匹配结果；不可判时没有得分与位置。"""

    roi: str
    template_sha256: str
    score: float | None
    location: PixelRect | None
    """得分最高的位置，客户区物理像素。"""


@dataclass(frozen=True, slots=True)
class SignatureResult:
    id: str
    screen: str
    priority: int
    outcome: Outcome
    detail: str | None
    """不命中时是第一项不通过的组成项，不可判时是不可判的组成项，各附原因。"""
    anchors: tuple[AnchorScore, ...]
    """判别过的锚点，按声明的顺序。"""


@dataclass(frozen=True, slots=True)
class Judgement:
    verdict: Verdict
    screen: str | None
    """`verdict` 为 `SCREEN` 时的画面 `id`。"""
    matched: tuple[str, ...]
    """最高档中命中的签名 `id`（画面与歧义时）。"""
    signatures: tuple[SignatureResult, ...]
    """每个参与判别的签名的结果，按声明的顺序；停用时为空。"""
    detail: str | None


def resolution_problem(width: int, height: int, expected: Resolution) -> str | None:
    """本帧的尺寸与期望的客户区分辨率宽高比不符时为说明，否则为 None。"""
    aspect = expected.width / expected.height
    if abs(width / height - aspect) <= ASPECT_TOLERANCE * aspect:
        return None
    return (
        f"分辨率不符：帧为 {width}×{height}，适配包期望 {expected.width}×{expected.height}，"
        f"宽高比相差超过 {ASPECT_TOLERANCE:.0%}"
    )


def decide(results: Sequence[SignatureResult]) -> Judgement:
    """按叠加优先级与不可判的签名，从各签名的结果得出这一帧的结果。"""
    signatures = tuple(results)
    matched = [r for r in signatures if r.outcome is Outcome.MATCH]
    unknown = [r for r in signatures if r.outcome is Outcome.UNDETERMINED]
    if not matched:
        if unknown:
            return Judgement(
                Verdict.UNDETERMINED, None, (), signatures, f"不可判的签名：{_ids(unknown)}"
            )
        return Judgement(Verdict.NONE, None, (), signatures, None)
    top = max(r.priority for r in matched)
    best = [r for r in matched if r.priority == top]
    screens = list(dict.fromkeys(r.screen for r in best))
    blocking = [
        u
        for u in unknown
        if u.priority > top or (u.priority == top and len(screens) == 1 and u.screen != screens[0])
    ]
    if blocking:
        return Judgement(
            Verdict.UNDETERMINED,
            None,
            (),
            signatures,
            f"不可判的签名可能改变结果：{_ids(blocking)}",
        )
    ids = tuple(r.id for r in best)
    if len(screens) == 1:
        return Judgement(Verdict.SCREEN, screens[0], ids, signatures, None)
    return Judgement(
        Verdict.AMBIGUOUS, None, ids, signatures, f"命中多个画面：{'、'.join(screens)}"
    )


def _ids(results: Iterable[SignatureResult]) -> str:
    return "、".join(r.id for r in results)


@dataclass(frozen=True, slots=True)
class _Pixel:
    column_x: float
    row_y: float
    rgb: tuple[int, int, int]
    tolerance: int
    color: str

    @classmethod
    def of(cls, check: PixelCheck) -> "_Pixel":
        value = int(check.color[1:], 16)
        rgb = (value >> 16, (value >> 8) & 0xFF, value & 0xFF)
        return cls(check.at.x, check.at.y, rgb, check.tolerance, check.color)

    def read(self, bgra: Bgra) -> tuple[int, int, int]:
        """点所在像素的 R、G、B：第 ⌊x·宽⌋ 列、第 ⌊y·高⌋ 行，超出时取最后一列或一行。"""
        height, width = bgra.shape[:2]
        column = min(math.floor(self.column_x * width), width - 1)
        row = min(math.floor(self.row_y * height), height - 1)
        blue, green, red = (int(v) for v in bgra[row, column, :3])
        return red, green, blue

    def passes(self, rgb: tuple[int, int, int]) -> bool:
        return all(abs(a - b) <= self.tolerance for a, b in zip(rgb, self.rgb, strict=True))


class SignatureJudge:
    """按一个适配包判别画面签名。结果只取决于依次处理的帧与当时 OCR 是否可用；不是线程安全的，
    同一时刻只在一个工作线程里用。"""

    def __init__(self, adapter: Adapter, templates: Mapping[str, Bgr | TemplateError]) -> None:
        """`templates` 按 SHA-256 给出读入的模板或读不出的原因，见 `load`。"""
        self.adapter = adapter
        self._signatures = tuple(s for s in adapter.signatures if s.calibrated)
        self._pixels = {
            s.id: tuple(_Pixel.of(check) for check in s.pixels) for s in adapter.signatures
        }
        self._templates = dict(templates)
        self._scaled: dict[str, Bgr | str] = {}
        self._scaled_for: tuple[int, int] | None = None
        self._texts: OrderedDict[tuple[PixelRect, bytes], tuple[str, ...]] = OrderedDict()
        self._text_reader: ReferenceType[TextReader] | None = None
        self._warned: tuple[int, int] | None = None

    @classmethod
    def load(cls, adapter: Adapter, data_dir: Path) -> "SignatureJudge":
        """从本地数据目录读入已标定的签名引用的模板；读不出的记下原因，引用它的锚点不可判。"""
        templates: dict[str, Bgr | TemplateError] = {}
        for signature in adapter.signatures:
            if not signature.calibrated:
                continue
            for anchor in signature.anchors:
                sha256 = anchor.template_sha256
                if sha256 not in templates:
                    try:
                        templates[sha256] = load_template(data_dir, adapter.id, sha256)
                    except TemplateError as error:
                        templates[sha256] = error
        return cls(adapter, templates)

    @property
    def uncalibrated(self) -> tuple[str, ...]:
        """尚未标定、不参与判别的签名。"""
        return tuple(s.id for s in self.adapter.signatures if not s.calibrated)

    @property
    def problems(self) -> tuple[str, ...]:
        """使签名不可判的模板问题。"""
        problems: list[str] = []
        for signature in self._signatures:
            for index, anchor in enumerate(signature.anchors):
                template = self._templates.get(anchor.template_sha256)
                if isinstance(template, TemplateError):
                    problems.append(f"签名 {signature.id} 的锚点 [{index}]：{template}")
        return tuple(problems)

    def judge(self, frame: RingFrame, bgra: Bgra, reader: TextReader | None) -> Judgement:
        """判别一帧。`reader` 为 None 即 OCR 不可用，固定文字项不可判。"""
        size = (frame.width, frame.height)
        self._use_text_reader(reader)
        problem = resolution_problem(frame.width, frame.height, self.adapter.resolution)
        if problem is not None:
            if self._warned != size:
                logger.warning("适配包 %s 的签名停用：%s", self.adapter.id, problem)
                self._warned = size
            return Judgement(Verdict.DISABLED, None, (), (), problem)
        self._warned = None
        if self._scaled_for != size:
            self._scaled.clear()
            self._scaled_for = size
        return decide([self._evaluate(s, frame, bgra, reader) for s in self._signatures])

    # 签名

    def _evaluate(
        self, signature: Signature, frame: RingFrame, bgra: Bgra, reader: TextReader | None
    ) -> SignatureResult:
        anchors: list[AnchorScore] = []

        def result(outcome: Outcome, detail: str | None) -> SignatureResult:
            return SignatureResult(
                signature.id, signature.screen, signature.priority, outcome, detail, tuple(anchors)
            )

        undetermined: list[str] = []
        for index, pixel in enumerate(self._pixels[signature.id]):
            rgb = pixel.read(bgra)
            if not pixel.passes(rgb):
                actual = "#{:02X}{:02X}{:02X}".format(*rgb)
                return result(
                    Outcome.NO_MATCH,
                    f"像素检查 [{index}]：{actual} 与 {pixel.color} 之差超过 {pixel.tolerance}",
                )
        for index, anchor in enumerate(signature.anchors):
            score, problem = self._match(anchor, bgra)
            anchors.append(score)
            if problem is not None:
                undetermined.append(f"锚点 [{index}]：{problem}")
            elif score.score is not None and score.score < anchor.min_score:
                return result(
                    Outcome.NO_MATCH,
                    f"锚点 [{index}]：得分 {score.score:.3f} 低于 {anchor.min_score:g}",
                )
        for index, item in enumerate(signature.texts):
            lines = self._read(item, frame, bgra, reader)
            if isinstance(lines, str):
                undetermined.append(f"固定文字 [{index}]：{lines}")
            elif not item.matches(lines):
                return result(
                    Outcome.NO_MATCH, f"固定文字 [{index}]：ROI {item.roi} 里没有“{item.text}”"
                )
        if undetermined:
            return result(Outcome.UNDETERMINED, "；".join(undetermined))
        return result(Outcome.MATCH, None)

    # 锚点

    def _match(self, anchor: Anchor, bgra: Bgra) -> tuple[AnchorScore, str | None]:
        """在 ROI 里匹配缩放后的模板；返回得分与位置，不可判时另给原因。"""
        unscored = AnchorScore(anchor.roi, anchor.template_sha256, None, None)
        template = self._scaled_template(anchor.template_sha256, bgra)
        if isinstance(template, str):
            return unscored, template
        height, width = bgra.shape[:2]
        rect = self.adapter.roi(anchor.roi).to_pixels(width, height)
        region_width, region_height = rect.right - rect.left, rect.bottom - rect.top
        template_height, template_width = template.shape[:2]
        if template_width > region_width or template_height > region_height:
            return unscored, (
                f"缩放后的模板 {template_width}×{template_height} 比 ROI {anchor.roi} 的 "
                f"{region_width}×{region_height} 大"
            )
        region = cv2.cvtColor(
            bgra[rect.top : rect.bottom, rect.left : rect.right], cv2.COLOR_BGRA2BGR
        )
        scores = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
        _, best, _, (x, y) = cv2.minMaxLoc(scores)
        score = min(1.0, max(0.0, best)) if math.isfinite(best) else 0.0
        left, top = rect.left + x, rect.top + y
        location = PixelRect(left, top, left + template_width, top + template_height)
        return AnchorScore(anchor.roi, anchor.template_sha256, score, location), None

    def _scaled_template(self, sha256: str, bgra: Bgra) -> Bgr | str:
        """按本帧与期望分辨率之比缩放的模板，或不可判的原因；按尺寸缓存。"""
        cached = self._scaled.get(sha256)
        if cached is not None:
            return cached
        base = self._templates.get(sha256)
        scaled: Bgr | str
        if base is None:
            scaled = f"模板 {sha256} 没有读入"
        elif isinstance(base, TemplateError):
            scaled = str(base)
        else:
            height, width = bgra.shape[:2]
            expected = self.adapter.resolution
            base_height, base_width = base.shape[:2]
            new_width = max(1, round(base_width * width / expected.width))
            new_height = max(1, round(base_height * height / expected.height))
            if (new_width, new_height) == (base_width, base_height):
                scaled = base
            else:
                shrink = new_width <= base_width and new_height <= base_height
                interpolation = cv2.INTER_AREA if shrink else cv2.INTER_LINEAR
                resized = cv2.resize(base, (new_width, new_height), interpolation=interpolation)
                scaled = np.asarray(resized, dtype=np.uint8)
            if not isinstance(scaled, str) and is_uniform(scaled):
                scaled = f"缩放到 {new_width}×{new_height} 后模板的全部像素相同"
        self._scaled[sha256] = scaled
        return scaled

    # 固定文字

    def _use_text_reader(self, reader: TextReader | None) -> None:
        if reader is not None and self._text_reader is not None and self._text_reader() is reader:
            return
        self._texts.clear()
        self._text_reader = None
        if reader is not None:
            # 不支持弱引用时，每帧重新读取，不持有已停用的可选服务。
            with suppress(TypeError):
                self._text_reader = ref(reader)

    def _read(
        self, item: RoiText, frame: RingFrame, bgra: Bgra, reader: TextReader | None
    ) -> tuple[str, ...] | str:
        """ROI 内的各行文字，或不可判的原因。同一 ROI 的像素没变时沿用上次读出的。"""
        if reader is None:
            return "OCR 不可用"
        height, width = bgra.shape[:2]
        rect = self.adapter.roi(item.roi).to_pixels(width, height)
        pixels = bgra[rect.top : rect.bottom, rect.left : rect.right]
        key = (rect, hashlib.blake2b(pixels.tobytes(), digest_size=16).digest())
        cached = self._texts.get(key)
        if cached is not None:
            self._texts.move_to_end(key)
            return cached
        try:
            lines = tuple(str(line) for line in reader.read_lines(frame, rect))
        except Exception as error:
            return f"OCR 出错：{error or type(error).__name__}"
        self._texts[key] = lines
        while len(self._texts) > TEXT_CACHE_SIZE:
            self._texts.popitem(last=False)
        return lines
