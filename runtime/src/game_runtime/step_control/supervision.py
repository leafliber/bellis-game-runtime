"""认证操作员目标的转发和会话内 planner 实例核对。"""

from weakref import ReferenceType, ref

from game_runtime.step_control.contracts import Goal, PlannerPort


class OperatorGoalRelay:
    """按认证会话和目标文本的变化递交。控制插件重建时沿用递交进度。"""

    def __init__(self) -> None:
        self.goal: Goal | None = None
        self.revision = 0
        self._source: tuple[str, str | None] | None = None
        self._sent = 0
        self._recipient: ReferenceType[PlannerPort] | None = None

    def update(self, session_id: str, text: str | None) -> None:
        source = (session_id, text)
        if self._source != source:
            self._source = source
            self.revision += 1
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
