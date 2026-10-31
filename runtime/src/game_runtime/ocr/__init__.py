"""OCR：普通插件 `ocr` 与它提供的 `ocr@1`，设计见 `docs/modules/perception.md` 的“OCR”。

模型制品的清单在 `game_runtime.ocr.models`，下载用离线工具 `uv run fetch-models`。
"""

from game_runtime.ocr._onnx import runtime_version as onnxruntime_version
from game_runtime.ocr.engine import (
    EngineOptions,
    Image,
    ModelLoadError,
    OcrEngine,
    RecognizedLine,
    Recognizer,
)
from game_runtime.ocr.models import DEFAULT_MODEL_SET, MODEL_SETS, ModelSet, ModelSetName
from game_runtime.ocr.plugin import OCR, OcrConfig, OcrPlugin, UnsupportedLanguageError
from game_runtime.ocr.service import (
    FixedTextOutcome,
    FrameLike,
    LanguageCheck,
    OcrClosedError,
    OcrLine,
    OcrResult,
    OcrService,
    crop_bgr,
    recognize_frame,
)

__all__ = [
    "DEFAULT_MODEL_SET",
    "MODEL_SETS",
    "OCR",
    "EngineOptions",
    "FixedTextOutcome",
    "FrameLike",
    "Image",
    "LanguageCheck",
    "ModelLoadError",
    "ModelSet",
    "ModelSetName",
    "OcrClosedError",
    "OcrConfig",
    "OcrEngine",
    "OcrLine",
    "OcrPlugin",
    "OcrResult",
    "OcrService",
    "RecognizedLine",
    "Recognizer",
    "UnsupportedLanguageError",
    "crop_bgr",
    "onnxruntime_version",
    "recognize_frame",
]
