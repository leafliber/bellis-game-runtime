"""认证操作员目标的转发和会话内 planner 实例核对。"""

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Literal, Protocol
from weakref import ReferenceType, ref

from game_runtime.operator import messages
from game_runtime.step_control.contracts import Category, Goal, PlannerPort, StepStatus
from game_runtime.step_control.decisions import DecisionKey


class PlaySession(Protocol):
    @property
    def session(self) -> messages.Session | None: ...

    @property
    def session_id(self) -> str | None: ...

    @property
    def may_autoplay(self) -> bool: ...

    def open_decision(self, decision: messages.DecisionOpened) -> None: ...
    def update_decision(self, update: messages.DecisionUpdated) -> None: ...
    def close_decision(self, closed: messages.DecisionClosed) -> None: ...
    def report_action(self, action: messages.Action) -> None: ...


type ResolutionSource = Literal["director", "vote"]


@dataclass(frozen=True, slots=True)
class SubmittedChoice:
    decision_id: str
    index: int
    source: Literal["director", "vote", "default"] | None
    traceparent: str | None


type ActionOutcome = Callable[[StepStatus, str, int | None], None]


class SemanticPauseError(RuntimeError):
    """接纳的节奏或等待投票状态暂缓普通动作。"""


class SemanticPlay(PlaySession, Protocol):
    @property
    def planning_goal(self) -> Goal | None: ...

    @property
    def may_verify(self) -> bool: ...

    def progress_held(self) -> bool: ...
    def waiting_for(self, decision_id: str) -> bool: ...
    def rejected(self, detail: str) -> None: ...
    def action_started(
        self,
        action_id: str,
        category: Category,
        summary: str,
        deadline_ns: int,
        choice: SubmittedChoice | None,
    ) -> ActionOutcome: ...


@dataclass(slots=True)
class SessionProgress:
    """控制产出者重建后仍有效的步数账目和未决输入状态。"""

    goal: Goal | None = None
    steps: int = 0
    goal_done: bool = False
    stuck: bool = False
    closed_choices: set[DecisionKey] = field(default_factory=set[DecisionKey])
    unknown_choice_revision: int | None = None
    ending: tuple[str, int] | None = None
    restart_attempt_revision: int | None = None
    fast_dialogue_line: tuple[int, int] | None = None

    def select_goal(self, goal: Goal | None) -> None:
        if goal != self.goal:
            self.goal = goal
            self.steps = 0
            self.goal_done = False
            self.stuck = False

    def choice_held(self, operator: "OperatorGoalRelay") -> bool:
        blocked_at = self.unknown_choice_revision
        return blocked_at is not None and (operator.goal is None or operator.revision <= blocked_at)


class OperatorGoalRelay:
    """递交认证会话中的目标指令。控制插件重建时沿用递交进度。"""

    def __init__(self) -> None:
        self.goal: Goal | None = None
        self.revision = 0
        self._source: tuple[str, int, str | None] | None = None
        self._sent = 0
        self._recipient: ReferenceType[PlannerPort] | None = None

    def update(
        self, session_id: str, revision: int, text: str | None, *, plan: Goal | None = None
    ) -> None:
        source = (session_id, revision, text)
        if self._source != source:
            self._source = source
            self.revision += 1
            if plan is not None:
                self.goal = replace(plan, generation=self.revision)
            else:
                self.goal = None if text is None else Goal(text, generation=self.revision)

    def forward(self, planner: PlannerPort) -> None:
        if (
            self._sent != self.revision
            or self._recipient is None
            or self._recipient() is not planner
        ):
            recipient = ref(planner)
            planner.submit_goal(self.goal)
            self._sent = self.revision
            self._recipient = recipient


class PlannerBinding:
    """经 planner 控制的会话在依赖中断后等待新的授权目标和可用状态。"""

    def __init__(self) -> None:
        self.used = False
        self._provider: ReferenceType[PlannerPort] | None = None
        self._blocked_at: int | None = None

    def bind(self, planner: PlannerPort) -> None:
        self.used = True
        self._provider = ref(planner)
        self._blocked_at = None

    def permits(self, planner: PlannerPort | None) -> bool:
        if self._blocked_at is not None:
            return False
        if planner is None:
            return not self.used
        return self._provider is not None and self._provider() is planner

    def mark_interrupted(self, revision: int) -> None:
        if self.used and self._blocked_at is None:
            self._blocked_at = revision

    def can_recover(self, operator: OperatorGoalRelay) -> bool:
        return (
            self._blocked_at is not None
            and operator.revision > self._blocked_at
            and operator.goal is not None
        )
