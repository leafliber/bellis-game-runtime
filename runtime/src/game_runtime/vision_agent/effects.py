"""把模型的效果候选转为控制契约，并核对本地声明。"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from game_runtime.adapter import Adapter, ScreenType
from game_runtime.narrative import compare_key
from game_runtime.step_control.contracts import EffectKind, ExpectedEffect


class EffectReply(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EffectKind
    text: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    roi: str | None = None
    signature: str | None = None
    screen_type: ScreenType | None = None

    def expected(self, adapter: Adapter, *, goal: bool = False) -> ExpectedEffect:
        match self.kind:
            case EffectKind.SIGNATURE:
                if not any(
                    item.id == self.signature and (item.anchors or item.pixels or item.texts)
                    for item in adapter.signatures
                ):
                    raise ValueError("缺少可核对的签名")
                return ExpectedEffect(self.kind, signature=self.signature)
            case EffectKind.SCREEN:
                if goal and self.screen_type in {ScreenType.UNKNOWN, ScreenType.STUCK}:
                    raise ValueError("目标完成须能确定性核对")
                return ExpectedEffect(self.kind, screen_type=self.screen_type)
            case EffectKind.TEXT_APPEARED | EffectKind.TEXT_DISAPPEARED:
                if self.roi is None or self.text is None or not compare_key(self.text):
                    raise ValueError("缺少文字或已标定的 ROI")
                adapter.roi(self.roi)
                return ExpectedEffect(self.kind, text=self.text, roi=self.roi)
            case EffectKind.CHANGED | EffectKind.UNCHANGED:
                if goal:
                    raise ValueError("单纯的像素变化不能作为整个目标的完成条件")
                return ExpectedEffect(self.kind)
