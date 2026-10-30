"""采集配置：帧环的尺寸与采集目标。

G0 的窗口识别规则由这里的配置给出，G1 起来自游戏适配包。挂接顺序与单写方规则见
`docs/modules/core-protocol.md` 的“帧环”，窗口识别见 `docs/modules/realtime-core.md` 的“窗口识别”。
"""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core_protocol import models
from game_runtime.frame_ring import DEFAULT_SLOT_COUNT

DEFAULT_MAX_WIDTH = 1920
"""帧环缺省能放下的最大帧宽（物理像素）。"""
DEFAULT_MAX_HEIGHT = 1080
"""帧环缺省能放下的最大帧高（物理像素）。"""

type _Condition = Annotated[str, Field(min_length=1)]


class WindowRule(BaseModel):
    """窗口识别规则：进程的可执行文件名，加窗口类或标题，给出的条件须全部满足。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["window"] = "window"
    process_name: _Condition
    """进程映像的文件名，不含目录，不区分大小写，如 `game.exe`。"""
    window_class: _Condition | None = None
    """窗口类名，全等匹配。"""
    title: _Condition | None = None
    """窗口标题，全等匹配。"""

    @model_validator(mode="after")
    def _class_or_title(self) -> Self:
        if self.window_class is None and self.title is None:
            raise ValueError("窗口类与标题至少给出一个")
        return self

    def target(self) -> models.WindowTarget:
        """`capture.set_target` 的 `window` 分支。"""
        return models.WindowTarget(
            process_name=self.process_name, window_class=self.window_class, title=self.title
        )


class CaptureConfig(BaseModel):
    """帧环的尺寸与采集目标。窗口比帧环大时帧写不进帧环，实时核记录错误，帧龄随之增长。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: Annotated[WindowRule | models.FakeTarget, Field(discriminator="type")]
    """采集目标：窗口识别规则，或只用于自动测试的假显示面（`FakeDisplay.target`）。"""
    max_width: Annotated[int, Field(ge=1)] = DEFAULT_MAX_WIDTH
    max_height: Annotated[int, Field(ge=1)] = DEFAULT_MAX_HEIGHT
    slot_count: Annotated[int, Field(ge=2)] = DEFAULT_SLOT_COUNT

    def set_target_params(self) -> models.CaptureSetTargetParams:
        """`capture.set_target` 的 params。"""
        target = self.target.target() if isinstance(self.target, WindowRule) else self.target
        return models.CaptureSetTargetParams(target=models.CaptureTarget(root=target))
