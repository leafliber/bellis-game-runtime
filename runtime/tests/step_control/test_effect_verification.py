"""已执行动作的文字验效使用仍属于当前画面的 OCR。"""

# pyright: reportPrivateUsage=false

import asyncio
from collections.abc import Mapping, Sequence
from unittest.mock import AsyncMock, Mock

import pytest

from core_protocol import models
from game_runtime.adapter import GAMES_DIR, PixelRect, ScreenType, load_adapter
from game_runtime.frame_ring import RingFrame
from game_runtime.kernel import ControlGrant
from game_runtime.kernel.control import Submission
from game_runtime.narrative import (
    Completeness,
    NarrativeRecord,
    NarrativeService,
)
from game_runtime.ocr import OcrLine, OcrResult, OcrService
from game_runtime.operator import Clock, MonotonicClock, messages
from game_runtime.scene import (
    Outcome,
    SceneHub,
    SceneState,
    SceneUpdate,
    ScreenClassification,
    ScreenSource,
    ScreenState,
    SignatureResult,
    Stability,
    Verdict,
)
from game_runtime.step_control.contracts import (
    EffectKind,
    ExpectedEffect,
    Goal,
    PlannerContext,
    PlannerPort,
    Policy,
    StepStatus,
)
from game_runtime.step_control.controller import StepConfig, StepController
from game_runtime.step_control.pacing import DEFAULT_PACING, ReadingPacing, ReadingWaitError
from game_runtime.step_control.plugin import SessionSignals
from game_runtime.step_control.risk import ActionRefusedError


@pytest.mark.parametrize("change", ["scope", "ocr", "transform", "deadline"])
def test_post_execution_ocr_discards_only_a_superseded_sample(change: str) -> None:
    async def exercise() -> None:
        now = 1_200_000_000
        until = now + 1_000_000_000
        executed_ns = now - 20_000_000
        clock = Mock(spec=Clock)
        clock.now_ns.side_effect = lambda: now
        adapter = load_adapter(GAMES_DIR / "synthetic-game")
        scene = SceneHub(problems=(), uncalibrated=())
        roi = adapter.roi("dialogue").to_pixels(96, 54)
        box = PixelRect(roi.left, roi.top, roi.left + 10, roi.top + 4)
        reads: list[int] = []

        def view(number: int, captured: int, transform: int = 1) -> SceneUpdate:
            frame = RingFrame(number, captured, transform, 96, 54, 384, bytes([number]) * 20736)
            state = SceneState(
                number,
                captured,
                transform,
                96,
                54,
                Stability.STABLE,
                250_000_000,
                0.0,
                False,
                None,
                None,
                Verdict.SCREEN,
                "dialogue",
                ("dialogue",),
                (),
                None,
            )
            baseline = ScreenClassification(ScreenType.DIALOGUE, ScreenSource.SIGNATURE, 1.0)
            return SceneUpdate(frame, state, ScreenState(baseline))

        before = view(1, now - 30_000_000)
        first = view(2, now - 10_000_000)
        second = view(3, now, transform=2 if change == "transform" else 1)

        async def recognize(frame: RingFrame, name: str) -> OcrResult:
            nonlocal now
            reads.append(frame.frame_number)
            await asyncio.sleep(0)
            if frame is first.frame:
                if change == "deadline":
                    now = until
                else:
                    scene.begin_frame(second.frame)
                    scene.publish(second)
            number = frame.frame_number + int(change == "ocr")
            return OcrResult(
                number, frame.transform_version, name, roi, (OcrLine("下一段", box, 1.0),)
            )

        ocr = Mock(spec=OcrService, adapter=adapter, recognize=AsyncMock(side_effect=recognize))
        permit = asyncio.Event()
        operator = Mock(session_id="fixture", may_autoplay=False, session=None)
        signals = SessionSignals()
        controller = StepController(
            adapter,
            scene,
            ocr,
            ControlGrant("step", "fixture", 1, permit.is_set),
            operator,
            Mock(spec=Policy),
            clock,
            StepConfig(),
            permit.is_set,
            lambda: None,
            signals.progress,
            signals.operator_goal,
            signals.planner_binding,
            ReadingPacing(DEFAULT_PACING, lambda: None, lambda: None),
        )
        scene.subscribe(controller.observe)
        scene.publish(before)
        before = scene.latest
        assert before is not None
        scene.publish(first)
        operator.session = messages.Session(
            adapter=adapter.id,
            state=messages.SessionState.RUNNING,
            confirmed=(),
            goal=None,
            ends_at_ns=until + 1_000_000_000,
        )
        operator.may_autoplay = True
        permit.set()

        expected = ExpectedEffect(EffectKind.TEXT_DISAPPEARED, text="上一段", roi="dialogue")
        if change == "ocr":
            with pytest.raises(ActionRefusedError, match="stale-ocr"):
                await controller._verify(before, expected, executed_ns, None, 0, until=until)
        else:
            status = await controller._verify(before, expected, executed_ns, None, 0, until=until)
            assert status is (StepStatus.VERIFIED if change == "scope" else StepStatus.UNKNOWN)
        assert reads == ([2, 3] if change == "scope" else [2])
        if change == "scope":
            assert scene.latest is not None
            assert controller._verified_scope is scene.latest.observation_scope

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "case",
    [
        "matched-destination",
        "next-line",
        "already-present",
        "unconfigured",
        "unseen-signature",
        "empty-read",
        "timed",
        "stale-options",
        "no-options",
        "discontinuity",
        "capture-gap",
        "other-transform",
        "session-changed",
        "revoked-during-ocr",
        "no-executed",
        "duplicate",
        "outside-block",
    ],
)
def test_fast_advance_verifies_only_the_predeclared_choice_destination(case: str) -> None:
    async def exercise() -> None:
        adapter = load_adapter(GAMES_DIR / "synthetic-game")
        if case == "unconfigured":
            adapter = adapter.model_copy(update={"decisions": {}})
        hub = SceneHub(problems=(), uncalibrated=())
        system_clock = MonotonicClock()
        jump_ns = 0
        clock = Mock(spec=Clock)
        clock.now_ns.side_effect = lambda: system_clock.now_ns() + jump_ns
        permit = asyncio.Event()
        prior_text = "旧句仍在。"
        before_ns = clock.now_ns()
        record = NarrativeRecord(
            session=1,
            occurrence=1,
            scope=1,
            screen="dialogue",
            speaker=None,
            text=prior_text,
            revision=1,
            completeness=Completeness.COMPLETE,
            first_frame=1,
            first_ns=before_ns - 2_000_000_000,
            last_frame=1,
            last_ns=before_ns - 2_000_000_000,
            revision_frame=1,
            complete_frame=1,
            closed_frame=None,
            low_confidence=(),
            cutscene=None,
        )
        source = Mock(spec=NarrativeService, latest=record, processed_frame=2)
        source.records.return_value = (record,)
        goal = Goal(
            "继续阅读",
            completion=ExpectedEffect(EffectKind.TEXT_DISAPPEARED, text=prior_text, roi="dialogue"),
            generation=1,
            max_steps=1,
        )
        planner = Mock(spec=PlannerPort)
        planner.current.return_value = PlannerContext(goal)
        operator = Mock(
            session_id="fixture",
            may_autoplay=True,
            session=messages.Session(
                adapter=adapter.id,
                state=messages.SessionState.RUNNING,
                confirmed=(),
                goal=goal.text,
                ends_at_ns=before_ns + 5_000_000_000,
            ),
        )
        signals = SessionSignals()
        actions: list[int] = []

        def publish(
            number: int,
            captured: int,
            screen: str,
            screen_type: ScreenType,
            transform: int = 1,
        ) -> SceneUpdate:
            frame = RingFrame(number, captured, transform, 96, 54, 384, bytes([number]) * 20736)
            hit = case == "already-present" or screen == "choice"
            choice = Outcome.MATCH if hit else Outcome.NO_MATCH
            state = SceneState(
                number,
                captured,
                transform,
                96,
                54,
                Stability.STABLE,
                250_000_000,
                0.0,
                False,
                None,
                None,
                Verdict.SCREEN,
                screen,
                (screen,),
                ()
                if case == "unseen-signature"
                else (SignatureResult("choice", "choice", 0, choice, None, ()),),
                None,
            )
            baseline = ScreenClassification(screen_type, ScreenSource.SIGNATURE, 1.0)
            hub.begin_frame(frame)
            hub.publish(SceneUpdate(frame, state, ScreenState(baseline)))
            latest = hub.latest
            assert latest is not None
            return latest

        async def recognize(frame: RingFrame, name: str) -> OcrResult:
            roi = adapter.roi(name).to_pixels(frame.width, frame.height)
            number = frame.frame_number
            text = "下一句已出现。" if case == "next-line" and number > 2 else prior_text
            if case == "empty-read" and name == "dialogue" and number > 2:
                return OcrResult(number, frame.transform_version, name, roi, ())
            if name == "choices":
                if case == "no-options":
                    return OcrResult(number, frame.transform_version, name, roi, ())
                number += int(case == "stale-options")
                if case == "revoked-during-ocr":
                    permit.clear()
                text = "继续向前"
            return OcrResult(
                number, frame.transform_version, name, roi, (OcrLine(text, roi, 0.99),)
            )

        async def submit(
            grant: ControlGrant,
            steps: Sequence[models.ControlStep],
            *,
            base_frame: int,
            valid_for: float,
            start_delay: float = 0.0,
        ) -> Submission:
            grant.check()
            assert base_frame == 2
            assert valid_for > 0
            (step,) = steps
            (action,) = step.actions
            assert isinstance(action.root, models.Click)
            actions.append(action.root.id)

            start_ns = clock.now_ns()
            block = models.ActionBlock(
                producer=models.Producer(id=grant.producer, generation=grant.generation),
                seq=1,
                base_frame=base_frame,
                start_ns=start_ns,
                deadline_ns=start_ns + (0 if case == "outside-block" else round(valid_for * 1e9)),
                steps=list(steps),
            )

            async def outcomes() -> Mapping[int, models.Executed | models.Dropped]:
                nonlocal jump_ns
                executed = clock.now_ns()
                if case == "discontinuity":
                    hub.invalidate()
                if case == "capture-gap":
                    jump_ns = 600_000_000
                if case == "session-changed":
                    operator.session_id = "next"
                screen, kind = {
                    "timed": ("timed-choice", ScreenType.TIMED_CHOICE),
                    "next-line": ("dialogue", ScreenType.DIALOGUE),
                    "empty-read": ("dialogue", ScreenType.DIALOGUE),
                }.get(case, ("choice", ScreenType.CHOICE))
                publish(
                    3,
                    max(executed + 1, clock.now_ns()),
                    screen,
                    kind,
                    (2 if case == "other-transform" else 1),
                )
                if case == "no-executed":
                    return {}
                if case == "duplicate":
                    return {
                        action.root.id: models.Dropped(
                            id=action.root.id, reason=models.DropReason.DUPLICATE
                        )
                    }
                return {action.root.id: models.Executed(id=action.root.id, at_ns=executed)}

            return Mock(
                spec=Submission,
                block=block,
                result=models.Accepted(),
                outcomes=AsyncMock(side_effect=outcomes),
            )

        pacing = DEFAULT_PACING.model_copy(
            update={"read_base": 0.0, "read_per_char": 0.0, "voice_unknown_wait": 0.001}
        )
        policy = Mock(spec=Policy)
        controller = StepController(
            adapter,
            hub,
            Mock(spec=OcrService, adapter=adapter, recognize=AsyncMock(side_effect=recognize)),
            ControlGrant("step", "fixture", 1, permit.is_set, submit),
            operator,
            policy,
            clock,
            StepConfig(effect_timeout=0.03),
            permit.is_set,
            lambda: planner,
            signals.progress,
            signals.operator_goal,
            signals.planner_binding,
            ReadingPacing(pacing, lambda: source, lambda: None),
        )
        hub.subscribe(controller.observe)
        controller.set_goal(goal)
        before = publish(2, before_ns, "dialogue", ScreenType.DIALOGUE)
        permit.set()
        await controller._tick(controller._revision)
        result = controller.recent[-1]
        if case == "matched-destination":
            assert result.status is StepStatus.VERIFIED
            box = adapter.dialogue_boxes[0]
            with pytest.raises(ReadingWaitError, match="dialogue-awaiting-next-line"):
                await controller._advance_dialogue(before, box, record, controller._revision)
        elif case == "next-line":
            assert any(item.status is StepStatus.VERIFIED for item in controller.recent)
        else:
            assert result.status is StepStatus.UNKNOWN
        assert len(actions) == signals.progress.steps == 1
        assert not signals.progress.goal_done
        planner.on_goal.assert_not_called()
        policy.propose.assert_not_called()

    asyncio.run(exercise())
