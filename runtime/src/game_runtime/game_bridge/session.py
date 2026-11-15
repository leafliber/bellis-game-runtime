"""认证语义连接的目标、决策实例和节奏状态。"""

import logging
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ValidationError

from game_runtime.adapter import Adapter
from game_runtime.channel import ChannelError, RpcError, RpcErrorCode, RpcPeer
from game_runtime.game_bridge.wire import ms, ns, params, selection_source, v1
from game_runtime.operator import Clock, SessionState, messages
from game_runtime.secret_store import SecretStore, SecretStoreError, SessionInfo
from game_runtime.step_control.contracts import Category, Goal, StepStatus
from game_runtime.step_control.supervision import ActionOutcome, SemanticPauseError, SubmittedChoice

if TYPE_CHECKING:
    from game_runtime.game_bridge.projection import Projection
    from game_runtime.step_control.plugin import SessionSignals

logger = logging.getLogger(__name__)
SESSION_ENTRY = "session"
HOLD_LIMIT_NS = 30_000_000_000
type Mode = Literal["autonomous", "waiting_vote", "paused", "manual"]


@dataclass(frozen=True, slots=True)
class RiskReading:
    score: float
    exceeded: bool

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("风险分须在 [0, 1] 内")


@dataclass(frozen=True, slots=True)
class BellisConfig:
    session_store: SecretStore
    confirmed: frozenset[str] = frozenset()
    test_session_file: Path | None = None
    risk: Callable[[], RiskReading | None] | None = None


def read_bellis_session(store: SecretStore) -> SessionInfo | None:
    text = store.get(SESSION_ENTRY)
    if text is None:
        return None
    try:
        info = SessionInfo.model_validate_json(text)
        if "v" in info.model_fields_set:
            info.credentials()
            return info
    except (ValidationError, ChannelError):
        pass
    raise SecretStoreError("Bellis 会话连接信息不合法")


@dataclass(frozen=True, slots=True)
class _Goal:
    id: str
    connection: int
    mode: Mode
    expires_ns: int
    accepted_ns: int
    traceparent: str | None
    decision_id: str | None


@dataclass(slots=True)
class _Decision:
    opened: messages.DecisionOpened
    traceparent: str | None
    latest: v1.GameDecisionPointOpened | None = None
    resolved: bool = False


@dataclass(frozen=True, slots=True)
class FailedAction:
    action_id: str
    goal_id: str
    choice: SubmittedChoice | None
    outcome: StepStatus
    detail: str
    at_ns: int
    traceparent: str | None


def _refuse(reason: str) -> RpcError:
    return RpcError(RpcErrorCode.INVALID_PARAMS, reason)


def _option_id(index: int) -> str:
    return f"option-{index}"


class BellisSession:
    """同一 Runtime 内跨语义重连保留的状态。能收紧控制，许可由实时核独立取得。"""

    def __init__(
        self,
        config: BellisConfig,
        info: SessionInfo,
        runtime_id: str,
        adapter: Adapter,
        clock: Clock,
        signals: "SessionSignals",
    ) -> None:
        self.config, self.info, self.runtime_id = config, info, runtime_id
        self.clock, self.signals = clock, signals
        required = {item.id for item in adapter.risk.preconditions}
        if config.confirmed - required:
            raise ValueError("确认的前置条件不属于选定的适配包")
        self.prerequisites = tuple(
            v1.Prerequisite(id=item.id, text=item.text, confirmed=item.id in config.confirmed)
            for item in adapter.risk.preconditions
        )
        self.adapter = v1.Adapter.model_validate(
            {
                "id": adapter.id,
                "version": adapter.version,
                "screenTypes": [
                    {"name": item.name, "base": item.base.value} for item in adapter.screen_types
                ],
            }
        )
        self.project: Projection | None = None
        self.peer: RpcPeer | None = None
        self.described_ns: int | None = None
        self._connection = 0
        self._plan_generation = 0
        self._authorization: _Goal | None = None
        self._before_vote: tuple[_Goal, int | None] | None = None
        self._plan_goal: Goal | None = None
        self._goals_seen: set[str] = set()
        self._point: _Decision | None = None
        self._manual = False
        self._unknown: FailedAction | None = None
        self.failures: deque[FailedAction] = deque(maxlen=128)
        self.reported_lookpoints: set[str] = set()
        self._seen_holds: set[str] = set()
        self._holds: dict[str, int] = {}
        self._hold_cap_ns: int | None = None
        self._first_action_ms: int | None = None
        self._inflight = 0
        self._deadline_ns: int | None = None
        self.last_rejection: v1.LastRejection | None = None

    @property
    def session_id(self) -> str:
        return self.info.session_id

    @property
    def session(self) -> messages.Session:
        goal = self._authorization
        state = SessionState.RUNNING if self._active(goal) else SessionState.PAUSED
        return messages.Session(
            adapter=self.adapter.id,
            state=state,
            confirmed=tuple(item.id for item in self.prerequisites if item.confirmed),
            goal=None if self._plan_goal is None else self._plan_goal.text,
            ends_at_ns=None if goal is None else goal.expires_ns,
            goal_revision=self._plan_generation,
        )

    @property
    def planning_goal(self) -> Goal | None:
        return self._plan_goal

    @property
    def may_autoplay(self) -> bool:
        return self._active(self._authorization) and self.ready()

    @property
    def may_verify(self) -> bool:
        # 点击改变画面后，scene 会先清掉旧帧，再发布新帧的判别。
        return (
            self._active(self._authorization)
            and self._safety_ready(self._risk())
            and self.project is not None
            and self.project.capture_ready(self.clock.now_ns())
        )

    def _active(self, goal: _Goal | None) -> bool:
        return (
            goal is not None
            and goal.connection == self._connection
            and self.live
            and self._unknown is None
            and not self._manual
            and goal.mode in ("autonomous", "waiting_vote")
            and self.clock.now_ns() < goal.expires_ns
        )

    @property
    def live(self) -> bool:
        return (
            self.peer is not None and self.peer.connection.is_open and self.described_ns is not None
        )

    @property
    def traceparent(self) -> str | None:
        goal = self._authorization
        return (
            goal.traceparent if goal is not None and self.clock.now_ns() < goal.expires_ns else None
        )

    @property
    def unknown(self) -> FailedAction | None:
        return self._unknown

    @property
    def interactables(self) -> tuple[str, ...]:
        point = self._point
        return (
            ()
            if point is None
            else tuple(option.text[:120] for option in point.opened.options[:32])
        )

    def action_deadline(self) -> int | None:
        """供核连接在发送时读取的最后一个合法纳秒。"""
        return self._deadline_ns

    def _risk(self) -> RiskReading | None:
        if self.config.risk is None:
            return None
        try:
            return self.config.risk()
        except Exception:
            return None

    def ready(self) -> bool:
        """当前已判别帧可用于接纳目标和提交新动作。"""
        return (
            self._safety_ready(self._risk())
            and self.project is not None
            and self.project.action_ready(self.signals.controller, self.clock.now_ns())
        )

    def _safety_ready(self, risk: RiskReading | None) -> bool:
        return (
            risk is not None
            and not risk.exceeded
            and all(item.confirmed for item in self.prerequisites)
        )

    def health(self) -> v1.GameMainHealth:
        now = self.clock.now_ns()
        goal = self._authorization
        live_goal = goal if goal is not None and now < goal.expires_ns else None
        risk = self._risk()
        controller = self.signals.controller
        rejection = self.last_rejection
        if risk is None:
            rejection = v1.LastRejection(
                atMs=ms(now), code="risk-unsupported", summary="本机风险来源尚不可用"
            )
        mode: Mode = (
            "manual"
            if self._manual
            else "paused"
            if self._unknown is not None or live_goal is None
            else live_goal.mode
        )
        return v1.GameMainHealth(
            atMs=ms(now),
            ready=(
                self._safety_ready(risk)
                and self.project is not None
                and self.project.health_ready(controller, now)
            ),
            mode=mode,
            goalId=None if live_goal is None else live_goal.id,
            decisionPointId=None
            if self._point is None or self._point.latest is None
            else self._point.opened.id,
            prerequisites=list(self.prerequisites),
            firstActionMs=self._first_action_ms,
            frameAgeMs=None if self.project is None else self.project.frame_age(now),
            queueDepth=self._inflight + (0 if controller is None else controller.queue_depth),
            riskScore=0.0 if risk is None else risk.score,
            riskLimitExceeded=risk is not None and risk.exceeded,
            lastRejection=rejection,
        )

    def same_session(self) -> bool:
        try:
            return read_bellis_session(self.config.session_store) == self.info
        except SecretStoreError:
            return False

    def attach(self, peer: RpcPeer) -> tuple[Callable[[], None], ...]:
        self.peer = peer
        self._connection += 1

        def handle(
            handler: Callable[[object], BaseModel],
        ) -> Callable[[object], dict[str, object]]:
            def request(value: object) -> dict[str, object]:
                if self.peer is not peer or not peer.connection.is_open or not self.same_session():
                    raise _refuse("Bellis 会话已失效")
                return handler(value).model_dump(mode="json")

            return request

        return tuple(
            peer.handle(method, handle(handler))
            for method, handler in (
                ("game.describe", self._describe),
                ("goal.submit", self._submit_goal),
                ("decision.resolve", self._resolve),
                ("pacing.hold", self._hold),
                ("pacing.release", self._release),
            )
        )

    def _describe(self, value: object) -> v1.GameMainDescription | v1.GameDescribeVersion:
        request = params(v1.GameDescribeParams, value)
        if request.major != v1.GAME_BRIDGE_MAJOR:
            return v1.GameDescribeVersion(major=v1.GAME_BRIDGE_MAJOR)
        if self.described_ns is not None:
            raise _refuse("语义握手已完成")
        now = self.clock.now_ns()
        self.described_ns = now
        return v1.GameMainDescription(
            major=v1.GAME_BRIDGE_MAJOR,
            runtimeId=self.runtime_id,
            nowMs=ms(now),
            adapter=self.adapter,
            prerequisites=list(self.prerequisites),
        )

    def _require_live(self) -> None:
        if not self.live:
            raise _refuse("语义握手尚未完成")

    def _submit_goal(self, value: object) -> v1.GameAck:
        request = params(v1.GameGoalSubmitParams, value)
        self._require_live()
        now = self.clock.now_ns()
        expires = ns(request.expiresAtMs)
        if expires <= now or request.goalId in self._goals_seen or not request.objective.strip():
            raise _refuse("目标已过期、已使用或缺少内容")
        if request.mode == "manual":
            raise _refuse("人工接管须经操作员入口")
        controller = self.signals.controller
        if request.mode != "paused" and (not self.ready() or controller is None):
            raise _refuse("逐步控制尚未就绪")
        before_vote = None
        if request.mode == "waiting_vote":
            point = self._point
            prior = self._authorization
            if (
                prior is None
                or prior.mode != "autonomous"
                or not self._active(prior)
                or point is None
                or point.latest is None
                or point.resolved
                or point.opened.id != request.decisionPointId
                or point.opened.deadline_ns is not None
                or controller is None
                or not controller.decision_available(point.opened.id)
            ):
                raise _refuse("投票须绑定当前无计时决策点")
            before_vote = prior, self._first_action_ms
        elif request.decisionPointId is not None:
            raise _refuse("只有等待投票的目标可绑定决策点")

        self._authorization = _Goal(
            request.goalId,
            self._connection,
            request.mode,
            expires,
            now,
            request.traceparent,
            request.decisionPointId,
        )
        self._goals_seen.add(request.goalId)
        self._before_vote = before_vote
        self._first_action_ms = None
        if request.mode != "waiting_vote":
            self._plan_generation += 1
            self._plan_goal = None
            if request.mode == "autonomous":
                self._unknown = None
                self._manual = False
                self._plan_goal = Goal(
                    request.objective, deadline_ns=expires, constraints=tuple(request.constraints)
                )
        self._announce()
        return v1.GameAck()

    def _resolve(self, value: object) -> v1.GameAck:
        request = params(v1.GameDecisionResolveParams, value)
        self._require_live()
        point = self._point
        controller = self.signals.controller
        if (
            not self.may_autoplay
            or controller is None
            or point is None
            or point.latest is None
            or point.opened.id != request.decisionPointId
            or point.resolved
            or (request.source == "vote" and not self.waiting_for(request.decisionPointId))
        ):
            raise _refuse("决策实例已过期、不匹配或已经接纳决定")
        index = next(
            (
                option.index
                for option in point.opened.options
                if _option_id(option.index) == request.optionId
            ),
            None,
        )
        if index is None or not controller.on_resolved(
            messages.DecisionResolved(id=request.decisionPointId, index=index),
            source=request.source,
            traceparent=request.traceparent,
        ):
            raise _refuse("选项或决策余量线已经失效")
        point.resolved = True
        return v1.GameAck()

    def _hold(self, value: object) -> v1.GameAck:
        request = params(v1.GamePacingHoldParams, value)
        self._require_live()
        now = self.clock.now_ns()
        if ns(request.expiresAtMs) <= now:
            raise _refuse("暂缓已经到期")
        self._prune_holds(now)
        if request.holdId not in self._seen_holds:
            self._seen_holds.add(request.holdId)
            if self._hold_cap_ns is None:
                self._hold_cap_ns = now + HOLD_LIMIT_NS
            self._holds[request.holdId] = min(ns(request.expiresAtMs), self._hold_cap_ns)
            logger.info("Bellis 暂缓开始 %s", request.holdId)
            self.signals.became_ready()
        return v1.GameAck()

    def _release(self, value: object) -> v1.GameAck:
        request = params(v1.GamePacingReleaseParams, value)
        self._require_live()
        self._seen_holds.add(request.holdId)
        if self._holds.pop(request.holdId, None) is not None:
            logger.info("Bellis 暂缓解除 %s", request.holdId)
        self._prune_holds(self.clock.now_ns())
        self.signals.became_ready()
        return v1.GameAck()

    def _prune_holds(self, now: int) -> None:
        for hold_id in [key for key, expires in self._holds.items() if expires <= now]:
            del self._holds[hold_id]
            logger.info("Bellis 暂缓结束 %s", hold_id)
        if not self._holds:
            self._hold_cap_ns = None

    def progress_held(self) -> bool:
        self._prune_holds(self.clock.now_ns())
        goal = self._authorization
        return bool(self._holds) or (goal is not None and goal.mode == "waiting_vote")

    def waiting_for(self, decision_id: str) -> bool:
        goal = self._authorization
        return (
            goal is not None
            and goal.mode == "waiting_vote"
            and goal.decision_id == decision_id
            and self._active(goal)
        )

    def tick(self) -> None:
        self._prune_holds(self.clock.now_ns())
        goal = self._authorization
        if goal is not None and self.clock.now_ns() >= goal.expires_ns:
            self.rejected("goal-expired")
            self.suspend()

    def detach(self) -> None:
        self.peer = None
        self.described_ns = None
        self.suspend()

    def suspend(self, *, manual: bool = False) -> None:
        self._manual |= manual
        if self._authorization is not None or self._plan_goal is not None:
            self._plan_generation += 1
        self._authorization = None
        self._before_vote = None
        self._plan_goal = None
        self._announce()

    def _announce(self) -> None:
        self.signals.session(self.session)
        self.signals.became_ready()

    def rejected(self, detail: str) -> None:
        code = re.sub(r"[^a-z0-9-]", "-", detail.lower())[:48]
        if re.fullmatch(r"[a-z][a-z0-9-]{0,47}", code) is None:
            code = "action-refused"
        self.last_rejection = v1.LastRejection(
            atMs=ms(self.clock.now_ns()), code=code, summary=detail[:200] or "动作被拒绝"
        )

    def notify(self, method: str, value: BaseModel) -> bool:
        peer = self.peer
        if peer is None or not self.live:
            return False
        try:
            peer.notify(method, value.model_dump(mode="json"))
        except ChannelError:
            peer.connection.abort()
            return False
        return True

    def open_decision(self, decision: messages.DecisionOpened) -> None:
        self._point = _Decision(decision, self.traceparent)
        self.publish_point()

    def update_decision(self, update: messages.DecisionUpdated) -> None:
        point = self._point
        if point is None or point.opened.id != update.id:
            return
        old = point.opened.deadline_ns
        if (old is None) != (update.deadline_ns is None):
            return
        if old is not None and update.deadline_ns is not None and update.deadline_ns > old:
            return
        point.opened = point.opened.model_copy(
            update={"deadline_ns": update.deadline_ns, "candidates": update.candidates}
        )
        self.publish_point()

    def publish_point(self) -> None:
        point = self._point
        controller = self.signals.controller
        if point is None or point.resolved or controller is None or not self.live:
            return
        decision = point.opened
        now = ms(self.clock.now_ns())
        timing = None
        if decision.deadline_ns is not None:
            cutoff = ms(decision.deadline_ns - round(controller.config.decision_margin * 1e9))
            if now >= cutoff or not decision.candidates:
                return
            timing = v1.Timing(resolveByMs=cutoff, deadlineMs=ms(decision.deadline_ns))
        if len(decision.options) > 64:
            return
        ranks = () if self.project is None else self.project.ranks(self.session_id, decision.id)
        details = {row.index: row for row in ranks}
        candidates: list[v1.Candidate] = []
        for rank, index in enumerate(decision.candidates, 1):
            row = details.get(index)
            candidates.append(
                v1.Candidate(
                    optionId=_option_id(index),
                    rank=rank,
                    reason="逐步策略的当前推荐"
                    if row is None
                    else row.reason.strip()[:240] or "当前推荐",
                    citation=None if row is None or not row.quotes else row.quotes[0][1],
                    previousFailure=None
                    if row is None or row.failed_outcome is None
                    else v1.PreviousFailure(
                        source=selection_source(row.failed_source), summary=row.failed_outcome[:240]
                    ),
                )
            )
        message = v1.GameDecisionPointOpened(
            decisionPointId=decision.id,
            atMs=now,
            prompt=decision.prompt[:360],
            options=[
                v1.Option(id=_option_id(option.index), label=option.text[:240])
                for option in decision.options
            ],
            candidates=candidates,
            timing=timing,
            traceparent=point.traceparent,
        )
        latest = point.latest
        if latest is not None and message.model_copy(update={"atMs": latest.atMs}) == latest:
            return
        if self.notify("decision_point.opened", message):
            point.latest = message

    def close_decision(self, closed: messages.DecisionClosed) -> None:
        if self._point is not None and self._point.opened.id == closed.id:
            self._point = None
        goal = self._authorization
        if goal is None or goal.mode != "waiting_vote" or goal.decision_id != closed.id:
            return
        prior, self._before_vote = self._before_vote, None
        if closed.by in ("operator", "default") and prior is not None and self._active(prior[0]):
            self._authorization, self._first_action_ms = prior
            self._announce()
        elif closed.by != "unknown":
            self.suspend()

    def report_action(self, action: messages.Action) -> None:
        """独立操作员提交计数；语义事实经核验结果回调产生。"""

    def action_started(
        self,
        action_id: str,
        category: Category,
        summary: str,
        deadline_ns: int,
        choice: SubmittedChoice | None,
    ) -> ActionOutcome:
        goal = self._authorization
        if goal is None or not self._active(goal) or not self.ready():
            raise SemanticPauseError("goal-unavailable")
        if not self.same_session():
            self.suspend()
            raise SemanticPauseError("bellis-session-changed")
        if category is Category.SELECT and choice is None:
            raise SemanticPauseError("decision-without-authority")
        point = self._point
        selection = None
        trace = goal.traceparent
        if choice is not None:
            if point is None or choice.decision_id != point.opened.id:
                raise SemanticPauseError("decision-unavailable")
            if point.latest is None and choice.source != "default":
                raise SemanticPauseError("decision-unpublished")
            if choice.source is None:
                raise SemanticPauseError("decision-without-authority")
            selection = v1.Selection(
                decisionPointId=choice.decision_id,
                optionId=_option_id(choice.index),
                source=choice.source,
            )
            trace = choice.traceparent or trace
            point.resolved = True
        self._inflight += 1
        # _active 已核对 now < expires_ns。语义授权的排他截止转换为核动作块的包容截止；
        # 更早的决策期限或动作块上限仍是原值。
        self._deadline_ns = min(deadline_ns, goal.expires_ns - 1)

        def finished(status: StepStatus, detail: str, executed_ns: int | None) -> None:
            self._inflight -= 1
            self._deadline_ns = None
            if status is StepStatus.VERIFIED and executed_ns is not None:
                if self._authorization is goal and self._first_action_ms is None:
                    self._first_action_ms = min(
                        2**32 - 1, ms(max(0, executed_ns - goal.accepted_ns))
                    )
                if self._connection == goal.connection:
                    self.notify(
                        "action.committed",
                        v1.GameActionCommitted.model_validate(
                            {
                                "actionId": action_id,
                                "atMs": ms(executed_ns),
                                "goalId": goal.id,
                                "category": category.value,
                                "summary": summary,
                                "selection": selection,
                                "traceparent": trace,
                            }
                        ),
                    )
            elif status in {StepStatus.UNKNOWN, StepStatus.NO_EFFECT, StepStatus.REJECTED}:
                failure = FailedAction(
                    action_id, goal.id, choice, status, detail, self.clock.now_ns(), trace
                )
                self.failures.append(failure)
                if status is StepStatus.UNKNOWN or (
                    choice is not None and status is StepStatus.NO_EFFECT
                ):
                    self._unknown = failure
                    self.rejected("action-outcome-unknown")

        return finished
