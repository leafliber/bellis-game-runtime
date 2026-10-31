"""Windows 自带 OCR（`Windows.Media.Ocr`）作 GS-04 的对照基线。

只在 Windows 上，要装可选依赖 `windows-ocr`（pywinrt）；系统须装有简体中文的 OCR 语言组件。
识别的是同一个区域裁出的图像，不经本仓库的后处理。
"""

import sys

if sys.platform != "win32":
    raise ImportError("Windows 自带 OCR 只在 Windows 上")

# 没装可选依赖时 pyright 找不到 winrt；装了时它自带类型标注。
# pyright: reportMissingImports=false, reportMissingModuleSource=false
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

import platform
from collections.abc import Mapping
from importlib import metadata
from typing import Any

import numpy as np
from winrt.windows.globalization import Language
from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
from winrt.windows.media.ocr import OcrEngine

from game_runtime.adapter import PixelRect
from game_runtime.ocr import FrameLike, crop_bgr
from gs04_ocr.errors import EngineUnavailableError

LANGUAGE = "zh-Hans-CN"


class WindowsOcrEngine:
    name = "windows"

    def __init__(self, language: str = LANGUAGE) -> None:
        tag = Language(language)
        if not OcrEngine.is_language_supported(tag):
            languages = [lang.language_tag for lang in OcrEngine.available_recognizer_languages]
            raise EngineUnavailableError(
                f"系统没有 {language} 的 OCR 语言组件；已有：{'、'.join(languages) or '无'}"
            )
        engine = OcrEngine.try_create_from_language(tag)
        if engine is None:
            raise EngineUnavailableError(f"创建 {language} 的 OcrEngine 失败")
        self._engine = engine
        self._language = language

    def describe(self) -> Mapping[str, Any]:
        return {
            "engine": self.name,
            "api": "Windows.Media.Ocr",
            "language": self._language,
            "windows": platform.version(),
            "winrt-windows-media-ocr": metadata.version("winrt-Windows.Media.Ocr"),
            "max_image_dimension": OcrEngine.max_image_dimension,
        }

    def recognize(self, frame: FrameLike, rect: PixelRect) -> list[str]:
        bgr = crop_bgr(frame, rect)
        height, width = bgr.shape[:2]
        bgra = np.empty((height, width, 4), np.uint8)
        bgra[:, :, :3] = bgr
        bgra[:, :, 3] = 255
        bitmap = SoftwareBitmap.create_copy_from_buffer(
            bgra.tobytes(), BitmapPixelFormat.BGRA8, width, height, BitmapAlphaMode.PREMULTIPLIED
        )
        result = self._engine.recognize_async(bitmap).get()
        return [line.text for line in result.lines]
