"""合成回执与可控制完成顺序的模型，验证 planner 的正常入口。"""

import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

import pytest
from pydantic import BaseModel, JsonValue

from core_protocol.models import ControlStep
from game_runtime.adapter import (
    GAME_ADAPTER,
    GAMES_DIR,
    Adapter,
    GameAdapterPlugin,
    ScreenType,
    load_adapter,
)
from game_runtime.frame_ring import RingFrame
from game_runtime.kernel import (
    ControlGrant,
    Kernel,
    KernelOptions,
    Manifest,
    NoConfig,
    Plugin,
    PluginContext,
    ResolvedEntry,
    State,
    Submission,
    Tier,
    catalog,
)
from game_runtime.models import (
    VLM,
    CallMetrics,
    ModelClient,
    ModelImage,
    ModelResult,
    PreparedKeyframe,
    Purpose,
    VlmConfig,
)
from game_runtime.narrative import Completeness, Cutscene, NarrativeRecord
from game_runtime.operator.messages import DecisionOption
from game_runtime.planner import Planner, PlannerConfig, PlannerPlugin, Status
from game_runtime.scene import (
    SCENE,
    SceneHub,
    SceneState,
    SceneUpdate,
    ScreenClassification,
    ScreenObservation,
    ScreenSource,
    ScreenState,
    Stability,
    Verdict,
)
from game_runtime.step_control.contracts import (
    PLANNER,
    DecisionEvent,
    DecisionView,
    EffectKind,
    ExpectedEffect,
    Goal,
    GoalEvent,
    ObservationReceipt,
    PlannerPort,
)

_NS = 1_000_000_000


@dataclass(slots=True)
class Call:
    purpose: Purpose
    data: JsonValue
    images: tuple[ModelImage | PreparedKeyframe, ...]
    answer: asyncio.Future[str]

    def finish(self, value: object) -> None:
        self.answer.set_result(json.dumps(value, ensure_ascii=False))


def _no_credential(name: str) -> str | None:
    raise AssertionError("合成 planner 测试不读取真实凭据")


class ScriptModel(ModelClient):
    def __init__(self, adapter: Adapter) -> None:
        config = VlmConfig.model_validate({"keyframe": {"image": {"max_long_edge": 4}}})
        super().__init__(
            config,
            adapter=adapter,
            permissions=frozenset(config.permissions),
            read_secret=_no_credential,
        )
        self.calls: asyncio.Queue[Call] = asyncio.Queue()
        self.samples: asyncio.Queue[PreparedKeyframe] = asyncio.Queue()
        self.active = 0
        self.max_active = 0
        self.finish_after_cancel = False

    async def next_call(self, purpose: Purpose) -> Call:
        call = await self.calls.get()
        assert call.purpose is purpose
        return call

    async def prepare_keyframe(self, image: ModelImage) -> PreparedKeyframe:
        prepared = await super().prepare_keyframe(image)
        self.samples.put_nowait(prepared)
        return prepared

    async def complete[T: BaseModel](
        self,
        purpose: Purpose,
        *,
        instruction: str,
        data: JsonValue,
        output: type[T],
        images: tuple[ModelImage | PreparedKeyframe, ...] = (),
    ) -> ModelResult[T]:
        assert instruction
        call = Call(purpose, data, images, asyncio.get_running_loop().create_future())
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.calls.put_nowait(call)
        try:
            if self.finish_after_cancel:
                try:
                    raw = await asyncio.shield(call.answer)
                except asyncio.CancelledError:
                    raw = await call.answer
            else:
                raw = await call.answer
            value = output.model_validate_json(raw, strict=True, extra="forbid")
            metrics = CallMetrics(purpose, "fake", "fake", 0.0, None, None, None, None)
            return ModelResult(value, (), metrics)
        finally:
            self.active -= 1


def line(n: int, body: str, *, scope: int = 1, cutscene: int | None = None) -> NarrativeRecord:
    return NarrativeRecord(
        1,
        n,
        scope,
        "dialogue",
        cutscene,
        "合成角色",
        body,
        1,
        Completeness.COMPLETE,
        n,
        n * _NS,
        n,
        n * _NS,
        n,
        n,
        n + 1,
        (),
    )


def scene(n: int, name: str | None, kind: ScreenType, *, since: int | None = None) -> SceneUpdate:
    frame = RingFrame(n, n * _NS, 2, 8, 6, 32, bytes(192))
    state = SceneState(
        n,
        frame.capture_ns,
        2,
        8,
        6,
        Stability.STABLE,
        1,
        0.0,
        since is not None,
        since,
        None,
        Verdict.SCREEN if name else Verdict.NONE,
        name,
        (),
        (),
        None,
    )
    return SceneUpdate(
        frame,
        state,
        ScreenState(
            ScreenClassification(
                kind,
                ScreenSource.SIGNATURE if name else None,
                1.0,
            )
        ),
    )


def decision(instance: str, *, deadline: int | None = None) -> DecisionEvent:
    return DecisionEvent(
        "session",
        instance,
        "preview",
        DecisionView(
            "选一条路径",
            (DecisionOption(index=0, text="左边"), DecisionOption(index=1, text="右边")),
            deadline,
        ),
        10,
    )


def ranking(*, quotes: tuple[int, ...] = ()) -> dict[str, object]:
    return {
        "ranked": [
            {"index": 1, "reason": "合成理由", "quotes": quotes},
            {"index": 0, "reason": "另一个合成理由"},
        ]
    }


async def until(check: Callable[[], bool]) -> None:
    while not check():
        await asyncio.sleep(0.001)


def rig(**config_overrides: JsonValue) -> tuple[Planner, ScriptModel]:
    adapter = load_adapter(GAMES_DIR / "synthetic-game")
    model = ScriptModel(adapter)
    config = PlannerConfig.model_validate({"summary_interval_s": 0.02} | config_overrides)
    planner = Planner(adapter, model, config, narrative=lambda: None, now_ns=lambda: _NS)
    planner.start()
    return planner, model


def test_delayed_ranking_cancels_summary_and_preserves_uncommitted_batch() -> None:
    async def scenario() -> None:
        planner, model = rig()
        try:
            planner.on_record(line(1, "合成证据只有这一句。"))
            summary = await model.next_call(Purpose.SUMMARY)
            event = decision("draft", deadline=90 * _NS)
            planner.on_decision(event)
            assert planner.candidates("session", "draft") == ()
            reason = await model.next_call(Purpose.REASONING)
            assert summary.answer.cancelled()
            planner.on_decision(replace(event, phase="opened"))
            reason.finish(ranking(quotes=(1,)))
            await until(lambda: planner.candidates("session", "draft") == (1, 0))
            assert planner.candidates("another-session", "draft") == ()
            assert planner.ranked("session", "draft")[0].quotes == ((1, "合成证据只有这一句。"),)
            resumed = await model.next_call(Purpose.SUMMARY)
            assert resumed.data == summary.data
            resumed.finish(
                {"summary": "已合并", "notes": [{"section": "clues", "text": "合成线索"}]}
            )
            await until(lambda: planner.situation.summary == "已合并")
            assert "合成线索" in planner.current().notes
            assert model.max_active == 1
        finally:
            await planner.close()

    async def bounded() -> None:
        async with asyncio.timeout(4):
            await scenario()

    asyncio.run(bounded())


@pytest.mark.parametrize(
    "answer", [ranking(quotes=(999,)), {"ranked": [{"index": 7, "reason": "伪造"}]}]
)
def test_only_actual_current_options_and_local_quotes_are_accepted(
    answer: dict[str, object],
) -> None:
    async def scenario() -> None:
        planner, model = rig()
        try:
            event = decision("check")
            planner.on_decision(event)
            reply = await model.next_call(Purpose.REASONING)
            reply.finish(answer)
            await until(lambda: model.active == 0)
            assert planner.candidates("session", "check") == ()
        finally:
            await planner.close()

    asyncio.run(asyncio.wait_for(scenario(), 4))


@pytest.mark.parametrize("blocked", ["untimed-choice", "goal"])
def test_new_timed_choice_preempts_stalled_reasoning_and_preserves_goal_work(blocked: str) -> None:
    async def scenario() -> None:
        planner, model = rig()
        try:
            if blocked == "goal":
                planner.submit_goal(Goal("授权目标需要继续规划"))
            else:
                planner.on_decision(decision("old"))
            previous = await model.next_call(Purpose.REASONING)
            planner.on_decision(decision("timed", deadline=_NS + 500_000_000))
            async with asyncio.timeout(0.3):
                timed = await model.next_call(Purpose.REASONING)
            assert previous.answer.cancelled()
            assert isinstance(timed.data, dict)
            assert timed.data["prompt"] == "选一条路径"
            timed.finish(ranking())
            await until(lambda: planner.candidates("session", "timed") == (1, 0))
            assert model.max_active == 1
            if blocked == "goal":
                resumed = await model.next_call(Purpose.REASONING)
                assert isinstance(resumed.data, dict)
                assert resumed.data["goal"] == "授权目标需要继续规划"
                resumed.finish({"subgoals": [{"text": "恢复规划后的步骤"}]})
                await until(lambda: planner.current().goal is not None)
        finally:
            await planner.close()

    asyncio.run(asyncio.wait_for(scenario(), 4))


def test_subgoal_completion_continues_the_authorized_goal_and_timeout_pauses() -> None:
    async def scenario() -> None:
        planner, model = rig()
        try:
            planner.submit_goal(Goal("完成整个合成任务"))
            assert planner.current().paused
            assert planner.current().planning
            draft = await model.next_call(Purpose.REASONING)
            draft.finish({"subgoals": [{"text": "阶段一"}, {"text": "阶段二"}]})
            await until(lambda: planner.current().goal is not None)
            first = planner.current().goal
            assert first is not None
            assert first.text == "阶段一"
            assert first.completion_by_agent
            assert not planner.current().planning
            planner.on_goal(GoalEvent(first.generation + 1000, "completed", 1))
            assert planner.current().goal == first
            planner.on_goal(GoalEvent(first.generation, "completed", 1))
            second = planner.current().goal
            assert second is not None
            assert second.text == "阶段二"
            assert model.calls.empty()
            planner.on_goal(GoalEvent(second.generation, "completed", 1))
            assert planner.situation.completed_subgoals == 2
            assert planner.situation.status is Status.PLANNING
            next_part = await model.next_call(Purpose.REASONING)
            assert isinstance(next_part.data, dict)
            assert next_part.data["goal"] == "完成整个合成任务"
            assert next_part.data["completed_steps"] == 2
            next_part.finish({"subgoals": [{"text": "阶段三"}]})
            await until(lambda: planner.current().goal is not None)
            third = planner.current().goal
            assert third is not None
            planner.on_goal(GoalEvent(third.generation, "timeout", 5))
            assert planner.current().paused
            assert planner.current().pause_reason == "stuck"
            replan = await model.next_call(Purpose.REASONING)
            replan.finish({"subgoals": [{"text": "备用路线"}]})
            await until(lambda: bool(planner.situation.replanning))
            assert planner.current().goal is None
            assert planner.current().paused
        finally:
            await planner.close()

    asyncio.run(asyncio.wait_for(scenario(), 4))


@pytest.mark.parametrize("close", [False, True])
def test_cleared_or_closed_planner_holds_instead_of_continuing_old_goal(close: bool) -> None:
    async def scenario() -> None:
        planner, model = rig()
        try:
            assert planner.current().paused
            request = Goal("授权任务")
            planner.submit_goal(request)
            (await model.next_call(Purpose.REASONING)).finish({"subgoals": [{"text": "原步骤"}]})
            await until(lambda: planner.current().goal is not None)
            active = planner.current().goal
            assert active is not None
            planner.submit_goal(request)
            assert planner.current().goal == active
            if close:
                await planner.close()
            else:
                planner.submit_goal(None)
            planner.on_goal(GoalEvent(active.generation, "completed", 1))
            held = planner.current()
            assert held.goal is None
            assert held.paused
            assert held.pause_reason == ("closed" if close else "idle")
            assert model.calls.empty()
        finally:
            await planner.close()

    asyncio.run(asyncio.wait_for(scenario(), 4))


@pytest.mark.parametrize("budget", [3, 8])
def test_overall_completion_has_its_own_generation_and_authorized_condition(budget: int) -> None:
    async def scenario() -> None:
        planner, model = rig()
        try:
            overall = ExpectedEffect(EffectKind.SCREEN, screen_type=ScreenType.MENU)
            planner.submit_goal(Goal("授权整体目标", overall, max_steps=budget))
            request = await model.next_call(Purpose.REASONING)
            request.finish(
                {
                    "subgoals": [{"text": "一个章节"}]
                    + ([{"text": "余下的章节"}] if budget == 3 else []),
                    "overall_completion": {
                        "kind": "screen",
                        "screen_type": ScreenType.SETTINGS.value,
                    },
                }
            )
            await until(lambda: planner.current().goal is not None)
            child = planner.current().goal
            assert child is not None
            planner.on_goal(GoalEvent(child.generation, "completed", 3))
            verify = planner.current().goal
            assert verify is not None
            assert verify.generation != child.generation
            assert verify.completion == overall
            assert verify.max_steps == budget - 3
            assert planner.situation.status is Status.ACTIVE
            planner.on_goal(GoalEvent(verify.generation, "completed", 0))
            assert planner.situation.status is Status.COMPLETE
            assert planner.current().paused
        finally:
            await planner.close()

    asyncio.run(asyncio.wait_for(scenario(), 4))


def test_late_goal_reply_after_clear_cannot_install_a_subgoal() -> None:
    async def scenario() -> None:
        planner, model = rig()
        model.finish_after_cancel = True
        try:
            planner.submit_goal(Goal("旧授权"))
            request = await model.next_call(Purpose.REASONING)
            planner.submit_goal(None)
            request.finish({"subgoals": [{"text": "迟到的旧子目标"}]})
            await until(lambda: model.active == 0)
            assert planner.current().paused
            assert planner.current().goal is None
            assert planner.situation.status is Status.IDLE
        finally:
            await planner.close()

    asyncio.run(asyncio.wait_for(scenario(), 4))


def test_model_gets_explicit_unavailable_narrative_and_only_declared_notes() -> None:
    async def scenario() -> None:
        planner, model = rig()
        try:
            planner.on_milestone("evidence", "局内察觉的合成线索。", 1)
            update = await model.next_call(Purpose.SUMMARY)
            assert isinstance(update.data, dict)
            assert update.data["narrative"] == "unavailable"
            assert update.data["lines"] == []
            update.finish(
                {"summary": "无效更新", "notes": [{"section": "undeclared", "text": "假"}]}
            )
            await until(lambda: model.active == 0)
            assert planner.situation.summary == ""
            update = await model.next_call(Purpose.SUMMARY)
            update.finish({"summary": "合成更新", "notes": [{"section": "clues", "text": "实录"}]})
            await until(lambda: planner.situation.summary == "合成更新")
        finally:
            await planner.close()

    asyncio.run(asyncio.wait_for(scenario(), 4))


@pytest.mark.parametrize("confirmed", [True, False])
def test_bad_ending_pause_and_failure_history_require_a_confirmed_choice(confirmed: bool) -> None:
    async def scenario() -> None:
        planner, model = rig(summary_interval_s=15, failure_limit=1)
        try:
            planner.on_scene(scene(1, "choice", ScreenType.CHOICE))
            planner.on_record(line(1, "合成前文。"))
            event = decision("first")
            planner.on_decision(event)
            (await model.next_call(Purpose.REASONING)).finish(ranking())
            await until(lambda: bool(planner.candidates("session", "first")))
            planner.on_scene(scene(2, "ending", ScreenType.BAD_ENDING))
            assert planner.current().paused
            receipt = (
                replace(event, phase="confirmed", index=1, by="operator")
                if confirmed
                else replace(event, phase="unknown")
            )
            planner.on_decision(receipt)
            end_call = await model.next_call(Purpose.REASONING)
            assert isinstance(end_call.data, dict)
            assert ("confirmed_option" in end_call.data) is confirmed
            ending = planner.situation.ending
            assert ending is not None
            expected_ending = replace(ending, summary="合成结果")
            end_call.finish({"summary": expected_ending.summary})
            # 切屏触发的普通摘要在坏结局摘要落定后开始。
            await model.next_call(Purpose.SUMMARY)
            assert planner.situation.ending == expected_ending
            assert planner.current().pause_reason == ("stuck" if confirmed else "waiting-restart")
            planner.submit_goal(None)
            assert planner.current().paused
            planner.submit_goal(Goal("继续"))
            assert not planner.current().paused
            assert planner.current().restarting
            planner.on_record(line(2, "合成前文。", scope=2))
            planner.on_scene(scene(3, "choice", ScreenType.CHOICE))
            assert not planner.current().restarting
            planner.on_decision(decision("second"))
            (await model.next_call(Purpose.REASONING)).finish(ranking())
            await until(lambda: bool(planner.candidates("session", "second")))
            assert planner.candidates("session", "second") == ((0, 1) if confirmed else (1, 0))
            observed = planner.ranked("session", "second")[-1].failed_outcome
            assert observed == (expected_ending.summary if confirmed else None)
        finally:
            await planner.close()

    asyncio.run(asyncio.wait_for(scenario(), 4))


def test_bad_ending_after_withdrawn_timed_decision_has_no_prior_attribution() -> None:
    async def scenario() -> None:
        planner, model = rig(summary_interval_s=15)
        try:
            planner.on_scene(scene(1, "choice", ScreenType.CHOICE))
            planner.on_record(line(1, "合成前文。"))
            first = decision("first")
            planner.on_decision(first)
            (await model.next_call(Purpose.REASONING)).finish(ranking())
            await until(lambda: bool(planner.candidates("session", "first")))
            planner.on_decision(replace(first, phase="confirmed", index=1, by="operator"))

            second = decision("second", deadline=2 * _NS)
            planner.on_decision(second)
            (await model.next_call(Purpose.REASONING)).finish(ranking())
            await until(lambda: bool(planner.candidates("session", "second")))
            planner.on_decision(replace(second, phase="withdrawn"))
            planner.on_scene(scene(2, "ending", ScreenType.BAD_ENDING))
            request = await model.next_call(Purpose.REASONING)
            assert isinstance(request.data, dict)
            assert "confirmed_option" not in request.data
            ending = planner.situation.ending
            assert ending is not None
            assert ending.decision_id is None
        finally:
            await planner.close()

    asyncio.run(asyncio.wait_for(scenario(), 4))


def test_cutscene_carries_only_prepared_frames_and_subtitles_once() -> None:
    async def scenario() -> None:
        planner, model = rig()
        try:
            for step in range(1, 13):
                planner.on_scene(scene(step * 10, None, ScreenType.CUTSCENE, since=10 * _NS))
                await model.samples.get()
                await asyncio.sleep(0)
            planner.on_record(line(11, "合成字幕。", cutscene=1))
            planner.on_cutscene(
                Cutscene(1, 1, 1, 10, 10 * _NS, 120, 120 * _NS, 121 * _NS, 121, (11,))
            )
            planner.on_scene(scene(121, "menu", ScreenType.MENU))
            request = await model.next_call(Purpose.KEYFRAME)
            assert 1 <= len(request.images) <= 8
            for image in request.images:
                assert isinstance(image, PreparedKeyframe)
                assert image.transform.width == 4
            assert isinstance(request.data, dict)
            assert request.data["subtitles"] == ["合成字幕。"]
            request.finish({"summary": "播片摘要"})
            await until(lambda: planner.situation.summary == "播片摘要")
            assert model.calls.empty()
        finally:
            await planner.close()

    asyncio.run(asyncio.wait_for(scenario(), 4))


def test_consecutive_cutscenes_resume_after_timed_rank() -> None:
    async def scenario() -> None:
        planner, model = rig()
        try:
            planner.on_scene(scene(1, None, ScreenType.CUTSCENE, since=_NS))
            first_sample = await model.samples.get()
            planner.on_scene(scene(2, "menu", ScreenType.MENU))
            first = await model.next_call(Purpose.KEYFRAME)
            assert first.images == (first_sample,)

            planner.on_scene(scene(3, None, ScreenType.CUTSCENE, since=3 * _NS))
            planner.on_scene(scene(4, "menu", ScreenType.MENU))
            planner.on_decision(decision("timed", deadline=2 * _NS))
            rank = await model.next_call(Purpose.REASONING)
            assert first.answer.cancelled()
            second_sample = await model.samples.get()
            assert second_sample.transform.frame_number == 3
            rank.finish(ranking())

            resumed = await model.next_call(Purpose.KEYFRAME)
            assert resumed.images == (first_sample,)
            resumed.finish({"summary": "第一段摘要"})
            second = await model.next_call(Purpose.KEYFRAME)
            assert second.images == (second_sample,)
            second.finish({"summary": "第二段摘要"})
            await until(lambda: planner.situation.summary == "第二段摘要")
            assert model.max_active == 1
        finally:
            await planner.close()

    asyncio.run(asyncio.wait_for(scenario(), 4))


def test_prepared_fast_path_frame_yields_to_same_frame_observation() -> None:
    async def scenario() -> None:
        planner, model = rig()
        try:
            planner.on_record(line(1, "合成前文。"))
            prior = await model.next_call(Purpose.SUMMARY)
            update = scene(12, "menu", ScreenType.MENU)
            planner.on_scene(update)
            assert planner.request_keyframe(update)
            await model.samples.get()
            frame = update.frame
            description = "智能体已描述本帧"
            planner.on_observation(
                ObservationReceipt(
                    frame.frame_number,
                    frame.transform_version,
                    frame.capture_ns,
                    description,
                    ScreenObservation(screen_type=ScreenType.MENU, confidence=0.8),
                )
            )
            prior.finish({"summary": "前文摘要"})
            summary = await model.next_call(Purpose.SUMMARY)
            assert isinstance(summary.data, dict)
            assert summary.data["observations"] == [description]
            assert not summary.images
        finally:
            await planner.close()

    asyncio.run(asyncio.wait_for(scenario(), 4))


class NoInputCore:
    async def invalidate_generation(self, grant: ControlGrant) -> None:
        raise AssertionError("普通插件没有控制产出者代次")

    async def wait_released(self, grant: ControlGrant) -> None:
        raise AssertionError("普通插件没有输入可释放")

    async def submit(
        self,
        grant: ControlGrant,
        steps: Sequence[ControlStep],
        *,
        base_frame: int,
        valid_for: float,
        start_delay: float = 0.0,
    ) -> Submission:
        raise AssertionError("规划不能提交输入")


def test_plugin_publishes_the_shared_port_without_narrative_and_revokes_on_shutdown() -> None:
    async def scenario() -> None:
        services: list[PlannerPort] = []

        class Scene(Plugin[NoConfig]):
            manifest = Manifest(
                id="fake-scene", tier=Tier.ORDINARY, version="1", provides=(SCENE.ref,)
            )

            async def activate(self, ctx: PluginContext[NoConfig]) -> None:
                ctx.provide(SCENE, SceneHub(problems=(), uncalibrated=()))

        class Model(Plugin[NoConfig]):
            manifest = Manifest(
                id="fake-model",
                tier=Tier.ORDINARY,
                version="1",
                provides=(VLM.ref,),
                inject=(GAME_ADAPTER.ref,),
            )

            async def activate(self, ctx: PluginContext[NoConfig]) -> None:
                ctx.provide(VLM, ScriptModel(ctx.service(GAME_ADAPTER)))

        class Consumer(Plugin[NoConfig]):
            manifest = Manifest(
                id="planner-consumer", tier=Tier.ORDINARY, version="1", inject=(PLANNER.ref,)
            )

            async def activate(self, ctx: PluginContext[NoConfig]) -> None:
                services.append(ctx.service(PLANNER))

        kernel = Kernel(
            catalog=catalog(GameAdapterPlugin, Scene, Model, PlannerPlugin, Consumer),
            core=NoInputCore(),
            options=KernelOptions(retry_delays=()),
        )
        kernel.add(
            [
                ResolvedEntry("planner", "planner", {}, False, frozenset()),
                ResolvedEntry("consumer", "planner-consumer", {}, False, frozenset()),
                ResolvedEntry("scene", "fake-scene", {}, False, frozenset()),
                ResolvedEntry("model", "fake-model", {}, False, frozenset()),
                ResolvedEntry(
                    "game", "game-adapter", {"game": "synthetic-game"}, False, frozenset()
                ),
            ]
        )
        try:
            await kernel.settled()
            assert kernel.status("planner").state is State.ACTIVE
            (port,) = services
            assert port.current().paused
            assert port.current().pause_reason == "idle"
        finally:
            await kernel.shutdown()
        assert port.current().pause_reason == "closed"

    asyncio.run(asyncio.wait_for(scenario(), 4))
