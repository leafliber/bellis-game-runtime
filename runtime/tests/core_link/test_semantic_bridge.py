"""语义桥经真实本机通道、实时核、合成画面和假设备。"""

import asyncio
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
from game_runtime.game_bridge.session import SESSION_ENTRY, BellisConfig, BellisSession, RiskReading
from game_runtime.game_bridge.wire import decode_request, ms, ns, v1
from game_runtime.kernel import Manifest, Plugin, PluginContext, ResolvedEntry, Tier
from game_runtime.ocr import OCR, FrameLike, OcrConfig, OcrLine, OcrResult, OcrService
from game_runtime.ocr.fake import ScriptedRecognizer
from game_runtime.operator import MonotonicClock
from game_runtime.scene import SceneHub, SceneUpdate, store_template
from game_runtime.secret_store import MemorySecretStore, SessionInfo
from game_runtime.step_control import (
    Category,
    Click,
    EffectKind,
    ExpectedEffect,
    Plan,
    PlannerContext,
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
from game_runtime.voice_activity import VoiceState, VoiceStatus
from synthetic_game.anchors import anchor_templates
from synthetic_game.layout import Layout, Rect
from synthetic_game.render import Button, ChoiceView, DialogueView, MenuView, Renderer, View

from .test_step_control import HEIGHT, WAIT, WIDTH, RecordingPlanner, planner_for, until

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
        key = self.scene, tenths if self.scene == "timed" else 0
        if key in self.known:
            return self.known[key]
        lines: list[OcrLine] = []
        if self.scene == "menu":
            buttons = tuple(
                Button(self.layout.menu_button(i), label, i == 0)
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
    risk: list[RiskReading]

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
async def rig(exe: Path, folder: Path) -> AsyncGenerator[Rig]:
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
    risk: list[RiskReading] = []
    peers: asyncio.Queue[Connection] = asyncio.Queue()
    session_id = f"test{secrets.token_hex(4)}"
    planner = RecordingPlanner(PlannerContext(None))
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
                FixturePolicy(),
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
                extra_plugins=(planner_for(lambda: planner),),
                extra_entries=(
                    ResolvedEntry(
                        id="plan",
                        plugin="fixture-planner",
                        config={},
                        disabled=False,
                        grant=frozenset(),
                    ),
                ),
                bellis=BellisConfig(
                    store, frozenset({"safe"}), info_file, lambda: risk[-1] if risk else None
                ),
            ) as run:
                await device.until_connected()
                yield Rig(run, frames, device, peers, store, planner, risk)
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
    ) -> StepStatus:
        if decision is not None and not started.done():
            # begin_frame 使旧画面失效后，新的签名判别可能仍在处理。
            scene.invalidate()
            assert not r.session.ready()
            started.set_result(controller.clock.now_ns())
        return await verify(before, expected, executed_ns, decision, revision)

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
            initial = await until(lambda: events.health[-1] if events.health else None)
            assert not initial.ready
            assert initial.lastRejection is not None
            assert initial.lastRejection.code == "risk-unsupported"
            r.risk.append(RiskReading(0.23, False))
            await ready(r, events)
            gap = delay_decision_effect_frames(r, monkeypatch)
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
            r.risk.append(RiskReading(0.17, False))
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
            r.risk.append(RiskReading(0.31, False))
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
