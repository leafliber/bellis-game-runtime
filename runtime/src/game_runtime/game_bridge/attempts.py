"""持久预约、核回执接收和原尝试只读核验的所有者。"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial

from core_protocol import models
from game_runtime._threads import in_thread
from game_runtime.game_bridge.task_ledger import (
    AttemptPhase,
    AttemptRecord,
    AttemptSpec,
    CoreCorrelation,
    CoreReceipt,
    EffectEvidence,
    LedgerConflictError,
    LedgerError,
    LedgerRefusedError,
    TaskLedger,
)
from game_runtime.kernel import ControlGrant
from game_runtime.step_control.supervision import SemanticPauseError

logger = logging.getLogger(__name__)
type VerifyEffect = Callable[[models.Executed, int], Awaitable[EffectEvidence | None]]


@dataclass(slots=True)
class _Work:
    grant: ControlGrant
    step: models.ControlStep
    spec: AttemptSpec
    recheck: Callable[[], None]
    verify: VerifyEffect
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    invalidated: asyncio.Event = field(default_factory=asyncio.Event)
    submit_settled: asyncio.Event = field(default_factory=asyncio.Event)
    submitter: asyncio.Task[None] | None = None
    core: CoreCorrelation | None = None
    receipt: CoreReceipt | None = None
    record: AttemptRecord | None = None
    attempted_write: bool = False
    abandoned: bool = False
    unsent: str | None = None
    invalid_reason: str | None = None
    conflict: bool = False

    def invalidate(self, reason: str) -> None:
        if self.invalid_reason is None:
            self.invalid_reason = reason
            self.invalidated.set()
            self.changed.set()


class AttemptHandler:
    def __init__(
        self,
        ledger: TaskLedger,
        clock_ns: Callable[[], int],
        admission: asyncio.Lock,
        authorize: Callable[[AttemptSpec], bool],
        on_record: Callable[[AttemptRecord], None],
        on_fault: Callable[[str], None],
        *,
        runtime_id: str,
    ) -> None:
        self._ledger = ledger
        self._clock_ns = clock_ns
        self._admission = admission
        self._authorize = authorize
        self._on_record = on_record
        self._on_fault = on_fault
        self._runtime_id = runtime_id
        self._receipt_checks = 0
        self._runner: asyncio.Task[AttemptRecord] | None = None
        self._work: _Work | None = None
        self._children: set[asyncio.Task[None]] = set()
        self._closed = False

    async def run(
        self,
        grant: ControlGrant,
        step: models.ControlStep,
        spec: AttemptSpec,
        *,
        recheck: Callable[[], None],
        verify: VerifyEffect,
    ) -> AttemptRecord:
        if self._closed or (self._runner is not None and not self._runner.done()):
            raise SemanticPauseError("attempt-outstanding")
        if step.offset_ns != 0 or step.held or len(step.actions) != 1:
            raise SemanticPauseError("attempt-requires-single-action")
        work = _Work(grant, step.model_copy(deep=True), spec, recheck, verify)
        runner = self._runner = asyncio.create_task(self._run(work), name="GR-38 动作尝试")
        runner.add_done_callback(self._finished)
        try:
            return await asyncio.shield(runner)
        except asyncio.CancelledError:
            if not work.attempted_write:
                work.abandoned = True
            raise

    @staticmethod
    def _finished(task: asyncio.Task[AttemptRecord]) -> None:
        if not task.cancelled():
            task.exception()  # 等待方可能已取消。

    def _start_child(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._children.add(task)
        task.add_done_callback(self._children.discard)

    def _require_authority(self, work: _Work) -> None:
        if (
            self._closed
            or self._receipt_checks != 0
            or work.abandoned
            or work.invalid_reason is not None
            or not self._authorize(work.spec)
        ):
            raise SemanticPauseError("attempt-authority-revoked")
        work.grant.check()
        if self._clock_ns() >= min(work.spec.submit_deadline_ns, work.spec.outcome_deadline_ns):
            raise SemanticPauseError("action-window-expired")

    async def _run(self, work: _Work) -> AttemptRecord:
        spec = work.spec
        try:
            async with self._admission:
                self._require_authority(work)
                first = await in_thread(partial(self._ledger.reserve_attempt, spec))
                if not first:
                    previous = await in_thread(partial(self._ledger.get_attempt, spec.attempt_id))
                    if previous is None:
                        raise LedgerConflictError("重复预约无法回读")
                    return previous
                self._work = work
            self._start_child(self._submit(work))
            record = await self._await_outcome(work)
            work.record = record
            self._on_record(record)
            self._queue_late_receipt(work)
            return record
        except LedgerRefusedError as error:
            raise SemanticPauseError(error.reason.value) from None
        except LedgerError as error:
            self._ledger_fault(error)
            raise SemanticPauseError("task-ledger-unavailable") from None

    async def _submit(self, work: _Work) -> None:
        work.submitter = asyncio.current_task()
        try:
            self._require_authority(work)
            remaining = work.spec.submit_deadline_ns - self._clock_ns()
            await work.grant.submit(
                (work.step,), base_frame=work.spec.base_frame.number, valid_for=remaining / 1e9
            )
        except Exception as error:
            if not work.attempted_write:
                if isinstance(error, LedgerError) and not isinstance(error, LedgerRefusedError):
                    self._ledger_fault(error)
                work.unsent = "submission-stopped-before-write"
            else:
                logger.info("原核请求应答不可用 %s", type(error).__name__)
        finally:
            work.submit_settled.set()
            work.changed.set()

    def submit_deadline(self, grant: ControlGrant) -> int:
        work = self._for_submitter(grant)
        return work.spec.submit_deadline_ns - 1

    def _for_submitter(self, grant: ControlGrant) -> _Work:
        work = self._work
        if (
            work is None
            or work.grant is not grant
            or work.submitter is not asyncio.current_task()
            or work.core is not None
            or work.record is not None
        ):
            raise SemanticPauseError("attempt-not-reserved")
        self._require_authority(work)
        return work

    async def bind_before_write(
        self, grant: ControlGrant, core_instance: int, block: models.ActionBlock
    ) -> CoreCorrelation:
        work = self._for_submitter(grant)
        if (
            block.base_frame != work.spec.base_frame.number
            or block.steps != [work.step]
            or block.producer.id != grant.producer
            or block.producer.generation != grant.generation
            or not block.start_ns < block.deadline_ns < work.spec.submit_deadline_ns
        ):
            raise SemanticPauseError("attempt-block-mismatch")
        core = CoreCorrelation(
            runtime_id=work.spec.runtime_id,
            core_instance=core_instance,
            producer_id=grant.producer,
            generation=grant.generation,
            block_seq=block.seq,
            action_id=work.step.actions[0].root.id,
        )
        if not await in_thread(partial(self._ledger.bind_core, work.spec.attempt_id, core)):
            raise LedgerConflictError("核关联已被使用")
        work.core = core
        return core

    def recheck_before_write(self, core: CoreCorrelation) -> None:
        work = self._work
        if work is None or work.core != core or work.attempted_write or work.record is not None:
            raise SemanticPauseError("attempt-core-mismatch")
        self._require_authority(work)
        work.recheck()
        self._require_authority(work)

    def begin_request(self, core: CoreCorrelation) -> None:
        work = self._work
        if work is None or work.core != core or work.attempted_write:
            raise SemanticPauseError("attempt-core-mismatch")
        work.attempted_write = True

    def submit_result(
        self, core: CoreCorrelation, result: models.Accepted | models.Rejected
    ) -> None:
        if isinstance(result, models.Rejected):
            self._offer_receipt(core, result)

    def action_receipt(self, core_instance: int, message: models.ActionReceiptParams) -> None:
        for outcome in message.outcomes:
            receipt = outcome.root
            core = CoreCorrelation(
                runtime_id=self._runtime_id,
                core_instance=core_instance,
                producer_id=message.producer.id,
                generation=message.producer.generation,
                block_seq=message.block_seq,
                action_id=receipt.id,
            )
            self._offer_receipt(core, receipt)

    def _offer_receipt(self, core: CoreCorrelation, receipt: CoreReceipt) -> None:
        work = self._work
        if work is None or work.core != core:
            self._inspect_conflict(core, receipt)
            return
        if not work.attempted_write:
            return
        if work.receipt is not None:
            if work.receipt != receipt:
                work.conflict = True
                work.invalidate("core-receipt-conflict")
                self._inspect_conflict(core, receipt, observed=work.receipt)
            return
        work.receipt = receipt
        work.changed.set()
        self._queue_late_receipt(work)

    def _inspect_conflict(
        self, core: CoreCorrelation, receipt: CoreReceipt, *, observed: CoreReceipt | None = None
    ) -> None:
        self._receipt_checks += 1
        self._start_child(self._check_conflict(core, receipt, observed))

    async def _check_conflict(
        self, core: CoreCorrelation, receipt: CoreReceipt, observed: CoreReceipt | None
    ) -> None:
        try:
            conflict = await in_thread(
                partial(
                    self._ledger.quarantine_conflicting_receipt, core, receipt, observed=observed
                )
            )
            if conflict:
                self.invalidate_evidence("core-receipt-conflict")
                self._on_fault("core-receipt-conflict")
        except LedgerError as error:
            self._ledger_fault(error)
        finally:
            self._receipt_checks -= 1

    def transport_lost(self, core_instance: int) -> None:
        work = self._work
        if work is not None and work.core is not None and work.core.core_instance == core_instance:
            work.invalidate("core-transport-lost")

    def invalidate_evidence(self, reason: str) -> None:
        if self._work is not None:
            self._work.invalidate(reason)

    async def _await_outcome(self, work: _Work) -> AttemptRecord:
        attempt_id = work.spec.attempt_id
        try:
            async with self._wait_until(work.spec.outcome_deadline_ns):
                while (
                    work.receipt is None
                    and work.unsent is None
                    and (not work.attempted_write or work.invalid_reason is None)
                ):
                    await work.changed.wait()
                    work.changed.clear()
        except TimeoutError:
            if not work.attempted_write:
                work.abandoned = True
                await work.submit_settled.wait()
        if work.unsent is not None:
            return await in_thread(
                partial(self._ledger.record_not_sent, attempt_id, proof=work.unsent)
            )
        if work.conflict:
            record = await self._pending(work, "core-receipt-conflict")
            self._on_fault("core-receipt-conflict")
            return record
        receipt, core = work.receipt, work.core
        if receipt is None or core is None:
            return await self._pending(work, work.invalid_reason or "receipt-timeout")
        record = await in_thread(partial(self._ledger.record_receipt, core, receipt))
        if record.phase is not AttemptPhase.AWAITING_EFFECT or not isinstance(
            receipt, models.Executed
        ):
            return record
        evidence = await self._verify(work, receipt)
        if evidence is None or work.invalid_reason is not None:
            record = await self._pending(work, work.invalid_reason or "effect-unverified")
            if work.conflict:
                self._on_fault("core-receipt-conflict")
            return record
        try:
            return await in_thread(partial(self._ledger.record_effect, attempt_id, evidence))
        except LedgerConflictError:
            return await self._pending(work, "effect-evidence-mismatch")

    def _wait_until(self, deadline_ns: int) -> asyncio.Timeout:
        return asyncio.timeout((deadline_ns - self._clock_ns()) / 1e9)

    async def _verify(self, work: _Work, receipt: models.Executed) -> EffectEvidence | None:
        if work.invalid_reason is not None:
            return None
        observation = asyncio.ensure_future(work.verify(receipt, work.spec.outcome_deadline_ns))
        invalidated = asyncio.create_task(work.invalidated.wait())
        tasks = (observation, invalidated)
        try:
            async with self._wait_until(work.spec.outcome_deadline_ns):
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                return (
                    observation.result() if observation.done() and not invalidated.done() else None
                )
        except Exception:
            return None
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _pending(self, work: _Work, reason: str) -> AttemptRecord:
        return await in_thread(partial(self._ledger.mark_pending, work.spec.attempt_id, reason))

    def _queue_late_receipt(self, work: _Work) -> None:
        record = work.record
        if record is not None and record.phase is AttemptPhase.PENDING and record.receipt is None:
            core, receipt = work.core, work.receipt
            if core is not None and receipt is not None:
                self._start_child(self._persist_late(work, core, receipt))

    async def _persist_late(self, work: _Work, core: CoreCorrelation, receipt: CoreReceipt) -> None:
        async with self._admission:
            try:
                record = await in_thread(partial(self._ledger.get_attempt, work.spec.attempt_id))
                if record is not None and record.phase is AttemptPhase.PENDING:
                    work.record = await in_thread(
                        partial(self._ledger.record_receipt, core, receipt)
                    )
            except LedgerError as error:
                self._ledger_fault(error)

    def _ledger_fault(self, error: LedgerError) -> None:
        logger.error("原尝试账目失效 %s", type(error).__name__)
        self._on_fault("task-ledger-unavailable")

    async def close(self) -> None:
        self._closed = True
        self.invalidate_evidence("runtime-stopping")
        if self._runner is not None:
            await asyncio.gather(self._runner, return_exceptions=True)
        if self._children:
            await asyncio.gather(*tuple(self._children), return_exceptions=True)
