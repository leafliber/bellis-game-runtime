"""`ocr@1` 的服务，不用模型文件：ROI 的换算与裁出、识别框换回客户区、尚未标定、语言核对的
固定文字一层的相符与不符、`scene@1` 的 `read_lines`、工作线程不阻塞事件循环、停用。识别器是按脚本
回答的假识别器。文字系统一层见 `test_script.py`。
"""

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from game_runtime.adapter import Adapter, NotCalibratedError, PixelRect
from game_runtime.frame_ring import RingFrame
from game_runtime.ocr import (
    OCR,
    LanguageCheck,
    OcrClosedError,
    OcrLine,
    OcrResult,
    OcrService,
    crop_bgr,
    recognize_frame,
)
from game_runtime.ocr.engine import Image, RecognizedLine
from game_runtime.ocr.fake import ScriptedRecognizer, scripted_line
from game_runtime.scene import OCR as SCENE_OCR
from game_runtime.scene import TextReader


def gradient_frame(width: int, height: int, *, stride_pad: int = 0) -> RingFrame:
    """B、G 分别是列号与行号（对 256 取余），R 为 0；每行末尾可多出 `stride_pad` 字节。"""
    ys, xs = np.mgrid[0:height, 0:width]
    image = np.zeros((height, width * 4 + stride_pad), np.uint8)
    pixels = image[:, : width * 4].reshape(height, width, 4)
    pixels[:, :, 0] = xs % 256
    pixels[:, :, 1] = ys % 256
    pixels[:, :, 3] = 255
    return RingFrame(
        frame_number=42,
        capture_ns=0,
        transform_version=5,
        width=width,
        height=height,
        row_stride=width * 4 + stride_pad,
        pixels=image.tobytes(),
    )


def result(*texts: str) -> OcrResult:
    lines = tuple(OcrLine(text, PixelRect(0, 0, 8, 8), 0.9) for text in texts)
    return OcrResult(1, 1, None, PixelRect(0, 0, 8, 8), lines)


# 裁出与换算


@pytest.mark.parametrize("stride_pad", [0, 12])
def test_the_crop_is_the_bgr_of_the_rect(stride_pad: int) -> None:
    frame = gradient_frame(300, 200, stride_pad=stride_pad)

    crop = crop_bgr(frame, PixelRect(10, 20, 110, 70))

    assert crop.shape == (50, 100, 3)
    assert crop.flags["C_CONTIGUOUS"]
    assert crop[0, 0].tolist() == [10, 20, 0]
    assert crop[-1, -1].tolist() == [109, 69, 0]


@pytest.mark.parametrize(
    "rect",
    [
        pytest.param(PixelRect(10, 10, 10, 20), id="empty"),
        pytest.param(PixelRect(-1, 0, 10, 10), id="left of the frame"),
        pytest.param(PixelRect(0, 0, 301, 10), id="right of the frame"),
        pytest.param(PixelRect(0, 190, 10, 201), id="below the frame"),
    ],
)
def test_rects_outside_the_frame_are_rejected(rect: PixelRect) -> None:
    with pytest.raises(ValueError, match="为空或超出"):
        crop_bgr(gradient_frame(300, 200), rect)


def test_boxes_are_moved_back_to_client_pixels() -> None:
    recognizer = ScriptedRecognizer([[scripted_line("门", (5, 6, 25, 30), 0.8)]])

    result = recognize_frame(recognizer, gradient_frame(300, 200), PixelRect(100, 50, 200, 90), "x")

    assert result.frame_number == 42
    assert result.transform_version == 5
    assert result.roi == "x"
    assert result.rect == PixelRect(100, 50, 200, 90)
    assert [(line.text, line.box, line.confidence) for line in result.lines] == [
        ("门", PixelRect(105, 56, 125, 80), 0.8)
    ]


def test_results_give_the_line_texts_in_reading_order() -> None:
    outcome = result("剩余 4.2", "秒")

    assert outcome.texts == ("剩余 4.2", "秒")
    assert outcome.text == "剩余 4.2秒"


# 服务


def test_a_named_roi_is_converted_for_the_frame_size(synthetic_adapter: Adapter) -> None:
    recognizer = ScriptedRecognizer()
    service = OcrService(synthetic_adapter, recognizer, model="fake")
    frame = gradient_frame(2560, 1440)

    async def scenario() -> OcrResult:
        try:
            return await service.recognize(frame, "speaker")
        finally:
            await service.aclose()

    outcome = asyncio.run(scenario())

    expected = synthetic_adapter.roi("speaker").to_pixels(2560, 1440)
    assert outcome.rect == expected
    assert outcome.roi == "speaker"
    (image,) = recognizer.images
    assert image.shape == (expected.bottom - expected.top, expected.right - expected.left, 3)
    assert image[0, 0].tolist() == [expected.left % 256, expected.top % 256, 0]


def test_an_uncalibrated_roi_is_reported_without_fallback(witch_trial_adapter: Adapter) -> None:
    recognizer = ScriptedRecognizer()
    service = OcrService(witch_trial_adapter, recognizer, model="fake")

    async def scenario() -> None:
        try:
            with pytest.raises(NotCalibratedError, match="ROI dialogue（对白框）尚未标定"):
                await service.recognize(gradient_frame(2560, 1440), "dialogue")
            with pytest.raises(LookupError, match="没有声明 ROI nowhere"):
                await service.recognize(gradient_frame(2560, 1440), "nowhere")
        finally:
            await service.aclose()

    asyncio.run(scenario())
    assert recognizer.images == []


def test_a_pixel_rect_is_recognized_as_given(synthetic_adapter: Adapter) -> None:
    recognizer = ScriptedRecognizer([[scripted_line("确定")]])
    service = OcrService(synthetic_adapter, recognizer, model="fake")

    async def scenario() -> OcrResult:
        try:
            return await service.recognize_rect(gradient_frame(640, 360), PixelRect(1, 2, 31, 22))
        finally:
            await service.aclose()

    outcome = asyncio.run(scenario())

    assert outcome.roi is None
    assert outcome.text == "确定"
    assert recognizer.images[0].shape == (20, 30, 3)


def labels_by_crop_position(
    adapter: Adapter, width: int, height: int, labels: dict[str, str]
) -> Callable[[Image], list[RecognizedLine]]:
    """按裁出区域的左上角认出是哪个 ROI，回答对应的文字：模拟画面上写着这些字。"""
    corners = {
        tuple(adapter.roi(name).to_pixels(width, height)[:2]): text for name, text in labels.items()
    }

    def answer(image: Image) -> list[RecognizedLine]:
        corner = (int(image[0, 0, 0]), int(image[0, 0, 1]))
        for (left, top), text in corners.items():
            if (left % 256, top % 256) == corner:
                return [scripted_line(text)]
        return []

    return answer


def test_language_check_passes_when_every_item_matches(synthetic_adapter: Adapter) -> None:
    labels = {"menu-start": "开始", "menu-shop": "商 店", "menu-help": "说明"}
    answer = labels_by_crop_position(synthetic_adapter, 1280, 720, labels)
    service = OcrService(synthetic_adapter, ScriptedRecognizer(answer=answer), model="fake")

    async def scenario() -> LanguageCheck:
        try:
            return await service.check_language(gradient_frame(1280, 720), "title-menu")
        finally:
            await service.aclose()

    check = asyncio.run(scenario())

    assert check.passed
    assert check.game == "synthetic-game"
    assert check.tag == "zh-Hans"
    assert [outcome.item.roi for outcome in check.outcomes] == [
        "menu-start",
        "menu-shop",
        "menu-help",
    ]
    assert check.mismatches == ()


def test_language_check_fails_when_any_item_differs(synthetic_adapter: Adapter) -> None:
    # 合成的不符样本：繁体的“說明”。
    labels = {"menu-start": "开始", "menu-shop": "商店", "menu-help": "說明"}
    answer = labels_by_crop_position(synthetic_adapter, 1280, 720, labels)
    service = OcrService(synthetic_adapter, ScriptedRecognizer(answer=answer), model="fake")

    async def scenario() -> LanguageCheck:
        try:
            return await service.check_language(gradient_frame(1280, 720), "title-menu")
        finally:
            await service.aclose()

    check = asyncio.run(scenario())

    assert not check.passed
    assert [outcome.item.text for outcome in check.mismatches] == ["说明"]
    assert check.mismatches[0].result.text == "說明"


def test_language_check_needs_items_on_the_screen(
    synthetic_adapter: Adapter, witch_trial_adapter: Adapter
) -> None:
    """固定文字为空（《魔女审判》）或这个画面没有声明任何一项都是 LookupError：只做文字系统一层。"""

    async def scenario() -> None:
        uncalibrated = OcrService(witch_trial_adapter, ScriptedRecognizer(), model="fake")
        synthetic = OcrService(synthetic_adapter, ScriptedRecognizer(), model="fake")
        try:
            with pytest.raises(LookupError, match="画面 dialogue 没有语言核对的固定文字"):
                await uncalibrated.check_language(gradient_frame(2560, 1440), "dialogue")
            with pytest.raises(LookupError, match="画面 dialogue 没有语言核对的固定文字"):
                await synthetic.check_language(gradient_frame(1280, 720), "dialogue")
        finally:
            await uncalibrated.aclose()
            await synthetic.aclose()

    asyncio.run(scenario())


class BlockingRecognizer:
    """在工作线程里等事件循环放行：如果识别跑在事件循环的线程上，就等不到放行。"""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self.thread: str | None = None

    def recognize(self, image: Image) -> list[RecognizedLine]:
        self.thread = threading.current_thread().name
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("事件循环没有放行")
        return [scripted_line("好")]


def test_recognition_runs_off_the_event_loop(synthetic_adapter: Adapter) -> None:
    recognizer = BlockingRecognizer()
    service = OcrService(synthetic_adapter, recognizer, model="fake")

    async def scenario() -> OcrResult:
        try:
            pending = asyncio.ensure_future(service.recognize(gradient_frame(1280, 720), "speaker"))
            # 识别开始后事件循环仍在运行，才能放行。
            while not recognizer.started.is_set():
                await asyncio.sleep(0)
            recognizer.release.set()
            return await pending
        finally:
            await service.aclose()

    outcome = asyncio.run(scenario())

    assert outcome.text == "好"
    assert recognizer.thread is not None
    assert recognizer.thread.startswith("ocr")


def test_a_closed_service_refuses_new_work(synthetic_adapter: Adapter) -> None:
    service = OcrService(synthetic_adapter, ScriptedRecognizer(), model="fake")

    async def scenario() -> None:
        await service.aclose()
        with pytest.raises(OcrClosedError):
            await service.recognize(gradient_frame(1280, 720), "speaker")

    asyncio.run(scenario())


def test_queued_work_ends_with_closed_when_the_service_stops(synthetic_adapter: Adapter) -> None:
    recognizer = BlockingRecognizer()
    service = OcrService(synthetic_adapter, recognizer, model="fake")

    async def scenario() -> None:
        frame = gradient_frame(1280, 720)
        running = asyncio.ensure_future(service.recognize(frame, "speaker"))
        while not recognizer.started.is_set():
            await asyncio.sleep(0)
        queued = asyncio.ensure_future(service.recognize(frame, "dialogue"))
        await asyncio.sleep(0)
        closing = asyncio.ensure_future(service.aclose())
        # 停用取消排队的识别；在途的那个还在等放行。
        with pytest.raises(OcrClosedError):
            await asyncio.wait_for(queued, timeout=5)
        recognizer.release.set()
        await closing
        assert (await running).text == "好"

    asyncio.run(scenario())


# scene@1 的 TextReader


def test_the_service_is_the_text_reader_scene_asks_for(synthetic_adapter: Adapter) -> None:
    service = OcrService(synthetic_adapter, ScriptedRecognizer(), model="fake")
    reader: TextReader = service  # 静态核对：满足 scene@1 的接口

    assert reader is service
    assert SCENE_OCR.ref == OCR.ref == "ocr@1"
    asyncio.run(service.aclose())


def test_read_lines_returns_the_texts_from_the_worker(synthetic_adapter: Adapter) -> None:
    threads: list[str] = []

    def answer(image: Image) -> list[RecognizedLine]:
        threads.append(threading.current_thread().name)
        return [scripted_line("开始"), scripted_line("游戏")]

    service = OcrService(synthetic_adapter, ScriptedRecognizer(answer=answer), model="fake")
    try:
        lines = service.read_lines(gradient_frame(1280, 720), PixelRect(460, 300, 820, 364))
        with pytest.raises(ValueError, match="为空或超出"):
            service.read_lines(gradient_frame(1280, 720), PixelRect(0, 0, 2000, 10))
    finally:
        asyncio.run(service.aclose())

    assert lines == ["开始", "游戏"]
    assert threads[0].startswith("ocr")


class CountingRecognizer:
    """记下同时在识别的最大个数。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = 0
        self.most = 0
        self.calls = 0

    def recognize(self, image: Image) -> list[RecognizedLine]:
        with self._lock:
            self._running += 1
            self.calls += 1
            self.most = max(self.most, self._running)
        try:
            threading.Event().wait(0.002)
            return [scripted_line(f"{image.shape[1]}")]
        finally:
            with self._lock:
                self._running -= 1


def test_read_lines_from_many_threads_queue_on_one_worker(synthetic_adapter: Adapter) -> None:
    recognizer = CountingRecognizer()
    service = OcrService(synthetic_adapter, recognizer, model="fake")
    frame = gradient_frame(640, 360)
    widths = [10 + index for index in range(24)]

    def read_width(width: int) -> list[str]:
        return service.read_lines(frame, PixelRect(0, 0, width, 20))

    try:
        with ThreadPoolExecutor(max_workers=6) as callers:
            read = list(callers.map(read_width, widths))
    finally:
        asyncio.run(service.aclose())

    assert read == [[str(width)] for width in widths]
    assert recognizer.calls == len(widths)
    assert recognizer.most == 1


def test_read_lines_after_closing_is_refused(synthetic_adapter: Adapter) -> None:
    service = OcrService(synthetic_adapter, ScriptedRecognizer(), model="fake")
    asyncio.run(service.aclose())

    with pytest.raises(OcrClosedError):
        service.read_lines(gradient_frame(64, 64), PixelRect(0, 0, 10, 10))
