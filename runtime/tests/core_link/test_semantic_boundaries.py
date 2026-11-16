"""语义事实与核动作块在各自协议边界上的回归。"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import cast

import pytest
from pydantic import BaseModel

from core_protocol import models
from game_runtime.adapter import GAMES_DIR, ScreenType, load_adapter
from game_runtime.channel import session_credentials
from game_runtime.core_link import CoreLink, CoreLinkOptions
from game_runtime.core_link.link import ActionDeadlineError
from game_runtime.frame_ring import RingFrame
from game_runtime.game_bridge import plugin as bridge_plugin
from game_runtime.game_bridge.projection import Projection
from game_runtime.game_bridge.session import BellisConfig, BellisSession, RiskReading
from game_runtime.game_bridge.wire import ns, v1
from game_runtime.operator import MonotonicClock
from game_runtime.planner import Planner, PlannerConfig
from game_runtime.scene import SceneAnalyzer, SceneHub, SceneUpdate, TextReader
from game_runtime.secret_store import MemorySecretStore, SessionInfo
from game_runtime.step_control import Category, StepStatus
from game_runtime.step_control.plugin import SessionSignals

from ..planner.test_planner import ScriptModel, scene
from .test_semantic_bridge import goal, ready, refused, renew, rig
from .test_semantic_bridge import semantic_core as semantic_core
from .test_step_control import WAIT, until


@pytest.fixture
def lookpoint_observations(monkeypatch: pytest.MonkeyPatch) -> list[v1.GameSituationObserved]:
    class ProjectionClock(MonotonicClock):
        def now_ns(self) -> int:
            return ns(105)

    async def run() -> list[v1.GameSituationObserved]:
        adapter = load_adapter(GAMES_DIR / "synthetic-game")
        planner = Planner(adapter, ScriptModel(adapter), PlannerConfig(), narrative=lambda: None)
        planner.on_milestone("clue", "新的合成线索", ns(102))
        hub = SceneHub(problems=(), uncalibrated=())
        projection = Projection(
            adapter,
            hub,
            CoreLink(CoreLinkOptions(Path(sys.executable))),
            lambda: planner,
            lambda: None,
            lambda: None,
            ns(500),
        )
        observed: list[v1.GameSituationObserved] = []

        def emit(method: str, message: BaseModel) -> bool:
            assert method == "situation.observed"
            assert isinstance(message, v1.GameSituationObserved)
            observed.append(message)
            return True

        with session_credentials("lookpoint") as credentials:
            info = SessionInfo.create(
                credentials, "lookpoint", launcher_pid=os.getpid(), started_at=datetime.now(UTC)
            )
            session = BellisSession(
                BellisConfig(MemorySecretStore()),
                info,
                "projection-test",
                adapter,
                ProjectionClock(),
                SessionSignals(),
            )
            session.described_ns = ns(90)
            monkeypatch.setattr(session, "notify", emit)
            for frame, at in enumerate((100, 103, 104), 1):
                update = scene(frame, "dialogue", ScreenType.DIALOGUE)
                hub.publish(
                    replace(
                        update,
                        frame=replace(update.frame, capture_ns=ns(at)),
                        state=replace(update.state, capture_ns=ns(at)),
                    )
                )
                projection.publish(session)
        return observed

    return asyncio.run(run())


def test_future_lookpoint_is_reported_on_a_covering_frame_once(
    lookpoint_observations: list[v1.GameSituationObserved],
) -> None:
    assert [
        (item.atMs, [point.atMs for point in item.lookpoints]) for item in lookpoint_observations
    ] == [(100, []), (103, [102]), (104, [])]


def test_frozen_bellis_validator_accepts_lookpoint_notifications(
    lookpoint_observations: list[v1.GameSituationObserved],
) -> None:
    source = os.environ.get("GAME_RUNTIME_TEST_BELLIS_VALIDATOR")
    if source is None:
        pytest.skip("GAME_RUNTIME_TEST_BELLIS_VALIDATOR 未指向冻结的 Bellis 校验器")
    node = shutil.which("node")
    assert node is not None, "冻结 Bellis 校验器需要 Node"
    params = [item.model_dump(mode="json") for item in lookpoint_observations]
    # 用同一条真实投影构造旧错误，确认实际 validator 会拒绝它。
    params.append({**params[0], "lookpoints": params[1]["lookpoints"]})
    wire = [{"jsonrpc": "2.0", "method": "situation.observed", "params": item} for item in params]
    result = subprocess.run(
        [
            node,
            "--input-type=module",
            "--eval",
            """
            import { readFileSync } from 'node:fs';
            import { pathToFileURL } from 'node:url';
            const { checkGameBridgeMessage, parseGameBridgeJson } =
              await import(pathToFileURL(process.argv[1]).href);
            const wire = JSON.parse(readFileSync(0, 'utf8'));
            const accepted = wire.map(message => checkGameBridgeMessage(
              'game-runtime.main', 'runtime', parseGameBridgeJson(JSON.stringify(message))
            ));
            process.stdout.write(JSON.stringify(accepted));
            """,
            str(Path(source).resolve(strict=True)),
        ],
        input=json.dumps(wire),
        encoding="utf-8",
        capture_output=True,
        check=True,
        timeout=10,
    )
    assert json.loads(result.stdout) == [True, True, True, False]


@pytest.mark.parametrize("stalled", [False, True], ids=["transient", "stalled"])
def test_health_during_effect_analysis_gap(
    semantic_core: Path,
    tmp_path: Path,
    stalled: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_plugin, "REPORT_INTERVAL", 0.05)

    async def run() -> None:
        async with rig(semantic_core, tmp_path) as r:
            r.frames.scene = "choice"
            effect_pixels = r.frames.render()
            r.frames.scene = "menu"
            r.risk.append(RiskReading(0.15, False))
            main, core, events = await r.connect(first=True)
            assert core is not None
            await ready(r, events)
            controller, project = r.run.controller, r.session.project
            assert controller is not None
            assert project is not None
            scene_service = controller.scene
            max_age = round(controller.config.max_frame_age * 1e9)
            release = Event()
            loop = asyncio.get_running_loop()
            blocked: asyncio.Future[int] = loop.create_future()
            analyze = SceneAnalyzer.analyze_update

            def gate_effect(
                analyzer: SceneAnalyzer, frame: RingFrame, reader: TextReader | None
            ) -> SceneUpdate:
                if frame.pixels == effect_pixels and not release.is_set():
                    loop.call_soon_threadsafe(blocked.set_result, frame.frame_number)
                    if not release.wait(WAIT):
                        raise TimeoutError("测试未释放判别线程")
                return analyze(analyzer, frame, reader)

            monkeypatch.setattr(SceneAnalyzer, "analyze_update", gate_effect)
            renewal = asyncio.create_task(renew(core, 1))
            try:
                await until(
                    lambda: (
                        True
                        if (health := r.run.core.last_health) is not None
                        and health.lease is models.LeaseStatus.VALID
                        else None
                    )
                )
                assert await main.request("goal.submit", goal("navigation", 8_000)) == {}
                blocked_frame = await asyncio.wait_for(blocked, WAIT)
                assert scene_service.latest is None
                capture_ns = scene_service.last_analyzed_capture_ns
                assert capture_ns is not None
                report_start = len(events.health)
                gap = await until(
                    lambda: next(
                        (
                            item
                            for item in events.health[report_start:]
                            if item.goalId == "navigation" and item.frameAgeMs is None
                        ),
                        None,
                    )
                )
                assert gap.ready
                assert r.session.may_verify
                assert not r.session.may_autoplay
                await refused(main, "goal.submit", goal("during-gap", 8_000))
                assert r.frames.down == ["menu"]
                assert not events.actions

                if not stalled:
                    release.set()
                    fact = await until(lambda: events.actions[0] if events.actions else None)
                    assert (fact.goalId, fact.category) == ("navigation", "navigate")
                    assert r.frames.down == ["menu"]
                    assert not r.session.failures
                    return

                expired = await until(
                    lambda: next(
                        (
                            item
                            for item in events.health[report_start:]
                            if item.goalId == "navigation" and ns(item.atMs) - capture_ns > max_age
                        ),
                        None,
                    )
                )
                assert not expired.ready
                health = r.run.core.last_health
                assert health is not None
                assert health.latest_frame is not None
                assert health.latest_frame.frame_number > blocked_frame
                assert project.capture_ready(r.session.clock.now_ns())
                assert scene_service.latest is None
                assert scene_service.last_analyzed_capture_ns == capture_ns
                await refused(main, "goal.submit", goal("past-frame-age", 8_000))
                failure = await until(lambda: r.session.unknown)
                assert failure.outcome is StepStatus.UNKNOWN
                assert r.frames.down == ["menu"]
                assert not events.actions
            finally:
                release.set()
                renewal.cancel()
                await asyncio.gather(renewal, return_exceptions=True)
                await main.close()
                await core.close()

    asyncio.run(run())


@pytest.mark.parametrize("earlier_limit_ms", [0, 200], ids=["goal-expiry", "inclusive-limit"])
def test_bellis_goal_cap_reaches_rust_action_block(
    semantic_core: Path,
    tmp_path: Path,
    earlier_limit_ms: int,
) -> None:
    async def run() -> None:
        async with rig(semantic_core, tmp_path) as r:
            r.frames.scene = "dialogue"
            r.risk.append(RiskReading(0.15, False))
            main, core, events = await r.connect(first=True)
            assert core is not None
            await ready(r, events)
            renewal = asyncio.create_task(renew(core, 1))
            try:
                await until(
                    lambda: (
                        True
                        if (
                            r.run.core.last_health is not None
                            and r.run.core.last_health.lease is models.LeaseStatus.VALID
                        )
                        else None
                    )
                )
                request = goal("native-deadline", 3_000)
                assert await main.request("goal.submit", request) == {}
                expiry = ns(cast(int, request["expiresAtMs"]))
                requested = expiry - ns(earlier_limit_ms)
                controller = r.run.controller
                assert controller is not None
                current = controller.scene.latest
                assert current is not None
                grant = r.run.kernel.active_control
                assert grant is not None
                steps = (
                    models.ControlStep(
                        offset_ns=0,
                        held=[],
                        actions=[models.Action(root=models.KeyPress(id=101, scan_code=0x1C))],
                    ),
                )
                finished = r.session.action_started(
                    "deadline-probe", Category.ADVANCE, "key_press", requested, None
                )
                try:
                    with pytest.raises(ActionDeadlineError):
                        await grant.submit(
                            steps,
                            base_frame=current.frame.frame_number,
                            valid_for=0.1,
                            start_delay=(expiry - time.perf_counter_ns()) / 1e9,
                        )
                    assert not any(
                        isinstance(event.event, models.FakeKeyDown) for event in r.device.events()
                    )
                    sent = await grant.submit(
                        steps,
                        base_frame=current.frame.frame_number,
                        valid_for=4.0,
                    )
                    assert isinstance(sent.result, models.Accepted)
                    expected = min(requested, expiry - 1)
                    assert sent.block.deadline_ns == expected
                    executed = (await sent.outcomes())[101]
                    assert isinstance(executed, models.Executed)
                    assert executed.at_ns <= expected < expiry
                    key = await r.device.until(
                        lambda device: next(
                            (e for e in device.events() if isinstance(e.event, models.FakeKeyDown)),
                            None,
                        )
                    )
                    assert key.at_ns < expiry
                finally:
                    # 此夹具不改变画面，测试提交边界与核回执。
                    finished(StepStatus.NO_EFFECT, "synthetic-frame-unchanged", None)
            finally:
                renewal.cancel()
                await asyncio.gather(renewal, return_exceptions=True)
                await main.close()
                await core.close()

    asyncio.run(run())
