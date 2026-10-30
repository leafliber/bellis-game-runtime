"""条目的状态与原因：给操作台与健康上报看的部分，见 `docs/modules/plugin-kernel.md`。"""

from dataclasses import dataclass
from enum import StrEnum

from game_runtime.kernel.manifest import Tier


class State(StrEnum):
    """profile 条目的生命周期状态。"""

    WAITING = "waiting"
    """依赖、权限或控制产出者的指定不满足，等条件满足后自动激活。"""
    LOADING = "loading"
    """正在激活。"""
    ACTIVE = "active"
    """运行中，服务已发布。"""
    QUIESCING = "quiescing"
    """静止中：已停止新工作，控制产出者还要等实时核确认输入已释放。"""
    UNLOADING = "unloading"
    """卸载中：按后注册先撤销的顺序撤销注册。"""
    DISPOSED = "disposed"
    """已卸载：条目被停用，不加载实例。"""
    FAILED = "failed"
    """激活失败，等退避重试或已停下等待操作员。"""


class ReasonCode(StrEnum):
    """机器可读的原因。"""

    SERVICE_MISSING = "service-missing"
    SERVICE_MAJOR_MISMATCH = "service-major-mismatch"
    PERMISSION_NOT_GRANTED = "permission-not-granted"
    CONTROL_NOT_DESIGNATED = "control-not-designated"
    CONTROL_HANDOVER_PENDING = "control-handover-pending"
    PROVIDER_STOPPING = "provider-stopping"
    DEPENDENT_QUIESCING = "dependent-quiescing"
    DISABLED = "disabled"
    CONFIG_INVALID = "config-invalid"
    ACTIVATION_FAILED = "activation-failed"
    RETRIES_EXHAUSTED = "retries-exhausted"
    RELEASE_TIMEOUT = "release-timeout"
    RELEASE_FAILED = "release-failed"


@dataclass(frozen=True, slots=True)
class Reason:
    """一条原因：`code` 给程序判断，`subject` 是涉及的服务、权限或条目，`message` 给人读。"""

    code: ReasonCode
    subject: str
    message: str


@dataclass(frozen=True, slots=True)
class EntryStatus:
    """一个 profile 条目此刻的状态。"""

    id: str
    plugin: str
    version: str
    tier: Tier
    state: State
    reasons: tuple[Reason, ...]
    instance_id: str | None
    enabled: bool
    attempts: int
    """连续激活失败的次数；激活成功或停用后再启用时清零。"""


@dataclass(frozen=True, slots=True)
class Transition:
    """一次状态或原因的变化。"""

    entry_id: str
    before: State
    after: State
    reasons: tuple[Reason, ...]
