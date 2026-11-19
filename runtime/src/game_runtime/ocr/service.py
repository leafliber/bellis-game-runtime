"""`ocr@1` 的服务：对给定帧的 ROI 识别文字、执行语言核对的两层，并为画面签名提供 `read_lines`。

识别在服务自己的一个工作线程里串行进行，不阻塞事件循环。设计见 `docs/modules/perception.md`
的“OCR”；固定文字的比较按 `RoiText.matches`，文字系统按 `game_runtime.ocr.script`
（`docs/modules/game-adapter.md` 的“语言核对”）。
"""

import asyncio
import functools
from collections import OrderedDict
from collections.abc import Buffer, Callable
from concurrent.futures import CancelledError, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Final, Protocol

import cv2
import numpy as np
from numpy.typing import NDArray

from game_runtime._threads import in_thread
from game_runtime.adapter import Adapter, FixedText, PixelRect
from game_runtime.ocr.engine import Image, RecognizedLine, Recognizer
from game_runtime.ocr.script import (
    Script,
    ScriptCheck,
    count_letters,
    script_of,
    supported_languages,
)

BYTES_PER_PIXEL = 4
_CACHE_ENTRIES: Final = 4
_MAX_CACHED_BYTES: Final = 1024 * 1024
type _CropKey = tuple[int, int, bool, bytes]


class UnsupportedLanguageError(Exception):
    """适配包声明的语言版本不受支持：不在模型组核对过的语言里，或它的文字系统不受支持。"""


class FrameLike(Protocol):
    """识别所需的帧：BGRA8，自上而下逐行，与 `RingFrame` 相同；`RingFrame` 满足它。

    识别在工作线程里读 `pixels`，调用返回之前像素不能改变（`RingFrame` 是拷贝，满足这一点）。
    """

    @property
    def frame_number(self) -> int: ...
    @property
    def transform_version(self) -> int: ...
    @property
    def width(self) -> int: ...
    @property
    def height(self) -> int: ...
    @property
    def row_stride(self) -> int: ...
    @property
    def pixels(self) -> Buffer: ...


@dataclass(frozen=True)
class OcrLine:
    """一行文字。"""

    text: str
    box: PixelRect
    """识别框：客户区物理像素，含左边与上边、不含右边与下边。"""
    confidence: float
    """识别置信度，[0, 1]。"""
    horizontal: bool | None = field(default=None, compare=False)
    """识别引擎提供的原始水平几何证据。"""


@dataclass(frozen=True)
class OcrResult:
    """一次识别的结果。"""

    frame_number: int
    transform_version: int
    roi: str | None
    """ROI 名；按物理像素矩形识别时为 None。"""
    rect: PixelRect
    """识别的区域：客户区物理像素。"""
    lines: tuple[OcrLine, ...]
    """从上到下、从左到右。"""

    @property
    def text(self) -> str:
        """各行依次连接，不加分隔。"""
        return "".join(line.text for line in self.lines)

    @property
    def texts(self) -> tuple[str, ...]:
        """各行的文字，按阅读顺序。"""
        return tuple(line.text for line in self.lines)


@dataclass(frozen=True)
class FixedTextOutcome:
    """语言核对的一项。"""

    item: FixedText
    result: OcrResult
    matched: bool


@dataclass(frozen=True)
class LanguageCheck:
    """在画面 `screen` 的一帧上做的语言核对。"""

    game: str
    tag: str
    """适配包声明的语言版本。"""
    screen: str
    outcomes: tuple[FixedTextOutcome, ...]

    @property
    def passed(self) -> bool:
        """这个画面声明的各项都相符。"""
        return bool(self.outcomes) and all(outcome.matched for outcome in self.outcomes)

    @property
    def mismatches(self) -> tuple[FixedTextOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if not outcome.matched)


def _crop_bgra(frame: FrameLike, rect: PixelRect) -> NDArray[np.uint8]:
    left, top, right, bottom = rect
    if not (0 <= left < right <= frame.width and 0 <= top < bottom <= frame.height):
        raise ValueError(f"区域 {tuple(rect)} 为空或超出 {frame.width}×{frame.height} 的帧")
    stride = frame.row_stride
    if stride < frame.width * BYTES_PER_PIXEL:
        raise ValueError(f"行跨度 {stride} 小于宽 {frame.width} × 4")
    data = np.frombuffer(memoryview(frame.pixels).cast("B"), np.uint8)
    if data.size < stride * frame.height:
        raise ValueError(f"像素只有 {data.size} 字节，少于 {stride * frame.height}")
    rows = data[: stride * frame.height].reshape(frame.height, stride)
    region = rows[top:bottom, left * BYTES_PER_PIXEL : right * BYTES_PER_PIXEL]
    return region.reshape(bottom - top, right - left, BYTES_PER_PIXEL)


def crop_bgr(frame: FrameLike, rect: PixelRect) -> Image:
    """从帧里拷贝出 `rect`（客户区物理像素）的 BGR 图像。"""
    return np.ascontiguousarray(_crop_bgra(frame, rect)[:, :, :3])


def recognize_frame(
    recognizer: Recognizer, frame: FrameLike, rect: PixelRect, roi: str | None = None
) -> OcrResult:
    """同步地识别帧里的 `rect`：裁出、识别，把识别框换算回客户区物理像素。

    `ocr@1` 在工作线程里调用它；离线工具（GS-04 评测）直接调用。
    """
    return _frame_result(frame, rect, roi, tuple(recognizer.recognize(crop_bgr(frame, rect))))


def _frame_result(
    frame: FrameLike, rect: PixelRect, roi: str | None, reading: tuple[RecognizedLine, ...]
) -> OcrResult:
    lines = tuple(
        OcrLine(
            line.text,
            PixelRect(
                line.box.left + rect.left,
                line.box.top + rect.top,
                line.box.right + rect.left,
                line.box.bottom + rect.top,
            ),
            line.confidence,
            line.horizontal,
        )
        for line in reading
    )
    return OcrResult(frame.frame_number, frame.transform_version, roi, rect, lines)


class OcrClosedError(RuntimeError):
    """服务已停用。"""


class OcrService:
    """`ocr@1`：只识别适配包给出的 ROI 或调用方给出的物理像素矩形，不扫整帧。

    识别在一个工作线程里按调用的先后串行进行；等待其它识别的时间也计入调用的时延。调用方被取消时，
    还没开始的识别不再进行，已开始的照常做完、结果丢弃。
    """

    def __init__(self, adapter: Adapter, recognizer: Recognizer, *, model: str) -> None:
        """适配包语言的文字系统不受支持即 `UnsupportedLanguageError`。"""
        tag = adapter.language.tag
        script = script_of(tag)
        if script is None:
            raise UnsupportedLanguageError(
                f"适配包 {adapter.id} 的语言 {tag} 的文字系统不受支持；支持 {supported_languages()}"
            )
        self.adapter = adapter
        """当前适配包，ROI、语言与语言核对的固定文字取自它。"""
        self.script: Script = script
        """适配包语言的文字系统。"""
        self.model = model
        """所用模型组的名字。"""
        self._recognizer = recognizer
        self._dialogue_rois = frozenset(box.text for box in adapter.dialogue_boxes)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr")
        # 仅识别线程访问。缓存保留裁片坐标，由每次调用重新附上帧号、变换和原点。
        self._cache: OrderedDict[_CropKey, tuple[RecognizedLine, ...]] = OrderedDict()
        self._closed = False

    async def recognize(self, frame: FrameLike, roi: str) -> OcrResult:
        """识别适配包里名为 `roi` 的 ROI。

        没有声明即 `LookupError`；尚未标定即 `NotCalibratedError`，不回退到缺省区域。
        归一化矩形按这一帧的宽高换算为物理像素（`NormalizedRect.to_pixels`）。
        """
        rect = self.adapter.roi(roi).to_pixels(frame.width, frame.height)
        return await self._run(functools.partial(self._recognize, frame, rect, roi))

    async def recognize_cropped_roi(self, frame: FrameLike, roi: str) -> OcrResult:
        """识别已裁出的标定 ROI，坐标仍以裁片为原点。"""
        self.adapter.roi(roi)
        rect = PixelRect(0, 0, frame.width, frame.height)
        return await self._run(functools.partial(self._recognize, frame, rect, roi))

    async def recognize_rect(self, frame: FrameLike, rect: PixelRect) -> OcrResult:
        """识别客户区物理像素的矩形 `rect`；为空或超出帧即 `ValueError`。"""
        rect = PixelRect(*rect)
        return await self._run(functools.partial(self._recognize, frame, rect))

    def _recognize(self, frame: FrameLike, rect: PixelRect, roi: str | None = None) -> OcrResult:
        crop = _crop_bgra(frame, rect)
        height, width = crop.shape[:2]
        dialogue = roi in self._dialogue_rois
        key = (
            (height, width, dialogue, crop.tobytes()) if crop.nbytes <= _MAX_CACHED_BYTES else None
        )
        if key is not None and (reading := self._cache.get(key)) is not None:
            self._cache.move_to_end(key)
            return _frame_result(frame, rect, roi, reading)

        image = np.ascontiguousarray(crop[:, :, :3])
        reading = tuple(self._recognizer.recognize(image))
        if not reading and dialogue:
            reading = self._recognize_enlarged(image)
        if key is not None:
            self._cache[key] = reading
            if len(self._cache) > _CACHE_ENTRIES:
                self._cache.popitem(last=False)
        return _frame_result(frame, rect, roi, reading)

    def _recognize_enlarged(self, image: Image) -> tuple[RecognizedLine, ...]:
        height, width = image.shape[:2]
        width, height = width * 2, height * 2
        enlarged = np.asarray(
            cv2.resize(image, (width, height), interpolation=cv2.INTER_CUBIC), dtype=np.uint8
        )
        lines: list[RecognizedLine] = []
        for line in self._recognizer.recognize(enlarged):
            left, top, right, bottom = line.box
            if 0 <= left < right <= width and 0 <= top < bottom <= height:
                box = PixelRect(left // 2, top // 2, (right + 1) // 2, (bottom + 1) // 2)
                lines.append(replace(line, box=box))
        return tuple(lines)

    async def check_language(self, frame: FrameLike, screen: str) -> LanguageCheck:
        """语言核对的固定文字一层：`frame` 是画面 `screen` 的稳定帧，对这个画面声明的每一项的 ROI
        做 OCR 比较。这个画面没有声明任何一项（含固定文字为空）即 `LookupError`。
        """
        adapter = self.adapter
        items = tuple(item for item in adapter.language.fixed_texts if item.screen == screen)
        if not items:
            raise LookupError(f"适配包 {adapter.id} 的画面 {screen} 没有语言核对的固定文字")
        outcomes: list[FixedTextOutcome] = []
        for item in items:
            result = await self.recognize(frame, item.roi)
            outcomes.append(FixedTextOutcome(item, result, item.matches(result.texts)))
        return LanguageCheck(adapter.id, adapter.language.tag, screen, tuple(outcomes))

    def check_script(self, text: str) -> ScriptCheck:
        """语言核对的文字系统一层：`text`（剧情记录里完整的句子）的字母里属于适配包语言的文字系统
        的比例与是否相符。纯计算，不做 OCR，同步；可以在事件循环里调用，停用后也可以。"""
        letters, matching = count_letters(text, self.script)
        return ScriptCheck(self.adapter.language.tag, self.script, text, letters, matching)

    def read_lines(self, frame: FrameLike, rect: PixelRect) -> list[str]:
        """同步地识别 `rect`（客户区物理像素）内的文字，按阅读顺序返回各行。

        这是 `scene@1` 的 `TextReader`。交给同一个工作线程，在调用方的线程里阻塞到识别做完；可以在
        多个线程里同时调用，按先后排队；不能在事件循环里调用（会阻塞它）。区域为空或超出帧即
        `ValueError`，服务已停用即 `OcrClosedError`。
        """
        job = functools.partial(self._recognize, frame, PixelRect(*rect))
        if self._closed:
            raise OcrClosedError("ocr@1 已停用")
        try:
            future = self._executor.submit(job)
        except RuntimeError as error:
            raise OcrClosedError("ocr@1 已停用") from error
        try:
            return list(future.result().texts)
        except CancelledError as error:
            raise OcrClosedError("ocr@1 已停用") from error

    async def aclose(self) -> None:
        """停用：不再接受识别，等在途的识别做完；还没开始的取消。"""
        self._closed = True
        await in_thread(functools.partial(self._executor.shutdown, wait=True, cancel_futures=True))
        self._cache.clear()

    async def _run(self, job: Callable[[], OcrResult]) -> OcrResult:
        if self._closed:
            raise OcrClosedError("ocr@1 已停用")
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self._executor, job)
        except RuntimeError as error:
            # 停用后线程池拒绝新的任务。
            if self._closed:
                raise OcrClosedError("ocr@1 已停用") from error
            raise
        except asyncio.CancelledError:
            # 停用时还没开始的识别被取消；调用方自己被取消时照常传出取消。
            task = asyncio.current_task()
            if self._closed and task is not None and task.cancelling() == 0:
                raise OcrClosedError("ocr@1 已停用") from None
            raise
