"""局内规划与模型调度。控制回执同步接纳，模型调用由一个任务串行处理。"""

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable, Coroutine, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import StrEnum
from itertools import count, islice
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from game_runtime.adapter import Adapter, NotCalibratedError, ScreenType
from game_runtime.models import ModelCallError, ModelClient, Purpose
from game_runtime.narrative import (
    Completeness,
    Cutscene,
    NarrativeRecord,
    NarrativeService,
    compare_key,
)
from game_runtime.planner.decisions import Attempt, Candidate, Decision, DecisionHistory, Knowledge
from game_runtime.planner.keyframes import Clip, CutsceneFrames
from game_runtime.planner.output import DecisionReply, EndingReply, GoalReply, SummaryReply
from game_runtime.scene import SceneUpdate, ScreenSource, Stability
from game_runtime.step_control.contracts import (
    DecisionEvent,
    ExpectedEffect,
    Goal,
    GoalEvent,
    ObservationReceipt,
    PlannerContext,
)

logger = logging.getLogger(__name__)
_GOAL_GENERATIONS = count(1)
_S = 1_000_000_000


class PlannerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    summary_interval_s: Annotated[float, Field(gt=0, le=300)] = 45.0
    cutscene_keyframe_interval: Annotated[float, Field(gt=0, le=300)] = 2.0
    batch_size: Annotated[int, Field(ge=1, le=64)] = 24
    subgoal_timeout_s: Annotated[float, Field(gt=0, le=3600)] = 120.0
    subgoal_max_steps: Annotated[int, Field(ge=1, le=500)] = 24
    failure_limit: Annotated[int, Field(ge=1, le=20)] = 3
    rejection_limit: Annotated[int, Field(ge=1, le=20)] = 3


class Status(StrEnum):
    IDLE = "idle"
    PLANNING = "planning"
    ACTIVE = "active"
    WAITING_RESTART = "waiting-restart"
    STUCK = "stuck"
    COMPLETE = "complete"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class Situation:
    screen_type: ScreenType
    status: Status
    summary: str
    line: str | None
    observation: str | None
    rereading: bool
    narrative_available: bool
    goal: str | None
    completed_subgoals: int
    replanning: tuple[str, ...]
    ending: "BadEnding | None"
    highlights: tuple[tuple[str, str, str, int], ...]


@dataclass(frozen=True, slots=True)
class BadEnding:
    summary: str
    decision_id: str | None = None
    option: str | None = None
    source: str | None = None


@dataclass(frozen=True, slots=True)
class _Step:
    text: str
    effect: ExpectedEffect | None
    overall: bool = False


class Planner:
    def __init__(
        self,
        adapter: Adapter,
        model: ModelClient,
        config: PlannerConfig,
        *,
        narrative: Callable[[], NarrativeService | None],
        now_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        self._adapter = adapter
        self._model = model
        self._config = config
        self._narrative = narrative
        self._now_ns = now_ns
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._flight: asyncio.Task[None] | None = None
        self._kind = ""
        self._closed = False
        self._revision = 0
        self._scope = 0
        self._session: int | None = None
        self._screen_type = ScreenType.UNKNOWN
        self._screen_key: str = ""
        self._scene_scope: object | None = None
        self._latest: NarrativeRecord | None = None
        self._knowledge = Knowledge()
        self._observing: ObservationReceipt | None = None
        self._dirty: dict[int, NarrativeRecord] = {}
        self._observations: dict[int, ObservationReceipt] = {}
        self._for_store: dict[int, ObservationReceipt] = {}
        self._writer: asyncio.Task[None] | None = None
        self._seen: dict[str, int] = {}
        self._rereading = False
        self._recent: deque[NarrativeRecord] = deque(maxlen=config.batch_size)
        self._highlights: deque[tuple[str, str, str, int]] = deque(maxlen=16)
        self._highlight_keys: set[tuple[str, str]] = set()
        self._milestones: deque[tuple[str, str, int]] = deque(maxlen=16)
        self._last_keyframe: object | None = None
        self._summary_due: float | None = None
        self._clips = CutsceneFrames(
            model, round(config.cutscene_keyframe_interval * _S), config.batch_size, self._wake.set
        )
        self._decisions = DecisionHistory()
        self._ending_scope: int | None = None
        self._ending_request: tuple[Attempt | None, int] | None = None
        self._ending_applied = False
        self._ending_detail: BadEnding | None = None
        self._restart_checkpoint: Knowledge | None = None
        self._restarting = False
        self._root_goal: str | None = None
        self._submitted: Goal | None = None
        self._overall_completion: ExpectedEffect | None = None
        self._verified_steps = 0
        self._goal: Goal | None = None
        self._goal_pending = False
        self._status = Status.IDLE
        self._steps: deque[_Step] = deque()
        self._is_overall = False
        self._completed_count = 0
        self._recent_completed: deque[str] = deque(maxlen=8)
        self._rejections = 0
        self._suggestions: tuple[str, ...] = ()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        self._closed = True
        self._invalidate_model()
        self._status = Status.CLOSED
        self._goal = None
        self._goal_pending = False
        self._steps.clear()
        for task in (self._task, self._writer):
            if task is not None:
                task.cancel()
        await self._clips.close()
        await asyncio.gather(
            *(task for task in (self._task, self._writer) if task is not None),
            return_exceptions=True,
        )
        self._for_store.clear()

    def current(self) -> PlannerContext:
        if self._closed:
            return PlannerContext(None, paused=True, pause_reason=Status.CLOSED.value)
        notes = "\n".join(
            (self._knowledge.summary, *(f"{k} {v}" for k, v in self._knowledge.notes))
        )
        paused = self._status is not Status.ACTIVE
        return PlannerContext(
            self._goal,
            notes[:6000],
            paused,
            self._status.value if paused else None,
            planning=self._status is Status.PLANNING,
            restarting=self._restarting,
        )

    @property
    def situation(self) -> Situation:
        return Situation(
            self._screen_type,
            self._status,
            self._knowledge.summary,
            None if self._latest is None else self._latest.text[:600],
            None if self._observing is None else self._observing.text,
            self._rereading,
            not self._closed and self._narrative() is not None,
            self._root_goal,
            self._completed_count,
            self._suggestions,
            self._ending_detail,
            tuple(self._highlights),
        )

    def submit_goal(self, goal: Goal | None) -> None:
        """只由宿主在操作员提交或清除授权目标时调用。"""
        if self._closed:
            return
        text = None if goal is None else goal.text.strip() or None
        if text is not None and len(text) > 2000:
            raise ValueError("目标文字过长")
        if goal == self._submitted:
            return
        self._submitted = goal
        self._invalidate_model()
        self._steps.clear()
        self._goal = None
        self._rejections = 0
        self._suggestions = ()
        if text is None:
            self._restarting = False
            self._root_goal = None
            self._overall_completion = None
            self._goal_pending = False
            self._completed_count = 0
            self._recent_completed.clear()
            self._status = Status.IDLE if self._ending_scope is None else Status.WAITING_RESTART
        elif self._ending_scope is not None:
            self._restarting = True
            self._goal_pending = False
            self._root_goal = self._root_goal or text
            self._activate(_Step(text, None))
        else:
            self._root_goal = text
            self._overall_completion = None if goal is None else goal.completion
            self._verified_steps = 0
            self._completed_count = 0
            self._recent_completed.clear()
            self._status = Status.PLANNING
            self._goal_pending = True
        self._wake.set()

    def ranked(self, session_id: str, instance_id: str) -> tuple[Candidate, ...]:
        decision = self._decisions.current
        if (
            self._closed
            or decision is None
            or (decision.event.session_id, decision.event.instance_id)
            != (
                session_id,
                instance_id,
            )
        ):
            return ()
        deadline = decision.event.view.deadline_ns
        return () if deadline is not None and self._now_ns() >= deadline else decision.ranked

    def candidates(self, session_id: str, instance_id: str) -> tuple[int, ...]:
        return tuple(item.index for item in self.ranked(session_id, instance_id))

    def on_decision(self, event: DecisionEvent) -> None:
        if self._closed:
            return
        if event.phase in ("preview", "opened"):
            if self._ending_scope is not None or not event.view.options:
                return
            prior = "" if self._latest is None else self._latest.text
            new = self._decisions.observe(
                event, self._screen_key, prior, self._scope, self._knowledge
            )
            if new is not None:
                self._interrupt_summary()
                if self._flight is not None and not self._flight.done():
                    if self._kind == "decision":
                        self._flight.cancel()
                    elif (
                        self._kind == "goal"
                        and event.view.deadline_ns is not None
                        and self._now_ns() < event.view.deadline_ns
                    ):
                        self._goal_pending = self._root_goal is not None
                        self._flight.cancel()
        else:
            active = self._decisions.current
            if (
                active is not None
                and active.matches(event)
                and self._kind == "decision"
                and self._flight is not None
            ):
                self._flight.cancel()
            self._decisions.receipt(event)
            self._attribute_ending()
        self._wake.set()

    def on_goal(self, event: GoalEvent) -> None:
        goal = self._goal
        if goal is None or event.generation != goal.generation:
            return
        if event.kind == "rejected":
            self._rejections += 1
            if self._rejections >= self._config.rejection_limit:
                self._stuck()
        elif event.kind in ("step-limit", "timeout"):
            self._stuck()
        elif not self._restarting:
            self._goal = None
            self._verified_steps += event.steps
            if self._is_overall:
                self._status = Status.COMPLETE
            else:
                self._completed_count += 1
                self._recent_completed.append(goal.text)
                self._advance_goal()
        self._wake.set()

    def on_observation(self, event: ObservationReceipt) -> None:
        if self._closed or not 0 < len(event.text.strip()) <= 600 or event.frame_number < 1:
            return
        if self._clips.on_observation(event) and self._flight is not None:
            self._flight.cancel()
        self._observing = event
        self._observations[event.frame_number] = event
        self._for_store[event.frame_number] = event
        if len(self._for_store) > self._config.batch_size * 2:
            del self._for_store[min(self._for_store)]
        if self._writer is None or self._writer.done():
            self._writer = asyncio.create_task(self._store_observations())
        if event.observation.screen_type is ScreenType.BAD_ENDING:
            self._bad_ending(event.capture_ns)
        self._schedule_summary()

    def on_scene(self, update: SceneUpdate) -> None:
        if self._closed:
            return
        self._clips.on_frame(update, wait_subtitles=self._narrative() is not None)
        if update.state.stability is not Stability.STABLE or update.state.playing:
            return
        kind = update.screen_state.classification.screen_type
        identity = update.state.screen or str(kind)
        if identity != self._screen_key:
            self._rejections = 0
            self._schedule_summary(immediate=bool(self._screen_key))
        if update.observation_scope is not self._scene_scope:
            self._scene_scope = update.observation_scope
            self._observing = None
        self._screen_type, self._screen_key = kind, identity
        if kind is ScreenType.BAD_ENDING:
            self._bad_ending(update.frame.capture_ns)
        elif (
            self._restarting
            and self._narrative() is None
            and update.screen_state.classification.source is ScreenSource.SIGNATURE
            and kind not in {ScreenType.SAVE_LOAD, ScreenType.LOADING, ScreenType.UNKNOWN}
        ):
            self._restore()

    def on_record(self, record: NarrativeRecord) -> None:
        if self._closed:
            return
        if self._session is not None and record.session != self._session:
            self._seen.clear()
            self._decisions = DecisionHistory()
        if self._session is not None and (
            record.session != self._session or record.scope != self._scope
        ):
            if self._restarting:
                self._restore()
            elif record.session != self._session or self._ending_scope is None:
                self._invalidate_model()
                self._dirty.clear()
                self._recent.clear()
                self._observations.clear()
                self._knowledge = Knowledge()
                self._clips.discard()
                self._goal = None
                self._steps.clear()
                if self._status is not Status.STUCK:
                    self._advance_goal()
        self._session, self._scope = record.session, record.scope
        self._latest = record
        self._clips.on_record(record)
        fingerprint = compare_key(record.text)
        old_scope = self._seen.get(fingerprint)
        self._rereading = (
            record.completeness is Completeness.COMPLETE
            and old_scope is not None
            and old_scope < record.scope
        )
        if record.closed:
            if record.completeness is Completeness.COMPLETE and fingerprint:
                self._seen.setdefault(fingerprint, record.scope)
            self._recent.append(record)
            self._dirty[record.occurrence] = record
            self._schedule_summary()

    def on_cutscene(self, cutscene: Cutscene) -> None:
        if not self._closed:
            self._clips.on_cutscene(cutscene)

    def narrative_unavailable(self) -> None:
        self._latest = None
        self._rereading = False
        self._clips.narrative_unavailable()

    def on_milestone(self, kind: str, text: str, at_ns: int) -> None:
        if self._closed or not text.strip():
            return
        self._add_highlight(kind, text[:240], "medium", at_ns)
        self._milestones.append((kind, text[:240], at_ns))
        self._schedule_summary(immediate=True)

    def request_keyframe(self, update: SceneUpdate) -> bool:
        """为已确认缺少智能体观察的快路径帧补笔记。"""
        if (
            self._closed
            or update.state.playing
            or update.state.stability is not Stability.STABLE
            or update.observation_scope is not self._scene_scope
            or update.observation_scope is self._last_keyframe
            or self._observing is not None
        ):
            return False
        if not self._clips.request(update):
            return False
        self._last_keyframe = update.observation_scope
        return True

    def _bad_ending(self, at_ns: int) -> None:
        if self._ending_scope is not None:
            return
        self._ending_scope = self._scope
        self._ending_detail = BadEnding("检测到坏结局")
        self._invalidate_model()
        self._status = Status.WAITING_RESTART
        self._steps.clear()
        self._goal = None
        self._goal_pending = False
        self._highlights.append(("bad-ending", "检测到坏结局", "high", at_ns))
        self._attribute_ending()
        self._wake.set()

    def _attribute_ending(self) -> None:
        if (
            self._ending_scope is None
            or self._ending_applied
            or self._decisions.current is not None
        ):
            return
        self._ending_applied = True
        attempt, failures = self._decisions.ending(self._ending_scope)
        if attempt is not None:
            self._ending_detail = BadEnding(
                "检测到坏结局", attempt.instance_id, attempt.option_text[:600], attempt.source
            )
        self._restart_checkpoint = Knowledge() if attempt is None else attempt.checkpoint
        self._ending_request = (attempt, self._revision)
        if failures >= self._config.failure_limit:
            self._stuck(replan=False)

    def _restore(self) -> None:
        self._knowledge = self._restart_checkpoint or Knowledge()
        self._recent.clear()
        self._dirty.clear()
        self._observations.clear()
        self._milestones.clear()
        self._latest = None
        self._clips.discard()
        self._restart_checkpoint = None
        self._ending_scope = None
        self._ending_request = None
        self._ending_applied = False
        self._ending_detail = None
        self._restarting = False
        self._invalidate_model()
        self._goal = None
        self._advance_goal()
        self._wake.set()

    def _activate(self, step: _Step) -> None:
        self._rejections = 0
        deadline = self._now_ns() + round(self._config.subgoal_timeout_s * _S)
        max_steps = self._config.subgoal_max_steps
        submitted = self._submitted
        if submitted is not None:
            if submitted.deadline_ns is not None:
                deadline = min(deadline, submitted.deadline_ns)
            if submitted.max_steps is not None:
                max_steps = min(max_steps, submitted.max_steps - self._verified_steps)
        if max_steps == 0 and not step.overall and self._steps and self._steps[-1].overall:
            step = self._steps.pop()
            self._steps.clear()
        if max_steps < 0 or (max_steps == 0 and not step.overall) or deadline <= self._now_ns():
            self._stuck(replan=False)
            return
        self._is_overall = step.overall
        self._status = Status.ACTIVE
        self._goal = Goal(
            step.text,
            step.effect,
            deadline,
            next(_GOAL_GENERATIONS),
            max_steps,
            completion_by_agent=step.effect is None and not self._restarting,
        )

    def _advance_goal(self) -> None:
        if self._steps:
            self._activate(self._steps.popleft())
        else:
            self._status = Status.IDLE if self._root_goal is None else Status.PLANNING
            self._goal_pending = self._root_goal is not None

    def _stuck(self, *, replan: bool = True) -> None:
        self._status = Status.STUCK
        self._goal = None
        self._steps.clear()
        self._goal_pending = replan and self._root_goal is not None and self._ending_scope is None
        self._add_highlight("stuck", "当前目标达到推进限制", "high", self._now_ns())

    def _add_highlight(self, kind: str, text: str, importance: str, at_ns: int) -> None:
        key = (kind, compare_key(text))
        if key not in self._highlight_keys:
            self._highlight_keys.add(key)
            self._highlights.append((kind, text, importance, at_ns))

    def _invalidate_model(self) -> None:
        self._revision += 1
        if self._flight is not None:
            self._flight.cancel()

    def _interrupt_summary(self) -> None:
        if self._flight is not None and self._kind in {"summary", "keyframe"}:
            self._flight.cancel()

    def _schedule_summary(self, *, immediate: bool = False) -> None:
        if not (self._dirty or self._observations or self._milestones):
            return
        if immediate or self._summary_due is None:
            delay = 0 if immediate else self._config.summary_interval_s
            self._summary_due = time.monotonic() + delay
        self._wake.set()

    async def _store_observations(self) -> None:
        while self._for_store:
            number = min(self._for_store)
            item = self._for_store.pop(number)
            narrative = self._narrative()
            if narrative is not None:
                try:
                    await narrative.remember_observation(
                        item.frame_number,
                        capture_ns=item.capture_ns,
                        transform_version=item.transform_version,
                        observation=item.observation,
                        text=item.text,
                    )
                except Exception:
                    logger.warning("画面观察写入失败")

    async def _run(self) -> None:
        while not self._closed:
            self._wake.clear()
            selected = self._next()
            if selected is None:
                if self._summary_due is None:
                    await self._wake.wait()
                else:
                    with suppress(TimeoutError):
                        async with asyncio.timeout(max(0.0, self._summary_due - time.monotonic())):
                            await self._wake.wait()
                continue
            self._kind, job = selected
            self._flight = asyncio.create_task(job)
            retain_clip = False
            try:
                await self._flight
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if self._closed or (task is not None and task.cancelling()):
                    raise
                retain_clip = True
            except ModelCallError as error:
                logger.info("planner %s %s", self._kind, error.reason)
                self._failed_goal()
            except (ValueError, LookupError, NotCalibratedError):
                logger.info("planner %s invalid-output", self._kind)
                self._failed_goal()
            finally:
                if self._kind == "keyframe":
                    self._clips.release(retain=retain_clip)
                self._flight = None
                self._kind = ""

    def _failed_goal(self) -> None:
        if self._kind == "goal" and self._status is Status.PLANNING and not self._goal_pending:
            self._stuck(replan=False)

    def _next(self) -> tuple[str, Coroutine[Any, Any, None]] | None:
        if self._ending_request is not None:
            ending, epoch = self._ending_request
            self._ending_request = None
            if epoch == self._revision:
                return "ending", self._summarize_ending(ending, epoch)
        decision = self._decisions.current
        if decision is not None and not decision.requested:
            decision.requested = True
            deadline = decision.event.view.deadline_ns
            if deadline is None or self._now_ns() < deadline:
                return "decision", self._rank(decision, self._revision)
        if self._goal_pending and self._root_goal is not None:
            self._goal_pending = False
            return "goal", self._plan_goal(self._root_goal, self._revision)
        clip = self._clips.take()
        if clip is not None:
            return "keyframe", self._summarize(self._revision, clip)
        if self._summary_due is not None and time.monotonic() >= self._summary_due:
            self._summary_due = None
            if self._dirty or self._observations or self._milestones:
                self._schedule_summary()
                return "summary", self._summarize(self._revision)
        return None

    def _context(self, rows: Sequence[NarrativeRecord]) -> dict[str, JsonValue]:
        narrative = self._narrative()
        status = (
            "available"
            if narrative is not None
            else ("unavailable" if self._adapter.dialogue_boxes else "no-dialogue-box")
        )
        return {
            "summary": self._knowledge.summary,
            "notes": dict(self._knowledge.notes),
            "sections": [section.model_dump(mode="json") for section in self._adapter.notes],
            "narrative": status,
            "scene": self._screen_type.value,
            "lines": [
                {
                    "id": line.occurrence,
                    "text": line.text[:300],
                    "completeness": line.completeness.value,
                    "low_confidence": bool(line.low_confidence),
                    "speaker": line.speaker
                    if narrative is None
                    else narrative.canonical_speaker(line),
                }
                for line in rows
            ],
        }

    def _batch(self) -> tuple[NarrativeRecord, ...]:
        return tuple(islice(self._dirty.values(), self._config.batch_size))

    async def _summarize(self, epoch: int, clip: Clip | None = None) -> None:
        rows = self._batch()
        obs = tuple(islice(self._observations.values(), self._config.batch_size))
        milestones = tuple(self._milestones)
        data = self._context(rows)
        data["observations"] = [item.text for item in obs]
        data["events"] = [{"kind": kind, "text": text} for kind, text, _ in milestones]
        if clip is not None:
            data["subtitles"] = [line.text[:300] for line in clip.subtitles]
        reply = await self._model.complete(
            Purpose.SUMMARY if clip is None else Purpose.KEYFRAME,
            instruction=(
                "Update the running summary and declared notes using only these observations. "
                "Preserve open questions and distinguish uncertain readings."
            ),
            data=data,
            output=SummaryReply,
            images=() if clip is None else clip.frames,
        )
        if self._revision != epoch:
            return
        notes = {item.section: item.text for item in reply.value.notes}
        if len(notes) != len(reply.value.notes) or not notes.keys() <= {
            item.id for item in self._adapter.notes
        }:
            raise ValueError("笔记只接受适配包声明的栏目")
        merged = dict(self._knowledge.notes) | notes
        self._knowledge = Knowledge(reply.value.summary, tuple(merged.items()))
        for line in rows:
            if self._dirty.get(line.occurrence) == line:
                del self._dirty[line.occurrence]
        for item in obs:
            if self._observations.get(item.frame_number) == item:
                del self._observations[item.frame_number]
        for item in milestones:
            with suppress(ValueError):
                self._milestones.remove(item)
        for item in reply.value.highlights:
            self._add_highlight(item.kind, item.text, item.importance, self._now_ns())
        if not (self._dirty or self._observations or self._milestones):
            self._summary_due = None

    async def _rank(self, decision: Decision, epoch: int) -> None:
        latest = self._latest
        rows = {line.occurrence: line for line in self._recent}
        if latest is not None:
            rows[latest.occurrence] = latest
        data = self._context(tuple(rows.values())[-self._config.batch_size :])
        refs = {line.occurrence: line for line in tuple(rows.values())[-self._config.batch_size :]}
        view = decision.event.view
        data["prompt"] = view.prompt[:600]
        data["options"] = [
            {"index": option.index, "text": option.text[:600]} for option in view.options
        ]
        data["failed"] = {str(i): detail for i, detail in self._decisions.tried(decision).items()}
        instruction = (
            "Rank only the given option indices. Cite only supplied line ids from this run."
        )
        if view.deadline_ns is None:
            result = await self._model.complete(
                Purpose.REASONING, instruction=instruction, data=data, output=DecisionReply
            )
        else:
            with suppress(TimeoutError):
                async with asyncio.timeout(max(0.0, (view.deadline_ns - self._now_ns()) / _S)):
                    result = await self._model.complete(
                        Purpose.REASONING, instruction=instruction, data=data, output=DecisionReply
                    )
                    self._install_rank(decision, epoch, result.value, refs)
            return
        self._install_rank(decision, epoch, result.value, refs)

    def _install_rank(
        self,
        decision: Decision,
        epoch: int,
        result: DecisionReply,
        refs: dict[int, NarrativeRecord],
    ) -> None:
        deadline = decision.event.view.deadline_ns
        if (
            epoch == self._revision
            and self._decisions.current is decision
            and (deadline is None or self._now_ns() < deadline)
        ):
            decision.ranked = self._decisions.ranks(decision, result, refs)

    async def _plan_goal(self, root: str, epoch: int) -> None:
        data = self._context(self._batch())
        data.update(
            {
                "goal": root,
                "completed_steps": self._completed_count,
                "recent_completed": list(self._recent_completed),
                "rois": [name for name, roi in self._adapter.rois.items() if roi.rect is not None],
                "signatures": [signature.id for signature in self._adapter.signatures],
            }
        )
        result = await self._model.complete(
            Purpose.REASONING,
            instruction=(
                "Plan a few ordered subgoals for the authorized goal. "
                "Give a separate verifiable overall completion condition where available. "
                "Leave overall completion empty unless it verifies the entire authorized goal."
            ),
            data=data,
            output=GoalReply,
        )
        if epoch != self._revision:
            return
        steps = [
            _Step(
                item.text,
                None
                if item.completion is None
                else item.completion.expected(self._adapter, goal=True),
            )
            for item in result.value.subgoals
        ]
        overall = self._overall_completion
        if overall is None and result.value.overall_completion is not None:
            overall = result.value.overall_completion.expected(self._adapter, goal=True)
        if overall is not None:
            steps.append(_Step(root, overall, overall=True))
        if self._status is Status.STUCK:
            self._suggestions = tuple(item.text for item in steps)
        else:
            self._steps.extend(steps)
            self._advance_goal()

    async def _summarize_ending(self, attempt: Attempt | None, epoch: int) -> None:
        data = self._context(self._batch())
        if attempt is not None:
            data["confirmed_option"] = attempt.option_text[:600]
            data["source"] = attempt.source
        result = await self._model.complete(
            Purpose.REASONING,
            instruction="Summarize the observed bad ending in one short sentence.",
            data=data,
            output=EndingReply,
        )
        if epoch == self._revision:
            if attempt is not None:
                self._decisions.annotate(attempt, result.value.summary)
            if self._ending_detail is not None:
                self._ending_detail = replace(self._ending_detail, summary=result.value.summary)
