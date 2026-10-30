"""插件清单，字段的精确含义见 `docs/modules/plugin-kernel.md` 的“清单”。"""

import re
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from game_runtime.kernel.control import CONTROL_SUBMIT

_NAME = r"[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)*"
_REF = re.compile(rf"({_NAME})@([0-9]+)")

PluginId = Annotated[str, StringConstraints(pattern=rf"^{_NAME}$", max_length=48)]
"""插件与 profile 条目的标识，例如 `ocr-sim`、`step.controller`。"""

ServiceRef = Annotated[str, StringConstraints(pattern=rf"^{_NAME}@[0-9]+$")]
"""服务引用 `名字@主版本`。"""

Permission = Annotated[
    str,
    StringConstraints(
        pattern=r"^(?:[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*)*(:[A-Za-z0-9.-]+)?"
        r"|net\.egress:\*\.[A-Za-z0-9.-]+)$"
    ),
]
"""权限，例如 `frames.read`、`net.egress:api.example.com`、`secret:ark`。"""


def parse_ref(ref: str) -> tuple[str, int]:
    """把 `名字@主版本` 拆成名字与主版本。"""
    match = _REF.fullmatch(ref)
    if match is None:
        raise ValueError(f"服务引用不合法：{ref}")
    return match.group(1), int(match.group(2))


class Tier(StrEnum):
    """分级见 `docs/architecture.md` 的“分层”。"""

    CRITICAL = "critical"
    """关键平面：当前游戏适配包与当前控制产出者。"""
    ORDINARY = "ordinary"
    """普通插件：只产出数据和候选。"""


class NoConfig(BaseModel):
    """没有配置的插件用的配置模型：只接受空对象。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Manifest(BaseModel):
    """插件清单。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: PluginId
    version: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    tier: Tier
    provides: tuple[ServiceRef, ...] = ()
    inject: tuple[ServiceRef, ...] = ()
    permissions: tuple[Permission, ...] = ()
    config: type[BaseModel] = NoConfig

    @model_validator(mode="after")
    def _check(self) -> Self:
        for field, refs in (("provides", self.provides), ("inject", self.inject)):
            names = [parse_ref(ref)[0] for ref in refs]
            duplicates = sorted({name for name in names if names.count(name) > 1})
            if duplicates:
                raise ValueError(f"{field} 中服务名重复：{'、'.join(duplicates)}")
        if len(set(self.permissions)) != len(self.permissions):
            raise ValueError("permissions 中有重复")
        if CONTROL_SUBMIT in self.permissions and self.tier is not Tier.CRITICAL:
            raise ValueError(f"申请 {CONTROL_SUBMIT} 的插件必须是关键平面（critical）")
        return self

    @property
    def is_control_producer(self) -> bool:
        """申请了 `control.submit`：可以被指定为控制产出者。"""
        return CONTROL_SUBMIT in self.permissions
