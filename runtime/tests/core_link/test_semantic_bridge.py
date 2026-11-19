"""语义桥经真实本机通道、实时核、合成画面和假设备。"""

import asyncio
import json
import math
import os
import secrets
import subprocess
import sys
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

import pytest

from core_protocol import models
from game_runtime.adapter import GAMES_DIR, Adapter, PixelRect, ScreenType, load_adapter
from game_runtime.channel import (
    ChannelError,
    Connection,
    RpcError,
    RpcErrorCode,
    RpcPeer,
    connect,
    listen,
    session_credentials,
)
from game_runtime.core_link import CaptureConfig, CoreLinkOptions
from game_runtime.core_link.fake_device import DeviceEvent, FakeDevice
from game_runtime.frame_ring import RingFrame
from game_runtime.frame_ring.fake import FakeDisplay
from game_runtime.frames import AdmissionPolicy
from game_runtime.game_bridge.projection import voice_activity_at
from game_runtime.game_bridge.session import SESSION_ENTRY, BellisConfig, BellisSession
from game_runtime.game_bridge.wire import decode_request, ms, ns, v1
from game_runtime.kernel import Manifest, Plugin, PluginContext, ResolvedEntry, Tier
from game_runtime.ocr import OCR, FrameLike, OcrConfig, OcrLine, OcrResult, OcrService
from game_runtime.ocr.fake import ScriptedRecognizer
from game_runtime.operator import MonotonicClock
from game_runtime.recording import Recording
from game_runtime.recording.layout import read_meta
from game_runtime.scene import SceneHub, SceneUpdate, store_template
from game_runtime.secret_store import MemorySecretStore, SessionInfo
from game_runtime.step_control import (
    Category,
    Click,
    EffectKind,
    ExpectedEffect,
    Goal,
    Plan,
    PlannerContext,
    Policy,
    PolicyInput,
    StepConfig,
    StepProposal,
    StepStatus,
    SupervisedRun,
    TextTarget,
    supervise,
)
from game_runtime.step_control.decisions import PendingDecision
from game_runtime.step_control.plugin import SessionSignals
from game_runtime.step_control.risk import ActionRefusedError
from game_runtime.telemetry import RuntimeTelemetry, SpanContext, TelemetrySpan
from game_runtime.telemetry.exporter import SpanRecord
from game_runtime.voice_activity import VoiceState, VoiceStatus
from synthetic_game.anchors import anchor_templates
from synthetic_game.layout import Layout, Rect
from synthetic_game.render import Button, ChoiceView, DialogueView, MenuView, Renderer, View

from .test_step_control import (
    HEIGHT,
    PHRASE,
    PHRASE_CHOICE_LINE,
    WAIT,
    WIDTH,
    PhraseMenu,
    RecordingPlanner,
    observe_phrase_crops,
    phrase_line,
    planner_for,
    until,
)

_TRACE = "00-11111111111111111111111111111111-2222222222222222-01"
_DECISION_TRACE = "00-11111111111111111111111111111111-3333333333333333-01"


@pytest.mark.parametrize("literal", [b"-0", b"1.0", b"1e0"])
def test_lexical_integers_are_checked_on_the_raw_game_request(literal: bytes) -> None:
    raw = b'{"jsonrpc":"2.0","id":1,"method":"game.describe","params":{"major":' + literal + b"}}"
    with pytest.raises(ChannelError, match="游戏桥 JSON 无效"):
        decode_request(raw)


@pytest.mark.parametrize(
    ("processed", "actual"), [(99, "unknown"), (110, "idle"), (121, "unknown")]
)
def test_voice_report_uses_current_frame_watermark(processed: int, actual: str) -> None:
    status = VoiceStatus(VoiceState.IDLE, None, 95, processed)
    assert voice_activity_at(status, 100, 120, 30) == actual


def test_hold_window_caps_overlapping_requests_and_cannot_be_replayed() -> None:
    class Clock:
        now = ns(1_000_000)

        def now_ns(self) -> int:
            return self.now

        async def sleep_until(self, deadline_ns: int) -> None:
            self.now = max(self.now, deadline_ns)

    async def run() -> None:
        clock = Clock()
        peers: asyncio.Queue[Connection] = asyncio.Queue()
        store = MemorySecretStore()
        with session_credentials("pacingtest") as credentials:
            server = await listen(
                credentials.path,
                credentials.key,
                on_connection=peers.put_nowait,
                on_rejected=lambda _: None,
                on_error=lambda error: pytest.fail(str(error)),
            )
            info = SessionInfo.create(
                credentials, "pacingtest", launcher_pid=os.getpid(), started_at=datetime.now(UTC)
            )
            store.set(SESSION_ENTRY, info.to_json())
            main = RpcPeer(
                await connect(credentials.path, "game-runtime.main", credentials.key),
                timeout=WAIT,
                json_decoder=decode_request,
            )
            bellis = RpcPeer(await asyncio.wait_for(peers.get(), WAIT), timeout=WAIT)
            session = BellisSession(
                BellisConfig(store),
                info,
                "pacing-run",
                load_adapter(GAMES_DIR / "synthetic-game"),
                clock,
                SessionSignals(),
            )
            session.attach(main)
            try:
                await bellis.request("game.describe", {"major": 1})
                with pytest.raises(RpcError) as rejected:
                    await bellis.request("lease.renew", {"token": 1, "expiresAtMs": 1_000_500})
                assert rejected.value.code == RpcErrorCode.METHOD_NOT_FOUND
                hold = {
                    "holdId": "one",
                    "reason": "解说",
                    "expiresAtMs": 1_060_000,
                    "traceparent": None,
                }
                assert await bellis.request("pacing.hold", hold) == {}
                clock.now += ns(15_000)
                assert await bellis.request("pacing.hold", hold | {"holdId": "two"}) == {}
                clock.now += ns(14_999)
                assert session.progress_held()
                clock.now += ns(1)
                assert not session.progress_held()
                assert await bellis.request("pacing.hold", hold) == {}
                assert not session.progress_held()
            finally:
                await bellis.close()
                await main.close()
                await server.close()

    asyncio.run(run())


@pytest.fixture
def semantic_core(request: pytest.FixtureRequest) -> Path:
    # 两条开发分支合流前，定向测试可用已构建的控制连接核。
    if path := os.environ.get("GAME_RUNTIME_SEMANTIC_TEST_CORE"):
        return Path(path).resolve(strict=True)
    core = cast(Path, request.getfixturevalue("core_exe"))
    help_text = subprocess.run([str(core), "--help"], capture_output=True, check=False).stderr
    if b"--bellis-control" not in help_text:
        pytest.skip("等待实时核控制连接合流")
    return core


class Frames:
    def __init__(self, adapter: Adapter) -> None:
        self.adapter = adapter
        self.layout = Layout(WIDTH, HEIGHT)
        self.renderer = Renderer(WIDTH, HEIGHT)
        self.scene = "menu"
        self.menu_selection = 0
        self.timer_end = 0.0
        self.ignore_choice = False
        self.down: list[str] = []
        self.known: dict[tuple[str, int], bytes] = {}
        self.recognized: dict[bytes, tuple[OcrLine, ...]] = {}

    def _line(self, text: str, rect: Rect | PixelRect) -> OcrLine:
        return OcrLine(
            text, PixelRect(rect.left + 2, rect.top + 2, rect.right - 2, rect.bottom - 2), 1.0
        )

    def render(self) -> bytes:
        tenths = max(0, math.ceil((self.timer_end - time.perf_counter()) * 10))
        variant = {"menu": self.menu_selection, "timed": tenths}.get(self.scene, 0)
        key = self.scene, variant
        if key in self.known:
            return self.known[key]
        lines: list[OcrLine] = []
        if self.scene == "menu":
            buttons = tuple(
                Button(self.layout.menu_button(i), label, i == self.menu_selection)
                for i, label in enumerate(("开始", "商店", "说明"))
            )
            view = View(MenuView("合成测试", buttons), None)
            lines.extend(self._line(button.label, button.rect) for button in buttons)
        elif self.scene in {"choice", "timed"}:
            buttons = tuple(
                Button(self.layout.choice_option(i), label, i == 0)
                for i, label in enumerate(("推门进去", "绕到后门"))
            )
            prompt = "该怎么走？"
            timer = f"剩余 {tenths / 10:.1f} 秒" if self.scene == "timed" else None
            fraction = min(1.0, tenths / 32) if timer is not None else None
            view = View(ChoiceView(prompt, buttons, timer, fraction), None)
            lines.append(self._line(prompt, self.layout.choice_prompt()))
            lines.extend(self._line(button.label, button.rect) for button in buttons)
            if timer is not None:
                lines.append(
                    self._line(timer, self.adapter.roi("timer-text").to_pixels(WIDTH, HEIGHT))
                )
        else:
            view = View(DialogueView("roof", "合成", "已走到下一幕。", True), None)
        pixels = self.renderer.render(view)
        self.recognized[pixels] = tuple(lines)
        self.known[key] = pixels
        return pixels

    def input(self, event: DeviceEvent) -> None:
        if isinstance(event.event, models.FakeMouseDown):
            self.down.append(self.scene)
            if self.scene == "menu":
                self.scene = "choice"
            elif not self.ignore_choice:
                self.scene = "dialogue"

    async def present(self, display: FakeDisplay) -> None:
        while True:
            display.present(self.render(), WIDTH, HEIGHT, capture_ns=time.perf_counter_ns())
            await asyncio.sleep(0.033)


class FixturePolicy:
    async def propose(self, inputs: PolicyInput) -> Plan:
        if inputs.scene.screen_state.classification.screen_type is not ScreenType.MENU:
            return Plan()
        return Plan(
            steps=(
                StepProposal(
                    Category.NAVIGATE,
                    Click(TextTarget("开始", "menu-start")),
                    ExpectedEffect(EffectKind.SCREEN, screen_type=ScreenType.CHOICE),
                ),
            )
        )


class RejectionPolicy(FixturePolicy):
    reject = True

    async def propose(self, inputs: PolicyInput) -> Plan:
        if self.reject and inputs.scene.screen_state.classification.screen_type is ScreenType.MENU:
            return Plan(
                steps=(
                    StepProposal(
                        Category.NAVIGATE,
                        Click(TextTarget("不存在的目标", "menu-start")),
                        ExpectedEffect(EffectKind.SCREEN, screen_type=ScreenType.CHOICE),
                    ),
                )
            )
        return await super().propose(inputs)


class GoalFollowingPlanner(RecordingPlanner):
    def submit_goal(self, goal: Goal | None) -> None:
        super().submit_goal(goal)
        self.context = PlannerContext(goal)


def ocr_for(frames: Frames) -> type[Plugin[OcrConfig]]:
    class PixelOcr(OcrService):
        def lines(self, frame: FrameLike, rect: PixelRect) -> tuple[OcrLine, ...]:
            candidates = (
                frames.recognized.get(bytes(frame.pixels), ())
                if isinstance(frame, RingFrame)
                else ()
            )
            return tuple(
                line
                for line in candidates
                if rect.left <= line.box.left < line.box.right <= rect.right
                and rect.top <= line.box.top < line.box.bottom <= rect.bottom
            )

        def read_lines(self, frame: FrameLike, rect: PixelRect) -> list[str]:
            return [line.text for line in self.lines(frame, rect)]

        async def recognize(self, frame: FrameLike, roi: str) -> OcrResult:
            rect = self.adapter.roi(roi).to_pixels(frame.width, frame.height)
            return OcrResult(
                frame.frame_number, frame.transform_version, roi, rect, self.lines(frame, rect)
            )

    class Ocr(Plugin[OcrConfig]):
        manifest = Manifest(
            id="semantic-ocr",
            version="1",
            tier=Tier.ORDINARY,
            provides=(OCR.ref,),
            config=OcrConfig,
        )

        async def activate(self, ctx: PluginContext[OcrConfig]) -> None:
            ocr = PixelOcr(frames.adapter, ScriptedRecognizer(), model="test")
            ctx.register(ocr.aclose)
            ctx.provide(OCR, ocr)

    return Ocr


@dataclass
class Events:
    health: list[v1.GameMainHealth]
    points: list[v1.GameDecisionPointOpened]
    actions: list[v1.GameActionCommitted]
    situations: list[v1.GameSituationObserved]


@dataclass
class Rig:
    run: SupervisedRun
    frames: Frames
    device: FakeDevice
    peers: asyncio.Queue[Connection]
    store: MemorySecretStore
    planner: RecordingPlanner

    async def connect(self, *, first: bool) -> tuple[RpcPeer, RpcPeer | None, Events]:
        links: dict[str, RpcPeer] = {}
        for _ in range(2 if first else 1):
            conn = await asyncio.wait_for(self.peers.get(), WAIT)
            links[conn.role] = RpcPeer(conn, timeout=WAIT)
        main = links["game-runtime.main"]
        core = links.get("game-runtime.core")
        events = Events([], [], [], [])
        main.on_notification(
            "health.reported",
            lambda value: events.health.append(v1.GameMainHealth.model_validate(value)),
        )
        main.on_notification(
            "decision_point.opened",
            lambda value: events.points.append(v1.GameDecisionPointOpened.model_validate(value)),
        )
        main.on_notification(
            "action.committed",
            lambda value: events.actions.append(v1.GameActionCommitted.model_validate(value)),
        )
        main.on_notification(
            "situation.observed",
            lambda value: events.situations.append(v1.GameSituationObserved.model_validate(value)),
        )
        main_desc = v1.GameMainDescription.model_validate(
            await main.request("game.describe", {"major": 1})
        )
        assert main_desc.runtimeId == self.session.runtime_id
        assert [(p.id, p.confirmed) for p in main_desc.prerequisites] == [("safe", True)]
        if core is not None:
            desc = v1.GameCoreDescription.model_validate(
                await core.request("game.describe", {"major": 1})
            )
            assert desc.runtimeId == main_desc.runtimeId
        return main, core, events

    @property
    def session(self) -> BellisSession:
        assert isinstance(self.run.operator, BellisSession)
        return self.run.operator


@asynccontextmanager
async def rig(
    exe: Path,
    folder: Path,
    *,
    policy: Policy | None = None,
    planner: RecordingPlanner | None = None,
    telemetry: Path | None = None,
    record: bool = False,
) -> AsyncGenerator[Rig]:
    if sys.platform == "win32":
        folder = Path("\\\\?\\" + str(folder.resolve()))
    source = GAMES_DIR / "synthetic-game" / "adapter.toml"
    directory = folder / "games/synthetic-game"
    directory.mkdir(parents=True)
    content = source.read_text(encoding="utf-8").replace(
        "[risk]\n", '[risk]\npreconditions = [{id = "safe", text = "假设备已就绪"}]\n'
    )
    (directory / "adapter.toml").write_text(content, encoding="utf-8")
    adapter = load_adapter(directory)
    for template in anchor_templates():
        store_template(folder / "data", adapter.id, template.png)
    frames = Frames(adapter)
    store = MemorySecretStore()
    peers: asyncio.Queue[Connection] = asyncio.Queue()
    session_id = f"test{secrets.token_hex(4)}"
    active_planner = planner or RecordingPlanner(PlannerContext(None))
    extra_entries = [
        ResolvedEntry("plan", "fixture-planner", {}, False, frozenset()),
    ]
    if telemetry is not None:
        extra_entries.append(
            ResolvedEntry(
                "telemetry", "telemetry", {"directory": str(telemetry)}, False, frozenset()
            )
        )
    loop = asyncio.get_running_loop()

    def input_event(event: DeviceEvent) -> None:
        loop.call_soon_threadsafe(frames.input, event)

    with (
        session_credentials(session_id) as credentials,
        FakeDisplay.create(WIDTH, HEIGHT) as display,
        FakeDevice(on_event=input_event) as device,
    ):
        server = await listen(
            credentials.path,
            credentials.key,
            on_connection=peers.put_nowait,
            on_rejected=lambda _: None,
            on_error=lambda error: pytest.fail(str(error)),
        )
        info = SessionInfo.create(
            credentials, session_id, launcher_pid=os.getpid(), started_at=datetime.now(UTC)
        )
        info_file = folder / "session-info.json"
        info_file.write_text(info.to_json(), encoding="utf-8")
        store.set(SESSION_ENTRY, info.to_json())
        presenting = asyncio.create_task(frames.present(display))
        try:
            async with supervise(
                adapter.id,
                policy or FixturePolicy(),
                CoreLinkOptions(
                    exe,
                    health_interval=0.05,
                    test_device=device.credentials,
                    capture=CaptureConfig(
                        target=display.target, max_width=WIDTH, max_height=HEIGHT
                    ),
                ),
                data_dir=folder / "data",
                store=MemorySecretStore(),
                games_dir=directory.parent,
                step=StepConfig(effect_timeout=0.55, decision_margin=0.7),
                admission=AdmissionPolicy(max_rate=20, idle_interval=0.06),
                ocr_plugin=ocr_for(frames),
                extra_plugins=(planner_for(lambda: active_planner),),
                extra_entries=extra_entries,
                record=record,
                bellis=BellisConfig(store, frozenset({"safe"}), info_file),
            ) as run:
                await device.until_connected()
                yield Rig(run, frames, device, peers, store, active_planner)
        finally:
            presenting.cancel()
            await asyncio.gather(presenting, return_exceptions=True)
            await server.close()


def now_ms() -> int:
    return ms(time.perf_counter_ns())


def goal(id: str, ttl: int, *, vote: str | None = None) -> dict[str, object]:
    return {
        "goalId": id,
        "objective": "测试授权目标",
        "constraints": ["只执行已核验的导航"],
        "mode": "autonomous" if vote is None else "waiting_vote",
        "decisionPointId": vote,
        "expiresAtMs": now_ms() + ttl,
        "traceparent": _TRACE,
    }


async def renew(core: RpcPeer, token: int) -> None:
    while True:
        assert (
            await core.request("lease.renew", {"token": token, "expiresAtMs": now_ms() + 450}) == {}
        )
        await asyncio.sleep(0.10)


async def ready(r: Rig, events: Events) -> None:
    try:
        await until(lambda: next((item for item in events.health if item.ready), None))
    except TimeoutError:
        pytest.fail(
            f"health={events.health[-1:]}, core={r.run.core.last_health}, "
            f"plugins={r.run.kernel.snapshot()}, frame={bool(r.frames.known)}"
        )
    assert r.run.core.last_health is not None


async def refused(peer: RpcPeer, method: str, fields: dict[str, object]) -> None:
    with pytest.raises(RpcError) as rejected:
        await peer.request(method, fields)
    assert rejected.value.code == RpcErrorCode.INVALID_PARAMS


def test_bellis_risk_projects_rejections_and_recovers_on_authorized_goal(
    semantic_core: Path, tmp_path: Path
) -> None:
    async def run() -> None:
        policy = RejectionPolicy()
        planner = GoalFollowingPlanner(PlannerContext(None))
        async with rig(semantic_core, tmp_path, policy=policy, planner=planner) as r:
            main, core, events = await r.connect(first=True)
            assert core is not None

            async def risk_at(score: float, goal_id: str | None) -> v1.GameMainHealth:
                return await until(
                    lambda: next(
                        (
                            item
                            for item in events.health[-1:]
                            if item.ready and item.goalId == goal_id and item.riskScore == score
                        ),
                        None,
                    )
                )

            renewal = asyncio.create_task(renew(core, 1))
            try:
                initial = await risk_at(0.0, None)
                assert not initial.riskLimitExceeded
                assert await main.request("goal.submit", goal("first", 30_000)) == {}
                rejected = await risk_at(1 / 3, "first")
                assert rejected.lastRejection is not None
                assert rejected.lastRejection.code == "target-not-found"
                assert not r.frames.down

                policy.reject = False
                r.frames.menu_selection = 1
                await until(lambda: events.actions[0] if events.actions else None)
                await risk_at(0.0, "first")
                assert r.frames.down == ["menu"]

                policy.reject = True
                r.frames.scene = "menu"
                await risk_at(1 / 3, "first")
                r.frames.menu_selection = 2
                await risk_at(2 / 3, "first")
                r.frames.menu_selection = 0
                capped = await risk_at(1.0, "first")
                assert not capped.riskLimitExceeded
                stuck = await until(
                    lambda: next(
                        (item for item in events.situations if item.screenType == "卡住"), None
                    )
                )
                assert stuck.failure is not None
                controller = r.run.controller
                assert controller is not None
                assert controller.stuck

                policy.reject = False
                r.frames.menu_selection = 1
                await until(
                    lambda: next(
                        (
                            item
                            for item in events.health
                            if item.atMs > capped.atMs and item.riskScore == 1.0
                        ),
                        None,
                    )
                )
                assert controller.stuck
                assert len(events.actions) == 1
                assert r.frames.down == ["menu"]
                assert await main.request("goal.submit", goal("recovery", 10_000)) == {}
                recovered = await until(
                    lambda: next(
                        (item for item in events.actions if item.goalId == "recovery"), None
                    )
                )
                assert recovered.category == "navigate"
                await risk_at(0.0, "recovery")
                assert not controller.stuck
                assert r.frames.down == ["menu", "menu"]
                assert not r.device.anomalies
            finally:
                renewal.cancel()
                await asyncio.gather(renewal, return_exceptions=True)
                await main.close()
                await core.close()

    asyncio.run(run())


def test_bellis_risk_source_expires_with_control_grant(semantic_core: Path, tmp_path: Path) -> None:
    async def run() -> None:
        async with rig(semantic_core, tmp_path) as r:
            main, core, events = await r.connect(first=True)
            assert core is not None
            try:
                await ready(r, events)
                controller = r.run.controller
                assert controller is not None
                assert controller.risk_score == 0.0
                await r.run.kernel.disable("step-control")
                assert controller.risk_score is None
                missing = await until(
                    lambda: next(
                        (
                            item
                            for item in events.health[-1:]
                            if item.lastRejection is not None
                            and item.lastRejection.code == "risk-unsupported"
                        ),
                        None,
                    )
                )
                assert not missing.ready
                assert not missing.riskLimitExceeded
                assert missing.riskScore == 0.0
                await refused(main, "goal.submit", goal("missing-risk", 10_000))
                assert not r.session.may_autoplay
                assert not r.frames.down
            finally:
                await main.close()
                await core.close()

    asyncio.run(run())


def test_bellis_action_and_telemetry_close_match_the_supervised_recording(
    semantic_core: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []
    close = RuntimeTelemetry.aclose

    async def run() -> None:
        async with rig(semantic_core, tmp_path, telemetry=tmp_path / "otel", record=True) as r:
            path = r.run.recording_path
            assert path is not None
            assert r.run.core.audio is None

            async def observe_close(telemetry: RuntimeTelemetry) -> None:
                closed.append(
                    not r.run.core.running
                    and r.run.core.frames is None
                    and read_meta(path).state == "recording"
                )
                await close(telemetry)

            monkeypatch.setattr(RuntimeTelemetry, "aclose", observe_close)
            main, core, events = await r.connect(first=True)
            assert core is not None
            await ready(r, events)
            renewing = asyncio.create_task(renew(core, 1))
            try:
                assert await main.request("goal.submit", goal("recorded-navigation", 5_000)) == {}
                action = await until(lambda: events.actions[0] if events.actions else None)
                assert action.goalId == "recorded-navigation"
                assert action.category == "navigate"
                assert action.traceparent is not None
                assert action.traceparent[3:35] == _TRACE[3:35]
                assert r.frames.down == ["menu"]
            finally:
                renewing.cancel()
                await asyncio.gather(renewing, return_exceptions=True)
        recording = Recording.open(path)
        assert recording.meta.state == "complete"
        assert recording.meta.audio is None
        before = [
            i for i, item in enumerate(recording.frames) if ms(item.capture_ns) <= action.atMs
        ]
        after = [i for i, item in enumerate(recording.frames) if ms(item.capture_ns) > action.atMs]
        assert before
        assert after
        assert recording.frame(before[-1]).pixels != recording.frame(after[-1]).pixels
        assert closed == [True]

    asyncio.run(run())


class _AtTime(MonotonicClock):
    def __init__(self, instant: int) -> None:
        self.instant = instant

    def now_ns(self) -> int:
        return self.instant


@pytest.mark.parametrize("fault", ["goal-expired", "resolve-expired", "detach"])
def test_commands_recheck_after_synchronous_tracer(
    semantic_core: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    async def run() -> None:
        async with rig(semantic_core, tmp_path, telemetry=tmp_path / "otel") as r:
            main, core, events = await r.connect(first=True)
            assert core is not None
            lease = asyncio.create_task(renew(core, 1))
            original = RuntimeTelemetry.command_received
            try:
                await ready(r, events)
                controller = r.run.controller
                assert controller is not None
                method = "goal.submit"
                payload: dict[str, object] = goal("late", 4_000)
                instant = ns(cast(int, payload["expiresAtMs"]))
                if fault == "resolve-expired":
                    r.frames.scene, r.frames.timer_end = "timed", time.perf_counter() + 3.6
                    assert (
                        await main.request(
                            "pacing.hold",
                            {
                                "holdId": "waiting",
                                "reason": "合成解说",
                                "expiresAtMs": now_ms() + 8_000,
                                "traceparent": _TRACE,
                            },
                        )
                        == {}
                    )
                    assert await main.request("goal.submit", goal("timed", 8_000)) == {}
                    point = await until(lambda: events.points[0] if events.points else None)
                    assert point.timing is not None
                    instant = ns(point.timing.resolveByMs + 15)
                    method = "decision.resolve"
                    payload = {
                        "decisionPointId": point.decisionPointId,
                        "optionId": "option-0",
                        "source": "director",
                        "traceparent": _DECISION_TRACE,
                    }
                    await asyncio.sleep(max(0, point.timing.resolveByMs - now_ms() - 180) / 1000)
                    assert controller.decision_available(point.decisionPointId)

                def trace(
                    service: RuntimeTelemetry, context: SpanContext, command: str
                ) -> TelemetrySpan | None:
                    span = original(service, context, command)
                    if command == method:
                        if fault == "detach":
                            r.session.detach()
                        elif method == "decision.resolve":
                            controller.clock = _AtTime(instant)
                        else:
                            r.session.clock = _AtTime(instant)
                    return span

                monkeypatch.setattr(RuntimeTelemetry, "command_received", trace)
                await refused(main, method, payload)
                if method == "goal.submit":
                    assert r.session.planning_goal is None
                    assert r.frames.down == []
                else:
                    assert r.session.planning_goal is not None
                    assert all(
                        fact.selection is not None and fact.selection.source == "default"
                        for fact in events.actions
                    )
            finally:
                lease.cancel()
                await asyncio.gather(lease, return_exceptions=True)
                await main.close()
                await core.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "fault", ["deadline", "press-budget", "detach", "tracer-error", "locator-error"]
)
def test_step_tracer_and_location_keep_real_submission_guards(
    semantic_core: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    directory = tmp_path / "otel"
    locate_elapsed: list[int] = []

    async def run() -> None:
        async with rig(semantic_core, tmp_path, telemetry=directory) as r:
            main, core, events = await r.connect(first=True)
            assert core is not None
            lease = asyncio.create_task(renew(core, 1))
            try:
                await ready(r, events)
                controller = r.run.controller
                assert controller is not None
                payload = goal("instrumented", 360 if fault == "press-budget" else 4_000)
                instant = ns(cast(int, payload["expiresAtMs"]))
                touched = asyncio.Event()
                original = TelemetrySpan.event

                def event(
                    span: TelemetrySpan, stage: str, *, at_monotonic_ns: int | None = None
                ) -> None:
                    original(span, stage, at_monotonic_ns=at_monotonic_ns)
                    if stage == "located" and not touched.is_set():
                        touched.set()
                        if fault == "detach":
                            r.session.detach()
                        elif fault == "deadline":
                            r.session.clock = _AtTime(instant)
                            controller.clock = _AtTime(instant)
                        elif fault == "press-budget":
                            r.session.clock = _AtTime(instant - 50_000)
                            controller.clock = r.session.clock
                        elif fault == "tracer-error":
                            raise ValueError("合成遥测异常")

                async def fail_location(_frame: SceneUpdate, _proposal: StepProposal) -> None:
                    started = time.perf_counter_ns()
                    await asyncio.sleep(0.035)
                    locate_elapsed.append(time.perf_counter_ns() - started)
                    touched.set()
                    raise ActionRefusedError("synthetic-location-refusal")

                monkeypatch.setattr(TelemetrySpan, "event", event)
                if fault == "locator-error":
                    monkeypatch.setattr(controller, "_locate", fail_location)
                assert await main.request("goal.submit", payload) == {}
                await asyncio.wait_for(touched.wait(), WAIT)
                if fault == "tracer-error":
                    fact = await until(lambda: events.actions[0] if events.actions else None)
                    assert fact.category == "navigate"
                    assert fact.traceparent not in {None, _TRACE}
                    assert r.frames.down == ["menu"]
                else:
                    await until(
                        lambda: any(
                            result.status is StepStatus.REJECTED for result in controller.recent
                        )
                    )
                    if fault == "press-budget":
                        assert any(
                            result.detail == "action-window-expired" for result in controller.recent
                        )
                    assert r.frames.down == []
                    assert not events.actions
            finally:
                lease.cancel()
                await asyncio.gather(lease, return_exceptions=True)
                await main.close()
                await core.close()

    asyncio.run(run())
    steps: list[SpanRecord] = []
    for path in directory.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            request = json.loads(line)
            spans = cast(list[SpanRecord], request["resourceSpans"][0]["scopeSpans"][0]["spans"])
            steps.extend(span for span in spans if span["name"] == "game.step")
    assert steps
    if fault == "locator-error":
        assert locate_elapsed
        assert any(
            int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"]) >= min(locate_elapsed)
            for span in steps
        )
        assert all("events" not in span for span in steps)
    elif fault != "tracer-error":
        assert all(
            [event["name"] for event in span.get("events", [])] == ["game.step.located"]
            for span in steps
        )


def delay_decision_effect_frames(
    r: Rig, monkeypatch: pytest.MonkeyPatch, *, resume_frames: asyncio.Event | None = None
) -> asyncio.Future[int]:
    controller = r.run.controller
    assert controller is not None
    scene = controller.scene
    assert isinstance(scene, SceneHub)
    verify = controller._verify  # pyright: ignore[reportPrivateUsage]
    started: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    publish = scene.publish

    def publish_unless_waiting(update: SceneUpdate) -> None:
        if resume_frames is None or not started.done() or resume_frames.is_set():
            publish(update)

    async def verify_during_transition(
        before: SceneUpdate,
        expected: ExpectedEffect,
        executed_ns: int,
        decision: PendingDecision | None,
        revision: int,
        *,
        until: int,
    ) -> StepStatus:
        if decision is not None and not started.done():
            # begin_frame 使旧画面失效后，新的签名判别可能仍在处理。
            scene.invalidate()
            assert not r.session.ready()
            started.set_result(controller.clock.now_ns())
        return await verify(before, expected, executed_ns, decision, revision, until=until)

    if resume_frames is not None:
        monkeypatch.setattr(scene, "publish", publish_unless_waiting)
    monkeypatch.setattr(controller, "_verify", verify_during_transition)
    return started


@pytest.mark.parametrize("expires_during_vote", [False, True], ids=["resume", "reauthorize"])
def test_bellis_vote_and_action_fact_follow_authorized_effect(
    semantic_core: Path, tmp_path: Path, expires_during_vote: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        async with rig(semantic_core, tmp_path) as r:
            main, core, events = await r.connect(first=True)
            assert core is not None
            await ready(r, events)
            gap = delay_decision_effect_frames(r, monkeypatch)
            assert "localRecovery" not in events.health[-1].model_fields_set
            assert r.run.core.last_health is not None
            assert r.run.core.last_health.lease is not models.LeaseStatus.VALID
            await refused(main, "goal.submit", goal("old", -5))
            renew_task = asyncio.create_task(renew(core, 1))
            try:
                hold = {"holdId": "commentary", "traceparent": _TRACE}
                assert (
                    await main.request(
                        "pacing.hold",
                        hold
                        | {
                            "reason": "合成解说",
                            "expiresAtMs": now_ms() + 10_000,
                        },
                    )
                    == {}
                )
                request = goal("navigation", 3_000 if expires_during_vote else 8_000)
                scope = {
                    "sessionId": "fixture01",
                    "runtimeId": "run-02",
                    "authorizationId": "scope",
                    "adapterId": "synthetic-fixture",
                    "maxActions": 2,
                    "expiresAtMs": request["expiresAtMs"],
                }
                await refused(main, "goal.submit", request | {"localDialogue": scope})
                await refused(
                    main, "goal.submit", request | {"localRecovery": scope | {"kind": "recover"}}
                )
                assert await main.request("goal.submit", request) == {}
                await until(lambda: next((g for g in r.planner.goals if g is not None), None))
                await asyncio.sleep(0.35)
                assert not r.frames.down
                assert not events.actions
                [accepted] = [g for g in r.planner.goals if g is not None]
                assert accepted.constraints == ("只执行已核验的导航",)
                assert accepted.deadline_ns == cast(int, request["expiresAtMs"]) * 1_000_000
                assert await main.request("pacing.release", hold) == {}
                opened = await until(lambda: events.points[0] if events.points else None)
                navigation = await until(lambda: events.actions[0] if events.actions else None)
                assert (navigation.goalId, navigation.traceparent, navigation.category) == (
                    "navigation",
                    _TRACE,
                    "navigate",
                )
                updated = await until(
                    lambda: next((item for item in events.points if item.candidates), None)
                )
                assert updated.decisionPointId == opened.decisionPointId
                assert (updated.prompt, updated.options, updated.traceparent) == (
                    opened.prompt,
                    opened.options,
                    opened.traceparent,
                )
                after_vote_hold = {"holdId": "vote-commentary", "traceparent": _DECISION_TRACE}
                if not expires_during_vote:
                    assert (
                        await main.request(
                            "pacing.hold",
                            after_vote_hold
                            | {"reason": "等解说完", "expiresAtMs": now_ms() + 5_000},
                        )
                        == {}
                    )
                assert (
                    await main.request(
                        "goal.submit",
                        goal("vote", 6_000, vote=opened.decisionPointId)
                        | {"traceparent": _DECISION_TRACE},
                    )
                    == {}
                )
                choice: dict[str, object] = {
                    "decisionPointId": opened.decisionPointId,
                    "optionId": "option-0",
                    "source": "vote",
                    "traceparent": _DECISION_TRACE,
                }
                await refused(main, "decision.resolve", choice | {"decisionPointId": "older-point"})
                await refused(main, "decision.resolve", choice | {"source": "operator"})
                recovery = scope | {
                    "kind": "prefix",
                    "maxActions": 1,
                    "decisionPointId": opened.decisionPointId,
                    "optionId": "option-0",
                }
                await refused(main, "decision.resolve", choice | {"localRecovery": recovery})
                if expires_during_vote:
                    remaining = cast(int, request["expiresAtMs"]) - now_ms()
                    await asyncio.sleep(max(0, remaining) / 1_000 + 0.05)
                assert await main.request("decision.resolve", choice) == {}
                await refused(main, "decision.resolve", choice)
                chosen = await until(
                    lambda: next((fact for fact in events.actions if fact.selection), None)
                )
                assert gap.done()
                assert chosen.selection == v1.Selection(
                    decisionPointId=opened.decisionPointId, optionId="option-0", source="vote"
                )
                assert chosen.goalId == "vote"
                assert chosen.traceparent == _DECISION_TRACE
                assert chosen.actionId != navigation.actionId
                assert r.frames.down == ["menu", "choice"]
                assert not r.device.anomalies
                assert [g for g in r.planner.goals if g is not None] == [accepted]
                r.frames.scene = "menu"
                if expires_during_vote:

                    def paused_at_menu() -> v1.GameMainHealth | None:
                        if not events.health or not events.situations:
                            return None
                        health, situation = events.health[-1], events.situations[-1]
                        if (
                            health.ready
                            and health.mode == "paused"
                            and health.goalId is None
                            and chosen.atMs < situation.atMs <= health.atMs
                            and situation.screenType == "菜单"
                        ):
                            return health
                        return None

                    await until(paused_at_menu)
                    assert r.frames.down == ["menu", "choice"]
                    assert await main.request("goal.submit", goal("next", 5_000)) == {}
                else:
                    await until(
                        lambda: next(
                            (
                                item
                                for item in events.health
                                if item.atMs > chosen.atMs
                                and item.mode == "autonomous"
                                and item.goalId == "navigation"
                            ),
                            None,
                        )
                    )
                    assert r.frames.down == ["menu", "choice"]
                    assert await main.request("pacing.release", after_vote_hold) == {}
                nxt = await until(lambda: events.actions[2] if len(events.actions) > 2 else None)
                assert nxt.goalId == ("next" if expires_during_vote else "navigation")
                assert nxt.traceparent == _TRACE
                if not expires_during_vote:
                    assert [g for g in r.planner.goals if g is not None] == [accepted]
            finally:
                renew_task.cancel()
                await asyncio.gather(renew_task, return_exceptions=True)
                await main.close()
                await core.close()

    asyncio.run(run())


@pytest.mark.parametrize("expires_during_crop", [False, True], ids=["authorized", "expired"])
def test_bellis_phrase_choice_rechecks_authorization_during_reread(
    semantic_core: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expires_during_crop: bool
) -> None:
    async def run() -> None:
        policy = PhraseMenu(PHRASE_CHOICE_LINE, ScreenType.CHOICE)
        async with rig(semantic_core, tmp_path, policy=policy) as r:
            controller = r.run.controller
            assert controller is not None
            pixels = r.frames.render()
            line = phrase_line(r.frames.adapter, PHRASE_CHOICE_LINE)
            r.frames.recognized[pixels] = (*r.frames.recognized[pixels], line)
            crop_entered_at: asyncio.Future[int] = asyncio.get_running_loop().create_future()
            crop_cancelled = asyncio.Event()
            resolving = False

            async def respond(result: OcrResult) -> OcrResult:
                if resolving and not crop_entered_at.done():
                    crop_entered_at.set_result(time.perf_counter_ns())
                    if expires_during_crop:
                        try:
                            await asyncio.Event().wait()
                        finally:
                            crop_cancelled.set()
                return result

            reads = observe_phrase_crops(monkeypatch, controller.ocr, respond)
            main, core, events = await r.connect(first=True)
            assert core is not None
            await ready(r, events)
            renew_task = asyncio.create_task(renew(core, 1))
            try:
                assert await main.request("goal.submit", goal("phrase", 5_000)) == {}
                opened = await until(lambda: events.points[0] if events.points else None)
                assert opened.prompt == PHRASE_CHOICE_LINE
                assert [option.label for option in opened.options] == [PHRASE]
                assert not r.frames.down
                assert len(reads) == 1
                vote = goal(
                    "phrase-vote",
                    400 if expires_during_crop else 3_000,
                    vote=opened.decisionPointId,
                )
                assert await main.request("goal.submit", vote) == {}
                choice: dict[str, object] = {
                    "decisionPointId": opened.decisionPointId,
                    "optionId": "option-0",
                    "source": "vote",
                    "traceparent": _DECISION_TRACE,
                }
                resolving = True
                assert await main.request("decision.resolve", choice) == {}
                entered_at = await asyncio.wait_for(crop_entered_at, WAIT)
                expiry_ms = cast(int, vote["expiresAtMs"])
                assert entered_at < ns(expiry_ms)
                assert len(reads) == 2
                assert reads[1].frame_number > reads[0].frame_number
                if expires_during_crop:
                    await asyncio.wait_for(crop_cancelled.wait(), WAIT)
                    await until(
                        lambda: next(
                            (
                                h
                                for h in events.health
                                if h.atMs >= expiry_ms and h.mode == "paused"
                            ),
                            None,
                        )
                    )
                    await refused(main, "decision.resolve", choice)
                    assert not r.frames.down
                    assert not events.actions
                else:
                    action = await until(lambda: events.actions[0] if events.actions else None)
                    assert action.goalId == "phrase-vote"
                    assert action.traceparent == _DECISION_TRACE
                    assert action.selection == v1.Selection(
                        decisionPointId=opened.decisionPointId, optionId="option-0", source="vote"
                    )
                    assert r.frames.down == ["menu"]
                    await until(
                        lambda: next(
                            (
                                h
                                for h in events.health
                                if h.atMs >= action.atMs
                                and h.mode == "autonomous"
                                and h.goalId == "phrase"
                            ),
                            None,
                        )
                    )
            finally:
                renew_task.cancel()
                await asyncio.gather(renew_task, return_exceptions=True)
                await main.close()
                await core.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "interruption", ["no-effect", "pause", "replace", "disconnect", "missing-frame", "expired"]
)
def test_interrupted_vote_cannot_restore_prior_goal(
    semantic_core: Path,
    tmp_path: Path,
    interruption: Literal[
        "no-effect", "pause", "replace", "disconnect", "missing-frame", "expired"
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        async with rig(semantic_core, tmp_path) as r:
            r.frames.scene = "choice"
            r.frames.ignore_choice = True
            main, core, events = await r.connect(first=True)
            assert core is not None
            await ready(r, events)
            resume_frames = asyncio.Event()
            gap = (
                delay_decision_effect_frames(r, monkeypatch, resume_frames=resume_frames)
                if interruption in {"disconnect", "missing-frame", "expired"}
                else None
            )
            renewal = asyncio.create_task(renew(core, 1))
            replacement = None
            try:
                assert await main.request("goal.submit", goal("prior", 8_000)) == {}
                point = await until(lambda: events.points[0] if events.points else None)
                vote = goal("vote", 8_000, vote=point.decisionPointId)
                assert await main.request("goal.submit", vote) == {}
                assert (
                    await main.request(
                        "decision.resolve",
                        {
                            "decisionPointId": point.decisionPointId,
                            "optionId": "option-0",
                            "source": "vote",
                            "traceparent": _DECISION_TRACE,
                        },
                    )
                    == {}
                )
                await until(lambda: True if r.frames.down else None)
                verifying_since = None if gap is None else await asyncio.wait_for(gap, WAIT)
                match interruption:
                    case "pause":
                        assert (
                            await main.request(
                                "goal.submit", goal("paused", 8_000) | {"mode": "paused"}
                            )
                            == {}
                        )
                    case "replace":
                        assert await main.request("goal.submit", goal("replacement", 8_000)) == {}
                    case "disconnect":
                        await main.close()
                        await until(lambda: r.session.unknown)
                        assert not events.actions
                        resume_frames.set()
                        replacement, _, events = await r.connect(first=False)
                        await ready(r, events)
                    case "expired":
                        expires_ns = ns(cast(int, vote["expiresAtMs"]))

                        class AtExpiry(MonotonicClock):
                            def now_ns(self) -> int:
                                return expires_ns

                        r.session.clock = AtExpiry()
                        assert not r.session.may_verify
                    case "missing-frame":
                        assert r.session.may_verify
                        assert r.session.unknown is None
                    case "no-effect":
                        pass  # 收到真实 Executed，保持画面不变使效果核验失败。
                failure = await until(lambda: r.session.unknown)
                assert failure.goal_id == "vote"
                assert failure.choice is not None
                assert failure.choice.source == "vote"
                assert failure.choice.decision_id == point.decisionPointId
                if interruption == "missing-frame":
                    controller = r.run.controller
                    assert controller is not None
                    assert verifying_since is not None
                    assert failure.at_ns >= verifying_since + round(
                        controller.config.effect_timeout * 1e9
                    )
                expected_id = {
                    "pause": "paused",
                    "replace": "replacement",
                    "no-effect": "vote",
                    "missing-frame": "vote",
                }.get(interruption)
                assert r.session.health().mode == "paused"
                assert r.session.health().goalId == expected_id
                resume_frames.set()
                r.frames.scene = "menu"
                await asyncio.sleep(0.35)
                assert r.frames.down == ["choice"]
                assert not events.actions
                assert failure in r.session.failures
            finally:
                renewal.cancel()
                await asyncio.gather(renewal, return_exceptions=True)
                if replacement is not None:
                    await replacement.close()
                await main.close()
                await core.close()

    asyncio.run(run())


@pytest.mark.parametrize("ignored", [False, True], ids=["verified", "unknown"])
def test_pacing_keeps_timed_selection_and_reconnect_keeps_unknown(
    semantic_core: Path, tmp_path: Path, ignored: bool
) -> None:
    async def run() -> None:
        async with rig(semantic_core, tmp_path) as r:
            main, core, events = await r.connect(first=True)
            assert core is not None
            await ready(r, events)
            renew_task = asyncio.create_task(renew(core, 1))
            replacement = None
            try:
                r.frames.scene, r.frames.timer_end = "timed", time.perf_counter() + 3.6
                r.frames.ignore_choice = ignored
                assert (
                    await main.request(
                        "pacing.hold",
                        {
                            "holdId": "commentary",
                            "reason": "正在解说",
                            "expiresAtMs": now_ms() + 60_000,
                            "traceparent": _TRACE,
                        },
                    )
                    == {}
                )
                assert await main.request("goal.submit", goal("timed", 8_000)) == {}
                opened = await until(lambda: events.points[0] if events.points else None)
                assert opened.timing is not None
                await refused(
                    main, "goal.submit", goal("invalid-vote", 5_000, vote=opened.decisionPointId)
                )
                assert opened.candidates[0].optionId == "option-1"
                r.frames.timer_end -= 0.5
                tightened = await until(
                    lambda: next(
                        (
                            point
                            for point in events.points
                            if point.timing is not None
                            and opened.timing is not None
                            and point.timing.deadlineMs < opened.timing.deadlineMs - 250
                        ),
                        None,
                    )
                )
                assert tightened.decisionPointId == opened.decisionPointId
                await until(lambda: True if r.frames.down else None)
                if not ignored:
                    fact = await until(lambda: events.actions[-1] if events.actions else None)
                    assert fact.goalId == "timed"
                    assert fact.selection == v1.Selection(
                        decisionPointId=opened.decisionPointId,
                        optionId="option-1",
                        source="default",
                    )
                    return
                retained = await until(lambda: r.session.unknown)
                assert retained.goal_id == "timed"
                assert retained.choice is not None
                assert retained.choice.source == "default"
                assert not events.actions
                assert r.frames.down == ["timed"]
                await main.close()
                replacement, _, new_events = await r.connect(first=False)
                await ready(r, new_events)
                await refused(replacement, "goal.submit", goal("timed", 5_000))
                assert retained is r.session.unknown
                assert r.frames.down == ["timed"]
                assert await replacement.request("goal.submit", goal("recovery", 5_000)) == {}
                assert r.session.unknown is None
                assert retained in r.session.failures
                r.store.delete(SESSION_ENTRY)
                await asyncio.wait_for(r.run.wait_ended(), WAIT)
                assert r.frames.down == ["timed"]
                assert not new_events.actions
            finally:
                renew_task.cancel()
                await asyncio.gather(renew_task, return_exceptions=True)
                if replacement is not None:
                    await replacement.close()
                await main.close()
                await core.close()

    asyncio.run(run())
