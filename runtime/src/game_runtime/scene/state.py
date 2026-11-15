"""通用画面分类与限制；规则见 `docs/modules/perception.md` 的“画面状态”。"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field

from game_runtime.adapter import ScreenType
from game_runtime.ocr.service import OcrLine


class ScreenSource(StrEnum):
    SIGNATURE = "signature"
    OBSERVATION = "observation"


class ScreenObservation(BaseModel):
    """视觉智能体给出的不可信分类。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    screen_type: ScreenType
    confidence: Annotated[float, Field(strict=True, ge=0.0, le=1.0)]


@dataclass(frozen=True, slots=True)
class ScreenClassification:
    screen_type: ScreenType
    source: ScreenSource | None
    """仅有签名来源的类型可作为放宽画面限制的依据。"""
    confidence: float


OBSERVED_RESTRICTIONS: Final = frozenset(
    {
        ScreenType.CHOICE,
        ScreenType.TIMED_CHOICE,
        ScreenType.SETTINGS,
        ScreenType.SAVE_LOAD,
        ScreenType.SHOP,
        ScreenType.LOADING,
        ScreenType.CUTSCENE,
        ScreenType.BAD_ENDING,
        ScreenType.STUCK,
        ScreenType.UNKNOWN,
    }
)


@dataclass(frozen=True, slots=True)
class ScreenState:
    baseline: ScreenClassification = ScreenClassification(ScreenType.UNKNOWN, None, 0.0)
    """签名、播片检测或未知。"""
    uncertain_signature: bool = False
    """签名歧义、不可判或停用时，保留未知限制；单纯不命中时仍可用观察识别普通画面。"""
    observations: tuple[ScreenObservation, ...] = ()
    """每种类型只保留最新的观察。"""
    text_targets: tuple[OcrLine, ...] = ()
    """同帧 ROI 中可供吸附的 OCR 识别框。"""

    @property
    def classification(self) -> ScreenClassification:
        """用于报告的类型。观察只补充未知的画面类型。"""
        if self.baseline.screen_type is not ScreenType.UNKNOWN or not self.observations:
            return self.baseline
        last = self.observations[-1]
        return ScreenClassification(last.screen_type, ScreenSource.OBSERVATION, last.confidence)

    @property
    def restrictions(self) -> frozenset[ScreenType]:
        """须同时遵守的画面限制。分类仍为未知时适用逐步控制的缺省限制。"""
        restricted = {
            item.screen_type
            for item in self.observations
            if item.screen_type in OBSERVED_RESTRICTIONS
        }
        if self.baseline.screen_type is not ScreenType.UNKNOWN or self.uncertain_signature:
            restricted.add(self.baseline.screen_type)
        return frozenset(restricted)
