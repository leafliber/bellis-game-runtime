"""GR-38 的账目、授权和真实 RPC 送达边界。"""

# pyright: reportPrivateUsage=false

import asyncio
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from threading import Event
from time import perf_counter_ns

import pytest

from core_protocol import models
from core_protocol.methods import INPUT_RELEASED, INPUT_VERIFICATION
from game_runtime.channel import RpcError, session_credentials
from game_runtime.core_link import BellisCoreOptions, CoreLinkOptions
from game_runtime.game_bridge.task_ledger import (
    AttemptPhase,
    AttemptRecord,
    CoreCorrelation,
    EffectEvidence,
    EffectResult,
    FrameStamp,
    LedgerConflictError,
    LedgerRefusedError,
    LedgerUnavailableError,
    Resolution,
    TaskAccount,
    TaskEnd,
    TaskLedger,
    TaskRequest,
)
from game_runtime.game_bridge.wire import v1, v2
from game_runtime.operator import MonotonicClock
from game_runtime.step_control.supervision import SemanticPauseError

from .test_attempt_integration import (
    LIMITS,
    OVERALL,
    TARGET,
    WAIT,
    fake_core,
    ledger_and_spec,
    semantic_connection,
    task_goal,
    task_session,
)


def block_worker(loop: asyncio.AbstractEventLoop, entered: asyncio.Event, release: Event) -> None:
    loop.call_soon_threadsafe(entered.set)
    if not release.wait(WAIT):
        raise TimeoutError("测试未释放账目线程")


def test_cancelled_completion_refreshes_committed_terminal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        path = tmp_path / "ledger.sqlite3"
        ledger, spec, _ = ledger_and_spec(path, overall=OVERALL)
        async with (
            fake_core(ledger) as rig,
            task_session(path, ledger, rig) as session,
            semantic_connection(session) as main,
        ):
            await main.request("goal.submit", task_goal(spec, "goal"))
            loop = asyncio.get_running_loop()
            entered, release = asyncio.Event(), Event()
            close = ledger.close_task

            def delayed_close(
                task_id: str,
                end: TaskEnd,
                *,
                before_write: Callable[[], None] | None = None,
            ) -> TaskAccount:
                completed = close(task_id, end, before_write=before_write)
                block_worker(loop, entered, release)
                return completed

            monkeypatch.setattr(ledger, "close_task", delayed_close)
            waiter = asyncio.create_task(
                session.complete_task(
                    session.session.goal_revision,
                    recheck=rig.grant.check,
                    on_recorded=lambda: pytest.fail("取消后不得发布完成事件"),
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), WAIT)
                waiter.cancel()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            task = ledger.get_task(spec.task_id)
            assert task is not None
            assert task.end is TaskEnd.COMPLETED
            health = session.health()
            assert isinstance(health, v2.GameMainHealthV2)
            assert health.activeTaskId is None
            assert session.planning_goal is None

    asyncio.run(run())


@pytest.mark.parametrize("committed", [False, True], ids=["quarantine-first", "effect-first"])
def test_receipt_conflict_races_with_effect_write_without_false_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    committed: bool,
) -> None:
    async def run() -> None:
        ledger, spec, step = ledger_and_spec(
            tmp_path / "ledger.sqlite3",
            receipt_timeout_ns=5_000_000_000,
        )
        initial = ledger.get_task(spec.task_id)
        assert initial is not None
        async with fake_core(ledger) as rig:
            loop = asyncio.get_running_loop()
            entered, release = asyncio.Event(), Event()
            original = ledger.record_effect

            def delayed_effect(attempt_id: str, evidence: EffectEvidence) -> AttemptRecord:
                if committed:
                    record = original(attempt_id, evidence)
                    block_worker(loop, entered, release)
                    return record
                block_worker(loop, entered, release)
                return original(attempt_id, evidence)

            async def verify(receipt: models.Executed, _: int) -> EffectEvidence:
                while perf_counter_ns() <= receipt.at_ns:
                    await asyncio.sleep(0)
                return EffectEvidence(
                    result=EffectResult.VERIFIED,
                    frame=FrameStamp(number=5, capture_ns=perf_counter_ns(), transform_version=1),
                )

            monkeypatch.setattr(ledger, "record_effect", delayed_effect)
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
            rig.executed(block)
            rig.reply.set()
            try:
                await asyncio.wait_for(entered.wait(), WAIT)
                rig.receipt(block, models.Dropped(id=7, reason=models.DropReason.RELEASED))
                async with asyncio.timeout(WAIT):
                    while not ledger.status().receipt_quarantined:
                        await asyncio.sleep(0.01)
            finally:
                release.set()
            if committed:
                with pytest.raises(SemanticPauseError, match="core-receipt-conflict"):
                    await waiter
                assert rig.records.empty()
                final = ledger.get_attempt(spec.attempt_id)
                assert final is not None
                assert final.phase is AttemptPhase.VERIFIED
            else:
                final = await waiter
                assert final.phase is AttemptPhase.PENDING
                assert final.effect is None
                assert final.detail == "core-receipt-conflict"
                assert rig.records.get_nowait() == final
                task = ledger.get_task(spec.task_id)
                assert task is not None
                assert task.last_progress_ns == initial.last_progress_ns
            assert rig.requests.empty()

    asyncio.run(run())


def test_expiry_read_during_binding_keeps_local_certain_non_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        ledger, spec, step = ledger_and_spec(
            tmp_path / "ledger.sqlite3",
            receipt_timeout_ns=1_000_000_000,
        )
        async with fake_core(ledger) as rig:
            loop = asyncio.get_running_loop()
            entered, release = asyncio.Event(), Event()
            bind = ledger.bind_core

            def delayed_bind(attempt_id: str, core: CoreCorrelation) -> bool:
                persisted = bind(attempt_id, core)
                block_worker(loop, entered, release)
                return persisted

            async def verify(_: models.Executed, __: int) -> EffectEvidence | None:
                pytest.fail("确证未写出不能取证")

            monkeypatch.setattr(ledger, "bind_core", delayed_bind)
            waiter = asyncio.create_task(
                rig.handler.run(
                    rig.grant,
                    step,
                    spec,
                    recheck=rig.grant.check,
                    verify=verify,
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), WAIT)
                while (remaining := spec.outcome_deadline_ns - perf_counter_ns()) > 0:
                    await asyncio.sleep(remaining / 1e9)
                pending = ledger.status().outstanding
                assert pending is not None
                assert pending.detail == "outcome-deadline"
            finally:
                release.set()
            final = await waiter
            assert final.phase is AttemptPhase.NOT_EXECUTED
            assert ledger.status().outstanding is None
            assert rig.requests.empty()

    asyncio.run(run())


def test_quarantined_status_refuses_a_decision_before_controller_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        path = tmp_path / "ledger.sqlite3"
        ledger, spec, _ = ledger_and_spec(path)
        async with (
            fake_core(ledger) as rig,
            task_session(path, ledger, rig) as session,
            semantic_connection(session) as main,
        ):
            await main.request("goal.submit", task_goal(spec, "goal"))
            decisions: list[str] = []

            def accept(request: v1.GameDecisionResolveParams, _: str | None) -> v1.GameAck:
                decisions.append(request.optionId)
                return v1.GameAck()

            monkeypatch.setattr(session, "_accept_choice", accept)
            request = {
                "decisionPointId": "point",
                "optionId": "option-0",
                "source": "director",
                "traceparent": None,
            }
            assert await main.request("decision.resolve", request) == {}
            assert ledger.reserve_attempt(spec)
            core = CoreCorrelation(
                runtime_id=spec.runtime_id,
                core_instance=1,
                producer_id=rig.grant.producer,
                generation=rig.grant.generation,
                block_seq=1,
                action_id=7,
            )
            assert ledger.bind_core(spec.attempt_id, core)
            ledger.record_receipt(core, models.Dropped(id=7, reason=models.DropReason.RELEASED))
            assert ledger.quarantine_conflicting_receipt(
                core, models.Executed(id=7, at_ns=perf_counter_ns())
            )
            with pytest.raises(RpcError, match="core-receipt-conflict"):
                await main.request("decision.resolve", request)
            assert decisions == ["option-0"]
            assert not session.may_autoplay

    asyncio.run(run())


def test_deadline_pending_cannot_get_a_local_non_dispatch_proof_after_epoch_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "ledger.sqlite3"
    ledger, spec, _ = ledger_and_spec(path)
    assert ledger.reserve_attempt(spec)
    monkeypatch.setattr(ledger, "_clock_ns", lambda: spec.outcome_deadline_ns)
    expired = ledger.status().outstanding
    assert expired is not None
    assert expired.detail == "outcome-deadline"

    successor = TaskLedger.start(path, TARGET)
    with pytest.raises(LedgerConflictError, match="无法按未写出"):
        successor.record_not_sent(spec.attempt_id, proof="foreign-process")
    assert successor.status().outstanding == expired


def test_late_authenticated_receipt_during_operator_resolution_is_archived(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        path = tmp_path / "ledger.sqlite3"
        ledger, spec, step = ledger_and_spec(path, receipt_timeout_ns=700_000_000)
        original = ledger.get_task(spec.task_id)
        assert original is not None
        with session_credentials("device") as device:
            options = CoreLinkOptions(
                Path(sys.executable),
                test_device=device,
                fake_verify_release=True,
                bellis=BellisCoreOptions("original", spec.runtime_id, tmp_path / "session.json"),
            )
            async with (
                fake_core(ledger, options=options) as rig,
                task_session(path, ledger, rig) as session,
                semantic_connection(session) as main,
            ):

                async def verify(_: models.Executed, __: int) -> EffectEvidence | None:
                    pytest.fail("结案后到达的回执只能归档")

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
                rig.reply.set()
                assert (await waiter).phase is AttemptPhase.PENDING
                released = models.InputReleasedParams(
                    release_id=1,
                    producer=block.producer,
                    reason=models.ReleaseReason.GENERATION_REVOKED,
                    verification=models.ReleaseVerification.UNVERIFIED,
                    released_at_ns=perf_counter_ns(),
                )
                verification = models.InputVerificationParams(
                    release_id=1,
                    result=models.VerificationResult.VERIFIED,
                    checked_at_ns=perf_counter_ns(),
                )
                rig.peer.notify(INPUT_RELEASED.name, released.model_dump(mode="json"))
                rig.peer.notify(INPUT_VERIFICATION.name, verification.model_dump(mode="json"))
                with pytest.raises(RpcError):
                    await rig.peer.request("test.sync")
                assert rig.link.has_verified_release()

                loop = asyncio.get_running_loop()
                entered, release = asyncio.Event(), Event()
                resolve = ledger.resolve_pending

                def delayed_resolution(
                    session_id: str,
                    attempt_id: str,
                    resolution: Resolution,
                    *,
                    release_verified: bool,
                ) -> AttemptRecord:
                    result = resolve(
                        session_id, attempt_id, resolution, release_verified=release_verified
                    )
                    block_worker(loop, entered, release)
                    return result

                monkeypatch.setattr(ledger, "resolve_pending", delayed_resolution)
                request = {
                    "attemptId": spec.attempt_id,
                    "resolution": "reconciled_unknown",
                    "sessionId": spec.session_id,
                }
                rpc = asyncio.create_task(main.request("action.resolve_outcome", request))
                try:
                    await asyncio.wait_for(entered.wait(), WAIT)
                    receipt = rig.executed(block)
                    async with asyncio.timeout(WAIT - 1):
                        while (
                            final := ledger.get_attempt(spec.attempt_id)
                        ) is not None and final.receipt is None:
                            await asyncio.sleep(0.01)
                finally:
                    release.set()
                assert await rpc == {
                    "attemptId": spec.attempt_id,
                    "resolution": "reconciled_unknown",
                }
                assert final is not None
                assert final.phase is AttemptPhase.OPERATOR_RESOLVED
                assert final.receipt == receipt
                assert final.resolution is Resolution.RECONCILED_UNKNOWN
                assert final.effect is None
                current = ledger.get_task(spec.task_id)
                assert current is not None
                assert current.last_progress_ns == original.last_progress_ns
                assert current.inputs_spent == original.inputs_spent + 1

    asyncio.run(run())


def test_process_claim_fences_epoch_and_releases_on_crash(tmp_path: Path) -> None:
    child_script = """
import os
import sys
from pathlib import Path
from time import perf_counter_ns
from game_runtime.game_bridge.task_ledger import (
    AttemptKind, AttemptSpec, FrameStamp, TaskLedger, TaskLimits, TaskRequest,
)
from game_runtime.step_control.contracts import EffectKind, ExpectedEffect
path, target = Path(sys.argv[1]), sys.argv[2]
now = perf_counter_ns()
ledger = TaskLedger.start(path, target, hold_owner=True)
ledger.open_task(
    TaskRequest(task_id="task", objective="fixture", expires_at_ns=now + 60_000_000_000),
    TaskLimits(calls=1, inputs=1, no_progress_ns=60_000_000_000),
    release_verified=True,
)
ledger.reserve_attempt(AttemptSpec(
    attempt_id="attempt", task_id="task", runtime_id="owner-runtime", session_id="owner",
    goal_id="goal", action_id="action", kind=AttemptKind.ORDINARY,
    base_frame=FrameStamp(number=1, capture_ns=now, transform_version=1),
    expected=ExpectedEffect(EffectKind.CHANGED),
    submit_deadline_ns=now + 40_000_000_000, outcome_deadline_ns=now + 50_000_000_000,
))
print("held", flush=True)
sys.stdin.read(1)
os._exit(23)
"""

    async def run() -> None:
        path = tmp_path / "ledger.sqlite3"
        alias_dir = tmp_path / "same-parent"
        alias_dir.mkdir()
        TaskLedger.provision(path, TARGET)
        with subprocess.Popen(
            [sys.executable, "-c", child_script, str(path), TARGET],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as child:
            try:
                assert child.stdout is not None
                reported = await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), WAIT)
                assert reported.strip() == b"held"
                with closing(sqlite3.connect(path)) as business:
                    epoch = business.execute("SELECT process_epoch FROM owner").fetchone()
                    with pytest.raises(LedgerUnavailableError, match="活动进程"):
                        TaskLedger.start(alias_dir / ".." / path.name, TARGET)
                    assert business.execute("SELECT process_epoch FROM owner").fetchone() == epoch
                assert child.stdin is not None
                child.stdin.write(b"x")
                child.stdin.flush()
                assert await asyncio.to_thread(child.wait, WAIT) == 23
            finally:
                if child.poll() is None:
                    child.kill()
        ledger = TaskLedger.start(path, TARGET, hold_owner=True)
        try:
            old = ledger.get_task("task")
            assert old is not None
            assert old.inputs_spent == 1
            assert old.end is TaskEnd.RESTARTED
            pending = ledger.status().outstanding
            assert pending is not None
            assert pending.phase is AttemptPhase.PENDING
            assert pending.detail == "process-restarted"
            assert pending.spec.runtime_id == "owner-runtime"
            with pytest.raises(LedgerRefusedError, match="attempt-outstanding"):
                ledger.open_task(
                    TaskRequest(
                        task_id="successor",
                        objective="fixture",
                        expires_at_ns=perf_counter_ns() + 5_000_000_000,
                    ),
                    LIMITS,
                    release_verified=True,
                )
        finally:
            ledger.close()

    asyncio.run(run())


def test_persisted_terminal_replay_does_not_publish_a_new_completion(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "ledger.sqlite3"
        ledger, spec, _ = ledger_and_spec(path, overall=OVERALL)
        async with (
            fake_core(ledger) as rig,
            task_session(path, ledger, rig) as session,
            semantic_connection(session) as main,
        ):
            await main.request("goal.submit", task_goal(spec, "goal"))
            old = ledger.close_task(spec.task_id, TaskEnd.COMPLETED)
            replay = await session.complete_task(
                session.session.goal_revision,
                recheck=lambda: pytest.fail("幂等回读不再取证"),
                on_recorded=lambda: pytest.fail("既有终态不能重发成功事件"),
            )
            assert replay == old
            assert session.planning_goal is None

    asyncio.run(run())


def test_subgoal_event_uses_committed_progress_past_old_idle_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Clock(MonotonicClock):
        def __init__(self, start: int) -> None:
            self.now = start

        def now_ns(self) -> int:
            return self.now

    async def run() -> None:
        path = tmp_path / "ledger.sqlite3"
        idle_ns = 2_000_000_000
        ledger, spec, _ = ledger_and_spec(path, limits=replace(LIMITS, no_progress_ns=idle_ns))
        original = ledger.get_task(spec.task_id)
        assert original is not None
        clock = Clock(original.last_progress_ns + idle_ns // 2)
        async with (
            fake_core(ledger) as rig,
            task_session(path, ledger, rig) as session,
            semantic_connection(session) as main,
        ):
            monkeypatch.setattr(ledger, "_clock_ns", clock.now_ns)
            session.clock = clock
            await main.request("goal.submit", task_goal(spec, "goal"))
            loop = asyncio.get_running_loop()
            entered, release = asyncio.Event(), Event()
            old_record = ledger.record_subgoal
            events: list[str] = []

            def delay_result(
                task_id: str,
                progress_id: str,
                *,
                before_write: Callable[[], None] | None = None,
            ) -> bool:
                recorded = old_record(task_id, progress_id, before_write=before_write)
                block_worker(loop, entered, release)
                return recorded

            monkeypatch.setattr(ledger, "record_subgoal", delay_result)
            waiter = asyncio.create_task(
                session.record_subgoal(
                    session.session.goal_revision,
                    "subgoal",
                    recheck=rig.grant.check,
                    on_recorded=lambda: events.append("accepted"),
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), WAIT)
                clock.now = original.last_progress_ns + idle_ns + 1
            finally:
                release.set()
            assert await waiter
            assert events == ["accepted"]
            assert session.may_autoplay
            assert not old_record(spec.task_id, "subgoal")

    asyncio.run(run())
