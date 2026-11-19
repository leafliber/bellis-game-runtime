"""监督运行的完整路径。只有合成游戏、真实实时核、假显示面和假输入设备。"""

import asyncio
import json
import math
import sys
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, JsonValue

from core_protocol import models
from game_runtime.adapter import GAMES_DIR, Adapter, PixelRect, ScreenType, load_adapter
from game_runtime.audio_ring.fake import FakeAudioOutput
from game_runtime.core_link import AudioConfig, CaptureConfig, CoreLink, CoreLinkOptions
from game_runtime.core_link.fake_device import DeviceEvent, FakeDevice
from game_runtime.frame_ring import RingFrame
from game_runtime.frame_ring.fake import FakeDisplay
from game_runtime.frames import AdmissionPolicy
from game_runtime.kernel import (
    ControlGrant,
    ControlRevokedError,
    Manifest,
    NoConfig,
    Plugin,
    PluginContext,
    ReasonCode,
    ResolvedEntry,
    State,
    Submission,
    Tier,
    read_profile,
    resolve_profile,
)
from game_runtime.models import (
    CallMetrics,
    ImageTransform,
    ModelClient,
    ModelImage,
    ModelResult,
    PreparedKeyframe,
    Purpose,
    VlmConfig,
)
from game_runtime.narrative import NARRATIVE_WRITE, NarrativePlugin, RoiPixels
from game_runtime.ocr import OCR, FrameLike, OcrConfig, OcrLine, OcrResult, OcrService
from game_runtime.ocr.fake import ScriptedRecognizer
from game_runtime.operator import ConsoleEvent, ConsoleOptions, EventKind, OperatorConsole
from game_runtime.scene import SceneUpdate, ScreenObservation, store_template
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
from game_runtime.voice_activity import (
    VOICE_ACTIVITY,
    VoiceActivityPlugin,
    VoiceHub,
    VoiceState,
    VoiceStatus,
)
from game_runtime.voice_activity.fake import EnergyModel
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
    def __init__(self, adapter: Adapter, text_cps: float = 500) -> None:
        self.adapter = adapter
        self.clock = ManualClock()
        self.game = SyntheticGame(
            GameConfig(WIDTH, HEIGHT, text_cps=text_cps, choice_seconds=3.2),
            self.clock,
        )
        self.reads: dict[int, tuple[OcrLine, ...]] = {}
        self.extra_lines: tuple[OcrLine, ...] = ()
        self.capture_by_frame: dict[int, int] = {}
        self.transform_version = 1
        self.presenting = False
        self.advance_game_clock = True
        self.ignore_choice = False
        self.ignore_dialogue_inputs = 0
        self.next_submission_fault: Literal["rejected", "dropped", "revoked"] | None = None
        self.bad_timer = False
        self.timer_override: str | None = None
        self.first_choice_label: str | None = None
        self.choice_background_line: OcrLine | None = None
        self.freeze_after_input = False
        self.inputs: list[tuple[GameScene, GameClick | GamePress]] = []
        self.submitted_click: tuple[int, int] | None = None
        self.submitted_click_frame: int | None = None

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
        if truth.scene is GameScene.CHOICE and self.choice_background_line is not None:
            lines.append(self.choice_background_line)
        if truth.timer_remaining is not None:
            seconds = math.ceil(truth.timer_remaining * 10) / 10
            timing = self.timer_override or (
                "无法识别" if self.bad_timer else f"剩余 {seconds:.1f} 秒"
            )
            lines.append(self._text(timing, self.rect("timer-text")))
        return (*lines, *self.extra_lines)

    async def present(self, display: FakeDisplay) -> None:
        last = time.perf_counter()
        while True:
            now = time.perf_counter()
            if self.advance_game_clock:
                self.clock.advance(now - last)
            last = now
            if self.presenting:
                frame = self.game.frame()
                captured = time.perf_counter_ns()
                self.reads[captured] = self._lines(self.game.truth())
                display.present(
                    frame.data,
                    WIDTH,
                    HEIGHT,
                    capture_ns=captured,
                    transform_version=self.transform_version,
                )
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
        if screen is GameScene.DIALOGUE and self.ignore_dialogue_inputs:
            self.ignore_dialogue_inputs -= 1
            return
        self.game.handle(action)


def ocr_for(world: World) -> type[Plugin[OcrConfig]]:
    class TestOcrService(OcrService):
        def _read(self, frame: FrameLike, rect: PixelRect) -> tuple[OcrLine, ...]:
            if isinstance(frame, RingFrame):
                lines = world.reads.get(frame.capture_ns, ())
                return tuple(line for line in lines if contains(rect, line.box))
            if isinstance(frame, RoiPixels):
                lines = world.reads.get(world.capture_by_frame.get(frame.frame_number, 0), ())
                region = world.rect(frame.roi)
                return tuple(
                    replace(
                        line,
                        box=PixelRect(
                            line.box.left - region.left,
                            line.box.top - region.top,
                            line.box.right - region.left,
                            line.box.bottom - region.top,
                        ),
                    )
                    for line in lines
                    if contains(region, line.box)
                )
            return ()

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

        async def recognize_cropped_roi(self, frame: FrameLike, roi: str) -> OcrResult:
            rect = PixelRect(0, 0, frame.width, frame.height)
            return OcrResult(
                frame.frame_number, frame.transform_version, roi, rect, self._read(frame, rect)
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


def voice_for(voice: VoiceHub) -> type[Plugin[NoConfig]]:
    class FixtureVoice(Plugin[NoConfig]):
        manifest = Manifest(
            id="fixture-voice", version="1", tier=Tier.ORDINARY, provides=(VOICE_ACTIVITY.ref,)
        )

        async def activate(self, ctx: PluginContext[NoConfig]) -> None:
            ctx.provide(VOICE_ACTIVITY, voice)

    return FixtureVoice


@dataclass
class Rig:
    run: SupervisedRun
    console: OperatorConsole
    device: FakeDevice
    world: World
    events: list[ConsoleEvent]
    audio: FakeAudioOutput | None = None

    async def start(self) -> None:
        while self.console.pending_confirmation is not None:
            await self.console.answer(True)
        await self.console.start()

    def follow_idle_voice(self, voice: VoiceHub) -> Callable[[], None]:
        controller = self.run.controller
        assert controller is not None

        def publish(update: SceneUpdate) -> None:
            at_ns = update.frame.capture_ns
            voice.publish([], VoiceStatus(VoiceState.IDLE, None, at_ns, at_ns))

        return controller.scene.subscribe(publish)

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
    profile: Sequence[ResolvedEntry] | None = None,
    reading: bool = False,
    fast_paths: bool = True,
    voice: VoiceHub | None = None,
    text_cps: float = 500,
    record: bool = False,
    hold_key: bool = False,
    after_core_start: Callable[[CoreLink], Awaitable[None]] | None = None,
) -> AsyncGenerator[Rig]:
    source = GAMES_DIR / "synthetic-game" / "adapter.toml"
    directory = root / "games" / "synthetic-game"
    directory.mkdir(parents=True)
    risk = 'preconditions = [{ id = "safe", text = "合成夹具已就绪" }]'
    if restricted:
        risk += '\nforbidden_zones = ["menu-start"]'
    content = source.read_text(encoding="utf-8").replace("[risk]\n", f"[risk]\n{risk}\n")
    if not reading and profile is None:
        # 单独策略的非阅读用例独立验证授权、决策与效果。
        before, rest = content.split("[[dialogue_boxes]]", 1)
        content = before + "[pacing]" + rest.split("[pacing]", 1)[1]
    if freeze_test:
        content = content.replace("press_duration = 0.05", "press_duration = 0.30")
    if hold_key:
        content = content.replace("hold_keys = []", "hold_keys = [30]\nmax_hold = 6.0")
    (directory / "adapter.toml").write_text(content, encoding="utf-8")
    adapter = load_adapter(directory)
    world = World(adapter, text_cps)
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
    add_narrative = reading and profile is None
    narrative_plugins = (NarrativePlugin,) if add_narrative else ()
    narrative_entries = (
        (
            ResolvedEntry(
                id="narrative",
                plugin="narrative",
                config={"data_dir": str(data), "ocr_interval": 0.05, "complete_after": 1.0},
                disabled=False,
                grant=frozenset({NARRATIVE_WRITE}),
            ),
        )
        if add_narrative
        else ()
    )
    voice_plugins = () if voice is None else (voice_for(voice),)
    voice_entries = (
        ()
        if voice is None
        else (
            ResolvedEntry(
                id="voice", plugin="fixture-voice", config={}, disabled=False, grant=frozenset()
            ),
        )
    )

    def input_event(event: DeviceEvent) -> None:
        loop.call_soon_threadsafe(world.input, event)

    class ObservedCoreLink(CoreLink):
        async def start(self) -> None:
            await super().start()
            if after_core_start is not None:
                await after_core_start(self)

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
                        world.submitted_click_frame = base_frame
            fault, world.next_submission_fault = world.next_submission_fault, None
            match fault:
                case "rejected":
                    # 重复动作身份使实时核在提交时拒绝整块。
                    steps = (*steps, *steps)
                case "dropped":
                    # 坐标动作依赖的基准帧不存在，实时核接受动作块但不会执行。
                    base_frame = 0
                case "revoked":
                    raise ControlRevokedError("合成用例在发送前报告代次失效")
                case None:
                    pass
            return await super().submit(
                grant, steps, base_frame=base_frame, valid_for=valid_for, start_delay=start_delay
            )

    try:
        with (
            FakeDisplay.create(WIDTH, HEIGHT) as display,
            FakeDevice(on_event=input_event) as device,
            (
                FakeAudioOutput.create(48_000, 2, block_capacity=16, block_count=8)
                if record
                else nullcontext()
            ) as audio,
            pytest.MonkeyPatch.context() as patch,
        ):
            patch.setattr("game_runtime.step_control.host.CoreLink", ObservedCoreLink)
            presenting = asyncio.create_task(world.present(display))
            try:
                async with supervise(
                    adapter.id,
                    (policy or Journey()) if profile is None else None,
                    CoreLinkOptions(
                        core_exe,
                        health_interval=0.05,
                        max_frame_age=0.15 if freeze_test else 0.5,
                        test_device=device.credentials,
                        capture=CaptureConfig(
                            target=display.target, max_width=WIDTH, max_height=HEIGHT
                        ),
                        audio=None
                        if audio is None
                        else AudioConfig(target=audio.target, block_capacity=16, block_count=8),
                    ),
                    games_dir=directory.parent,
                    data_dir=data,
                    store=store,
                    ocr_plugin=ocr_for(world),
                    step=None
                    if profile is not None
                    else StepConfig(
                        effect_timeout=0.85,
                        max_frame_age=0.15 if freeze_test else 0.5,
                        action_valid_for=0.45 if freeze_test else 0.25,
                        decision_margin=decision_margin,
                        fast_paths=fast_paths,
                    ),
                    profile=profile,
                    record=record,
                    name="supervised" if record else None,
                    admission=AdmissionPolicy(max_rate=20, idle_interval=0.08),
                    extra_plugins=narrative_plugins
                    + voice_plugins
                    + (
                        ()
                        if planner is None
                        else (planner_for(planner if callable(planner) else lambda: planner),)
                    ),
                    extra_entries=narrative_entries
                    + voice_entries
                    + (
                        ()
                        if planner is None
                        else (
                            ResolvedEntry(
                                id="plan",
                                plugin="fixture-planner",
                                config={},
                                disabled=False,
                                grant=frozenset(),
                            ),
                        )
                    ),
                ) as running:
                    await device.until_connected()
                    if profile is None:
                        assert running.kernel.status(STEP_ENTRY).state is State.ACTIVE
                    remove = None
                    if reading:
                        controller = running.controller
                        assert controller is not None
                        remove = controller.scene.subscribe(
                            lambda update: world.capture_by_frame.__setitem__(
                                update.frame.frame_number, update.frame.capture_ns
                            )
                        )
                    try:
                        yield Rig(running, console, device, world, events, audio)
                    finally:
                        if remove is not None:
                            remove()
            finally:
                presenting.cancel()
                await asyncio.gather(presenting, return_exceptions=True)
    finally:
        await console.close()


@pytest.mark.parametrize("blocked", [None, "model-grant", "planner-disabled"])
def test_supervised_profile_requires_model_and_planner(
    core_exe: Path, root: Path, monkeypatch: pytest.MonkeyPatch, blocked: str | None
) -> None:
    class FixtureModel(ModelClient):
        def __init__(
            self,
            config: VlmConfig,
            *,
            adapter: Adapter,
            permissions: frozenset[str],
        ) -> None:
            super().__init__(config, adapter=adapter, permissions=permissions)
            self.calls: list[Purpose] = []
            self.grants = permissions
            clients.append(self)

        async def complete[T: BaseModel](
            self,
            purpose: Purpose,
            *,
            instruction: str,
            data: JsonValue,
            output: type[T],
            images: tuple[ModelImage | PreparedKeyframe, ...] = (),
        ) -> ModelResult[T]:
            self.calls.append(purpose)
            transforms: tuple[ImageTransform, ...] = ()
            if purpose is Purpose.REASONING:
                reply = {
                    "subgoals": [{"text": "识别合成菜单"}],
                    "overall_completion": {"kind": "screen", "screen_type": ScreenType.MENU.value},
                }
            elif purpose is Purpose.AGENT:
                (image,) = images
                assert isinstance(image, ModelImage)
                frame = image.frame
                transforms = (
                    ImageTransform(
                        frame.frame_number,
                        frame.transform_version,
                        PixelRect(0, 0, frame.width, frame.height),
                        frame.width,
                        frame.height,
                        "pixels",
                    ),
                )
                reply = {
                    "observation": {"screen_type": ScreenType.MENU.value, "confidence": 1.0},
                    "description": "合成菜单已出现",
                    "goal_evidence": "画面显示合成菜单",
                }
            else:
                assert purpose is Purpose.SUMMARY
                reply = {"summary": "识别到了菜单"}
            metrics = CallMetrics(purpose, "fake", "fake", 0.0, None, None, None, None)
            return ModelResult(output.model_validate_json(json.dumps(reply)), transforms, metrics)

    clients: list[FixtureModel] = []
    monkeypatch.setattr("game_runtime.models.plugin.ModelClient", FixtureModel)
    model_config = VlmConfig.model_validate(
        {purpose.value: {"secret": "launcher-test"} for purpose in Purpose}
    )
    grants = frozenset(model_config.permissions)
    entries = resolve_profile(read_profile(GAMES_DIR.parent / "profiles/supervised.example.json"))
    for index, entry in enumerate(entries):
        match entry.plugin:
            case "ocr":
                entries[index] = replace(entry, plugin="test-ocr")
            case "vlm":
                if blocked == "model-grant":
                    grants -= {"secret:launcher-test"}
                entries[index] = replace(
                    entry, config=model_config.model_dump(mode="json"), grant=grants
                )
            case "planner":
                if blocked == "planner-disabled":
                    entries[index] = replace(entry, disabled=True)
            case "voice-activity":
                entries[index] = replace(entry, disabled=blocked is not None)
            case "step-control":
                entries[index] = replace(entry, id="drive")
            case _:
                pass

    async def main() -> None:
        with FakeAudioOutput.create(48_000, 2, block_capacity=960, block_count=32) as audio:

            def fake_audio_config(adapter: Adapter) -> AudioConfig:
                assert adapter.id == "synthetic-game"
                return AudioConfig(
                    target=audio.target,
                    block_capacity=audio.block_capacity,
                    block_count=32,
                )

            def model_for_voice(_plugin: VoiceActivityPlugin, _data_dir: Path) -> EnergyModel:
                return EnergyModel()

            monkeypatch.setattr("game_runtime.step_control.host.audio_config", fake_audio_config)
            monkeypatch.setattr(VoiceActivityPlugin, "load_model", model_for_voice)
            async with rig(core_exe, root, profile=entries) as r:
                r.world.presenting = True
                drive = r.run.kernel.status("drive")
                assert r.console.pending_confirmation is not None
                assert not r.run.operator.may_autoplay
                assert not r.world.inputs
                assert (r.run.core.audio is not None) is (blocked is None)
                if blocked is not None:
                    assert drive.state is State.WAITING
                    assert r.run.kernel.active_control is None
                    assert r.run.controller is None
                    assert any(reason.subject == PLANNER.ref for reason in drive.reasons)
                    if blocked == "model-grant":
                        vlm = r.run.kernel.status("vlm")
                        assert any(
                            reason.code is ReasonCode.PERMISSION_NOT_GRANTED
                            and reason.subject == "secret:launcher-test"
                            for reason in vlm.reasons
                        )
                        assert not clients
                    else:
                        assert r.run.kernel.status("vlm").state is State.ACTIVE
                        assert [client.calls for client in clients] == [[]]
                    return

                assert drive.state is State.ACTIVE
                assert r.run.kernel.status("narrative").state is State.ACTIVE
                assert r.run.kernel.status("voice").state is State.ACTIVE
                assert len(clients) == 1
                client = clients[0]
                assert client.grants == grants
                assert not client.calls
                await r.start()
                r.console.set_goal("识别菜单")
                await until(lambda: True if Purpose.AGENT in client.calls else None)
                assert Purpose.REASONING in client.calls
                assert not r.world.inputs
                await r.run.kernel.disable("vlm")
                assert r.run.kernel.status("drive").state is State.WAITING
                assert r.run.kernel.active_control is None
                assert r.run.controller is None

    asyncio.run(main())


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return Path("\\\\?\\" + str(tmp_path.resolve())) if sys.platform == "win32" else tmp_path


PHRASE = "开始"
PHRASE_LINE = "请选择开始或返回"
PHRASE_CHOICE_LINE = "覆盖存档后才开始或返回"


def phrase_line(adapter: Adapter, text: str, *, dx: int = 0, dy: int = 0) -> OcrLine:
    roi = adapter.roi("menu-start").to_pixels(WIDTH, HEIGHT)
    box = PixelRect(roi.left + 8 + dx, roi.top + 3 + dy, roi.right - 24 + dx, roi.top + 14 + dy)
    return OcrLine(text, box, 0.99, horizontal=True)


def observe_phrase_crops(
    monkeypatch: pytest.MonkeyPatch,
    ocr: OcrService,
    respond: Callable[[OcrResult], Awaitable[OcrResult]] | None = None,
) -> list[OcrResult]:
    reads: list[OcrResult] = []

    async def recognize_rect(frame: FrameLike, rect: PixelRect) -> OcrResult:
        # 把复读识别框放在估算框偏左的位置，区分两者的中心。
        box = PixelRect(rect.left + 1, rect.top + 1, rect.left + 5, rect.top + 7)
        result = OcrResult(
            frame.frame_number,
            frame.transform_version,
            None,
            rect,
            (OcrLine(PHRASE, box, 0.98, horizontal=True),),
        )
        reads.append(result)
        return result if respond is None else await respond(result)

    monkeypatch.setattr(ocr, "recognize_rect", recognize_rect)
    return reads


class PhraseMenu:
    def __init__(self, text: str, effect: ScreenType = ScreenType.DIALOGUE) -> None:
        self.text = text
        self.effect = effect

    async def propose(self, inputs: PolicyInput) -> Plan:
        if inputs.scene.screen_state.classification.screen_type is not ScreenType.MENU:
            return Plan()
        return Plan(
            steps=(
                StepProposal(
                    Category.NAVIGATE,
                    Click(TextTarget(self.text, "menu-start", point=(0, 0), phrase=PHRASE)),
                    ExpectedEffect(EffectKind.SCREEN, screen_type=self.effect),
                ),
            )
        )


@pytest.mark.parametrize("mode", ["click", "choice", "choice-no-effect"])
def test_phrase_click_uses_the_reread_box_and_choice_uses_a_new_frame(
    core_exe: Path, root: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    async def main() -> None:
        line = PHRASE_LINE if mode == "click" else PHRASE_CHOICE_LINE
        async with rig(core_exe, root, policy=PhraseMenu(line)) as r:
            controller = r.run.controller
            assert controller is not None
            reads = observe_phrase_crops(monkeypatch, controller.ocr)
            r.world.extra_lines = (phrase_line(r.world.adapter, line),)
            r.world.presenting = True
            handle = r.world.game.handle
            await r.start()
            if mode != "click":
                point = await until(lambda: r.console.current_decision)
                assert point.deadline_ns is None
                assert point.prompt == line
                assert [option.text for option in point.options] == [PHRASE]
                assert len(reads) == 1
                assert r.world.submitted_click is None

                r.world.extra_lines = (phrase_line(r.world.adapter, line, dx=12),)
                handle(GamePress(Key.DOWN))
                moved_at = time.perf_counter_ns()
                await until(
                    lambda: (
                        frame
                        if (frame := controller.scene.latest) is not None
                        and frame.frame.capture_ns > moved_at
                        else None
                    )
                )
                if mode == "choice-no-effect":

                    def ignore_input(_action: GameClick | GamePress) -> None:
                        pass

                    monkeypatch.setattr(r.world.game, "handle", ignore_input)
                assert r.console.choose(0).id == point.id

            status = StepStatus.UNKNOWN if mode == "choice-no-effect" else StepStatus.VERIFIED
            result = await until(
                lambda: next((s for s in controller.recent if s.status is status), None)
            )
            final_read = reads[-1]
            (verified_line,) = final_read.lines
            box = verified_line.box
            point_xy = ((box.left + box.right) // 2, (box.top + box.bottom) // 2)
            assert r.world.inputs == [(GameScene.MENU, GameClick(*point_xy))]
            assert r.world.submitted_click_frame == final_read.frame_number
            assert point_xy[0] != (final_read.rect.left + final_read.rect.right) // 2
            if mode == "click":
                assert len(reads) == 1
                assert result.category is Category.NAVIGATE
                return

            assert len(reads) == 2
            first, second = reads
            assert second.frame_number > first.frame_number
            assert second.transform_version == first.transform_version
            assert second.rect.left == first.rect.left + 12
            assert result.category is Category.SELECT
            if mode == "choice":
                await until(lambda: r.closed("operator", 0))
            else:
                await until(lambda: r.closed("unknown", None))
                handle(GamePress(Key.DOWN))
                await until(
                    lambda: next(
                        (
                            s
                            for s in controller.recent
                            if s.detail == "choice-awaiting-authorization"
                        ),
                        None,
                    )
                )
                assert r.console.current_decision is None
                assert len(r.world.inputs) == 1
                assert len(reads) == 2

    asyncio.run(main())


@pytest.mark.parametrize(
    ("failure", "detail", "extra_reads"),
    [
        ("changed-source", "target-not-found", 0),
        ("duplicate-source", "ambiguous-target", 0),
        ("source-confidence", "low-ocr-confidence", 0),
        ("reread-confidence", "low-ocr-confidence", 1),
        ("reread-mismatch", "phrase-ocr-mismatch", 1),
        ("transform-changed", "decision-transform-changed", 0),
        ("reread-timeout", "phrase-ocr-timeout", 1),
    ],
)
def test_phrase_choice_refuses_changed_or_missing_evidence(
    core_exe: Path,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    detail: str,
    extra_reads: int,
) -> None:
    async def main() -> None:
        async with rig(core_exe, root, policy=PhraseMenu(PHRASE_CHOICE_LINE)) as r:
            controller = r.run.controller
            assert controller is not None
            original = phrase_line(r.world.adapter, PHRASE_CHOICE_LINE)
            opened = False

            async def respond(result: OcrResult) -> OcrResult:
                if not opened:
                    return result
                match failure:
                    case "reread-timeout":
                        await asyncio.Event().wait()
                    case "reread-confidence":
                        return replace(result, lines=(replace(result.lines[0], confidence=0.2),))
                    case "reread-mismatch":
                        return replace(result, lines=(replace(result.lines[0], text="开始吧"),))
                    case _:
                        pass
                return result

            reads = observe_phrase_crops(monkeypatch, controller.ocr, respond)
            r.world.extra_lines = (original,)
            r.world.presenting = True
            await r.start()
            point = await until(lambda: r.console.current_decision)
            opened = True
            match failure:
                case "changed-source":
                    r.world.extra_lines = (replace(original, text="覆盖进度后才开始或返回"),)
                case "duplicate-source":
                    r.world.extra_lines = (
                        original,
                        phrase_line(r.world.adapter, original.text, dy=13),
                    )
                case "source-confidence":
                    r.world.extra_lines = (replace(original, confidence=0.2),)
                case "transform-changed":
                    r.world.transform_version += 1
                case _:
                    pass
            changed_at = time.perf_counter_ns()
            await until(
                lambda: (
                    frame
                    if (frame := controller.scene.latest) is not None
                    and frame.frame.capture_ns > changed_at
                    and frame.frame.transform_version == r.world.transform_version
                    else None
                )
            )
            assert r.console.choose(0).id == point.id
            await until(lambda: r.closed("withdrawn", None))
            assert any(
                s.status is StepStatus.REJECTED and s.detail == detail for s in controller.recent
            )
            assert len(reads) == 1 + extra_reads
            assert r.world.submitted_click is None
            assert not r.world.inputs

    asyncio.run(main())


@pytest.mark.parametrize("reason", ["scene-changed", "goal-changed"])
def test_phrase_reread_cannot_submit_after_scene_or_goal_changes(
    core_exe: Path, root: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    async def main() -> None:
        initial = Goal("原授权目标", generation=1)
        planner = RecordingPlanner(PlannerContext(initial))
        reading = asyncio.Event()
        finish = asyncio.Event()

        class Once(PhraseMenu):
            async def propose(self, inputs: PolicyInput) -> Plan:
                return Plan() if reading.is_set() else await super().propose(inputs)

        async def respond(result: OcrResult) -> OcrResult:
            reading.set()
            await finish.wait()
            return result

        async with rig(core_exe, root, policy=Once(PHRASE_LINE), planner=planner) as r:
            controller = r.run.controller
            assert controller is not None
            observe_phrase_crops(monkeypatch, controller.ocr, respond)
            r.world.extra_lines = (phrase_line(r.world.adapter, PHRASE_LINE),)
            r.world.presenting = True
            await r.start()
            await asyncio.wait_for(reading.wait(), WAIT)
            if reason == "scene-changed":
                r.world.click("menu.shop")
                await until(
                    lambda: (
                        frame
                        if (frame := controller.scene.latest) is not None
                        and frame.state.screen == "shop"
                        else None
                    )
                )
            else:
                planner.context = PlannerContext(Goal("新授权目标", generation=2))
            finish.set()
            result = await until(
                lambda: next(
                    (
                        s
                        for s in controller.recent
                        if s.status is StepStatus.REJECTED and s.detail == reason
                    ),
                    None,
                )
            )
            assert result.category is Category.NAVIGATE
            assert r.world.submitted_click is None
            assert not r.world.inputs

    asyncio.run(main())


@pytest.mark.parametrize(
    ("text", "reason", "rereads"),
    [("删除存档后才开始", "target-text-forbidden", 1), ("开始或重新开始", "ambiguous-target", 0)],
)
def test_phrase_rejects_forbidden_source_text_and_repeated_phrase(
    core_exe: Path,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    reason: str,
    rereads: int,
) -> None:
    async def main() -> None:
        async with rig(core_exe, root, policy=PhraseMenu(text)) as r:
            controller = r.run.controller
            assert controller is not None
            reads = observe_phrase_crops(monkeypatch, controller.ocr)
            r.world.extra_lines = (phrase_line(r.world.adapter, text),)
            r.world.presenting = True
            await r.start()
            result = await until(
                lambda: next((s for s in controller.recent if s.detail == reason), None)
            )
            assert result.status is StepStatus.REJECTED
            assert len(reads) == rereads
            assert r.console.current_decision is None
            assert r.world.submitted_click is None
            assert not r.world.inputs

    asyncio.run(main())


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


@pytest.mark.parametrize("fast_paths", [True, False])
def test_reading_pacing_routes_only_a_complete_line_with_current_voice_status(
    core_exe: Path, root: Path, fast_paths: bool
) -> None:
    class VisualStub:
        calls = 0

        async def propose(self, inputs: PolicyInput) -> Plan:
            self.calls += 1
            return Plan(
                steps=(
                    StepProposal(
                        Category.NAVIGATE, Press(ENTER), ExpectedEffect(EffectKind.CHANGED)
                    ),
                )
            )

    async def main() -> None:
        voice = VoiceHub(VoiceStatus(VoiceState.IDLE, None, 0, 0))
        policy = VisualStub()
        planner = RecordingPlanner(PlannerContext(None))
        async with rig(
            core_exe,
            root,
            policy=policy,
            planner=planner,
            reading=True,
            fast_paths=fast_paths,
            voice=voice,
            text_cps=12,
        ) as r:
            c = r.run.controller
            assert c is not None
            r.world.click("menu.start")
            r.world.advance_game_clock = False
            r.world.clock.advance(0.25)
            r.world.presenting = True
            await r.start()

            await until(
                lambda: (
                    any(step.detail == "dialogue-awaiting-complete" for step in c.recent) or None
                )
            )
            assert not r.world.inputs
            assert policy.calls == 0
            assert r.world.game.truth().visible_text != r.world.game.truth().text
            r.world.clock.advance(2.0)
            r.world.advance_game_clock = True
            stamped = await until(
                lambda: next(
                    (
                        step.frame_number
                        for step in c.recent
                        if step.detail == "dialogue-reading" and step.frame_number is not None
                    ),
                    None,
                )
            )
            old_idle = r.world.capture_by_frame[stamped]
            await until(
                lambda: any(step.detail == "dialogue-voice-unknown" for step in c.recent) or None
            )
            await until(
                lambda: (
                    True
                    if (latest := c.scene.latest) is not None and latest.frame.capture_ns > old_idle
                    else None
                )
            )
            voice.publish([], VoiceStatus(VoiceState.IDLE, None, old_idle, old_idle))
            await asyncio.sleep(0.2)
            assert not r.world.inputs
            assert policy.calls == 0
            r.follow_idle_voice(voice)
            await until(lambda: r.world.inputs or None)
            screen, action = r.world.inputs[0]
            assert screen is GameScene.DIALOGUE
            if fast_paths:
                assert policy.calls == 0
                assert isinstance(action, GameClick)
                assert action.x in range(
                    r.world.rect("dialogue").left, r.world.rect("dialogue").right
                )
                [receipt] = [
                    event for event in planner.events if isinstance(event, ObservationReceipt)
                ]
                assert receipt.text == "雨停了，钟楼的指针停在七点。"
                assert receipt.observation.screen_type is ScreenType.DIALOGUE
            else:
                assert policy.calls == 1
                assert action == GamePress(Key.ENTER)

    asyncio.run(main())


def test_dialogue_voice_gate_is_rechecked_after_delayed_visual_policy(
    core_exe: Path, root: Path
) -> None:
    class DelayedVisual:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.respond = asyncio.Event()

        async def propose(self, inputs: PolicyInput) -> Plan:
            self.started.set()
            await self.respond.wait()
            return Plan(
                steps=(
                    StepProposal(
                        Category.NAVIGATE, Press(ENTER), ExpectedEffect(EffectKind.CHANGED)
                    ),
                )
            )

    async def main() -> None:
        voice = VoiceHub(VoiceStatus(VoiceState.IDLE, None, 0, 0))
        policy = DelayedVisual()
        async with rig(
            core_exe,
            root,
            policy=policy,
            reading=True,
            fast_paths=False,
            voice=voice,
        ) as r:
            c = r.run.controller
            assert c is not None
            r.world.click("menu.start")
            r.world.presenting = True
            await r.start()
            await until(
                lambda: any(step.detail == "dialogue-voice-unknown" for step in c.recent) or None
            )
            stop_idle = r.follow_idle_voice(voice)
            await asyncio.wait_for(policy.started.wait(), WAIT)
            stop_idle()
            now = time.perf_counter_ns()
            voice.publish([], VoiceStatus(VoiceState.BUSY, None, now, now))
            policy.respond.set()
            await until(
                lambda: any(step.detail == "dialogue-voice-busy" for step in c.recent) or None
            )
            assert not r.world.inputs
            r.follow_idle_voice(voice)
            await until(lambda: r.world.inputs or None)
            assert r.world.inputs[0] == (GameScene.DIALOGUE, GamePress(Key.ENTER))

    asyncio.run(main())


def test_dialogue_fast_path_yields_to_an_agent_judged_goal(core_exe: Path, root: Path) -> None:
    class Judge:
        async def propose(self, inputs: PolicyInput) -> Plan:
            return judged_complete(inputs)

    async def main() -> None:
        voice = VoiceHub(VoiceStatus(VoiceState.IDLE, None, 0, 0))
        planner = RecordingPlanner(
            PlannerContext(Goal("核对合成开场", generation=41, completion_by_agent=True))
        )
        async with rig(
            core_exe, root, policy=Judge(), planner=planner, reading=True, voice=voice
        ) as r:
            c = r.run.controller
            assert c is not None
            r.world.click("menu.start")
            r.world.presenting = True
            await r.start()
            await until(
                lambda: any(step.detail == "dialogue-voice-unknown" for step in c.recent) or None
            )
            r.follow_idle_voice(voice)
            await until(
                lambda: next(
                    (
                        event
                        for event in planner.events
                        if isinstance(event, GoalEvent) and event.kind == "completed"
                    ),
                    None,
                )
            )
            assert not r.world.inputs

    asyncio.run(main())


@pytest.mark.parametrize(
    ("fault", "reason"),
    [("rejected", "invalid"), ("dropped", "geometry_changed"), ("revoked", "producer-revoked")],
)
def test_fast_dialogue_can_retry_a_submission_proven_not_executed(
    core_exe: Path, root: Path, fault: Literal["rejected", "dropped", "revoked"], reason: str
) -> None:
    async def main() -> None:
        voice = VoiceHub()
        async with rig(core_exe, root, reading=True, voice=voice) as r:
            c = r.run.controller
            assert c is not None
            r.world.next_submission_fault = fault
            r.world.click("menu.start")
            r.follow_idle_voice(voice)
            r.world.presenting = True
            await r.start()
            await until(
                lambda: (
                    any(
                        item.status is StepStatus.REJECTED and item.detail == reason
                        for item in c.recent
                    )
                    or None
                )
            )
            await until(lambda: r.world.inputs or None, timeout=3)
            assert len(r.world.inputs) == 1
            assert r.world.inputs[0][0] is GameScene.DIALOGUE
            await until(
                lambda: any(item.status is StepStatus.VERIFIED for item in c.recent) or None
            )

    asyncio.run(main())


def test_fast_dialogue_verifies_the_choice_overlay_with_the_old_text_still_present(
    core_exe: Path, root: Path
) -> None:
    async def main() -> None:
        voice = VoiceHub()
        async with rig(core_exe, root, reading=True, voice=voice) as r:
            controller = r.run.controller
            assert controller is not None
            world = r.world
            world.click("menu.start")
            for _ in range(2):
                world.clock.advance(0.5)
                world.click("dialogue.box")
            world.clock.advance(0.5)
            before = world.game.truth()
            assert before.scene is GameScene.DIALOGUE
            assert before.visible_text
            world.choice_background_line = world._text(  # pyright: ignore[reportPrivateUsage]
                before.visible_text, world.rect("dialogue")
            )
            r.follow_idle_voice(voice)
            world.presenting = True
            await r.start()
            await until(
                lambda: (
                    any(
                        step.status is StepStatus.VERIFIED and step.category is Category.ADVANCE
                        for step in controller.recent
                    )
                    or None
                )
            )
            after = controller.scene.latest
            assert after is not None
            assert after.state.screen == "choice"
            text = await controller.ocr.recognize(after.frame, "dialogue")
            assert text.text == before.visible_text
            [(screen, action)] = world.inputs
            assert screen is GameScene.DIALOGUE
            assert isinstance(action, GameClick)

    asyncio.run(main())


@pytest.mark.parametrize("outcome", ["no-effect", "unknown", "retry-rejected"])
def test_fast_dialogue_retries_no_effect_once_and_holds_unknown(
    core_exe: Path, root: Path, outcome: str
) -> None:
    async def main() -> None:
        voice = VoiceHub()
        async with rig(core_exe, root, reading=True, voice=voice) as r:
            c = r.run.controller
            assert c is not None
            r.world.ignore_dialogue_inputs = 2
            r.world.freeze_after_input = outcome == "unknown"
            r.world.click("menu.start")
            r.follow_idle_voice(voice)
            r.world.presenting = True
            await r.start()
            if outcome == "unknown":
                await until(
                    lambda: any(item.status is StepStatus.UNKNOWN for item in c.recent) or None
                )
                r.world.freeze_after_input = False
                r.world.presenting = True
            elif outcome == "retry-rejected":
                await until(lambda: r.world.inputs or None)
                r.world.next_submission_fault = "rejected"
                await until(
                    lambda: any(item.status is StepStatus.REJECTED for item in c.recent) or None
                )
            else:
                await until(
                    lambda: (
                        sum(item.status is StepStatus.NO_EFFECT for item in c.recent) == 2 or None
                    ),
                    timeout=4,
                )
            await until(
                lambda: (
                    any(item.detail == "dialogue-awaiting-next-line" for item in c.recent) or None
                )
            )
            assert len(r.world.inputs) == (2 if outcome == "no-effect" else 1)
            assert r.world.game.truth().text == "雨停了，钟楼的指针停在七点。"

    asyncio.run(main())
