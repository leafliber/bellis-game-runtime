"""控制产出者：提交资格、提交动作块与实时核一侧的接口，见 `docs/modules/plugin-kernel.md`。"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Protocol

from core_protocol import models

CONTROL_SUBMIT = "control.submit"
"""向实时核提交动作块的权限，只授予当前控制产出者。"""


class ControlRevokedError(RuntimeError):
    """控制产出者的提交资格已收回（不在 ACTIVE，或已被更换）。"""


class Submission(Protocol):
    """一块已提交的动作：实时核的回应，以及其中一次性动作的结果。"""

    @property
    def block(self) -> models.ActionBlock:
        """提交的动作块。"""
        ...

    @property
    def result(self) -> models.Accepted | models.Rejected:
        """实时核接受还是拒绝了它。"""
        ...

    async def outcomes(self) -> Mapping[int, models.Executed | models.Dropped]:
        """等到每个一次性动作都有了结果（`action.receipt`），按动作身份给出。被拒绝的块与没有一次性
        动作的块立即返回空。`executed` 只表示已交给输入后端，不代表游戏中的效果已发生。"""
        ...


class SubmitFn(Protocol):
    """`CoreControl.submit` 的形状。"""

    def __call__(
        self,
        grant: "ControlGrant",
        steps: Sequence[models.ControlStep],
        *,
        base_frame: int,
        valid_for: float,
        start_delay: float = 0.0,
    ) -> Awaitable[Submission]: ...


class ControlGrant:
    """一个控制产出者实例的提交资格：产出者身份与代次。

    资格只在该实例处于 ACTIVE 时有效。进入 QUIESCING 的同时失效，这就是“停止提交动作块”；
    提交动作块的路径在发送前调用 `check()`。插件以 `ctx.control.submit(...)` 提交。
    """

    __slots__ = ("_is_open", "_submit", "generation", "instance_id", "producer")

    def __init__(
        self,
        producer: str,
        instance_id: str,
        generation: int,
        is_open: Callable[[], bool],
        submit: SubmitFn | None = None,
    ) -> None:
        self.producer = producer
        """profile 条目 id。"""
        self.instance_id = instance_id
        """插件实例 id，每次激活都不同。"""
        self.generation = generation
        """代次：每次激活控制产出者时加一，本次主进程运行内单调递增。"""
        self._is_open = is_open
        self._submit = submit

    @property
    def is_open(self) -> bool:
        return self._is_open()

    def check(self) -> None:
        """资格已收回时抛 `ControlRevokedError`。"""
        if not self._is_open():
            raise ControlRevokedError(f"{self.instance_id} 的代次 {self.generation} 已停止提交")

    async def submit(
        self,
        steps: Sequence[models.ControlStep],
        *,
        base_frame: int,
        valid_for: float,
        start_delay: float = 0.0,
    ) -> Submission:
        """以这份资格向实时核提交一块动作，见 `CoreControl.submit`。资格已收回时抛
        `ControlRevokedError`，什么也不发。"""
        self.check()
        if self._submit is None:
            raise RuntimeError(f"{self.instance_id} 的提交资格没有接到实时核")
        return await self._submit(
            self, steps, base_frame=base_frame, valid_for=valid_for, start_delay=start_delay
        )


class CoreControl(Protocol):
    """实时核一侧的接口。插件内核用前两项完成控制产出者的 QUIESCING；`submit` 经提交资格
    交给控制产出者。"""

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

    async def submit(
        self,
        grant: ControlGrant,
        steps: Sequence[models.ControlStep],
        *,
        base_frame: int,
        valid_for: float,
        start_delay: float = 0.0,
    ) -> Submission:
        """以 `grant` 的身份与代次提交一块动作：起始时刻为现在加 `start_delay` 秒，有效截止时刻再加
        `valid_for` 秒；坐标按基准帧 `base_frame`（产出者做决定时看的那一帧的帧号）的客户区解释。

        发送前调用 `grant.check()`，资格已收回即抛 `ControlRevokedError`。返回时已收到实时核的回应。
        """
        ...
