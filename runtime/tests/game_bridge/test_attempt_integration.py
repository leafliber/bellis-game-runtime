"""围绕实际 CoreLink 请求边界与认证语义协议的假设备验收。"""

# pyright: reportPrivateUsage=false

import asyncio
import os
import sqlite3
import sys
from collections.abc import AsyncGenerator, Callable
from contextlib import ExitStack, asynccontextmanager, closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter_ns
from typing import cast

import pytest

from core_protocol import models
from core_protocol.methods import (
    ACTION_RECEIPT,
    ACTION_SUBMIT,
    CORE_HEALTH,
    CORE_HELLO,
    INPUT_RELEASED,
    INPUT_VERIFICATION,
    PROTOCOL_VERSION,
)
from game_runtime.adapter import GAMES_DIR, ScreenType, load_adapter
from game_runtime.channel import Connection, RpcError, RpcErrorCode, RpcPeer, connect, listen
from game_runtime.channel.address import session_credentials
from game_runtime.core_link import BellisCoreOptions, CoreLink, CoreLinkOptions
from game_runtime.core_link.link import CORE_ROLE, _Instance
from game_runtime.core_link.process import ManagedProcess
from game_runtime.game_bridge.attempts import AttemptHandler
from game_runtime.game_bridge.projection import Projection
from game_runtime.game_bridge.session import (
    SESSION_ENTRY,
    BellisConfig,
    BellisSession,
    TaskAuthorityConfig,
)
from game_runtime.game_bridge.task_ledger import (
    AttemptKind,
    AttemptPhase,
    AttemptRecord,
    AttemptSpec,
    CallKind,
    EffectEvidence,
    EffectResult,
    FrameStamp,
    LedgerRefusedError,
    Resolution,
    TaskEnd,
    TaskLedger,
    TaskLimits,
    TaskRequest,
)
from game_runtime.game_bridge.wire import ms, v2
from game_runtime.kernel import ControlGrant
from game_runtime.ocr import OcrService
from game_runtime.operator import MonotonicClock
from game_runtime.scene import SceneHub
from game_runtime.secret_store import MemorySecretStore, SessionInfo
from game_runtime.step_control.contracts import Category, EffectKind, ExpectedEffect
from game_runtime.step_control.controller import StepConfig, StepController
from game_runtime.step_control.pacing import DEFAULT_PACING, ReadingPacing
from game_runtime.step_control.plugin import SessionSignals
from game_runtime.step_control.supervision import SemanticPauseError

from ..core_link.test_semantic_bridge import FixturePolicy
from ..core_link.test_semantic_bridge import rig as supervised_rig
from ..core_link.test_semantic_bridge import semantic_core as semantic_core
from ..planner.test_planner import scene

TARGET = "fixed-fixture"
LIMITS = TaskLimits(calls=3, inputs=3, no_progress_ns=30_000_000_000)
WAIT = 5


@dataclass
class CoreRig:
    link: CoreLink
    peer: RpcPeer
    grant: ControlGrant
    handler: AttemptHandler
    requests: asyncio.Queue[models.ActionBlock]
    records: asyncio.Queue[AttemptRecord]
    reply: asyncio.Event
    stop: asyncio.Event

    def executed(
        self, block: models.ActionBlock, *, action_id: int | None = None
    ) -> models.Executed:
        receipt = models.Executed(
            id=block.steps[0].actions[0].root.id if action_id is None else action_id,
            at_ns=perf_counter_ns(),
        )
        self.receipt(block, receipt)
        return receipt

    def receipt(self, block: models.ActionBlock, outcome: models.Executed | models.Dropped) -> None:
        self.peer.notify(
            ACTION_RECEIPT.name,
            models.ActionReceiptParams(
                producer=block.producer,
                block_seq=block.seq,
                outcomes=[models.ActionOutcome(root=outcome)],
            ).model_dump(mode="json"),
        )


@asynccontextmanager
async def fake_core(
    ledger: TaskLedger,
    *,
    options: CoreLinkOptions | None = None,
    authorize: Callable[[AttemptSpec], bool] | None = None,
    on_record: Callable[[AttemptRecord], None] | None = None,
) -> AsyncGenerator[CoreRig]:
    loop = asyncio.get_running_loop()
    requests: asyncio.Queue[models.ActionBlock] = asyncio.Queue()
    records: asyncio.Queue[AttemptRecord] = asyncio.Queue()
    reply, stop = asyncio.Event(), asyncio.Event()
    link = CoreLink(options or CoreLinkOptions(Path(sys.executable), rpc_timeout=WAIT))

    def reported(record: AttemptRecord) -> None:
        if on_record is not None:
            on_record(record)
        records.put_nowait(record)

    handler = AttemptHandler(
        ledger,
        perf_counter_ns,
        asyncio.Lock(),
        authorize or (lambda spec: not stop.is_set() and spec.session_id == "original"),
        reported,
        lambda _: stop.set(),
        runtime_id="first-runtime",
    )
    link.bind_attempts(handler)
    peers: asyncio.Queue[Connection] = asyncio.Queue()
    with session_credentials("attempts") as credentials, ExitStack() as resources:
        server = await listen(
            credentials.path,
            credentials.key,
            on_connection=peers.put_nowait,
            on_rejected=lambda error: pytest.fail(str(error)),
            on_error=lambda error: pytest.fail(str(error)),
        )
        conn = await connect(credentials.path, CORE_ROLE, credentials.key)
        peer = RpcPeer(conn, timeout=WAIT)
        instance = _Instance(1, cast(ManagedProcess, None), server, resources, loop.create_future())
        link._instance = instance
        link._instances = instance.number
        link._on_connection(instance, await asyncio.wait_for(peers.get(), WAIT))

        async def submit(value: object) -> object:
            requests.put_nowait(models.ActionBlock.model_validate(value))
            await reply.wait()
            return {"type": "accepted"}

        peer.handle(ACTION_SUBMIT.name, submit)
        await peer.request(
            CORE_HELLO.name, {"protocol_version": PROTOCOL_VERSION, "core_version": "test"}
        )
        grant = ControlGrant(
            "step-control",
            "test-producer",
            1,
            lambda: conn.is_open and not stop.is_set(),
            link.submit,
        )
        try:
            yield CoreRig(link, peer, grant, handler, requests, records, reply, stop)
        finally:
            conn.abort()
            await peer.close()
            if instance.peer is not None:
                await instance.peer.close()
            await handler.close()
            await server.close()


def ledger_and_spec(
    path: Path, *, receipt_timeout_ns: int = 2_000_000_000
) -> tuple[TaskLedger, AttemptSpec, models.ControlStep]:
    TaskLedger.provision(path, TARGET)
    ledger = TaskLedger.start(path, TARGET)
    now = perf_counter_ns()
    request = TaskRequest(task_id="task", objective="打开菜单", expires_at_ns=now + 30_000_000_000)
    ledger.open_task(request, LIMITS, release_verified=True)
    spec = AttemptSpec(
        attempt_id="attempt",
        task_id=request.task_id,
        runtime_id="first-runtime",
        session_id="original",
        goal_id="goal",
        action_id="first-action",
        kind=AttemptKind.ORDINARY,
        base_frame=FrameStamp(number=4, capture_ns=now, transform_version=1),
        expected=ExpectedEffect(EffectKind.CHANGED),
        submit_deadline_ns=now + receipt_timeout_ns,
        outcome_deadline_ns=now + receipt_timeout_ns,
    )
    step = models.ControlStep(
        offset_ns=0,
        held=[],
        actions=[models.Action(root=models.Click(id=7, x=4, y=6, button=models.MouseButton.LEFT))],
    )
    return ledger, spec, step


@asynccontextmanager
async def task_session(
    path: Path, ledger: TaskLedger, rig: CoreRig
) -> AsyncGenerator[BellisSession]:
    adapter = load_adapter(GAMES_DIR / "synthetic-game")
    store = MemorySecretStore()
    signals = SessionSignals()
    clock = MonotonicClock()
    authority = TaskAuthorityConfig(
        control_target_id=TARGET,
        adapter_id=adapter.id,
        ledger_path=path,
        limits=LIMITS,
    )
    with session_credentials("original") as creds:
        info = SessionInfo.create(
            creds, "original", launcher_pid=os.getpid(), started_at=datetime.now(UTC)
        )
        store.set(SESSION_ENTRY, info.to_json())
        session = BellisSession(
            BellisConfig(store, task_authority=authority),
            info,
            "first-runtime",
            adapter,
            clock,
            signals,
            ledger=ledger,
        )
        hub = SceneHub(problems=(), uncalibrated=())
        session.project = Projection(
            adapter, hub, rig.link, lambda: None, lambda: None, lambda: None, 10_000_000_000
        )
        signals.controller = StepController(
            adapter,
            hub,
            cast(OcrService, None),
            rig.grant,
            session,
            FixturePolicy(),
            clock,
            StepConfig(),
            lambda: rig.link.healthy,
            lambda: None,
            signals.progress,
            signals.operator_goal,
            signals.planner_binding,
            ReadingPacing(DEFAULT_PACING, lambda: None, lambda: None),
            semantic=session,
        )
        now = perf_counter_ns()
        view = scene(4, "menu", ScreenType.MENU)
        hub.publish(
            replace(
                view,
                frame=replace(view.frame, capture_ns=now),
                state=replace(view.state, capture_ns=now),
            )
        )
        rig.peer.notify(
            CORE_HEALTH.name,
            models.CoreHealthParams(
                now_ns=now,
                capture=models.CaptureState.CAPTURING,
                inputs_held=False,
                latest_frame=models.LatestFrame(frame_number=4, capture_ns=now),
                lease=models.LeaseStatus.NOT_GRANTED,
                audio=models.CaptureState.IDLE,
            ).model_dump(mode="json"),
        )
        with pytest.raises(RpcError):
            await rig.peer.request("test.sync")
        yield session


@asynccontextmanager
async def semantic_connection(session: BellisSession) -> AsyncGenerator[RpcPeer]:
    credentials = session.info.credentials()
    incoming: asyncio.Queue[Connection] = asyncio.Queue()
    server = await listen(
        credentials.path,
        credentials.key,
        on_connection=incoming.put_nowait,
        on_rejected=lambda error: pytest.fail(str(error)),
        on_error=lambda error: pytest.fail(str(error)),
    )
    client = RpcPeer(
        await connect(credentials.path, "game-runtime.main", credentials.key), timeout=WAIT
    )
    peer, cleanup = session.open_peer(await asyncio.wait_for(incoming.get(), WAIT))
    try:
        description = v2.GameMainDescriptionV2.model_validate(
            await client.request("game.describe", {"major": 2})
        )
        assert description.controlTargetId == TARGET
        await session.refresh_ledger()
        yield client
    finally:
        session.detach()
        for dispose in cleanup:
            dispose()
        await client.close()
        await peer.close()
        await server.close()


def task_goal(spec: AttemptSpec, goal_id: str, *, mode: str = "autonomous") -> dict[str, object]:
    return {
        "taskId": spec.task_id,
        "goalId": goal_id,
        "objective": "打开主菜单",
        "constraints": [],
        "mode": mode,
        "decisionPointId": None,
        "expiresAtMs": ms(perf_counter_ns()) + 10000,
        "traceparent": None,
    }


def test_p04_goals_and_reconnect_share_budget_and_restart_ends_original_task(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        path = tmp_path / "ledger.sqlite3"
        ledger, spec, _ = ledger_and_spec(path)
        async with fake_core(ledger) as rig, task_session(path, ledger, rig) as session:
            async with semantic_connection(session) as main:
                await main.request("goal.submit", task_goal(spec, "goal"))
                first = session.model_budget(session.session.goal_revision)
                assert first is not None
                assert await first("first", CallKind.PLANNER)
                assert not await first("first", CallKind.PLANNER)
                await main.request("goal.submit", task_goal(spec, "p04"))
                assert not await first("old-goal", CallKind.POLICY)
                second = session.model_budget(session.session.goal_revision)
                assert second is not None
                assert await second("second", CallKind.RANK)
            assert not session.may_autoplay
            assert not await second("old-connection", CallKind.RANK)
            async with semantic_connection(session) as main:
                health = session.health()
                assert isinstance(health, v2.GameMainHealthV2)
                assert health.activeTaskId == spec.task_id
                await main.request("goal.submit", task_goal(spec, "p04-after-reconnect"))
                third = session.model_budget(session.session.goal_revision)
                assert third is not None
                assert await third("third", CallKind.POLICY)
                assert not await third("exhausted", CallKind.POLICY)
                with pytest.raises(RpcError, match="call-budget"):
                    await main.request("goal.submit", task_goal(spec, "p04-exhausted"))
            assert rig.requests.empty()
        restarted = TaskLedger.start(path, TARGET)
        assert restarted.status().active_task is None
        old = restarted.get_task(spec.task_id)
        assert old is not None
        assert old.calls_spent == LIMITS.calls
        assert old.end is TaskEnd.RESTARTED

    asyncio.run(run())


def test_original_readonly_effect_finishes_across_pause_without_new_action(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "ledger.sqlite3"
        ledger, spec, step = ledger_and_spec(path, receipt_timeout_ns=5_000_000_000)
        session: BellisSession

        def authorize(work: AttemptSpec) -> bool:
            return session.task_for_attempt(work)

        def record(result: AttemptRecord) -> None:
            session.attempt_recorded(result)

        async with (
            fake_core(ledger, authorize=authorize, on_record=record) as rig,
            task_session(path, ledger, rig) as session,
            semantic_connection(session) as main,
        ):
            effects: asyncio.Queue[object] = asyncio.Queue()
            main.on_notification("action.committed", effects.put_nowait)
            await main.request("goal.submit", task_goal(spec, "goal"))
            project = session.project
            assert project is not None
            update = project.scene.latest
            assert update is not None
            frame = update.frame
            base = FrameStamp(
                number=frame.frame_number,
                capture_ns=frame.capture_ns,
                transform_version=frame.transform_version,
            )
            original = session.attempt_spec(
                spec.action_id,
                spec.kind,
                base,
                spec.expected,
                spec.submit_deadline_ns,
                spec.outcome_deadline_ns,
            )
            session.action_started(
                original.action_id, Category.NAVIGATE, "click", original.submit_deadline_ns, None
            )
            entered, finish = asyncio.Event(), asyncio.Event()

            async def verify(receipt: models.Executed, deadline_ns: int) -> EffectEvidence:
                assert deadline_ns == original.outcome_deadline_ns
                entered.set()
                await finish.wait()
                at = perf_counter_ns()
                assert receipt.at_ns < at < deadline_ns
                return EffectEvidence(
                    result=EffectResult.VERIFIED,
                    frame=FrameStamp(
                        number=5,
                        capture_ns=at,
                        transform_version=original.base_frame.transform_version,
                    ),
                )

            def recheck() -> None:
                rig.grant.check()
                if project.scene.latest is not update:
                    raise SemanticPauseError("frame-changed")

            waiter = asyncio.create_task(
                rig.handler.run(rig.grant, step, original, recheck=recheck, verify=verify)
            )
            block = await asyncio.wait_for(rig.requests.get(), WAIT)
            rig.executed(block)
            rig.reply.set()
            await asyncio.wait_for(entered.wait(), WAIT)
            await main.request("goal.submit", task_goal(spec, "pause", mode="paused"))
            assert not session.may_autoplay
            with pytest.raises(RpcError, match="attempt-outstanding"):
                await main.request("goal.submit", task_goal(spec, "other"))
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            finish.set()
            result = await asyncio.wait_for(rig.records.get(), WAIT)
            assert result.phase is AttemptPhase.VERIFIED
            assert result.spec == original
            assert rig.requests.empty()
            assert effects.empty()

    asyncio.run(run())


def test_actual_core_request_rechecks_after_durable_binding(tmp_path: Path) -> None:
    async def run() -> None:
        ledger, spec, step = ledger_and_spec(tmp_path / "ledger.sqlite3")
        async with fake_core(ledger) as rig:
            checked = asyncio.Event()

            def invalidated_frame() -> None:
                record = ledger.get_attempt(spec.attempt_id)
                assert record is not None
                assert record.phase is AttemptPhase.AWAITING_RECEIPT
                assert record.core is not None
                assert record.core.action_id == 7
                checked.set()
                raise SemanticPauseError("frame-changed")

            async def unused_verify(_: models.Executed, __: int) -> EffectEvidence | None:
                pytest.fail("没有发出动作时不能核验效果")

            result = await rig.handler.run(
                rig.grant, step, spec, recheck=invalidated_frame, verify=unused_verify
            )
            assert result.phase is AttemptPhase.NOT_EXECUTED
            assert checked.is_set()
            assert rig.requests.empty()
            account = ledger.get_task(spec.task_id)
            assert account is not None
            assert account.inputs_spent == 1
            replay = await rig.handler.run(
                rig.grant, step, spec, recheck=invalidated_frame, verify=unused_verify
            )
            assert replay == result
            assert rig.requests.empty()

    asyncio.run(run())


def test_authenticated_receipt_before_ack_survives_waiter_cancellation(tmp_path: Path) -> None:
    async def run() -> None:
        ledger, spec, step = ledger_and_spec(tmp_path / "ledger.sqlite3")
        async with fake_core(ledger) as rig:

            async def verify(receipt: models.Executed, deadline_ns: int) -> EffectEvidence:
                assert deadline_ns == spec.outcome_deadline_ns
                while perf_counter_ns() <= receipt.at_ns:
                    await asyncio.sleep(0)
                return EffectEvidence(
                    result=EffectResult.VERIFIED,
                    frame=FrameStamp(
                        number=5,
                        capture_ns=perf_counter_ns(),
                        transform_version=1,
                    ),
                )

            waiter = asyncio.create_task(
                rig.handler.run(
                    rig.grant,
                    step,
                    spec,
                    recheck=rig.grant.check,
                    verify=verify,
                )
            )
            block = await asyncio.wait_for(rig.requests.get(), WAIT)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            rig.executed(block, action_id=77)
            original = rig.executed(block)
            record = await asyncio.wait_for(rig.records.get(), WAIT)
            assert record.phase is AttemptPhase.VERIFIED
            assert record.receipt == original
            assert not rig.reply.is_set()
            assert rig.requests.empty()
            rig.reply.set()

    asyncio.run(run())


def test_committed_receipt_conflict_quarantines_through_restart(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "ledger.sqlite3"
        ledger, spec, step = ledger_and_spec(path)
        async with fake_core(ledger) as rig:

            async def verify(receipt: models.Executed, _: int) -> EffectEvidence:
                while perf_counter_ns() <= receipt.at_ns:
                    await asyncio.sleep(0)
                return EffectEvidence(
                    result=EffectResult.VERIFIED,
                    frame=FrameStamp(number=5, capture_ns=perf_counter_ns(), transform_version=1),
                )

            finishing = asyncio.create_task(
                rig.handler.run(rig.grant, step, spec, recheck=rig.grant.check, verify=verify)
            )
            block = await asyncio.wait_for(rig.requests.get(), WAIT)
            executed = rig.executed(block)
            rig.reply.set()
            committed = await finishing
            assert committed.phase is AttemptPhase.VERIFIED
            rig.receipt(block, executed)
            assert not ledger.status().receipt_quarantined
            rig.receipt(block, models.Dropped(id=executed.id, reason=models.DropReason.DUPLICATE))
            await asyncio.wait_for(rig.stop.wait(), WAIT)
            assert ledger.status().receipt_quarantined
        reopened = TaskLedger.start(path, TARGET)
        assert reopened.status().receipt_quarantined
        assert reopened.get_attempt(spec.attempt_id) == committed
        with pytest.raises(LedgerRefusedError, match="core-receipt-conflict"):
            reopened.open_task(
                TaskRequest(
                    task_id="next", objective="再开一局", expires_at_ns=spec.outcome_deadline_ns
                ),
                LIMITS,
                release_verified=True,
            )

    asyncio.run(run())


def test_additive_quarantine_migration_preserves_v1_pending_attempt(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    ledger, spec, _ = ledger_and_spec(path)
    assert ledger.reserve_attempt(spec)
    original = ledger.mark_pending(spec.attempt_id, "previous-unknown")
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TABLE receipt_quarantine")
        connection.execute("PRAGMA user_version = 1")
    reopened = TaskLedger.start(path, TARGET)
    assert reopened.status().outstanding == original
    assert not reopened.status().receipt_quarantined


def test_timeout_remains_pending_on_late_receipt_and_restart(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "ledger.sqlite3"
        ledger, spec, step = ledger_and_spec(path, receipt_timeout_ns=500_000_000)
        async with fake_core(ledger) as rig:

            async def unused_verify(_: models.Executed, __: int) -> EffectEvidence | None:
                pytest.fail("迟到回执不得启动画面核验")

            waiter = asyncio.create_task(
                rig.handler.run(
                    rig.grant,
                    step,
                    spec,
                    recheck=rig.grant.check,
                    verify=unused_verify,
                )
            )
            block = await asyncio.wait_for(rig.requests.get(), WAIT)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            pending = await asyncio.wait_for(rig.records.get(), WAIT)
            assert pending.phase is AttemptPhase.PENDING
            assert pending.detail == "receipt-timeout"
            late = rig.executed(block)
            rig.reply.set()
            async with asyncio.timeout(WAIT):
                while (
                    record := ledger.get_attempt(spec.attempt_id)
                ) is not None and record.receipt is None:
                    await asyncio.sleep(0.01)
            assert record is not None
            assert record.receipt == late
            assert record.phase is AttemptPhase.PENDING
        restarted = TaskLedger.start(path, TARGET)
        assert restarted.status().outstanding == record
        with pytest.raises(LedgerRefusedError, match="release-unverified"):
            restarted.resolve_pending(
                "original", spec.attempt_id, Resolution.RECONCILED_UNKNOWN, release_verified=False
            )
        assert (
            restarted.resolve_pending(
                "original", spec.attempt_id, Resolution.RECONCILED_UNKNOWN, release_verified=True
            ).phase
            is AttemptPhase.OPERATOR_RESOLVED
        )
        assert restarted.status().outstanding is None

    asyncio.run(run())


def test_v2_selected_per_connection_and_new_task_uses_fixed_config(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "ledger.sqlite3"
        TaskLedger.provision(path, TARGET)
        ledger = TaskLedger.start(path, TARGET)
        store = MemorySecretStore()
        adapter = load_adapter(GAMES_DIR / "synthetic-game")
        authority = TaskAuthorityConfig(
            control_target_id=TARGET, adapter_id=adapter.id, ledger_path=path, limits=LIMITS
        )
        proof = asyncio.Event()
        incoming: asyncio.Queue[Connection] = asyncio.Queue()
        with session_credentials("semantic") as creds:
            info = SessionInfo.create(
                creds, "semantic", launcher_pid=os.getpid(), started_at=datetime.now(UTC)
            )
            store.set(SESSION_ENTRY, info.to_json())
            session = BellisSession(
                BellisConfig(store, task_authority=authority),
                info,
                "runtime",
                adapter,
                MonotonicClock(),
                SessionSignals(),
                ledger=ledger,
                release_verified=lambda _: proof.is_set(),
            )
            server = await listen(
                creds.path,
                creds.key,
                on_connection=incoming.put_nowait,
                on_rejected=lambda error: pytest.fail(str(error)),
                on_error=lambda error: pytest.fail(str(error)),
            )
            request = {
                "taskId": "task",
                "objective": "打开菜单",
                "expiresAtMs": ms(perf_counter_ns()) + 5000,
                "overallCompletion": None,
            }
            try:
                for index in range(2):
                    client = RpcPeer(
                        await connect(creds.path, "game-runtime.main", creds.key), timeout=WAIT
                    )
                    peer, cleanup = session.open_peer(await asyncio.wait_for(incoming.get(), WAIT))
                    try:
                        assert await client.request("game.describe", {"major": 1}) == {"major": 2}
                        with pytest.raises(RpcError, match="语义握手") as unnegotiated:
                            await client.request("task.open", request)
                        assert unnegotiated.value.code == RpcErrorCode.INVALID_PARAMS
                        description = v2.GameMainDescriptionV2.model_validate(
                            await client.request("game.describe", {"major": 2})
                        )
                        assert description.controlTargetId == TARGET
                        if index == 0:
                            with pytest.raises(RpcError, match="release-unverified"):
                                await client.request("task.open", request)
                            proof.set()
                        else:
                            proof.clear()  # 精确重送保留已授权的原任务。
                        assert await client.request("task.open", request) == {"taskId": "task"}
                        health = session.health()
                        assert isinstance(health, v2.GameMainHealthV2)
                        assert health.activeTaskId == "task"
                    finally:
                        session.detach()
                        for dispose in cleanup:
                            dispose()
                        await client.close()
                        await peer.close()
            finally:
                await server.close()

    asyncio.run(run())


def test_fake_release_argv_needs_explicit_isolated_bellis_setup(tmp_path: Path) -> None:
    with session_credentials("device") as device:
        options = CoreLinkOptions(executable=Path(sys.executable), test_device=device)
        assert "--fake-verify-release" not in options.argv()
        with pytest.raises(ValueError, match="隔离"):
            replace(options, fake_verify_release=True)
        bellis = BellisCoreOptions("session", "runtime", tmp_path / "session.json")
        selected = replace(options, bellis=bellis, fake_verify_release=True)
        assert selected.argv().count("--fake-verify-release") == 1
        with pytest.raises(ValueError, match="真实输入后端不能"):
            replace(selected, real_input=True)
        with pytest.raises(ValueError, match="明确的输入后端"):
            replace(selected, test_device=None)
        with pytest.raises(ValueError, match="隔离"):
            replace(selected, bellis=replace(bellis, test_session_file=None))


def test_supervise_projects_original_pending_identity_and_refuses_unverified_release(
    semantic_core: Path,
    tmp_path: Path,
) -> None:
    async def run() -> None:
        path = tmp_path / "ledger.sqlite3"
        previous, spec, _ = ledger_and_spec(path)
        assert previous.reserve_attempt(spec)
        previous.mark_pending(spec.attempt_id, "previous-unknown")
        authority = TaskAuthorityConfig(
            control_target_id=TARGET,
            adapter_id="synthetic-game",
            ledger_path=path,
            limits=LIMITS,
        )
        async with supervised_rig(semantic_core, tmp_path, task_authority=authority) as rig:
            links: dict[str, RpcPeer] = {}
            for _ in range(2):
                conn = await asyncio.wait_for(rig.peers.get(), WAIT)
                links[conn.role] = RpcPeer(conn, timeout=WAIT)
            main, core = links["game-runtime.main"], links["game-runtime.core"]
            reports: asyncio.Queue[v2.GameMainHealthV2] = asyncio.Queue()
            main.on_notification(
                "health.reported",
                lambda value: reports.put_nowait(v2.GameMainHealthV2.model_validate(value)),
            )
            try:
                assert rig.session.attempts is not None
                assert rig.run.kernel.active_control is None
                assert await main.request("game.describe", {"major": 1}) == {"major": 2}
                description = v2.GameMainDescriptionV2.model_validate(
                    await main.request("game.describe", {"major": 2})
                )
                assert description.controlTargetId == TARGET
                await core.request("game.describe", {"major": 1})
                health = await asyncio.wait_for(reports.get(), WAIT)
                assert health.activeTaskId is None
                assert not health.ready
                reported = health.pendingActionOutcome
                assert reported is not None
                assert (reported.runtimeId, reported.sessionId, reported.attemptId) == (
                    spec.runtime_id,
                    spec.session_id,
                    spec.attempt_id,
                )
                with pytest.raises(RpcError, match="release-unverified"):
                    await main.request(
                        "action.resolve_outcome",
                        {
                            "sessionId": spec.session_id,
                            "attemptId": spec.attempt_id,
                            "resolution": Resolution.RECONCILED_UNKNOWN.value,
                        },
                    )
                assert not rig.run.core.has_verified_release()
                assert rig.run.kernel.active_control is None
            finally:
                await main.close()
                await core.close()

    asyncio.run(run())


def test_release_requires_current_authenticated_pair_newer_than_the_action(tmp_path: Path) -> None:
    async def run() -> None:
        ledger, _, _ = ledger_and_spec(tmp_path / "ledger.sqlite3")
        with session_credentials("device") as device:
            options = CoreLinkOptions(
                Path(sys.executable),
                test_device=device,
                fake_verify_release=True,
                bellis=BellisCoreOptions("session", "runtime", tmp_path / "session.json"),
            )
            async with fake_core(ledger, options=options) as rig:
                release = models.InputReleasedParams(
                    release_id=6,
                    producer=None,
                    reason=models.ReleaseReason.CORE_START,
                    verification=models.ReleaseVerification.UNVERIFIED,
                    released_at_ns=perf_counter_ns(),
                )
                checked = models.InputVerificationParams(
                    release_id=5,
                    result=models.VerificationResult.VERIFIED,
                    checked_at_ns=perf_counter_ns(),
                )
                rig.peer.notify(INPUT_RELEASED.name, release.model_dump(mode="json"))
                rig.peer.notify(INPUT_VERIFICATION.name, checked.model_dump(mode="json"))

                async def sync() -> None:
                    # 同一有序连接的通知均应在这个请求前被处理。
                    with pytest.raises(RpcError):
                        await rig.peer.request("test.sync")

                await sync()
                assert not rig.link.has_verified_release()
                right = checked.model_copy(update={"release_id": release.release_id})
                rig.peer.notify(INPUT_VERIFICATION.name, right.model_dump(mode="json"))
                await sync()
                assert rig.link.has_verified_release()
                rig.link._last_action_request_ns = release.released_at_ns
                assert not rig.link.has_verified_release()
                rig.link._last_action_request_ns = 0
                assert rig.link._instance is not None
                assert rig.link._instance.peer is not None
                rig.link._instance.peer.connection.abort()
                assert not rig.link.has_verified_release()

    asyncio.run(run())
