"""GS-04 评测的引擎：本仓库的 `ocr@1` 引擎（按模型组）与 Windows 自带 OCR。"""

import platform
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, cast

from game_runtime.adapter import PixelRect
from game_runtime.local_data import local_data_dir
from game_runtime.ocr import (
    MODEL_SETS,
    EngineOptions,
    FrameLike,
    ModelSetName,
    OcrEngine,
    onnxruntime_version,
    recognize_frame,
)
from gs04_ocr.errors import EngineUnavailableError

WINDOWS = "windows"
ENGINE_NAMES = (*MODEL_SETS, WINDOWS)


class Engine(Protocol):
    name: str

    def describe(self) -> Mapping[str, Any]:
        """写进报告的引擎信息：版本、配置与模型制品的摘要。"""
        ...

    def recognize(self, frame: FrameLike, rect: PixelRect) -> list[str]:
        """识别帧里的 `rect`，按从上到下、从左到右给出各行文字。"""
        ...


class PaddleEngine:
    """`ocr@1` 的引擎：`game_runtime.ocr.OcrEngine` 加 `recognize_frame`，与运行时同一条路径。"""

    def __init__(self, name: str, options: EngineOptions, data_dir: Path | None = None) -> None:
        model_set = MODEL_SETS[cast(ModelSetName, name)]
        directory = data_dir or local_data_dir()
        try:
            self._engine = OcrEngine.load(model_set, directory, options)
        except Exception as error:
            raise EngineUnavailableError(str(error)) from error
        self.name = name
        self._options = options
        self._artifacts = {
            artifact.name: {
                "version": artifact.version,
                "file": artifact.file,
                "sha256": artifact.sha256,
                "url": artifact.url,
                "license": artifact.license,
            }
            for artifact in model_set.artifacts
        }

    def describe(self) -> Mapping[str, Any]:
        options = self._options
        return {
            "engine": self.name,
            "onnxruntime": onnxruntime_version(),
            "threads": options.threads,
            "det_max_side": options.det_max_side,
            "merge_gap_ratio": options.merge_gap_ratio,
            "rec_pad_ratio": options.rec_pad_ratio,
            "min_confidence": options.min_confidence,
            "artifacts": self._artifacts,
        }

    def recognize(self, frame: FrameLike, rect: PixelRect) -> list[str]:
        return [line.text for line in recognize_frame(self._engine, frame, rect).lines]


def create_engine(name: str, options: EngineOptions, data_dir: Path | None = None) -> Engine:
    if name in MODEL_SETS:
        return PaddleEngine(name, options, data_dir)
    if name != WINDOWS:
        raise EngineUnavailableError(f"没有引擎 {name}；可选：{'、'.join(ENGINE_NAMES)}")
    if sys.platform != "win32":
        raise EngineUnavailableError(
            f"Windows 自带 OCR 只在 Windows 上，本机是 {platform.system()}"
        )
    try:
        from gs04_ocr.windows_ocr import WindowsOcrEngine
    except ImportError as error:
        raise EngineUnavailableError(
            f"没装可选依赖 windows-ocr（{error}）；用 uv run --extra windows-ocr gs04 …"
        ) from None
    return WindowsOcrEngine()
