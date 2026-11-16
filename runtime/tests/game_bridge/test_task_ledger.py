import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from core_protocol.models import Dropped, DropReason, Executed, Rejected, RejectReason, Uncertain
from game_runtime.game_bridge import task_ledger
from game_runtime.game_bridge.task_ledger import (
    AttemptKind,
    AttemptPhase,
    AttemptSpec,
    CallKind,
    CoreCorrelation,
    CoreReceipt,
    EffectEvidence,
    EffectResult,
    FrameStamp,
    LedgerConflictError,
    LedgerRefusedError,
    LedgerUnavailableError,
    Refusal,
    Resolution,
    TaskAccount,
    TaskEnd,
    TaskLedger,
    TaskLimits,
    TaskRequest,
)
from game_runtime.step_control.contracts import EffectKind, ExpectedEffect

TARGET = "configured-slot"
TASK = "task"
ATTEMPT = "attempt"
SESSION = "original-session"
FRAME = FrameStamp(number=7, capture_ns=90, transform_version=3)
EFFECT = ExpectedEffect(EffectKind.SIGNATURE, signature="menu-open")
CORE = CoreCorrelation(
    runtime_id="original-runtime",
    core_instance=2,
    producer_id="step-control",
    generation=3,
    block_seq=4,
    action_id=(1 << 64) - 1,
)


class Clock:
    def __init__(self) -> None:
        self.now = 100

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "ledger 零.sqlite3"
    TaskLedger.provision(path, TARGET)
    return path


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def ledger(db_path: Path, clock: Clock) -> TaskLedger:
    return TaskLedger.start(db_path, TARGET, clock_ns=clock)


def _request(task_id: str = TASK, *, expires: int = 1000) -> TaskRequest:
    return TaskRequest(
        task_id=task_id, objective="打开菜单", expires_at_ns=expires, overall_completion=EFFECT
    )


def _limits(*, calls: int = 3, inputs: int = 2, idle: int = 800) -> TaskLimits:
    return TaskLimits(calls=calls, inputs=inputs, no_progress_ns=idle)


def _open(ledger: TaskLedger, *, calls: int = 3, inputs: int = 2) -> TaskAccount:
    return ledger.open_task(_request(), _limits(calls=calls, inputs=inputs), release_verified=True)


def _attempt(
    attempt_id: str = ATTEMPT, *, kind: AttemptKind = AttemptKind.ORDINARY, goal_id: str = "goal"
) -> AttemptSpec:
    return AttemptSpec(
        attempt_id=attempt_id,
        task_id=TASK,
        runtime_id=CORE.runtime_id,
        session_id=SESSION,
        goal_id=goal_id,
        action_id="semantic-action",
        kind=kind,
        base_frame=FRAME,
        expected=EFFECT,
        submit_deadline_ns=400,
        outcome_deadline_ns=500,
        decision_point_id="choice-a" if kind is AttemptKind.CHOICE else None,
    )


def _bound(ledger: TaskLedger, spec: AttemptSpec | None = None) -> None:
    planned = _attempt() if spec is None else spec
    assert ledger.reserve_attempt(planned)
    assert ledger.bind_core(planned.attempt_id, CORE)


def _executed(clock: Clock) -> Executed:
    clock.now = 120
    return Executed(id=CORE.action_id, at_ns=115)


def _evidence(result: EffectResult = EffectResult.VERIFIED) -> EffectEvidence:
    return EffectEvidence(
        result=result, frame=FrameStamp(number=8, capture_ns=125, transform_version=3)
    )


def _account(ledger: TaskLedger) -> TaskAccount:
    task = ledger.get_task(TASK)
    assert task is not None
    return task


def test_provision_is_explicit_and_start_refuses_lost_swapped_or_uninitialized_store(
    db_path: Path, ledger: TaskLedger, tmp_path: Path
) -> None:
    old = _open(ledger)
    with pytest.raises(FileExistsError):
        TaskLedger.provision(db_path, TARGET)
    with pytest.raises(LedgerUnavailableError):
        TaskLedger.start(db_path, "other-configured-slot")
    assert _account(ledger) == old
    other_path = tmp_path / "other.sqlite3"
    TaskLedger.provision(other_path, "other-configured-slot")
    with pytest.raises(LedgerUnavailableError):
        TaskLedger.start(other_path, TARGET)
    uninitialized = tmp_path / "blank.sqlite3"
    uninitialized.touch()
    with pytest.raises(LedgerUnavailableError):
        TaskLedger.start(uninitialized, TARGET)

    db_path.rename(tmp_path / "saved.sqlite3")
    with pytest.raises(LedgerUnavailableError):
        TaskLedger.start(db_path, TARGET)
    with pytest.raises(LedgerUnavailableError):
        ledger.reserve_call(TASK, "call", CallKind.PLANNER)
    assert not db_path.exists()


def test_same_task_budget_survives_new_goals_and_replay(ledger: TaskLedger) -> None:
    with pytest.raises(LedgerRefusedError, match=Refusal.RELEASE_UNVERIFIED):
        ledger.open_task(_request(), _limits(), release_verified=False)
    original = _open(ledger)
    for number, kind in enumerate(CallKind):
        assert ledger.check_goal(TASK).request == original.request
        assert ledger.reserve_call(TASK, f"call-{number}", kind)
        assert not ledger.reserve_call(TASK, f"call-{number}", kind)
    with pytest.raises(LedgerConflictError, match="调用身份"):
        ledger.reserve_call(TASK, "call-0", CallKind.POLICY)
    with pytest.raises(LedgerRefusedError, match=Refusal.CALL_BUDGET):
        ledger.reserve_call(TASK, "next", CallKind.POLICY)
    with pytest.raises(LedgerRefusedError, match=Refusal.CALL_BUDGET):
        ledger.check_goal(TASK)

    # 最后一次已付费的调用仍可产生原目标内的动作。换目标与接纳回包重送均不重置预算。
    for index in range(2):
        spec = _attempt(f"attempt-{index}", goal_id=f"goal-{index}")
        assert ledger.reserve_attempt(spec)
        assert not ledger.reserve_attempt(spec)
        ledger.record_not_sent(spec.attempt_id, proof="before-request")
    with pytest.raises(LedgerRefusedError, match=Refusal.INPUT_BUDGET):
        ledger.reserve_attempt(_attempt())
    reconnect = ledger.open_task(_request(), _limits(calls=99, inputs=99), release_verified=False)
    assert reconnect == _account(ledger) == ledger.status().active_task
    assert reconnect.limits == original.limits
    assert (reconnect.calls_spent, reconnect.inputs_spent) == (3, 2)
    with pytest.raises(LedgerConflictError, match="任务 ID"):
        ledger.open_task(
            replace(_request(), objective="切换别处"), _limits(), release_verified=True
        )
    with pytest.raises(LedgerRefusedError, match=Refusal.TASK_ALREADY_ACTIVE):
        ledger.open_task(_request("next"), _limits(), release_verified=True)
    with pytest.raises(LedgerRefusedError, match=Refusal.TASK_NOT_ACTIVE):
        ledger.check_goal("next")


def test_no_progress_boundary_uses_verified_subgoals_and_replay_cannot_extend_it(
    ledger: TaskLedger, clock: Clock
) -> None:
    ledger.open_task(_request(), _limits(idle=100), release_verified=True)
    clock.now = 140
    assert ledger.record_subgoal(TASK, "verified-step")
    clock.now = 200
    assert not ledger.record_subgoal(TASK, "verified-step")
    assert ledger.check_goal(TASK).last_progress_ns == 140
    assert ledger.reserve_call(TASK, "new-goal-policy", CallKind.POLICY)
    clock.now = 240
    with pytest.raises(LedgerRefusedError, match=Refusal.NO_PROGRESS):
        ledger.check_goal(TASK)
    assert ledger.status().active_task is None
    assert _account(ledger).end is TaskEnd.NO_PROGRESS
    assert not ledger.record_subgoal(TASK, "verified-step")


def test_absolute_task_deadline_and_original_action_deadline(
    ledger: TaskLedger, clock: Clock
) -> None:
    ledger.open_task(_request(expires=390), _limits(idle=900), release_verified=True)
    with pytest.raises(LedgerConflictError, match="超出原任务"):
        ledger.reserve_attempt(_attempt())
    spec = replace(_attempt(), submit_deadline_ns=380)
    assert ledger.reserve_attempt(spec)
    clock.now = 380
    with pytest.raises(LedgerRefusedError, match=Refusal.EXPIRED):
        ledger.bind_core(ATTEMPT, CORE)
    ledger.record_not_sent(ATTEMPT, proof="before-request-deadline")
    clock.now = 390
    assert ledger.status().active_task is None
    assert _account(ledger).end is TaskEnd.EXPIRED
    with pytest.raises(LedgerRefusedError, match=Refusal.EXPIRED):
        ledger.reserve_call(TASK, "late", CallKind.PLANNER)


def _race(perform: Callable[[int], bool]) -> list[bool | Refusal]:
    barrier = Barrier(2)

    def run(index: int) -> bool | Refusal:
        barrier.wait(timeout=5)
        try:
            return perform(index)
        except LedgerRefusedError as exc:
            return exc.reason

    with ThreadPoolExecutor(max_workers=2) as threads:
        return list(threads.map(run, (0, 1)))


def test_concurrent_duplicate_is_not_another_dispatch_and_budget_has_one_last_slot(
    ledger: TaskLedger,
) -> None:
    _open(ledger, calls=2)
    replay = _race(lambda _: ledger.reserve_call(TASK, "shared", CallKind.RANK))
    assert replay.count(True) == replay.count(False) == 1
    results = _race(lambda i: ledger.reserve_call(TASK, f"new-{i}", CallKind.POLICY))
    assert results.count(True) == results.count(Refusal.CALL_BUDGET) == 1
    assert _account(ledger).calls_spent == 2


def test_concurrent_attempts_are_serialized_before_writing(ledger: TaskLedger) -> None:
    _open(ledger, inputs=2)
    results = _race(lambda i: ledger.reserve_attempt(_attempt(f"attempt-{i}")))
    assert results.count(True) == results.count(Refusal.ATTEMPT_OUTSTANDING) == 1
    assert _account(ledger).inputs_spent == 1
    attempt = ledger.status().outstanding
    assert attempt is not None
    assert _race(lambda _: ledger.bind_core(attempt.spec.attempt_id, CORE)).count(True) == 1


def test_concurrent_completion_and_attempt_reservation_cannot_both_commit(
    ledger: TaskLedger,
) -> None:
    _open(ledger)

    def compete(index: int) -> bool:
        if index == 0:
            return ledger.close_task(TASK, TaskEnd.COMPLETED).end is TaskEnd.COMPLETED
        _bound(ledger)
        return True

    results = _race(compete)
    assert results.count(True) == 1
    state = ledger.status()
    if _account(ledger).end is TaskEnd.COMPLETED:
        assert Refusal.TASK_NOT_ACTIVE in results
        assert state.outstanding is None
        assert _account(ledger).inputs_spent == 0
    else:
        assert Refusal.ATTEMPT_OUTSTANDING in results
        assert state.outstanding is not None
        assert state.outstanding.phase is AttemptPhase.AWAITING_RECEIPT
        assert _account(ledger).inputs_spent == 1


@pytest.mark.parametrize("pending", [False, True])
def test_recorded_attempt_blocks_completion_and_survives_revocation(
    ledger: TaskLedger, pending: bool
) -> None:
    _open(ledger)
    _bound(ledger)
    if pending:
        ledger.mark_pending(ATTEMPT, "capture-interrupted")
    outstanding = ledger.status().outstanding
    assert outstanding is not None
    with pytest.raises(LedgerRefusedError, match=Refusal.ATTEMPT_OUTSTANDING):
        ledger.close_task(TASK, TaskEnd.COMPLETED)
    assert _account(ledger).end is None
    assert ledger.close_task(TASK, TaskEnd.REVOKED).end is TaskEnd.REVOKED
    assert ledger.status().outstanding == outstanding


@pytest.mark.parametrize("fail", ["calls", "attempts"])
def test_rejected_write_rolls_back_identity_and_budget(
    ledger: TaskLedger, db_path: Path, fail: str
) -> None:
    _open(ledger)
    sql = (
        "CREATE TRIGGER reject_write BEFORE INSERT ON calls BEGIN SELECT RAISE(ABORT, 'fault'); END"
        if fail == "calls"
        else (
            "CREATE TRIGGER reject_write BEFORE INSERT ON attempts "
            "BEGIN SELECT RAISE(ABORT, 'fault'); END"
        )
    )

    def reserve() -> bool:
        if fail == "calls":
            return ledger.reserve_call(TASK, "planned", CallKind.PLANNER)
        return ledger.reserve_attempt(_attempt())

    with closing(sqlite3.connect(db_path)) as setup:
        setup.execute(sql)
        with pytest.raises(LedgerUnavailableError):
            reserve()
        assert (_account(ledger).calls_spent, _account(ledger).inputs_spent) == (0, 0)
        assert ledger.get_attempt(ATTEMPT) is None
        setup.execute("DROP TRIGGER reject_write")
    assert reserve()


def test_failed_commit_does_not_return_first_bind_or_leave_an_association(
    ledger: TaskLedger, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open(ledger)
    assert ledger.reserve_attempt(_attempt())
    monkeypatch.setattr(task_ledger, "_LOCK_TIMEOUT_S", 0)
    with closing(sqlite3.connect(db_path, isolation_level=None)) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM attempts").fetchall()
        with pytest.raises(LedgerUnavailableError) as failure:
            ledger.bind_core(ATTEMPT, CORE)
        assert isinstance(failure.value.__cause__, sqlite3.OperationalError)
        reader.execute("ROLLBACK")
    attempt = ledger.get_attempt(ATTEMPT)
    assert attempt is not None
    assert attempt.core is None
    assert attempt.phase is AttemptPhase.RESERVED
    assert _account(ledger).inputs_spent == 1
    assert ledger.bind_core(ATTEMPT, CORE)
    assert not ledger.bind_core(ATTEMPT, CORE)


def test_committed_bind_with_lost_ack_cannot_be_used_to_send_on_retry(
    ledger: TaskLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open(ledger)
    assert ledger.reserve_attempt(_attempt())
    transaction = task_ledger._transaction  # pyright: ignore[reportPrivateUsage]

    @contextmanager
    def lose_ack(path: Path) -> Generator[sqlite3.Connection]:
        with transaction(path) as db:
            yield db
        raise LedgerUnavailableError("commit response lost")

    with monkeypatch.context() as failed:
        failed.setattr(task_ledger, "_transaction", lose_ack)
        with pytest.raises(LedgerUnavailableError, match="commit response lost"):
            ledger.bind_core(ATTEMPT, CORE)
    persisted = ledger.status().outstanding
    assert persisted is not None
    assert persisted.phase is AttemptPhase.AWAITING_RECEIPT
    assert persisted.core == CORE
    assert not ledger.bind_core(ATTEMPT, CORE)
    assert _account(ledger).inputs_spent == 1


def test_invalid_store_record_does_not_look_like_idle(ledger: TaskLedger, db_path: Path) -> None:
    _open(ledger)
    assert ledger.reserve_attempt(_attempt())
    with closing(sqlite3.connect(db_path)) as writer, writer:
        writer.execute("UPDATE attempts SET spec = ?", (b"{}",))
    with pytest.raises(LedgerUnavailableError, match="不完整"):
        ledger.status()
    with pytest.raises(LedgerUnavailableError, match="不完整"):
        ledger.reserve_attempt(_attempt("other"))


def test_original_attempt_can_finish_after_revocation_with_exact_core_and_frame(
    ledger: TaskLedger, clock: Clock
) -> None:
    original = _open(ledger)
    spec = _attempt()
    _bound(ledger, spec)
    with pytest.raises(LedgerConflictError, match="尝试 ID"):
        ledger.reserve_attempt(replace(spec, goal_id="new-goal"))
    with pytest.raises(LedgerConflictError, match="其它核动作"):
        ledger.bind_core(ATTEMPT, replace(CORE, block_seq=5))
    with pytest.raises(LedgerConflictError, match="已执行回执"):
        ledger.record_effect(ATTEMPT, _evidence())
    assert ledger.close_task(TASK, TaskEnd.REVOKED).end is TaskEnd.REVOKED
    assert ledger.status().active_task is None
    with pytest.raises(LedgerRefusedError, match=Refusal.ATTEMPT_OUTSTANDING):
        ledger.open_task(_request("new-task"), _limits(), release_verified=True)
    receipt = _executed(clock)
    with pytest.raises(LedgerConflictError, match="核关联"):
        ledger.record_receipt(replace(CORE, core_instance=4), receipt)
    with pytest.raises(LedgerConflictError, match="核关联"):
        ledger.record_receipt(CORE, Executed(id=0, at_ns=receipt.at_ns))
    awaiting = ledger.record_receipt(CORE, receipt)
    assert awaiting.core == CORE
    assert awaiting.spec == spec
    assert awaiting.phase is AttemptPhase.AWAITING_EFFECT

    for frame in (
        replace(_evidence().frame, transform_version=2),
        replace(_evidence().frame, number=7),
        replace(_evidence().frame, capture_ns=receipt.at_ns),
        replace(_evidence().frame, capture_ns=spec.outcome_deadline_ns),
    ):
        with pytest.raises(LedgerConflictError, match="执行后画面"):
            ledger.record_effect(ATTEMPT, EffectEvidence(result=EffectResult.VERIFIED, frame=frame))
    clock.now = 130
    done = ledger.record_effect(ATTEMPT, _evidence())
    assert done.phase is AttemptPhase.VERIFIED
    assert done.resolution is None
    assert ledger.record_receipt(CORE, receipt) == done
    assert ledger.record_effect(ATTEMPT, _evidence()) == done
    assert _account(ledger).last_progress_ns == original.last_progress_ns
    assert ledger.status().outstanding is None


@pytest.mark.parametrize(
    ("receipt", "phase"),
    [
        (Dropped(id=CORE.action_id, reason=DropReason.SUPERSEDED), AttemptPhase.NOT_EXECUTED),
        (Rejected(reason=RejectReason.EXPIRED, message="expired"), AttemptPhase.NOT_EXECUTED),
        (Dropped(id=CORE.action_id, reason=DropReason.DUPLICATE), AttemptPhase.PENDING),
        (Rejected(reason=RejectReason.STALE_SEQ, message="old seq"), AttemptPhase.PENDING),
        (Uncertain(id=CORE.action_id, at_ns=115), AttemptPhase.PENDING),
        (Executed(id=CORE.action_id, at_ns=99), AttemptPhase.PENDING),
        (Executed(id=CORE.action_id, at_ns=400), AttemptPhase.PENDING),
    ],
)
def test_core_receipts_distinguish_reliable_non_execution(
    ledger: TaskLedger, clock: Clock, receipt: CoreReceipt, phase: AttemptPhase
) -> None:
    _open(ledger)
    _bound(ledger)
    clock.now = 420
    record = ledger.record_receipt(CORE, receipt)
    assert record.phase is phase
    assert record.receipt == receipt
    assert record.effect is None
    assert ledger.record_receipt(CORE, receipt) == record


def test_confirmed_local_not_sent_keeps_proof_and_does_not_refund_budget(
    ledger: TaskLedger,
) -> None:
    _open(ledger, inputs=1)
    _bound(ledger)
    record = ledger.record_not_sent(ATTEMPT, proof="permit-revoked-before-request")
    assert record.phase is AttemptPhase.NOT_EXECUTED
    assert record.receipt is None
    assert record.detail == "permit-revoked-before-request"
    assert ledger.record_not_sent(ATTEMPT, proof=record.detail) == record
    with pytest.raises(LedgerConflictError, match="无法按未写出"):
        ledger.record_not_sent(ATTEMPT, proof="a-different-fact")
    with pytest.raises(LedgerRefusedError, match=Refusal.INPUT_BUDGET):
        ledger.check_goal(TASK)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (AttemptKind.ORDINARY, AttemptPhase.NO_EFFECT),
        (AttemptKind.CHOICE, AttemptPhase.PENDING),
        (AttemptKind.RESTART, AttemptPhase.PENDING),
    ],
)
def test_only_ordinary_no_effect_can_finish_without_operator(
    ledger: TaskLedger, clock: Clock, kind: AttemptKind, expected: AttemptPhase
) -> None:
    original = _open(ledger)
    _bound(ledger, _attempt(kind=kind))
    ledger.record_receipt(CORE, _executed(clock))
    clock.now = 130
    assert ledger.record_effect(ATTEMPT, _evidence(EffectResult.NO_EFFECT)).phase is expected
    assert _account(ledger).last_progress_ns == original.last_progress_ns
    assert (ledger.status().outstanding is not None) == (expected is AttemptPhase.PENDING)


def test_verified_effect_extends_progress_only_once(ledger: TaskLedger, clock: Clock) -> None:
    ledger.open_task(_request(), _limits(idle=100), release_verified=True)
    _bound(ledger)
    ledger.record_receipt(CORE, _executed(clock))
    clock.now = 150
    done = ledger.record_effect(ATTEMPT, _evidence())
    assert done.phase is AttemptPhase.VERIFIED
    clock.now = 240
    assert ledger.record_effect(ATTEMPT, _evidence()) == done
    assert ledger.check_goal(TASK).last_progress_ns == 150
    clock.now = 250
    assert ledger.status().active_task is None


@pytest.mark.parametrize("result", list(EffectResult))
def test_future_effect_frame_cannot_close_attempt_or_admit_goal(
    ledger: TaskLedger, clock: Clock, result: EffectResult
) -> None:
    original = _open(ledger)
    _bound(ledger)
    waiting = ledger.record_receipt(CORE, _executed(clock))
    evidence = _evidence(result)
    assert clock.now < evidence.frame.capture_ns
    with pytest.raises(LedgerConflictError, match="执行后画面"):
        ledger.record_effect(ATTEMPT, evidence)
    assert ledger.get_attempt(ATTEMPT) == waiting
    assert _account(ledger).last_progress_ns == original.last_progress_ns
    with pytest.raises(LedgerRefusedError, match=Refusal.ATTEMPT_OUTSTANDING):
        ledger.check_goal(TASK)

    clock.now = evidence.frame.capture_ns
    done = ledger.record_effect(ATTEMPT, evidence)
    if result is EffectResult.VERIFIED:
        assert done.phase is AttemptPhase.VERIFIED
        assert ledger.check_goal(TASK).last_progress_ns == clock.now
    else:
        assert done.phase is AttemptPhase.NO_EFFECT
        assert ledger.check_goal(TASK).last_progress_ns == original.last_progress_ns


def test_late_receipt_preserves_pending_and_resolution_is_separate_idempotent_fact(
    ledger: TaskLedger, clock: Clock
) -> None:
    original = _open(ledger)
    _bound(ledger)
    clock.now = 500
    pending = ledger.record_receipt(CORE, Dropped(id=CORE.action_id, reason=DropReason.RELEASED))
    assert pending.phase is AttemptPhase.PENDING
    assert pending.detail == "outcome-deadline"
    with pytest.raises(LedgerConflictError):
        ledger.record_not_sent(ATTEMPT, proof="late-unrelated-claim")
    with pytest.raises(LedgerConflictError, match="原会话"):
        ledger.resolve_pending(
            "new-session", ATTEMPT, Resolution.CONFIRMED_EXECUTED, release_verified=True
        )
    with pytest.raises(LedgerRefusedError, match=Refusal.RELEASE_UNVERIFIED):
        ledger.resolve_pending(
            SESSION, ATTEMPT, Resolution.CONFIRMED_EXECUTED, release_verified=False
        )
    done = ledger.resolve_pending(
        SESSION, ATTEMPT, Resolution.CONFIRMED_EXECUTED, release_verified=True
    )
    assert done.phase is AttemptPhase.OPERATOR_RESOLVED
    assert done.effect is None
    assert done.receipt == pending.receipt
    assert done.resolution is Resolution.CONFIRMED_EXECUTED
    assert ledger.resolve_pending(SESSION, ATTEMPT, done.resolution, release_verified=False) == done
    with pytest.raises(LedgerConflictError, match="人工事实冲突"):
        ledger.resolve_pending(
            SESSION, ATTEMPT, Resolution.RECONCILED_UNKNOWN, release_verified=True
        )
    assert _account(ledger).last_progress_ns == original.last_progress_ns
    assert ledger.status().outstanding is None


def test_interleaved_process_generations_fence_the_old_instance(
    ledger: TaskLedger, db_path: Path, clock: Clock
) -> None:
    _open(ledger)
    assert ledger.reserve_call(TASK, "charged", CallKind.POLICY)
    assert ledger.reserve_attempt(_attempt())
    successor = TaskLedger.start(db_path, TARGET, clock_ns=clock)
    with pytest.raises(LedgerUnavailableError, match="进程代"):
        ledger.bind_core(ATTEMPT, CORE)
    with pytest.raises(LedgerUnavailableError, match="进程代"):
        ledger.record_not_sent(ATTEMPT, proof="stale")
    with pytest.raises(LedgerUnavailableError, match="进程代"):
        ledger.status()
    stopped = _account(successor)
    assert stopped.end is TaskEnd.RESTARTED
    assert (stopped.calls_spent, stopped.inputs_spent) == (
        1,
        1,
    )
    assert successor.status().active_task is None
    pending = successor.status().outstanding
    assert pending is not None
    assert pending.phase is AttemptPhase.PENDING
    assert pending.core is None
    assert successor.open_task(_request(), _limits(calls=99), release_verified=False) == stopped
    assert not successor.reserve_call(TASK, "charged", CallKind.POLICY)
    assert not successor.reserve_attempt(_attempt())
    with pytest.raises(LedgerConflictError):
        successor.bind_core(ATTEMPT, CORE)
    with pytest.raises(LedgerRefusedError, match=Refusal.ATTEMPT_OUTSTANDING):
        successor.open_task(_request("new"), _limits(), release_verified=True)
    successor.resolve_pending(
        SESSION, ATTEMPT, Resolution.RECONCILED_UNKNOWN, release_verified=True
    )
    third = TaskLedger.start(db_path, TARGET, clock_ns=clock)
    assert (
        third.open_task(_request("new"), _limits(), release_verified=True).request.task_id == "new"
    )
    with pytest.raises(LedgerUnavailableError, match="进程代"):
        successor.resolve_pending(
            SESSION, ATTEMPT, Resolution.RECONCILED_UNKNOWN, release_verified=False
        )


def _crash_worker(path: Path, phase: str) -> None:
    clock = Clock()
    ledger = TaskLedger.start(path, TARGET, clock_ns=clock)
    _open(ledger)
    assert ledger.reserve_attempt(_attempt())
    if phase != "reserved":
        assert ledger.bind_core(ATTEMPT, CORE)
    if phase in ("receipt", "resolution"):
        ledger.record_receipt(CORE, _executed(clock))
    if phase == "resolution":
        ledger.mark_pending(ATTEMPT, "capture-interrupted")
        ledger.resolve_pending(
            SESSION, ATTEMPT, Resolution.RECONCILED_UNKNOWN, release_verified=True
        )
    os._exit(23)


@pytest.mark.parametrize("phase", ["reserved", "bound", "receipt", "resolution"])
def test_process_crash_after_committed_state_keeps_original_attempt(
    db_path: Path, phase: str
) -> None:
    dead = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            str(db_path),
            phase,
        ],
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert dead.returncode == 23, dead.stderr.decode(errors="replace")
    ledger = TaskLedger.start(db_path, TARGET, clock_ns=lambda: 1)
    assert _account(ledger).end is TaskEnd.RESTARTED
    assert _account(ledger).inputs_spent == 1
    original = ledger.get_attempt(ATTEMPT)
    assert original is not None
    assert original.spec == _attempt()
    assert original.core == (None if phase == "reserved" else CORE)
    if phase == "resolution":
        assert ledger.status().outstanding is None
        assert original.phase is AttemptPhase.OPERATOR_RESOLVED
        assert original.effect is None
        assert (
            ledger.resolve_pending(
                SESSION, ATTEMPT, Resolution.RECONCILED_UNKNOWN, release_verified=False
            )
            == original
        )
    else:
        assert original.phase is AttemptPhase.PENDING
        assert original.detail == "process-restarted"
        if phase == "receipt":
            assert original.receipt == Executed(id=CORE.action_id, at_ns=115)
        with pytest.raises(LedgerRefusedError, match=Refusal.ATTEMPT_OUTSTANDING):
            ledger.check_goal(TASK)


if __name__ == "__main__":
    _crash_worker(Path(sys.argv[1]), sys.argv[2])
