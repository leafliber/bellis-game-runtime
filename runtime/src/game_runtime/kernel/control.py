"""控制产出者：提交资格与实时核一侧的接口，见 `docs/modules/plugin-kernel.md`。"""

from collections.abc import Callable
from typing import Protocol

CONTROL_SUBMIT = "control.submit"
"""向实时核提交动作块的权限，只授予当前控制产出者。"""


class ControlRevokedError(RuntimeError):
    """控制产出者的提交资格已收回（不在 ACTIVE，或已被更换）。"""


class ControlGrant:
    """一个控制产出者实例的提交资格：产出者身份与代次。

    资格只在该实例处于 ACTIVE 时有效。进入 QUIESCING 的同时失效，这就是“停止提交动作块”；
    提交动作块的路径在发送前调用 `check()`。
    """

    __slots__ = ("_is_open", "generation", "instance_id", "producer")

    def __init__(
        self, producer: str, instance_id: str, generation: int, is_open: Callable[[], bool]
    ) -> None:
        self.producer = producer
        """profile 条目 id。"""
        self.instance_id = instance_id
        """插件实例 id，每次激活都不同。"""
        self.generation = generation
        """代次：每次激活控制产出者时加一，本次主进程运行内单调递增。"""
        self._is_open = is_open

    @property
    def is_open(self) -> bool:
        return self._is_open()

    def check(self) -> None:
        """资格已收回时抛 `ControlRevokedError`。"""
        if not self._is_open():
            raise ControlRevokedError(f"{self.instance_id} 的代次 {self.generation} 已停止提交")


class CoreControl(Protocol):
    """实时核一侧的接口。插件内核只用它完成控制产出者的 QUIESCING。"""

    async def invalidate_generation(self, grant: ControlGrant) -> None:
        """请求实时核作废该代次：此后拒绝该代次的动作块，并释放它负责的全部输入。

        返回表示请求已送达，不表示已释放。
        """
        ...

    async def wait_released(self, grant: ControlGrant) -> None:
        """等到实时核确认该代次负责的输入已全部释放。

        作废请求送达失败时内核仍会调用它：实时核按失效规则自行释放后（例如断连后退出、
        重启后先释放），实现应据此完成等待。
        """
        ...
