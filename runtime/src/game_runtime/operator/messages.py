"""操作员连接上的消息：控制台与主进程之间，两端都是 Python。

设计见 `docs/modules/game-bridge.md` 的“操作员连接的消息”。约定同核协议：JSON-RPC 2.0，params
是对象，结构体拒绝未知字段，`_ns` 结尾的时刻与帧环同一时钟（`time.perf_counter_ns()`）。方法名以
`operator.` 起头，是本仓库自有的，不冒用游戏桥的消息名（契约的源在 Bellis，GD-03）。
"""

from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

SESSION: Final = "operator.session"
"""控制台 → 主进程，通知：会话状态。连上后立即发一次，此后每次变化都发。"""
DECISION_OPENED: Final = "operator.decision_opened"
"""主进程 → 控制台，通知：打开一个决策点。"""
DECISION_RESOLVED: Final = "operator.decision_resolved"
"""控制台 → 主进程，通知：操作员的选择。"""
DECISION_CLOSED: Final = "operator.decision_closed"
"""主进程 → 控制台，通知：决策点已关闭。"""
ACTION: Final = "operator.action"
"""主进程 → 控制台，通知：已提交的一个动作，计入会话记录。"""

NonNegative = Annotated[int, Field(ge=0, le=2**64 - 1)]
Text = Annotated[str, Field(min_length=1, max_length=2000)]


class _Message(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    def params(self) -> dict[str, Any]:
        """作为 JSON-RPC params 的对象。"""
        return self.model_dump(mode="json")


class SessionState(StrEnum):
    """测试会话的状态：`waiting` → `running` ⇄ `paused` → `ended`。"""

    WAITING = "waiting"
    """等待确认与开始。"""
    RUNNING = "running"
    """许可已授予并在续约。"""
    PAUSED = "paused"
    """令牌已吊销，等操作员继续。"""
    ENDED = "ended"
    """会话已结束，不能再开始。"""


class Session(_Message):
    """`operator.session` 的 params。"""

    adapter: Text
    """适配包 id。"""
    state: SessionState
    confirmed: tuple[Text, ...]
    """已确认的前置条件 id，按适配包里的顺序。"""
    goal: Text | None
    """当前目标的文字；没有时为 None。"""
    ends_at_ns: NonNegative | None
    """到时的时刻；还没开始时为 None。"""


class DecisionOption(_Message):
    """决策点的一个选项。"""

    index: NonNegative
    text: Text


class DecisionOpened(_Message):
    """`operator.decision_opened` 的 params。"""

    id: Text
    """本会话内唯一。"""
    prompt: Text
    options: tuple[DecisionOption, ...] = Field(min_length=1)
    candidates: tuple[NonNegative, ...]
    """按推荐顺序的选项 `index`。"""
    deadline_ns: NonNegative | None
    """有计时的截止时刻；无计时时为 None。"""

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        indexes = [option.index for option in self.options]
        if len(set(indexes)) != len(indexes):
            raise ValueError("选项的 index 重复")
        if len(set(self.candidates)) != len(self.candidates):
            raise ValueError("候选重复")
        if not set(self.candidates) <= set(indexes):
            raise ValueError("候选须是选项的 index")
        return self

    def option(self, index: int) -> DecisionOption | None:
        """`index` 的选项；没有时为 None。"""
        return next((option for option in self.options if option.index == index), None)


class DecisionResolved(_Message):
    """`operator.decision_resolved` 的 params：操作员的选择。"""

    id: Text
    index: NonNegative


class DecisionClosed(_Message):
    """`operator.decision_closed` 的 params。"""

    id: Text
    index: NonNegative | None
    """实际执行的选项；没有执行时为 None。"""
    by: Literal["operator", "default", "withdrawn"]
    """`operator`：按操作员的选择；`default`：到余量线执行排第一的候选；`withdrawn`：画面已变，不再执行。"""

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.by == "withdrawn") != (self.index is None):
            raise ValueError("withdrawn 时 index 为 null，其余须给出 index")
        return self


class Action(_Message):
    """`operator.action` 的 params：已提交的一个动作。"""

    category: Literal["advance", "navigate", "select"]
    summary: Text
