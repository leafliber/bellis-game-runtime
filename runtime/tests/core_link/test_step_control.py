"""监督运行的完整路径。只有合成游戏、真实实时核、假显示面和假输入设备。"""

import asyncio
import math
import sys
import time
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from core_protocol import models
from game_runtime.adapter import GAMES_DIR, Adapter, PixelRect, ScreenType, load_adapter
from game_runtime.core_link import CaptureConfig, CoreLink, CoreLinkOptions
from game_runtime.core_link.fake_device import DeviceEvent, FakeDevice
from game_runtime.frame_ring import RingFrame
from game_runtime.frame_ring.fake import FakeDisplay
from game_runtime.frames import AdmissionPolicy
from game_runtime.kernel import (
    ControlGrant,
    Manifest,
    NoConfig,
    Plugin,
    PluginContext,
    ResolvedEntry,
    State,
    Submission,
    Tier,
)
from game_runtime.ocr import OCR, FrameLike, OcrConfig, OcrLine, OcrResult, OcrService
from game_runtime.ocr.fake import ScriptedRecognizer
from game_runtime.operator import ConsoleEvent, ConsoleOptions, EventKind, OperatorConsole
from game_runtime.scene import ScreenObservation, store_template
from game_runtime.secret_store import MemorySecretStore
from game_runtime.step_control import (
    PLANNER,
    STEP_ENTRY,
    Category,
    Click,
    DecisionEvent,
    EffectKind,
    ExpectedEffect,
    Goal,
    GoalEvent,
    GoalJudgment,
    ObservationReceipt,
    Plan,
    PlannerContext,
    Policy,
    PolicyInput,
    Press,
    StepConfig,
    StepProposal,
    StepStatus,
    SupervisedRun,
    TextTarget,
    supervise,
)
from synthetic_game import Click as GameClick
from synthetic_game import GameConfig, Key, ManualClock, SyntheticGame, Truth
from synthetic_game import Press as GamePress
from synthetic_game import SceneType as GameScene
from synthetic_game.anchors import anchor_templates

WIDTH, HEIGHT = 640, 360
WAIT = 10.0
ENTER = 0x1C


async def until[T](probe: Callable[[], T | None], timeout: float = WAIT) -> T:
    async with asyncio.timeout(timeout):
        while (result := probe()) is None:
            await asyncio.sleep(0.015)
    return result


def contains(outer: PixelRect, inner: PixelRect) -> bool:
    return (
        outer.left <= inner.left < inner.right <= outer.right
        and outer.top <= inner.top < inner.bottom <= outer.bottom
    )


class World:
    def __init__(self, adapter: Adapter) -> None:
        self.adapter = adapter
        self.clock = ManualClock()
        self.game = SyntheticGame(
            GameConfig(WIDTH, HEIGHT, text_cps=500, choice_seconds=3.2),
            self.clock,
        )
        self.reads: dict[int, tuple[OcrLine, ...]] = {}
        self.presenting = False
        self.ignore_choice = False
        self.bad_timer = False
        self.timer_override: str | None = None
        self.first_choice_label: str | None = None
        self.freeze_after_input = False
        self.inputs: list[tuple[GameScene, GameClick | GamePress]] = []
        self.submitted_click: tuple[int, int] | None = None

    def rect(self, roi: str) -> PixelRect:
        return self.adapter.roi(roi).to_pixels(WIDTH, HEIGHT)

    def click(self, target: str) -> None:
        rect = next(item.rect for item in self.game.truth().targets if item.id == target)
        self.game.handle(GameClick(*rect.center))

    def show_choice(self, *, timed: bool = False) -> None:
        self.click("menu.start")
        for _ in range(3):
            self.clock.advance(0.5)
            self.click("dialogue.box")
        assert self.game.truth().scene is GameScene.CHOICE
        if timed:
            self.click("choice.0")
            for _ in range(2):
                self.clock.advance(0.5)
                self.click("dialogue.box")
            self.click("popup.ok")
            assert self.game.truth().scene is GameScene.TIMED_CHOICE

    def _text(self, text: str, box: PixelRect) -> OcrLine:
        return OcrLine(text, PixelRect(box.left + 2, box.top + 2, box.right - 2, box.bottom - 2), 1)

    def _lines(self, truth: Truth) -> tuple[OcrLine, ...]:
        lines: list[OcrLine] = []
        for target in truth.targets:
            if target.label:
                rect = target.rect
                label = (
                    self.first_choice_label or target.label
                    if target.id == "choice.0"
                    else target.label
                )
                lines.append(
                    self._text(label, PixelRect(rect.left, rect.top, rect.right, rect.bottom))
                )
        if truth.scene is GameScene.DIALOGUE and truth.visible_text:
            lines.append(self._text(truth.visible_text, self.rect("dialogue")))
        if truth.scene in (GameScene.CHOICE, GameScene.TIMED_CHOICE):
            lines.append(self._text(truth.text, self.rect("choice-prompt")))
        if truth.timer_remaining is not None:
            seconds = math.ceil(truth.timer_remaining * 10) / 10
            timing = self.timer_override or (
                "无法识别" if self.bad_timer else f"剩余 {seconds:.1f} 秒"
            )
            lines.append(self._text(timing, self.rect("timer-text")))
        return tuple(lines)

    async def present(self, display: FakeDisplay) -> None:
        last = time.perf_counter()
        while True:
            now = time.perf_counter()
            self.clock.advance(now - last)
            last = now
            if self.presenting:
                frame = self.game.frame()
                captured = time.perf_counter_ns()
                self.reads[captured] = self._lines(self.game.truth())
                display.present(frame.data, WIDTH, HEIGHT, capture_ns=captured)
            await asyncio.sleep(0.035)

    def input(self, event: DeviceEvent) -> None:
        match event.event:
            case models.FakeMouseDown(button=models.MouseButton.LEFT):
                assert self.submitted_click is not None
                action = GameClick(*self.submitted_click)
            case models.FakeKeyDown(scan_code=code) if code == ENTER:
                action = GamePress(Key.ENTER)
            case _:
                return
        screen = self.game.truth().scene
        self.inputs.append((screen, action))
        if self.freeze_after_input:
            self.presenting = False
        if self.ignore_choice and screen in (GameScene.CHOICE, GameScene.TIMED_CHOICE):
            return
        self.game.handle(action)


def ocr_for(world: World) -> type[Plugin[OcrConfig]]:
    class TestOcrService(OcrService):
        def _read(self, frame: FrameLike, rect: PixelRect) -> tuple[OcrLine, ...]:
            lines = world.reads.get(frame.capture_ns, ()) if isinstance(frame, RingFrame) else ()
            return tuple(line for line in lines if contains(rect, line.box))

        def read_lines(self, frame: FrameLike, rect: PixelRect) -> list[str]:
            return [line.text for line in self._read(frame, rect)]

        async def recognize(self, frame: FrameLike, roi: str) -> OcrResult:
            rect = self.adapter.roi(roi).to_pixels(frame.width, frame.height)
            return OcrResult(
                frame.frame_number, frame.transform_version, roi, rect, self._read(frame, rect)
            )

        async def recognize_rect(self, frame: FrameLike, rect: PixelRect) -> OcrResult:
            return OcrResult(
                frame.frame_number, frame.transform_version, None, rect, self._read(frame, rect)
            )

    class TestOcr(Plugin[OcrConfig]):
        manifest = Manifest(
            id="test-ocr",
            version="1",
            tier=Tier.ORDINARY,
            provides=(OCR.ref,),
            config=OcrConfig,
        )

        async def activate(self, ctx: PluginContext[OcrConfig]) -> None:
            ocr = TestOcrService(world.adapter, ScriptedRecognizer(), model="test")
            ctx.register(ocr.aclose)
            ctx.provide(OCR, ocr)

    return TestOcr


class Journey:
    """只给合成夹具的可核对提议，控制器不知道夹具的剧情或输入结果。"""

    async def propose(self, inputs: PolicyInput) -> Plan:
        if inputs.decision is not None:
            return Plan(ranked_options=(1, 0))
        match inputs.scene.screen_state.classification.screen_type:
            case ScreenType.MENU:
                assert any(line.text == "开始" for line in inputs.ocr)
                return Plan(
                    steps=(
                        StepProposal(
                            Category.NAVIGATE,
                            Click(TextTarget("开始", "menu-start")),
                            ExpectedEffect(EffectKind.SCREEN, screen_type=ScreenType.DIALOGUE),
                        ),
                        StepProposal(
                            Category.ADVANCE, Press(ENTER), ExpectedEffect(EffectKind.CHANGED)
                        ),
                    )
                )
            case ScreenType.DIALOGUE:
                return Plan(
                    steps=(
                        StepProposal(
                            Category.ADVANCE, Press(ENTER), ExpectedEffect(EffectKind.CHANGED)
                        ),
                    )
                )
            case ScreenType.UNKNOWN:
                return Plan(
                    steps=(
                        StepProposal(
                            Category.NAVIGATE,
                            Click(TextTarget("确定", "popup-ok")),
                            ExpectedEffect(EffectKind.SCREEN, screen_type=ScreenType.TIMED_CHOICE),
                        ),
                    )
                )
            case _:
                return Plan()


class RecordingPlanner:
    def __init__(self, context: PlannerContext) -> None:
        self.context = context
        self.goals: list[Goal | None] = []
        self.events: list[DecisionEvent | GoalEvent | ObservationReceipt] = []
        self.preview_ns: dict[tuple[str, str], int] = {}

    def current(self) -> PlannerContext:
        return self.context

    def submit_goal(self, goal: Goal | None) -> None:
        self.goals.append(goal)

    def candidates(self, session_id: str, instance_id: str) -> tuple[int, ...]:
        ready_at = self.preview_ns.get((session_id, instance_id))
        if ready_at is None or time.perf_counter_ns() < ready_at:
            return ()
        return (1, 0)

    def on_decision(self, event: DecisionEvent) -> None:
        if event.phase == "preview" and len(event.view.options) >= 2:
            self.preview_ns[event.session_id, event.instance_id] = (
                time.perf_counter_ns() + 150_000_000
            )
        self.events.append(event)

    def on_goal(self, event: GoalEvent) -> None:
        self.events.append(event)

    def on_observation(self, event: ObservationReceipt) -> None:
        self.events.append(event)


def planner_for(planner: Callable[[], RecordingPlanner]) -> type[Plugin[NoConfig]]:
    class FixturePlanner(Plugin[NoConfig]):
        manifest = Manifest(
            id="fixture-planner", version="1", tier=Tier.ORDINARY, provides=(PLANNER.ref,)
        )

        async def activate(self, ctx: PluginContext[NoConfig]) -> None:
            ctx.provide(PLANNER, planner())

    return FixturePlanner


@dataclass
class Rig:
    run: SupervisedRun
    console: OperatorConsole
    device: FakeDevice
    world: World
    events: list[ConsoleEvent]

    async def start(self) -> None:
        while self.console.pending_confirmation is not None:
            await self.console.answer(True)
        await self.console.start()

    def closed(self, by: str, index: int | None) -> bool | None:
        return (
            any(
                event.kind is EventKind.DECISION_CLOSED
                and event.fields["by"] == by
                and event.fields["index"] == index
                for event in self.events
            )
            or None
        )

    def released(self, reason: models.ReleaseReason) -> bool | None:
        return (
            any(
                isinstance(event.event, models.FakeReleaseAll)
                and event.event.reason is reason
                and not event.held_after
                for event in self.device.events()
            )
            or None
        )


@asynccontextmanager
async def rig(
    core_exe: Path,
    root: Path,
    *,
    restricted: bool = False,
    policy: Policy | None = None,
    freeze_test: bool = False,
    planner: RecordingPlanner | Callable[[], RecordingPlanner] | None = None,
    decision_margin: float = 0.75,
) -> AsyncGenerator[Rig]:
    source = GAMES_DIR / "synthetic-game" / "adapter.toml"
    directory = root / "games" / "synthetic-game"
    directory.mkdir(parents=True)
    risk = 'preconditions = [{ id = "safe", text = "合成夹具已就绪" }]'
    if restricted:
        risk += '\nforbidden_zones = ["menu-start"]'
    content = source.read_text(encoding="utf-8").replace("[risk]\n", f"[risk]\n{risk}\n")
    if freeze_test:
        content = content.replace("press_duration = 0.05", "press_duration = 0.30")
    (directory / "adapter.toml").write_text(content, encoding="utf-8")
    adapter = load_adapter(directory)
    world = World(adapter)
    data = root / "data"
    for template in anchor_templates():
        store_template(data, adapter.id, template.png)

    store = MemorySecretStore()
    console = OperatorConsole(
        adapter, store=store, data_dir=data, options=ConsoleOptions(start_delay=0)
    )
    events: list[ConsoleEvent] = []
    console.add_listener(events.append)
    await console.open()
    loop = asyncio.get_running_loop()

    def input_event(event: DeviceEvent) -> None:
        loop.call_soon_threadsafe(world.input, event)

    class ObservedCoreLink(CoreLink):
        async def submit(
            self,
            grant: ControlGrant,
            steps: Sequence[models.ControlStep],
            *,
            base_frame: int,
            valid_for: float,
            start_delay: float = 0.0,
        ) -> Submission:
            # 假后端的瞬时按下事件没有坐标。保留提交的坐标，并只在收到按下事件后应用。
            for step in steps:
                for action in step.actions:
                    if isinstance(action.root, models.Click):
                        world.submitted_click = (action.root.x, action.root.y)
            return await super().submit(
                grant, steps, base_frame=base_frame, valid_for=valid_for, start_delay=start_delay
            )

    try:
        with (
            FakeDisplay.create(WIDTH, HEIGHT) as display,
            FakeDevice(on_event=input_event) as device,
            pytest.MonkeyPatch.context() as patch,
        ):
            patch.setattr("game_runtime.step_control.host.CoreLink", ObservedCoreLink)
            presenting = asyncio.create_task(world.present(display))
            try:
                async with supervise(
                    adapter.id,
                    policy or Journey(),
                    CoreLinkOptions(
                        core_exe,
                        health_interval=0.05,
                        max_frame_age=0.15 if freeze_test else 0.5,
                        test_device=device.credentials,
                        capture=CaptureConfig(
                            target=display.target, max_width=WIDTH, max_height=HEIGHT
                        ),
                    ),
                    games_dir=directory.parent,
                    data_dir=data,
                    store=store,
                    ocr_plugin=ocr_for(world),
                    step=StepConfig(
                        effect_timeout=0.85,
                        max_frame_age=0.15 if freeze_test else 0.5,
                        action_valid_for=0.45 if freeze_test else 0.25,
                        decision_margin=decision_margin,
                    ),
                    admission=AdmissionPolicy(max_rate=20, idle_interval=0.08),
                    extra_plugins=()
                    if planner is None
                    else (planner_for(planner if callable(planner) else lambda: planner),),
                    extra_entries=()
                    if planner is None
                    else (
                        ResolvedEntry(
                            id="plan",
                            plugin="fixture-planner",
                            config={},
                            disabled=False,
                            grant=frozenset(),
                        ),
                    ),
                ) as running:
                    await device.until_connected()
                    assert running.kernel.status(STEP_ENTRY).state is State.ACTIVE
                    yield Rig(running, console, device, world, events)
            finally:
                presenting.cancel()
                await asyncio.gather(presenting, return_exceptions=True)
    finally:
        await console.close()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return Path("\\\\?\\" + str(tmp_path.resolve())) if sys.platform == "win32" else tmp_path


def test_host_advances_then_resolves_operator_and_timed_choices(core_exe: Path, root: Path) -> None:
    async def main() -> None:
        planner = RecordingPlanner(PlannerContext(None))
        async with rig(core_exe, root, planner=planner) as r:
            controller = r.run.controller
            assert controller is not None
            assert r.console.pending_confirmation is not None
            assert not r.world.inputs
            r.console.set_goal("合成目标")
            await r.start()
            await until(
                lambda: next(
                    (event for event in controller.recent if event.detail == "no-frame"), None
                )
            )
            r.world.presenting = True
            opened = await until(lambda: r.console.current_decision)
            assert opened.deadline_ns is None
            assert any(step.status is StepStatus.VERIFIED for step in controller.recent)
            assert not any(screen is GameScene.CHOICE for screen, _ in r.world.inputs)

            planner.context = PlannerContext(None, paused=True, pause_reason="剧本等待")
            chosen = r.console.choose(0)
            assert chosen.id == opened.id
            await until(
                lambda: next(
                    (item for item in controller.recent if item.detail == "剧本等待"), None
                )
            )
            assert not any(screen is GameScene.CHOICE for screen, _ in r.world.inputs)
            await r.console.pause()
            await until(lambda: r.released(models.ReleaseReason.LEASE_REVOKED))
            planner.context = PlannerContext(None)
            await r.console.start()
            await until(lambda: r.closed("operator", 0))
            await until(lambda: r.closed("default", 1))
            assert r.world.game.truth().node == "roof"
            assert sum(screen is GameScene.TIMED_CHOICE for screen, _ in r.world.inputs) == 1
            assert [goal.text for goal in planner.goals if goal is not None] == ["合成目标"]
            decision_events = [
                event
                for event in planner.events
                if isinstance(event, DecisionEvent) and event.instance_id == opened.id
            ]
            assert [event.phase for event in decision_events] == ["preview", "opened", "confirmed"]
            assert all(event.session_id == r.run.operator.session_id for event in decision_events)
            assert decision_events[-1].index == 0
            [timed_opened] = [
                event
                for event in r.events
                if event.kind is EventKind.DECISION_OPENED
                and event.fields["deadline_ns"] is not None
            ]
            assert not timed_opened.fields["candidates"]
            assert any(
                event.kind is EventKind.DECISION_UPDATED
                and event.fields["id"] == timed_opened.fields["id"]
                and event.fields["candidates"] == [1, 0]
                for event in r.events
            )

            await r.run.kernel.disable(STEP_ENTRY)
            await until(lambda: r.released(models.ReleaseReason.GENERATION_REVOKED))
            assert r.run.controller is None
            assert not r.device.held
            assert not r.device.anomalies

    asyncio.run(main())


def test_real_core_enforces_static_zone_on_the_host_path(core_exe: Path, root: Path) -> None:
    async def main() -> None:
        async with rig(core_exe, root, restricted=True) as r:
            controller = r.run.controller
            assert controller is not None
            r.world.presenting = True
            await r.start()
            rejected = await until(
                lambda: next(
                    (event for event in controller.recent if event.status is StepStatus.REJECTED),
                    None,
                )
            )
            assert "not_allowed" in rejected.detail or "not-allowed" in rejected.detail
            assert not r.world.inputs

    asyncio.run(main())


def test_execution_receipt_without_choice_effect_closes_unknown_once(
    core_exe: Path, root: Path
) -> None:
    async def main() -> None:
        async with rig(core_exe, root) as r:
            r.world.presenting = True
            r.world.ignore_choice = True
            r.console.set_goal("合成授权")
            await r.start()
            opened = await until(lambda: r.console.current_decision)
            r.console.choose(0)
            await until(lambda: r.closed("unknown", None))
            assert r.console.current_decision is None
            [closed] = [event for event in r.events if event.kind is EventKind.DECISION_CLOSED]
            assert closed.fields["executed_at_ns"] is not None
            r.world.first_choice_label = "推門进去"
            r.world.game.handle(GamePress(Key.DOWN))
            await asyncio.sleep(0.2)
            assert sum(screen is GameScene.CHOICE for screen, _ in r.world.inputs) == 1
            assert r.world.game.truth().scene is GameScene.CHOICE
            await r.run.kernel.disable(STEP_ENTRY)
            await r.run.kernel.enable(STEP_ENTRY)
            assert r.run.kernel.status(STEP_ENTRY).state is State.ACTIVE
            await asyncio.sleep(0.2)
            assert r.console.current_decision is None
            assert sum(screen is GameScene.CHOICE for screen, _ in r.world.inputs) == 1
            r.console.set_goal("合成授权")
            reopened = await until(lambda: r.console.current_decision)
            assert reopened.id != opened.id
            assert reopened.options[0].text == "推門进去"
            assert sum(screen is GameScene.CHOICE for screen, _ in r.world.inputs) == 1

    asyncio.run(main())


def test_planning_previews_timed_choice_without_authorizing_input(
    core_exe: Path, root: Path
) -> None:
    async def main() -> None:
        planner = RecordingPlanner(PlannerContext(None, paused=True, planning=True))
        async with rig(core_exe, root, planner=planner) as r:
            r.world.show_choice(timed=True)
            r.console.set_goal("合成授权")
            r.world.presenting = True
            await r.start()
            opened = await until(lambda: r.console.current_decision)
            assert not opened.candidates
            assert opened.deadline_ns is not None
            await until(
                lambda: (
                    r.console.current_decision
                    if r.console.current_decision is not None
                    and r.console.current_decision.candidates == (1, 0)
                    else None
                )
            )
            assert r.console.choose(0).id == opened.id
            await asyncio.sleep(0.12)
            assert not r.world.inputs
            planner.context = PlannerContext(Goal("合成子目标", generation=31))
            await until(lambda: r.closed("operator", 0))
            assert len(r.world.inputs) == 1
            assert [
                item.fields["id"] for item in r.events if item.kind is EventKind.DECISION_OPENED
            ] == [opened.id]

    asyncio.run(main())


def test_new_timer_evidence_preserves_default_against_a_late_operator_reply(
    core_exe: Path, root: Path
) -> None:
    async def main() -> None:
        planner = RecordingPlanner(PlannerContext(None, paused=True, planning=True))
        async with rig(core_exe, root, planner=planner) as r:
            r.world.show_choice(timed=True)
            r.console.set_goal("合成授权")
            r.world.presenting = True
            await r.start()
            opened = await until(lambda: r.console.current_decision)
            original_deadline = opened.deadline_ns
            assert original_deadline is not None
            await until(
                lambda: next(
                    (
                        e
                        for e in r.events
                        if e.kind is EventKind.DECISION_UPDATED and e.fields["candidates"] == [1, 0]
                    ),
                    None,
                )
            )
            r.console.choose(0)
            r.world.timer_override = "剩余 0.6 秒"
            shortened = await until(
                lambda: next(
                    (
                        e
                        for e in r.events
                        if e.kind is EventKind.DECISION_UPDATED
                        and e.fields["deadline_ns"] < original_deadline - 1_000_000_000
                    ),
                    None,
                )
            )
            assert shortened.fields["id"] == opened.id
            assert not r.world.inputs
            planner.context = PlannerContext(Goal("合成子目标", generation=31))
            await until(lambda: r.closed("default", 1))
            assert sum(screen is GameScene.TIMED_CHOICE for screen, _ in r.world.inputs) == 1
            assert r.world.game.truth().node == "roof"

    asyncio.run(main())


@pytest.mark.parametrize("ignored", [False, True])
def test_timed_execution_receipt_and_screen_verification_are_separate(
    core_exe: Path, root: Path, ignored: bool
) -> None:
    async def main() -> None:
        planner = RecordingPlanner(PlannerContext(None))
        async with rig(core_exe, root, planner=planner, decision_margin=0.19) as r:
            r.world.show_choice(timed=True)
            r.world.ignore_choice = ignored
            r.world.presenting = True
            await r.start()
            opened = await until(lambda: r.console.current_decision)
            assert opened.deadline_ns is not None
            await until(
                lambda: r.closed("unknown" if ignored else "default", None if ignored else 1)
            )
            [closed] = [event for event in r.events if event.kind is EventKind.DECISION_CLOSED]
            assert closed.fields["executed_at_ns"] < opened.deadline_ns
            assert closed.at_ns > opened.deadline_ns
            assert sum(screen is GameScene.TIMED_CHOICE for screen, _ in r.world.inputs) == 1
            if ignored:
                assert r.world.game.truth().node == "late"

    asyncio.run(main())


def test_invalid_timer_never_submits_a_selection(core_exe: Path, root: Path) -> None:
    async def main() -> None:
        async with rig(core_exe, root) as r:
            controller = r.run.controller
            assert controller is not None
            r.world.bad_timer = True
            r.world.presenting = True
            await r.start()
            await until(lambda: r.console.current_decision)
            r.console.choose(0)
            await until(lambda: r.closed("operator", 0))
            await until(
                lambda: next(
                    (
                        event
                        for event in controller.recent
                        if event.detail == "invalid-decision-timer"
                    ),
                    None,
                )
            )
            await until(lambda: True if r.world.game.truth().node == "late" else None)
            assert not any(screen is GameScene.TIMED_CHOICE for screen, _ in r.world.inputs)
            assert not any(
                event.kind is EventKind.DECISION_OPENED and event.fields["deadline_ns"] is not None
                for event in r.events
            )

    asyncio.run(main())


@pytest.mark.parametrize("blocked_by", ["scene-changed", "planner-paused"])
def test_delayed_plan_rechecks_the_scene_and_planner_before_submit(
    core_exe: Path, root: Path, blocked_by: str
) -> None:
    class SlowMenu(Journey):
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.finish = asyncio.Event()

        async def propose(self, inputs: PolicyInput) -> Plan:
            if inputs.scene.screen_state.classification.screen_type is ScreenType.MENU:
                self.started.set()
                await self.finish.wait()
            return await super().propose(inputs)

    async def main() -> None:
        policy = SlowMenu()
        planner = RecordingPlanner(PlannerContext(None))
        async with rig(core_exe, root, policy=policy, planner=planner) as r:
            controller = r.run.controller
            assert controller is not None
            r.world.presenting = True
            await r.start()
            await asyncio.wait_for(policy.started.wait(), WAIT)
            if blocked_by == "scene-changed":
                r.world.click("menu.shop")

                def shop_is_visible() -> bool | None:
                    current = controller.scene.latest
                    return True if current is not None and current.state.screen == "shop" else None

                await until(shop_is_visible)
            else:
                planner.context = PlannerContext(None, paused=True, pause_reason="用户等待")
            policy.finish.set()
            await until(
                lambda: next(
                    (event for event in controller.recent if event.detail == blocked_by), None
                )
            )
            assert not r.world.inputs
            assert r.world.game.truth().scene is (
                GameScene.SHOP if blocked_by == "scene-changed" else GameScene.MENU
            )

    asyncio.run(main())


def test_stale_capture_releases_input_and_stops_the_plan(core_exe: Path, root: Path) -> None:
    async def main() -> None:
        async with rig(core_exe, root, freeze_test=True) as r:
            controller = r.run.controller
            assert controller is not None
            r.world.freeze_after_input = True
            r.world.presenting = True
            await r.start()
            await until(lambda: r.released(models.ReleaseReason.FRAME_STALE))
            await until(
                lambda: next(
                    (item for item in controller.recent if item.detail == "stale-frame"), None
                )
            )
            assert len(r.world.inputs) == 1
            assert not r.device.held
            operation = r.device.events()
            released = next(
                e
                for e in operation
                if isinstance(e.event, models.FakeReleaseAll)
                and e.event.reason is models.ReleaseReason.FRAME_STALE
            )
            assert any(
                isinstance(event.event, models.FakeMouseUp) and event.seq < released.seq
                for event in operation
            )

    asyncio.run(main())


def test_planner_goal_and_accepted_observation_keep_their_identity(
    core_exe: Path, root: Path
) -> None:
    class SlowObservation(Journey):
        def __init__(self) -> None:
            self.frame_number: int | None = None

        async def propose(self, inputs: PolicyInput) -> Plan:
            assert inputs.notes == "合成笔记"
            assert inputs.goal is not None
            assert inputs.goal.generation == 7
            self.frame_number = inputs.scene.frame.frame_number
            await asyncio.sleep(0.6)
            candidate = await super().propose(inputs)
            return replace(
                candidate,
                observation=ScreenObservation(screen_type=ScreenType.MENU, confidence=0.9),
                observation_text="菜单中可以开始。",
            )

    async def main() -> None:
        planner = RecordingPlanner(
            PlannerContext(Goal("推进一步", generation=7, max_steps=1), "合成笔记")
        )
        policy = SlowObservation()
        async with rig(core_exe, root, policy=policy, planner=planner) as r:
            r.world.presenting = True
            await r.start()
            await until(
                lambda: next(
                    (
                        event
                        for event in planner.events
                        if isinstance(event, GoalEvent) and event.kind == "step-limit"
                    ),
                    None,
                )
            )
            [observed] = [
                event for event in planner.events if isinstance(event, ObservationReceipt)
            ]
            assert observed.frame_number == policy.frame_number
            assert observed.text == "菜单中可以开始。"
            [limited] = [
                event
                for event in planner.events
                if isinstance(event, GoalEvent) and event.kind == "step-limit"
            ]
            assert (limited.generation, limited.steps) == (7, 1)
            assert len(r.world.inputs) == 1

    asyncio.run(main())


def judged_complete(inputs: PolicyInput) -> Plan:
    assert inputs.goal is not None
    frame = inputs.scene.frame
    return Plan(
        observation=ScreenObservation(
            screen_type=inputs.scene.screen_state.classification.screen_type, confidence=0.9
        ),
        goal_judgment=GoalJudgment(
            completed=True,
            evidence="当前画面显示子目标已完成。",
            generation=inputs.goal.generation,
            frame_number=frame.frame_number,
            transform_version=frame.transform_version,
        ),
    )


def test_agent_judgment_completes_goal_at_step_limit_and_advances_to_next(
    core_exe: Path, root: Path
) -> None:
    first = Goal("核对开场文字", generation=7, max_steps=1, completion_by_agent=True)
    second = Goal(
        "进入对白",
        completion=ExpectedEffect(EffectKind.SCREEN, screen_type=ScreenType.DIALOGUE),
        generation=8,
    )

    class AdvancingPlanner(RecordingPlanner):
        def on_goal(self, event: GoalEvent) -> None:
            super().on_goal(event)
            if (event.generation, event.kind) == (first.generation, "completed"):
                self.context = PlannerContext(second)

    class OpeningText(Journey):
        async def propose(self, inputs: PolicyInput) -> Plan:
            assert inputs.goal == first
            if inputs.scene.screen_state.classification.screen_type is ScreenType.MENU:
                plan = await super().propose(inputs)
                return replace(plan, steps=plan.steps[:1])
            return judged_complete(inputs)

    async def main() -> None:
        planner = AdvancingPlanner(PlannerContext(first))
        async with rig(core_exe, root, policy=OpeningText(), planner=planner) as r:
            r.world.presenting = True
            await r.start()
            await until(lambda: GoalEvent(8, "completed", 0) in planner.events or None)
            assert [event for event in planner.events if isinstance(event, GoalEvent)] == [
                GoalEvent(7, "completed", 1, "当前画面显示子目标已完成。"),
                GoalEvent(8, "completed", 0),
            ]
            assert len(r.world.inputs) == 1

    asyncio.run(main())


@pytest.mark.parametrize(
    ("invalidated_by", "reason"),
    [
        ("goal", "goal-changed"),
        ("scene", "scene-changed"),
        ("frame", "goal-judgment-discarded"),
        ("pause", "planner-paused"),
        ("deterministic", "goal-judgment-discarded"),
    ],
)
def test_delayed_goal_judgment_requires_the_current_goal_and_observation(
    core_exe: Path, root: Path, invalidated_by: str, reason: str
) -> None:
    class DelayedJudgment:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.finish = asyncio.Event()

        async def propose(self, inputs: PolicyInput) -> Plan:
            if self.started.is_set():
                return Plan()
            plan = judged_complete(inputs)
            self.started.set()
            await self.finish.wait()
            if invalidated_by == "frame":
                judgment = plan.goal_judgment
                assert judgment is not None
                assert judgment.frame_number > 1
                plan = replace(
                    plan, goal_judgment=replace(judgment, frame_number=judgment.frame_number - 1)
                )
            return plan

    async def main() -> None:
        goal = (
            Goal(
                "进入对白",
                completion=ExpectedEffect(EffectKind.SCREEN, screen_type=ScreenType.DIALOGUE),
                generation=7,
            )
            if invalidated_by == "deterministic"
            else Goal("核对菜单", generation=7, completion_by_agent=True)
        )
        planner = RecordingPlanner(PlannerContext(goal))
        policy = DelayedJudgment()
        async with rig(core_exe, root, policy=policy, planner=planner) as r:
            controller = r.run.controller
            assert controller is not None
            r.world.presenting = True
            await r.start()
            await asyncio.wait_for(policy.started.wait(), WAIT)
            if invalidated_by == "goal":
                planner.context = PlannerContext(replace(goal, generation=8))
            elif invalidated_by == "scene":
                r.world.click("menu.shop")

                def shop_is_visible() -> bool | None:
                    current = controller.scene.latest
                    return True if current is not None and current.state.screen == "shop" else None

                await until(shop_is_visible)
            elif invalidated_by == "pause":
                planner.context = PlannerContext(goal, paused=True, pause_reason="等待操作员")
            policy.finish.set()
            await until(
                lambda: next((item for item in controller.recent if item.detail == reason), None)
            )
            assert not any(
                isinstance(event, GoalEvent) and event.kind == "completed"
                for event in planner.events
            )
            assert not r.world.inputs

    asyncio.run(main())


def test_losing_a_paused_planner_does_not_fall_back_to_default_policy(
    core_exe: Path, root: Path
) -> None:
    async def main() -> None:
        planner = RecordingPlanner(PlannerContext(None, paused=True, pause_reason="坏结局"))
        async with rig(core_exe, root, planner=planner) as r:
            controller = r.run.controller
            assert controller is not None
            r.world.presenting = True
            await r.start()
            await until(lambda: next((s for s in controller.recent if s.detail == "坏结局"), None))
            await r.run.kernel.disable("plan")
            await until(
                lambda: next(
                    (s for s in controller.recent if s.detail == "planner-unavailable"), None
                )
            )
            await r.run.kernel.disable(STEP_ENTRY)
            await r.run.kernel.enable(STEP_ENTRY)
            await asyncio.sleep(0.7)
            assert not r.world.inputs
            assert r.world.game.truth().scene is GameScene.MENU

    asyncio.run(main())


def test_replaced_planner_cannot_authorize_a_plan_created_before_replacement(
    core_exe: Path, root: Path
) -> None:
    class SlowMenu(Journey):
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.finish = asyncio.Event()

        async def propose(self, inputs: PolicyInput) -> Plan:
            self.started.set()
            await self.finish.wait()
            return await super().propose(inputs)

    async def main() -> None:
        old = RecordingPlanner(PlannerContext(None))
        replacement = RecordingPlanner(PlannerContext(None))
        active = [old]
        policy = SlowMenu()
        async with rig(core_exe, root, policy=policy, planner=lambda: active[0]) as r:
            r.world.presenting = True
            r.console.set_goal("明确继续")
            await r.start()
            await asyncio.wait_for(policy.started.wait(), WAIT)
            await r.run.kernel.disable("plan")
            active[0] = replacement
            await r.run.kernel.enable("plan")
            policy.finish.set()
            await asyncio.sleep(0.7)
            assert not r.world.inputs
            r.console.set_goal("明确继续")
            await until(
                lambda: next((goal for goal in replacement.goals if goal is not None), None)
            )
            await until(lambda: True if r.world.inputs else None)
            assert [g.text for g in replacement.goals if g is not None] == ["明确继续"]

    asyncio.run(main())


def test_controller_reactivation_keeps_the_current_goal_step_budget(
    core_exe: Path, root: Path
) -> None:
    class HoldAfterFirst(Journey):
        def __init__(self) -> None:
            self.waiting = asyncio.Event()
            self.resume = asyncio.Event()

        async def propose(self, inputs: PolicyInput) -> Plan:
            plan = await super().propose(inputs)
            if inputs.scene.screen_state.classification.screen_type is ScreenType.MENU:
                return replace(plan, steps=plan.steps[:1])
            self.waiting.set()
            await self.resume.wait()
            return plan

    async def main() -> None:
        goal = Goal("只推进两步", generation=17, max_steps=2)
        planner = RecordingPlanner(PlannerContext(goal))
        policy = HoldAfterFirst()
        async with rig(core_exe, root, policy=policy, planner=planner) as r:
            r.world.presenting = True
            await r.start()
            await asyncio.wait_for(policy.waiting.wait(), WAIT)
            assert len(r.world.inputs) == 1
            await r.run.kernel.disable(STEP_ENTRY)
            policy.resume.set()
            await r.run.kernel.enable(STEP_ENTRY)
            await until(lambda: GoalEvent(17, "step-limit", 2) in planner.events or None)
            assert len(r.world.inputs) == 2

    asyncio.run(main())


def test_bad_ending_restarts_only_after_new_authorization(core_exe: Path, root: Path) -> None:
    class NoActions:
        async def propose(self, inputs: PolicyInput) -> Plan:
            return Plan()

    async def main() -> None:
        planner = RecordingPlanner(PlannerContext(Goal("旧目标", generation=7), restarting=True))
        async with rig(core_exe, root, policy=NoActions(), planner=planner) as r:
            c = r.run.controller
            assert c is not None
            r.console.set_goal("继续")
            r.world.show_choice()
            r.world.click("choice.2")
            r.world.presenting = True
            await r.start()
            await until(lambda: "screen-blocked" in {item.detail for item in c.recent} or None)
            assert not r.world.inputs
            r.console.set_goal("继续")
            await until(lambda: "screen-verified" in {item.detail for item in c.recent} or None)
            assert r.world.game.truth().scene is GameScene.DIALOGUE
            [(source, click)] = r.world.inputs
            assert source is GameScene.BAD_ENDING
            assert isinstance(click, GameClick)

    asyncio.run(main())
