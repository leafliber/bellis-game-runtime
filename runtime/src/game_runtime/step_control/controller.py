"""受监督的逐步控制。只向内核授予的当前代次提交一个立即生效的动作。"""

import asyncio
import logging
from collections import Counter, deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from core_protocol import models
from game_runtime.adapter import Adapter, ScreenType
from game_runtime.adapter.schema import DecisionTimer, DialogueBox
from game_runtime.channel import ChannelError
from game_runtime.core_link.link import ActionDeadlineError
from game_runtime.kernel import ControlGrant, ControlRevokedError
from game_runtime.narrative import NarrativeRecord
from game_runtime.ocr import OcrResult, OcrService
from game_runtime.operator import Clock, SessionState, messages
from game_runtime.scene import (
    SceneService,
    SceneUpdate,
    ScreenObservation,
    ScreenSource,
    Stability,
    Verdict,
)
from game_runtime.step_control.contracts import (
    Category,
    Click,
    DecisionEvent,
    DecisionView,
    EffectKind,
    ExpectedEffect,
    Goal,
    GoalEvent,
    GoalJudgment,
    ObservationReceipt,
    Plan,
    PlannerPort,
    Policy,
    PolicyInput,
    Press,
    StepProposal,
    StepResult,
    StepStatus,
    TextTarget,
)
from game_runtime.step_control.decisions import (
    DecisionEvidence,
    PendingDecision,
    timer_deadline,
)
from game_runtime.step_control.pacing import ReadingPacing, ReadingWaitError, line_revision
from game_runtime.step_control.risk import (
    STOPPED,
    ActionRefusedError,
    Located,
    assess,
    core_action,
    forbidden_text,
    locate,
    requires_choice,
    text_key,
    verify_ocr,
)
from game_runtime.step_control.supervision import (
    OperatorGoalRelay,
    PlannerBinding,
    PlaySession,
    ResolutionSource,
    SemanticPauseError,
    SemanticPlay,
    SessionProgress,
    SubmittedChoice,
)

logger = logging.getLogger(__name__)

type Seconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class StepConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_frame_age: Seconds = 0.5
    action_valid_for: Seconds = 0.25
    receipt_timeout: Seconds = 2.0
    effect_timeout: Seconds = 1.5
    policy_timeout: Seconds = 8.0
    decision_margin: Seconds = 0.75
    cutscene_timeout: Seconds = 600.0
    snap_radius: Annotated[int, Field(ge=1, le=256)] = 48
    max_rejections: Annotated[int, Field(ge=1, le=20)] = 3
    fast_paths: bool = True


def _ns(seconds: float) -> int:
    return round(seconds * 1e9)


class StepController:
    def __init__(
        self,
        adapter: Adapter,
        scene: SceneService,
        ocr: OcrService,
        grant: ControlGrant,
        operator: PlaySession,
        policy: Policy,
        clock: Clock,
        config: StepConfig,
        core_ready: Callable[[], bool],
        planner: Callable[[], PlannerPort | None],
        progress: SessionProgress,
        operator_goal: OperatorGoalRelay,
        planner_binding: PlannerBinding,
        reading: ReadingPacing,
        *,
        semantic: SemanticPlay | None = None,
    ) -> None:
        self.adapter = adapter
        self.scene = scene
        self.ocr = ocr
        self.grant = grant
        self.operator = operator
        self._semantic = semantic
        self.policy = policy
        self.clock = clock
        self.config = config
        self._core_ready = core_ready
        self._planner = planner
        self._reading = reading
        self._planner_binding = planner_binding
        self._planner_paused = False
        self._planner_planning = False
        self._pause_reason: str | None = None
        self._operator_goal = operator_goal
        self._wake = asyncio.Event()
        self._work: asyncio.Task[None] | None = None
        self._closed = False
        self._session: messages.Session | None = None
        self._progress = progress
        self._notes = ""
        self._revision = 0
        self._recent: deque[StepResult] = deque(maxlen=24)
        self._plan: deque[StepProposal] = deque()
        self._verified_scope: object | None = None
        self._last_proposed: object | None = None
        self._last_action_ns: int | None = None
        self._next_id = 0
        self._pending: PendingDecision | None = None
        self._decision_prefix = uuid4().hex
        self._decision_count = 0
        self._choice_submitting = False
        self._language_screen: str | None = None
        self._language_failed = False
        self._rejected_screen: tuple[str | None, ScreenType] | None = None
        self._rejects = 0

    @property
    def recent(self) -> tuple[StepResult, ...]:
        return tuple(self._recent)

    @property
    def queue_depth(self) -> int:
        return len(self._plan)

    @property
    def stuck(self) -> bool:
        return self._progress.stuck

    async def propose(self, inputs: PolicyInput) -> Plan:
        async with asyncio.timeout(self.config.policy_timeout):
            return await self.policy.propose(inputs)

    def wake(self) -> None:
        self._wake.set()

    def observe(self, update: SceneUpdate) -> None:
        if (
            update.state.stability is Stability.STABLE
            and update.state.verdict is Verdict.SCREEN
            and (screen := update.state.screen) is not None
        ):
            ending = self._progress.ending
            if update.screen_state.classification.screen_type is ScreenType.BAD_ENDING:
                if ending is None or ending[0] != screen:
                    self._progress.ending = (screen, self._operator_goal.revision)
            else:
                self._progress.ending = None
        self.wake()

    def on_session(self, session: messages.Session) -> None:
        if self._session == session:
            return
        old = self._session
        self._session = session
        session_id = self.operator.session_id
        previous_revision = self._operator_goal.revision
        if session_id is not None:
            self._operator_goal.update(
                session_id,
                session.goal_revision,
                session.goal,
                plan=None if self._semantic is None else self._semantic.planning_goal,
            )
        if old is None or self._operator_goal.revision != previous_revision:
            if not self._planner_binding.used:
                self.set_goal(self._operator_goal.goal)
            else:
                self.interrupt()
        else:
            # 暂停时保留仍可核对的决策点。之前未提交的动作须重新规划。
            self.interrupt(keep_decision=session.state is not SessionState.ENDED)
        if not self._progress.choice_held(self._operator_goal):
            self._progress.unknown_choice_revision = None

    def set_goal(self, goal: Goal | None, *, notes: str = "", keep_decision: bool = False) -> None:
        self._progress.select_goal(goal)
        self._notes = notes
        self._rejects = 0
        self.interrupt(keep_decision=keep_decision)

    def on_resolved(
        self,
        resolved: messages.DecisionResolved,
        *,
        source: ResolutionSource | None = None,
        traceparent: str | None = None,
    ) -> bool:
        pending = self._pending
        if pending is None or self._choice_submitting:
            return False
        if source is not None and not self.decision_available(resolved.id):
            return False
        accepted = pending.accept(
            resolved,
            self.clock.now_ns(),
            _ns(self.config.decision_margin),
            source=source,
            traceparent=traceparent,
        )
        if accepted:
            self.wake()
        return accepted

    def decision_available(self, decision_id: str) -> bool:
        pending, latest = self._pending, self.scene.latest
        return (
            pending is not None
            and pending.id == decision_id
            and pending.selected is None
            and not self._choice_submitting
            and self._allowed()
            and latest is not None
            and latest.state.screen == pending.evidence.screen
            and self._fresh(latest)
        )

    def on_release(self, reason: models.ReleaseReason) -> None:
        self.interrupt(keep_decision=reason is models.ReleaseReason.LEASE_REVOKED)
        if reason is models.ReleaseReason.FOCUS_LOST:
            current = self.scene.latest
            if current is not None:
                self._last_proposed = current.observation_scope

    def interrupt(self, *, keep_decision: bool = False) -> None:
        self._revision += 1
        self._plan.clear()
        self._last_proposed = None
        if not keep_decision and self._pending is not None:
            self._close_decision(
                "unknown" if self._choice_submitting else "withdrawn",
                consume=self._choice_submitting,
            )
        if self._work is not None and self._work is not asyncio.current_task():
            self._work.cancel()
        self.wake()

    def stop(self) -> None:
        self._closed = True
        self.interrupt()

    async def run(self) -> None:
        while not self._closed:
            self._wake.clear()
            self._work = asyncio.create_task(self._tick(self._revision))
            try:
                async with asyncio.timeout(
                    self.config.policy_timeout
                    + self.config.receipt_timeout
                    + self.config.effect_timeout
                ):
                    await self._work
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
            except (ReadingWaitError, SemanticPauseError) as wait:
                self._plan.clear()
                self._last_proposed = None
                self._record(StepStatus.WAITING, str(wait), self.scene.latest)
            except ActionRefusedError as error:
                self._plan.clear()
                latest = self.scene.latest
                if latest is not None:
                    self._reject(str(error), latest)
            except Exception:
                logger.exception("逐步控制候选处理失败")
                self._plan.clear()
                if self._pending is not None:
                    self._close_decision("withdrawn")
                self._record(StepStatus.REJECTED, "dependency-failed", self.scene.latest)
            finally:
                self._work = None
            if not self._closed:
                # 无帧时也处理会话截止、选择余量线与核验超时。
                with suppress(TimeoutError):
                    async with asyncio.timeout(0.05):
                        await self._wake.wait()

    def _record(
        self,
        status: StepStatus,
        detail: str,
        update: SceneUpdate | None = None,
        category: Category | None = None,
        started_ns: int | None = None,
    ) -> None:
        elapsed = None if started_ns is None else self.clock.now_ns() - started_ns
        frame = None if update is None else update.frame.frame_number
        item = StepResult(status, detail, frame, category, elapsed)
        if status is StepStatus.WAITING and self._recent:
            prior = self._recent[-1]
            if prior.status is status and prior.detail == detail:
                return
        self._recent.append(item)
        logger.info(
            "逐步控制 %s %s frame=%s category=%s",
            status,
            detail,
            frame,
            category,
            extra={"step_result": item},
        )
        if status is StepStatus.REJECTED:
            if self._semantic is not None:
                self._semantic.rejected(detail)
            self._goal_feedback("rejected", detail)

    def _planner_event(self, event: DecisionEvent | GoalEvent | ObservationReceipt) -> None:
        try:
            planner = self._planner()
            if planner is None or not self._planner_binding.permits(planner):
                return
            match event:
                case DecisionEvent():
                    planner.on_decision(event)
                case GoalEvent():
                    planner.on_goal(event)
                case ObservationReceipt():
                    planner.on_observation(event)
        except Exception:
            logger.exception("逐步控制回执未交给 planner")

    def _accept_observation(
        self, update: SceneUpdate, observation: ScreenObservation, text: str | None
    ) -> bool:
        if not self.scene.observe(update, observation):
            return False
        if text is not None:
            frame = update.frame
            self._planner_event(
                ObservationReceipt(
                    frame.frame_number, frame.transform_version, frame.capture_ns, text, observation
                )
            )
        return True

    def _goal_feedback(
        self, kind: Literal["completed", "step-limit", "timeout", "rejected"], detail: str = ""
    ) -> None:
        if self._progress.goal is not None:
            self._planner_event(
                GoalEvent(self._progress.goal.generation, kind, self._progress.steps, detail)
            )

    def _complete_goal(self, update: SceneUpdate, detail: str = "") -> None:
        self._plan.clear()
        self._progress.goal_done = True
        self._record(StepStatus.WAITING, "goal-complete", update)
        self._goal_feedback("completed", detail)

    def _step_limit(self, update: SceneUpdate) -> None:
        self._plan.clear()
        self._progress.stuck = True
        self._record(StepStatus.STUCK, "goal-step-limit", update)
        self._goal_feedback("step-limit")

    def _complete_judged_goal(
        self,
        judgment: GoalJudgment,
        inputs: PolicyInput,
        current: SceneUpdate,
        original_session: tuple[str | None, messages.Session | None],
        revision: int,
    ) -> bool:
        goal, frame = inputs.goal, inputs.scene.frame
        if (
            self._progress.goal_done
            or goal is None
            or not goal.completion_by_agent
            or goal != self._progress.goal
            or original_session != (self.operator.session_id, self.operator.session)
            or (judgment.generation, judgment.frame_number, judgment.transform_version)
            != (goal.generation, frame.frame_number, frame.transform_version)
            or not self._fresh(current)
            or current.state.playing
            or current.state.stability is not Stability.STABLE
        ):
            self._record(StepStatus.WAITING, "goal-judgment-discarded", current)
            return False
        self._require_authority(revision)
        self._complete_goal(current, judgment.evidence.strip())
        return True

    def _sync_planner(self) -> None:
        planner = self._planner()
        binding = self._planner_binding
        if planner is None:
            if binding.used:
                self._hold_for_planner("planner-unavailable")
            return

        replacing = binding.used and not binding.permits(planner)
        if replacing:
            self._hold_for_planner("planner-replaced")
            if not binding.can_recover(self._operator_goal):
                return
        self._operator_goal.forward(planner)
        context = planner.current()
        if replacing or not binding.used:
            binding.bind(planner)
        if context.paused and not self._planner_paused:
            self.interrupt(keep_decision=True)
        pending = self._pending
        keep_decision = self._waiting_for(pending) or (
            self._planner_planning
            and not context.paused
            and pending is not None
            and pending.deadline_ns is not None
        )
        self._planner_paused = context.paused
        self._planner_planning = (
            context.paused and context.planning and self._operator_goal.goal is not None
        )
        self._pause_reason = context.pause_reason
        if context.goal != self._progress.goal:
            self.set_goal(context.goal, notes=context.notes, keep_decision=keep_decision)
        else:
            self._notes = context.notes

    def _hold_for_planner(self, reason: str) -> None:
        self._planner_binding.mark_interrupted(self._operator_goal.revision)
        if not self._planner_paused or self._pause_reason != reason:
            self.interrupt()
        self._planner_paused = True
        self._planner_planning = False
        self._pause_reason = reason

    def _allowed(self, *, verifying: bool = False) -> bool:
        session = self.operator.session
        return (
            not self._closed
            and self.grant.is_open
            and (
                self._semantic.may_verify
                if verifying and self._semantic is not None
                else self.operator.may_autoplay
            )
            and session is not None
            and (session.ends_at_ns is None or self.clock.now_ns() < session.ends_at_ns)
        )

    def _waiting_for(self, decision: PendingDecision | None) -> bool:
        return (
            self._semantic is not None
            and decision is not None
            and self._semantic.waiting_for(decision.id)
        )

    def _pacing_wait(self) -> None:
        if self._semantic is not None and self._semantic.progress_held():
            raise SemanticPauseError("pacing-held")

    def _fresh(self, update: SceneUpdate) -> bool:
        age = max(0, self.clock.now_ns() - update.frame.capture_ns)
        return age <= _ns(self.config.max_frame_age)

    def _waiting_time(self) -> bool:
        interval = max(
            1 / self.adapter.whitelist.max_rate, self._reading.params.min_action_interval
        )
        return (
            self._last_action_ns is not None
            and self.clock.now_ns() - self._last_action_ns < _ns(interval)
        )

    def _dialogue_box(self, update: SceneUpdate) -> DialogueBox | None:
        if update.screen_state.classification.screen_type is not ScreenType.DIALOGUE:
            return None
        screen = update.state.screen
        explicit = next((box for box in self.adapter.dialogue_boxes if box.screen == screen), None)
        return explicit or next(
            (box for box in self.adapter.dialogue_boxes if box.screen is None), None
        )

    async def _dialogue_read(self, update: SceneUpdate, box: DialogueBox) -> OcrResult:
        if self.adapter.rois[box.text].rect is None:
            raise ReadingWaitError("dialogue-roi-unavailable")
        read = await self.ocr.recognize(update.frame, box.text)
        verify_ocr(update, read)
        return read

    def _dialogue_ready(
        self, update: SceneUpdate, box: DialogueBox, read: OcrResult
    ) -> NarrativeRecord:
        record = self._reading.require_ready(update, box, self.clock.now_ns())
        if text_key(read.text) != text_key(record.text):
            raise ReadingWaitError("dialogue-text-changed")
        return record

    async def _advance_dialogue(
        self, update: SceneUpdate, box: DialogueBox, initial: NarrativeRecord, revision: int
    ) -> None:
        if (initial.session, initial.occurrence) == self._progress.fast_dialogue_line:
            raise ReadingWaitError("dialogue-awaiting-next-line")
        read = await self._dialogue_read(update, box)
        here = self.scene.with_ocr(update, (read,))
        if here is None:
            raise ActionRefusedError("scene-changed")
        record = self._dialogue_ready(here, box, read)
        if line_revision(initial) != line_revision(record):
            raise ReadingWaitError("dialogue-text-changed")
        lines = here.screen_state.text_targets
        counts = Counter(text_key(line.text) for line in lines)
        line = next(
            (line for line in lines if line.text.strip() and counts[text_key(line.text)] == 1), None
        )
        if line is None:
            raise ReadingWaitError("dialogue-target-ambiguous")
        proposal = StepProposal(
            Category.ADVANCE,
            Click(TextTarget(text=line.text, roi=box.text)),
            ExpectedEffect(EffectKind.TEXT_DISAPPEARED, text=line.text, roi=box.text),
        )
        rect = line.box
        found = Located(
            ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2), line, (read,)
        )
        speaker = "" if record.speaker is None else f"{record.speaker}："
        observation = ScreenObservation(screen_type=ScreenType.DIALOGUE, confidence=1.0)
        if not self._accept_observation(here, observation, f"{speaker}{record.text}"[:600]):
            raise ActionRefusedError("scene-changed")
        await self._issue_with_retry(here, proposal, found, revision, fast_dialogue=record)

    def _restart_permitted(self, update: SceneUpdate) -> bool:
        ending = self._progress.ending
        revision = self._operator_goal.revision
        if (
            ending is None
            or update.state.screen != ending[0]
            or ending[0] not in self.adapter.restarts
            or revision <= ending[1]
            or revision == self._progress.restart_attempt_revision
            or self._operator_goal.goal is None
            or update.state.stability is not Stability.STABLE
        ):
            return False
        planner = self._planner()
        if planner is None or not self._planner_binding.permits(planner):
            return False
        context = planner.current()
        return context.restarting and not context.paused and context.goal is not None

    async def _restart(self, update: SceneUpdate, revision: int) -> None:
        screen = update.state.screen
        if screen is None:
            return
        target = self.adapter.restarts[screen]
        proposal = StepProposal(
            Category.NAVIGATE,
            Click(TextTarget(text=target.text, roi=target.roi)),
            ExpectedEffect(EffectKind.SIGNATURE, signature=target.success_signature),
        )
        found = await self._locate(update, proposal)
        await self._issue(update, proposal, found, revision, restart=True)

    async def _tick(self, revision: int) -> None:
        self._sync_planner()
        if revision != self._revision:
            return
        pending = self._pending
        now = self.clock.now_ns()
        if pending is not None and pending.deadline_ns is not None and now >= pending.deadline_ns:
            self._close_decision("withdrawn")
            self._record(StepStatus.REJECTED, "decision-expired")
            return
        if self._progress.choice_held(self._operator_goal):
            self._record(StepStatus.WAITING, "choice-awaiting-authorization")
            return
        waiting_vote = self._waiting_for(pending)
        if self._planner_paused and not self._planner_planning and not waiting_vote:
            self._record(StepStatus.WAITING, self._pause_reason or "planner-paused")
            return
        if not self._allowed():
            self._record(StepStatus.WAITING, "operator-permission")
            return
        update = self.scene.latest
        if update is None or not self._fresh(update):
            detail = "no-frame" if update is None else "stale-frame"
            self._record(StepStatus.WAITING, detail)
            return
        if not self._core_ready():
            self._record(StepStatus.WAITING, "core-lease")
            return
        if update.state.playing:
            since = update.state.playing_since_ns
            if since is not None and now - since >= _ns(self.config.cutscene_timeout):
                self._progress.stuck = True
                self._record(StepStatus.STUCK, "cutscene-timeout", update)
            self._plan.clear()
            return
        if self._language_failed or (
            not waiting_vote and (self._progress.stuck or self._progress.goal_done)
        ):
            return
        goal = self._progress.goal
        if (
            not waiting_vote
            and goal is not None
            and goal.deadline_ns is not None
            and now >= goal.deadline_ns
        ):
            self._plan.clear()
            self._progress.stuck = True
            self._record(StepStatus.STUCK, "goal-timeout", update)
            self._goal_feedback("timeout")
            return
        if update.state.verdict is Verdict.SCREEN and any(
            choice[0] != update.state.screen for choice in self._progress.closed_choices
        ):
            self._progress.closed_choices.clear()
        timed = (
            update.screen_state.classification.screen_type is ScreenType.TIMED_CHOICE
            and update.screen_state.classification.source is ScreenSource.SIGNATURE
        )
        if update.state.stability is not Stability.STABLE and (
            not timed or update.state.stability is Stability.TRANSITION
        ):
            if (
                pending is not None
                and update.state.verdict is Verdict.SCREEN
                and update.state.screen != pending.evidence.screen
            ):
                self._close_decision("withdrawn")
            return
        if pending is not None:
            await self._advance_decision(update, pending, revision)
            return
        if self._planner_planning:
            if timed and update.observation_scope is not self._last_proposed:
                self._last_proposed = update.observation_scope
                await self._open_from_scene(update, revision)
            return
        if self._waiting_time():
            return
        if not await self._check_language(update):
            return
        if (
            goal is not None
            and goal.completion is not None
            and await self._matches(goal.completion, update)
        ):
            self._require_authority(revision)
            latest = self.scene.latest
            if (
                latest is not None
                and latest.observation_scope is update.observation_scope
                and self._fresh(latest)
            ):
                self._complete_goal(latest)
            return
        at_limit = (
            goal is not None
            and goal.max_steps is not None
            and self._progress.steps >= goal.max_steps
        )
        if at_limit:
            self._plan.clear()
            if (
                goal is None
                or not goal.completion_by_agent
                or requires_choice(update)
                or update.screen_state.restrictions & STOPPED
                or update.observation_scope is self._last_proposed
            ):
                self._step_limit(update)
                return
        if update.screen_state.restrictions & STOPPED:
            self._plan.clear()
            if self._restart_permitted(update):
                self._pacing_wait()
                if update.observation_scope is not self._last_proposed:
                    self._last_proposed = update.observation_scope
                    await self._restart(update, revision)
            else:
                self._record(StepStatus.WAITING, "screen-blocked", update)
            return
        if requires_choice(update):
            self._plan.clear()
            if update.observation_scope is self._last_proposed:
                return
            self._last_proposed = update.observation_scope
            await self._open_from_scene(update, revision)
            return
        self._pacing_wait()
        box = self._dialogue_box(update)
        if box is not None and not at_limit:
            record = self._reading.require_ready(update, box, self.clock.now_ns())
            if (
                self.config.fast_paths
                and box.advance == "click-text"
                and update.screen_state.classification.source is ScreenSource.SIGNATURE
                and (goal is None or not goal.completion_by_agent)
            ):
                self._plan.clear()
                await self._advance_dialogue(update, box, record, revision)
                return
        if self._plan and update.observation_scope is not self._verified_scope:
            self._plan.clear()
        continuing = bool(self._plan)
        if not self._plan:
            if update.observation_scope is self._last_proposed:
                return
            self._last_proposed = update.observation_scope
            context = await self._read_policy_context(update)
            inputs = self._inputs(context)
            original_session = (self.operator.session_id, self.operator.session)
            plan = await self.propose(inputs)
            if plan.observation is not None and not self._accept_observation(
                update, plan.observation, plan.observation_text
            ):
                self._reject("scene-changed", update)
                return
            latest = self.scene.latest
            if latest is None or latest.observation_scope is not update.observation_scope:
                self._reject("scene-changed", update)
                return
            update = latest
            judgment = plan.goal_judgment
            if judgment is not None and judgment.completed:
                completed = self._complete_judged_goal(
                    judgment, inputs, update, original_session, revision
                )
                if at_limit and not completed:
                    self._step_limit(update)
                return
            if at_limit:
                self._step_limit(update)
                return
            if plan.report:
                self._record(StepStatus.WAITING, plan.report[:200], update)
            if plan.open_decision:
                await self._open_from_scene(update, revision)
                return
            self._plan.extend(plan.steps)
        if self._plan:
            proposal = self._plan.popleft()
            if continuing and not isinstance(proposal.action, Press):
                target = proposal.action.target
                if target.point is not None:
                    if target.roi is None or target.text is None:
                        self._plan.clear()
                        self._last_proposed = None
                        self.wake()
                        return
                    proposal = replace(
                        proposal,
                        action=replace(proposal.action, target=replace(target, point=None)),
                    )
            await self._ordinary(update, proposal, revision)

    def _inputs(self, update: SceneUpdate, decision: DecisionView | None = None) -> PolicyInput:
        return PolicyInput(
            update,
            update.screen_state.text_targets,
            self._progress.goal,
            self._notes,
            self.recent,
            decision,
        )

    async def _read_policy_context(self, update: SceneUpdate) -> SceneUpdate:
        screen = update.state.screen
        rois = [
            item.roi
            for signature in self.adapter.signatures
            if signature.id in update.state.matched
            for item in signature.texts
        ]
        if update.screen_state.classification.screen_type is ScreenType.DIALOGUE:
            rois.extend(
                box.text for box in self.adapter.dialogue_boxes if box.screen in (None, screen)
            )
        reads: list[OcrResult] = []
        for name in dict.fromkeys(rois):
            if self.adapter.rois[name].rect is not None:
                result = await self.ocr.recognize(update.frame, name)
                verify_ocr(update, result)
                reads.append(result)
        current = self.scene.with_ocr(update, reads)
        if current is None:
            raise ActionRefusedError("scene-changed")
        return current

    async def _check_language(self, update: SceneUpdate) -> bool:
        screen = update.state.screen
        if screen is None or screen == self._language_screen:
            return True
        if any(item.screen == screen for item in self.adapter.language.fixed_texts):
            result = await self.ocr.check_language(update.frame, screen)
            if self.scene.with_ocr(update, ()) is None:
                self._record(StepStatus.WAITING, "language-frame-changed", update)
                return False
            if not result.passed:
                self._language_failed = True
                self._record(StepStatus.REJECTED, "language-mismatch", update)
                return False
        self._language_screen = screen
        return True

    def _reject(self, why: str, update: SceneUpdate, category: Category | None = None) -> None:
        self._record(StepStatus.REJECTED, why, update, category)
        if why in {"scene-changed", "stale-frame", "operator-permission"}:
            return
        screen = (update.state.screen, update.screen_state.classification.screen_type)
        self._rejects = self._rejects + 1 if self._rejected_screen == screen else 1
        self._rejected_screen = screen
        if self._rejects >= self.config.max_rejections:
            self._progress.stuck = True
            self._record(StepStatus.STUCK, "repeated-rejections", update, category)

    async def _locate(self, update: SceneUpdate, proposal: StepProposal) -> Located:
        if isinstance(proposal.action, Press):
            return Located()
        return await locate(
            self.scene, self.ocr, update, proposal.action.target, radius=self.config.snap_radius
        )

    async def _ordinary(self, update: SceneUpdate, proposal: StepProposal, revision: int) -> None:
        try:
            found = await self._locate(update, proposal)
            here = self._current(update, found, revision)
            if assess(self.adapter, here, proposal, found):
                self._plan.clear()
                if requires_choice(here):
                    await self._open_from_scene(here, revision)
                elif found.line is not None and found.reads and here.state.screen is not None:
                    await self._open(
                        here,
                        DecisionEvidence(
                            here.state.screen, found.line.text, (found.line,), None, proposal
                        ),
                        revision,
                    )
                else:
                    raise ActionRefusedError("decision-evidence-missing")
                return
            status = await self._issue_with_retry(here, proposal, found, revision)
            if status is not StepStatus.VERIFIED or proposal.expected.kind in {
                EffectKind.CHANGED,
                EffectKind.UNCHANGED,
            }:
                self._plan.clear()
            else:
                self._rejects = 0
                self.wake()
        except ActionRefusedError as error:
            self._plan.clear()
            self._reject(str(error), update, proposal.category)

    async def _issue_with_retry(
        self,
        here: SceneUpdate,
        proposal: StepProposal,
        found: Located,
        revision: int,
        *,
        fast_dialogue: NarrativeRecord | None = None,
    ) -> StepStatus:
        status = await self._issue(here, proposal, found, revision, fast_dialogue=fast_dialogue)
        if status is not StepStatus.NO_EFFECT or found.line is None:
            return status
        fresh = await self._retry_frame(here, revision)
        if fresh is None:
            return status
        retry = await self._locate(fresh, proposal)
        retry_frame = self._current(fresh, retry, revision)
        if assess(self.adapter, retry_frame, proposal, retry):
            return status
        return await self._issue(
            retry_frame, proposal, retry, revision, fast_dialogue=fast_dialogue
        )

    def _require_authority(self, revision: int, *, decision: PendingDecision | None = None) -> None:
        if revision != self._revision or not self._allowed():
            raise ActionRefusedError("operator-permission")
        if self._progress.choice_held(self._operator_goal):
            raise ActionRefusedError("choice-awaiting-authorization")
        if not self._core_ready():
            raise ActionRefusedError("core-lease")
        planner = self._planner()
        if not self._planner_binding.permits(planner):
            why = "planner-unavailable" if planner is None else "planner-replaced"
            self._hold_for_planner(why)
            raise ActionRefusedError(why)
        waiting_vote = self._waiting_for(decision)
        if planner is not None and not waiting_vote:
            context = planner.current()
            timed_planning = (
                self._semantic is not None
                and decision is not None
                and decision.deadline_ns is not None
                and context.planning
            )
            if context.paused and not timed_planning:
                raise ActionRefusedError("planner-paused")
            if context.goal != self._progress.goal:
                raise ActionRefusedError("goal-changed")
        if (
            not waiting_vote
            and self._progress.goal is not None
            and self._progress.goal.deadline_ns is not None
            and self.clock.now_ns() >= self._progress.goal.deadline_ns
        ):
            raise ActionRefusedError("goal-timeout")

    def _current(
        self,
        update: SceneUpdate,
        found: Located,
        revision: int,
        *,
        decision: PendingDecision | None = None,
    ) -> SceneUpdate:
        self._require_authority(revision, decision=decision)
        if (
            self._progress.goal is not None
            and self._progress.goal.max_steps is not None
            and self._progress.steps >= self._progress.goal.max_steps
        ):
            raise ActionRefusedError("goal-step-limit")
        current = self.scene.with_ocr(update, found.reads)
        if current is None:
            raise ActionRefusedError("scene-changed")
        if not self._fresh(current):
            raise ActionRefusedError("stale-frame")
        if found.line is not None and found.line not in current.screen_state.text_targets:
            raise ActionRefusedError("stale-ocr")
        return current

    async def _retry_frame(self, base: SceneUpdate, revision: int) -> SceneUpdate | None:
        end = self.clock.now_ns() + _ns(self.config.effect_timeout)
        while self._allowed() and revision == self._revision and self.clock.now_ns() < end:
            current = self.scene.latest
            if current is not None and current.observation_scope is not base.observation_scope:
                return None
            if (
                current is not None
                and current.frame.frame_number > base.frame.frame_number
                and current.state.stability is Stability.STABLE
                and self._fresh(current)
                and not self._waiting_time()
            ):
                return current
            await self.clock.sleep_until(min(end, self.clock.now_ns() + _ns(0.025)))
        return None

    async def _read_timer(self, update: SceneUpdate, timer: DecisionTimer) -> tuple[OcrResult, int]:
        timing = await self.ocr.recognize(update.frame, timer.roi)
        verify_ocr(update, timing)
        deadline = timer_deadline(timer, timing.text, update.frame.capture_ns)
        if deadline is None:
            raise ActionRefusedError("invalid-decision-timer")
        return timing, deadline

    async def _scene_evidence(
        self, update: SceneUpdate
    ) -> tuple[SceneUpdate, DecisionEvidence, tuple[OcrResult, ...]]:
        screen = update.state.screen
        declared = None if screen is None else self.adapter.decisions.get(screen)
        if screen is None or declared is None:
            raise ActionRefusedError("decision-calibration-unavailable")
        options = await self.ocr.recognize(update.frame, declared.options)
        reads = [options]
        prompt = "请选择"
        if declared.prompt is not None:
            read = await self.ocr.recognize(update.frame, declared.prompt)
            reads.append(read)
            prompt = read.text.strip()[:2000] or prompt
        deadline = None
        if declared.timer is not None:
            timing, deadline = await self._read_timer(update, declared.timer)
            reads.append(timing)
        for read in reads:
            verify_ocr(update, read)
        here = self.scene.with_ocr(update, reads)
        if here is None:
            raise ActionRefusedError("scene-changed")
        lines = tuple(
            line
            for line in options.lines
            if line in here.screen_state.text_targets
            and not forbidden_text(self.adapter, line.text)
        )
        if (
            not lines
            or any(len(line.text) > 2000 for line in lines)
            or len({text_key(line.text) for line in lines}) != len(lines)
        ):
            raise ActionRefusedError("invalid-decision-options")
        return here, DecisionEvidence(screen, prompt, lines, deadline), tuple(reads)

    async def _open_from_scene(self, update: SceneUpdate, revision: int) -> None:
        try:
            here, evidence, _ = await self._scene_evidence(update)
            await self._open(here, evidence, revision)
        except ActionRefusedError as error:
            self._reject(str(error), update)

    def _can_preview(self, update: SceneUpdate, evidence: DecisionEvidence, revision: int) -> bool:
        if (
            update.state.screen != evidence.screen
            or update.state.playing
            or not self._fresh(update)
            or revision != self._revision
            or not self._allowed()
            or not self._core_ready()
            or self._progress.choice_held(self._operator_goal)
        ):
            return False
        planner = self._planner()
        if not self._planner_binding.permits(planner):
            return False
        if planner is None:
            return True
        context = planner.current()
        return not context.paused or (
            context.planning
            and self._operator_goal.goal is not None
            and evidence.deadline_ns is not None
            and update.screen_state.classification.source is ScreenSource.SIGNATURE
            and update.screen_state.classification.screen_type is ScreenType.TIMED_CHOICE
        )

    async def _open(self, update: SceneUpdate, evidence: DecisionEvidence, revision: int) -> None:
        if evidence.identity in self._progress.closed_choices or self._pending is not None:
            return
        if not self._can_preview(update, evidence, revision):
            return
        self._decision_count += 1
        identity = f"{self._decision_prefix}-{self._decision_count}"
        session = self.operator.session_id
        if session is None:
            raise ActionRefusedError("operator-permission")
        view = evidence.view
        self._planner_event(
            DecisionEvent(session, identity, "preview", view, update.frame.frame_number)
        )
        planner = self._planner()
        ranking = (
            ()
            if planner is None
            else self._validated_rank(planner.candidates(session, identity), evidence)
        )
        if evidence.deadline_ns is not None:
            budget = evidence.deadline_ns - _ns(self.config.decision_margin) - self.clock.now_ns()
            if planner is None and budget > 0:
                with suppress(TimeoutError):
                    async with asyncio.timeout(budget / 1e9):
                        rank = await self.propose(self._inputs(update, evidence.view))
                    ranking = self._validated_rank(rank.ranked_options, evidence)
        # 排名可以晚于同一倒计时内的下一帧。执行之前仍按新帧重新定位。
        latest = self.scene.latest
        if latest is None or not self._can_preview(latest, evidence, revision):
            self._planner_event(
                DecisionEvent(session, identity, "withdrawn", view, update.frame.frame_number)
            )
            raise ActionRefusedError("scene-changed")
        if evidence.deadline_ns is not None and self.clock.now_ns() >= evidence.deadline_ns:
            self._progress.closed_choices.add(evidence.identity)
            self._planner_event(
                DecisionEvent(session, identity, "withdrawn", view, update.frame.frame_number)
            )
            return
        decision = messages.DecisionOpened(
            id=identity,
            prompt=evidence.prompt,
            options=evidence.view.options,
            candidates=ranking,
            deadline_ns=evidence.deadline_ns,
        )
        self.operator.open_decision(decision)
        self._pending = PendingDecision(
            decision.id, evidence, ranking, evidence.deadline_ns, update.frame.frame_number
        )
        self._planner_event(
            DecisionEvent(session, identity, "opened", view, update.frame.frame_number)
        )
        self.wake()

    @staticmethod
    def _validated_rank(ranking: tuple[int, ...], evidence: DecisionEvidence) -> tuple[int, ...]:
        if all(type(i) is int and 0 <= i < len(evidence.lines) for i in ranking) and (
            len(set(ranking)) == len(ranking)
        ):
            return ranking
        return ()

    def _publish_decision_update(
        self, pending: PendingDecision, update: SceneUpdate, old_deadline: int | None
    ) -> None:
        planner = self._planner()
        session = self.operator.session_id
        previous = pending.candidates
        shortened = pending.deadline_ns != old_deadline
        if planner is not None and session is not None and self._planner_binding.permits(planner):
            if shortened:
                self._planner_event(
                    DecisionEvent(
                        session, pending.id, "opened", pending.view, update.frame.frame_number
                    )
                )
            pending.candidates = self._validated_rank(
                planner.candidates(session, pending.id), pending.evidence
            )
        if shortened or pending.candidates != previous:
            self.operator.update_decision(
                messages.DecisionUpdated(
                    id=pending.id, candidates=pending.candidates, deadline_ns=pending.deadline_ns
                )
            )

    async def _refresh_decision_timer(self, update: SceneUpdate, pending: PendingDecision) -> None:
        if pending.deadline_ns is None or update.frame.frame_number <= max(
            pending.opened_frame, pending.last_timing_frame
        ):
            return
        timer = self.adapter.decisions[pending.evidence.screen].timer
        if timer is None:
            raise ActionRefusedError("invalid-decision-timer")
        timing, deadline = await self._read_timer(update, timer)
        if self.scene.with_ocr(update, (timing,)) is None:
            raise ActionRefusedError("scene-changed")
        pending.refresh_deadline(deadline)
        pending.last_timing_frame = update.frame.frame_number

    async def _advance_decision(
        self, update: SceneUpdate, pending: PendingDecision, revision: int
    ) -> None:
        if update.state.screen != pending.evidence.screen:
            self._close_decision("withdrawn")
            return
        margin = _ns(self.config.decision_margin)
        try:
            old_deadline = pending.deadline_ns
            await self._refresh_decision_timer(update, pending)
            self._publish_decision_update(pending, update, old_deadline)
            now = self.clock.now_ns()
            if pending.deadline_ns is not None and now >= pending.deadline_ns:
                self._close_decision("withdrawn")
                return
            if self._planner_paused and not (
                self._waiting_for(pending)
                or (
                    self._semantic is not None
                    and self._planner_planning
                    and pending.deadline_ns is not None
                )
            ):
                return
            choice = pending.choose(now, margin)
            if choice is None:
                if pending.deadline_ns is not None and now >= pending.deadline_ns - margin:
                    self._record(StepStatus.REJECTED, "decision-no-verified-candidate", update)
                    self._close_decision("withdrawn")
                return
            if update.frame.frame_number <= pending.opened_frame or self._waiting_time():
                return
            index, by = choice
            if pending.evidence.proposed is None:
                here, evidence, reads = await self._scene_evidence(update)
                if evidence.identity != pending.evidence.identity:
                    raise ActionRefusedError("decision-instance-changed")
                old_deadline = pending.deadline_ns
                pending.refresh_deadline(evidence.deadline_ns)
                self._publish_decision_update(pending, here, old_deadline)
                refreshed = pending.choose(self.clock.now_ns(), margin)
                if refreshed is None:
                    raise ActionRefusedError("decision-no-verified-candidate")
                index, by = refreshed
                line = evidence.lines[index]
                box = line.box
                found = Located(
                    ((box.left + box.right) // 2, (box.top + box.bottom) // 2), line, reads
                )
                proposal = StepProposal(
                    Category.SELECT,
                    Click(
                        TextTarget(
                            text=line.text, roi=self.adapter.decisions[evidence.screen].options
                        )
                    ),
                    ExpectedEffect(EffectKind.CHANGED),
                )
            else:
                proposal = pending.evidence.proposed
                found = await self._locate(update, proposal)
                here = update
            source = pending.selected_source if by == "operator" else "default"
            choice_at_submission = SubmittedChoice(
                pending.id,
                index,
                source,
                pending.selected_traceparent if by == "operator" else None,
            )
            status = await self._issue(
                here, proposal, found, revision, decision=pending, choice=choice_at_submission
            )
            if status is StepStatus.VERIFIED:
                self._close_decision(by, index)
            elif status is StepStatus.REJECTED:
                self._close_decision("withdrawn")
            else:
                self._close_decision("unknown")
        except ActionRefusedError as error:
            if str(error) not in {
                "scene-changed",
                "planner-paused",
                "goal-changed",
                "operator-permission",
                "core-lease",
            }:
                self._reject(str(error), update, Category.SELECT)
                self._close_decision("withdrawn")
        finally:
            self._choice_submitting = False

    def _close_decision(
        self,
        by: Literal["operator", "default", "withdrawn", "unknown"],
        index: int | None = None,
        *,
        consume: bool = True,
    ) -> None:
        pending, self._pending = self._pending, None
        if pending is None:
            return
        if by == "unknown":
            self._progress.unknown_choice_revision = self._operator_goal.revision
            if self._semantic is not None:
                self._progress.closed_choices.add(pending.evidence.identity)
        elif consume:
            self._progress.closed_choices.add(pending.evidence.identity)
        with suppress(ChannelError):
            self.operator.close_decision(
                messages.DecisionClosed(
                    id=pending.id, index=index, by=by, executed_at_ns=pending.executed_at_ns
                )
            )
        session = self.operator.session_id
        if session is not None:
            confirmed = by if by in ("operator", "default") else None
            latest = self.scene.latest
            frame = pending.opened_frame if latest is None else latest.frame.frame_number
            self._planner_event(
                DecisionEvent(
                    session,
                    pending.id,
                    "confirmed" if by in ("operator", "default") else by,
                    pending.view,
                    frame,
                    index,
                    confirmed,
                    pending.executed_at_ns,
                    pending.selected_source if by == "operator" else None,
                )
            )

    async def _issue(
        self,
        update: SceneUpdate,
        proposal: StepProposal,
        found: Located,
        revision: int,
        *,
        decision: PendingDecision | None = None,
        choice: SubmittedChoice | None = None,
        restart: bool = False,
        fast_dialogue: NarrativeRecord | None = None,
    ) -> StepStatus:
        auto_choice = decision is not None and decision.evidence.proposed is None
        if (
            decision is not None
            and not auto_choice
            and proposal.expected.kind in {EffectKind.CHANGED, EffectKind.UNCHANGED}
        ):
            raise ActionRefusedError("selection-needs-specific-effect")
        if (
            not auto_choice
            and proposal.expected.kind not in {EffectKind.CHANGED, EffectKind.UNCHANGED}
            and await self._matches(proposal.expected, update)
        ):
            raise ActionRefusedError("effect-already-present")
        here = self._current(update, found, revision, decision=decision)
        box = self._dialogue_box(here)
        reading: NarrativeRecord | None = None
        if box is not None:
            read = next((item for item in found.reads if item.roi == box.text), None)
            if read is None:
                read = await self._dialogue_read(here, box)
                here = self._current(here, found, revision, decision=decision)
            reading = self._dialogue_ready(here, box, read)
        if fast_dialogue is not None and (
            reading is None or line_revision(reading) != line_revision(fast_dialogue)
        ):
            raise ReadingWaitError("dialogue-text-changed")
        if restart and not self._restart_permitted(here):
            raise ActionRefusedError("restart-not-authorized")
        if assess(
            self.adapter,
            here,
            proposal,
            found,
            authorized_decision=decision is not None,
            authorized_restart=restart,
        ):
            raise ActionRefusedError("decision-required")
        if self._waiting_time():
            raise ActionRefusedError("action-interval")
        if decision is None:
            self._pacing_wait()
        started = self.clock.now_ns()
        session = self.operator.session
        end = started + _ns(self.config.action_valid_for)
        for bound in (
            None if session is None else session.ends_at_ns,
            None if decision is None else decision.deadline_ns,
            None
            if self._progress.goal is None or self._waiting_for(decision)
            else self._progress.goal.deadline_ns,
        ):
            if bound is not None:
                end = min(end, bound)
        if end - started <= _ns(self.adapter.whitelist.press_duration):
            raise ActionRefusedError("action-window-expired")
        self._next_id += 1
        action_id = self._next_id
        action = core_action(proposal, found, action_id)
        category = Category.SELECT if decision is not None else proposal.category
        outcome = None
        if self._semantic is not None:
            outcome = self._semantic.action_started(
                f"{self._decision_prefix}-{action_id}", category, action.root.type, end, choice
            )
        self._last_action_ns = started
        self._progress.steps += 1
        previous_dialogue_line = self._progress.fast_dialogue_line
        if fast_dialogue is not None:
            self._progress.fast_dialogue_line = fast_dialogue.session, fast_dialogue.occurrence
        if restart:
            self._progress.restart_attempt_revision = self._operator_goal.revision
        self._choice_submitting = decision is not None
        status, detail = StepStatus.UNKNOWN, "submission-unknown"
        receipt: models.Executed | models.Dropped | models.Uncertain | None = None
        try:
            async with asyncio.timeout(self.config.receipt_timeout):
                sent = await self.grant.submit(
                    (models.ControlStep(offset_ns=0, held=[], actions=[action]),),
                    base_frame=here.frame.frame_number,
                    valid_for=(end - started) / 1e9,
                )
                if isinstance(sent.result, models.Rejected):
                    status, detail = StepStatus.REJECTED, sent.result.reason.value
                else:
                    if self._semantic is None:
                        with suppress(ChannelError):
                            self.operator.report_action(
                                messages.Action(category=category.value, summary=action.root.type)
                            )
                    receipt = (await sent.outcomes()).get(action_id)
                    if receipt is not None and (
                        receipt.id != action_id
                        or (
                            isinstance(receipt, models.Executed)
                            and not started <= receipt.at_ns < end
                        )
                    ):
                        receipt = None
                    if isinstance(receipt, models.Dropped):
                        detail = receipt.reason.value
                        if receipt.reason is not models.DropReason.DUPLICATE:
                            status = StepStatus.REJECTED
                    elif isinstance(receipt, models.Executed):
                        if decision is not None:
                            decision.executed_at_ns = receipt.at_ns
                        detail = "executed-effect-unknown"
                    else:
                        detail = "receipt-uncertain"
            if isinstance(receipt, models.Executed):
                status = await self._verify(
                    here, proposal.expected, receipt.at_ns, decision, revision
                )
                if status is StepStatus.VERIFIED:
                    detail = "screen-verified"
                elif status is not StepStatus.UNKNOWN:
                    detail = status.value
        except ControlRevokedError:
            status, detail = StepStatus.REJECTED, "producer-revoked"
        except ActionDeadlineError:
            status, detail = StepStatus.REJECTED, "action-window-expired"
        except (TimeoutError, ChannelError):
            pass
        except ActionRefusedError:
            detail = "verification-invalidated"
        except asyncio.CancelledError:
            status, detail = StepStatus.UNKNOWN, "interrupted-after-submit"
            self._record(status, detail, here, category, started)
            if decision is not None:
                self._close_decision("unknown")
            raise
        except Exception:
            detail = "execution-verification-failed"
            logger.exception("无法确认已提交动作的效果")
        finally:
            if outcome is not None:
                outcome(
                    status, detail, receipt.at_ns if isinstance(receipt, models.Executed) else None
                )
        if status is StepStatus.REJECTED:
            if fast_dialogue is not None:
                # 首次明确未执行可重试。若这是无效果后的重发，原提交仍保留。
                self._progress.fast_dialogue_line = previous_dialogue_line
            self._reject(detail, here, category)
        else:
            self._record(status, detail, here, category, started)
        return status

    async def _verify(
        self,
        before: SceneUpdate,
        expected: ExpectedEffect,
        executed_ns: int,
        decision: PendingDecision | None,
        revision: int,
    ) -> StepStatus:
        until = self.clock.now_ns() + _ns(self.config.effect_timeout)
        last = before.frame.frame_number
        unchanged = False
        deadline = None if decision is None else decision.deadline_ns
        if deadline is not None and executed_ns >= deadline:
            return StepStatus.UNKNOWN
        early_destination: str | None = None
        while (
            self._allowed(verifying=True)
            and revision == self._revision
            and self.clock.now_ns() < until
        ):
            after = self.scene.latest
            if (
                after is not None
                and after.frame.frame_number > last
                and executed_ns < after.frame.capture_ns < until
                and self._fresh(after)
                and not after.state.playing
            ):
                last = after.frame.frame_number
                if after.frame.transform_version != before.frame.transform_version:
                    return StepStatus.UNKNOWN
                if decision is not None and decision.evidence.proposed is None:
                    destination = (
                        after.state.screen
                        if after.state.verdict is Verdict.SCREEN
                        and after.state.screen != decision.evidence.screen
                        else None
                    )
                    before_deadline = deadline is None or after.frame.capture_ns < deadline
                    # 过了截止才稳定时，须有执行后、截止前已进入同一画面的签名证据。
                    if before_deadline or after.state.screen == decision.evidence.screen:
                        early_destination = destination
                    verified = after.state.stability is Stability.STABLE and (
                        destination is not None
                        and (before_deadline or destination == early_destination)
                    )
                else:
                    verified = False
                    if after.state.stability is Stability.STABLE:
                        unchanged = after.observation_scope is before.observation_scope
                        if expected.kind is EffectKind.CHANGED:
                            verified = (
                                before.frame.pixels != after.frame.pixels
                                and after.state.verdict is not Verdict.AMBIGUOUS
                            )
                        elif expected.kind is EffectKind.UNCHANGED:
                            verified = unchanged
                        else:
                            verified = not unchanged and await self._matches(expected, after)
                if verified and self._allowed(verifying=True) and revision == self._revision:
                    self._verified_scope = after.observation_scope
                    return StepStatus.VERIFIED
            self._wake.clear()
            with suppress(TimeoutError):
                async with asyncio.timeout(min(0.05, max(0, until - self.clock.now_ns()) / 1e9)):
                    await self._wake.wait()
        latest = self.scene.latest
        if (
            self._allowed(verifying=True)
            and revision == self._revision
            and decision is None
            and unchanged
            and latest is not None
            and self._fresh(latest)
            and latest.observation_scope is before.observation_scope
        ):
            return StepStatus.NO_EFFECT
        return StepStatus.UNKNOWN

    async def _matches(self, expected: ExpectedEffect, update: SceneUpdate) -> bool:
        match expected.kind:
            case EffectKind.SIGNATURE:
                return (
                    update.state.verdict is Verdict.SCREEN
                    and expected.signature in update.state.matched
                )
            case EffectKind.SCREEN:
                classification = update.screen_state.classification
                return (
                    classification.source is ScreenSource.SIGNATURE
                    and expected.screen_type is classification.screen_type
                )
            case EffectKind.TEXT_APPEARED | EffectKind.TEXT_DISAPPEARED:
                if expected.roi is not None and expected.text is not None:
                    result = await self.ocr.recognize(update.frame, expected.roi)
                    verify_ocr(update, result)
                    current = self.scene.with_ocr(update, (result,))
                    if current is None:
                        raise ActionRefusedError("scene-changed")
                    if current.state.verdict in (
                        Verdict.DISABLED,
                        Verdict.UNDETERMINED,
                        Verdict.AMBIGUOUS,
                    ):
                        return False
                    found = any(
                        text_key(expected.text) in text_key(line.text)
                        for line in current.screen_state.text_targets
                    )
                    return found == (expected.kind is EffectKind.TEXT_APPEARED)
            case EffectKind.CHANGED | EffectKind.UNCHANGED:
                pass
        return False
