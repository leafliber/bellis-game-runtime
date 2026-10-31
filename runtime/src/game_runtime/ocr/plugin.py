"""普通插件 `ocr`：依赖 `game-adapter@1`，提供 `ocr@1`。

设计见 `docs/modules/perception.md` 的“OCR”。激活时按摘要核对本地数据目录里的模型制品并加载，
只读本地文件、不联网；制品没有或不符即激活失败。只识别调用方交来的帧，不申请 `frames.read`。
"""

import functools
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_runtime._threads import in_thread
from game_runtime.adapter import GAME_ADAPTER
from game_runtime.kernel import Manifest, Plugin, PluginContext, ServiceToken, Tier
from game_runtime.local_data import local_data_dir
from game_runtime.ocr.engine import EngineOptions, OcrEngine, Recognizer
from game_runtime.ocr.models import DEFAULT_MODEL_SET, MODEL_SETS, ModelSet, ModelSetName
from game_runtime.ocr.service import OcrService

OCR = ServiceToken[OcrService]("ocr", 1)
"""对给定帧的 ROI 识别文字、执行语言核对。`scene@1` 以 `TextReader` 取用同一个服务。"""


class UnsupportedLanguageError(Exception):
    """适配包声明的语言版本不在模型组核对过的语言里。"""


class OcrConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: ModelSetName = DEFAULT_MODEL_SET
    """模型组，见 `game_runtime.ocr.models.MODEL_SETS`。"""
    threads: int | None = Field(default=EngineOptions.threads, ge=1, le=64)
    """ONNX Runtime 的算子内线程数；null 由 ONNX Runtime 决定。缺省随 GS-04 定。"""
    min_confidence: float = Field(default=EngineOptions.min_confidence, ge=0.0, le=1.0)
    """识别置信度低于它的行丢弃。"""
    data_dir: Path | None = None
    """本地数据目录，绝对路径；缺省按 `game_runtime.local_data.local_data_dir`。"""

    @model_validator(mode="after")
    def _absolute(self) -> Self:
        if self.data_dir is not None and not self.data_dir.is_absolute():
            raise ValueError("data_dir 须为绝对路径")
        return self


def load_engine(model_set: ModelSet, data_dir: Path, options: EngineOptions) -> Recognizer:
    """加载识别引擎。测试替换它以免依赖模型文件。"""
    return OcrEngine.load(model_set, data_dir, options)


class OcrPlugin(Plugin[OcrConfig]):
    """激活时核对并加载模型（在线程里进行，不阻塞事件循环），提供 `ocr@1`。"""

    manifest = Manifest(
        id="ocr",
        version="1",
        tier=Tier.ORDINARY,
        provides=(OCR.ref,),
        inject=(GAME_ADAPTER.ref,),
        config=OcrConfig,
    )

    async def activate(self, ctx: PluginContext[OcrConfig]) -> None:
        adapter = ctx.service(GAME_ADAPTER)
        model_set = MODEL_SETS[ctx.config.model]
        tag = adapter.language.tag
        if not model_set.supports(tag):
            supported = "、".join(sorted(model_set.languages))
            raise UnsupportedLanguageError(
                f"适配包 {adapter.id} 的语言 {tag} 不在模型组 {model_set.name} 核对过的语言"
                f"（{supported}）里"
            )
        options = EngineOptions(
            threads=ctx.config.threads, min_confidence=ctx.config.min_confidence
        )
        data_dir = ctx.config.data_dir or local_data_dir()
        recognizer = await in_thread(functools.partial(load_engine, model_set, data_dir, options))
        service = OcrService(adapter, recognizer, model=model_set.name)
        ctx.register(service.aclose)
        ctx.provide(OCR, service)
