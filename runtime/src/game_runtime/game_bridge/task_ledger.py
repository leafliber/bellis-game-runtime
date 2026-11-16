"""GR-38 单个可信被控对象的任务预算与动作账目。

游戏桥持有账库并传入已认证、已核验的事实。首次扣减或关联须等事务提交成功，随后仍走原许可门禁。
重连复用同一实例，主进程启动才换代。库里只存身份与核验事实，供原尝试对账。
"""

import sqlite3
from collections.abc import Callable, Generator
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from time import perf_counter_ns
from typing import Annotated
from uuid import uuid4

from pydantic import Field, TypeAdapter, ValidationError

from core_protocol.models import Dropped, DropReason, Executed, Rejected, RejectReason, Uncertain
from game_runtime.step_control.contracts import EffectKind, ExpectedEffect


class TaskEnd(StrEnum):
    COMPLETED = "completed"
    REVOKED = "revoked"
    EXPIRED = "expired"
    NO_PROGRESS = "no-progress"
    RESTARTED = "restarted"


class Refusal(StrEnum):
    TASK_NOT_ACTIVE = "task-not-active"
    TASK_ALREADY_ACTIVE = "task-already-active"
    EXPIRED = "expired"
    NO_PROGRESS = "no-progress"
    CALL_BUDGET = "call-budget"
    INPUT_BUDGET = "input-budget"
    ATTEMPT_OUTSTANDING = "attempt-outstanding"
    RELEASE_UNVERIFIED = "release-unverified"


class LedgerError(RuntimeError):
    """游戏桥无法继续本次账目操作。"""


class LedgerUnavailableError(LedgerError):
    """账库缺失、身份失效、读写失败或提交失败。"""


class LedgerConflictError(LedgerError):
    """身份或不可改写的事实冲突。"""


class LedgerRefusedError(LedgerError):
    def __init__(self, reason: Refusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskRequest:
    task_id: str
    objective: str
    expires_at_ns: int
    overall_completion: ExpectedEffect | None = None

    def __post_init__(self) -> None:
        if not self.task_id or not self.objective or self.expires_at_ns <= 0:
            raise ValueError("任务身份、根目标与截止须有效")
        if self.overall_completion is not None and self.overall_completion.kind in (
            EffectKind.CHANGED,
            EffectKind.UNCHANGED,
        ):
            raise ValueError("整体终局须由签名、画面类型或指定 ROI 的文字确定")


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskLimits:
    calls: int
    inputs: int
    no_progress_ns: int

    def __post_init__(self) -> None:
        if min(self.calls, self.inputs) < 0 or self.no_progress_ns <= 0:
            raise ValueError("任务预算无效")


@dataclass(frozen=True, slots=True)
class TaskAccount:
    request: TaskRequest
    limits: TaskLimits
    opened_at_ns: int
    last_progress_ns: int
    calls_spent: int
    inputs_spent: int
    end: TaskEnd | None


class CallKind(StrEnum):
    PLANNER = "planner"
    RANK = "rank"
    POLICY = "policy"


class AttemptKind(StrEnum):
    ORDINARY = "ordinary"
    CHOICE = "choice"
    RESTART = "restart"


@dataclass(frozen=True, slots=True, kw_only=True)
class FrameStamp:
    number: int
    capture_ns: int
    transform_version: int

    def __post_init__(self) -> None:
        if self.number < 1 or self.capture_ns < 0 or self.transform_version < 0:
            raise ValueError("帧身份、采集时间或变换版本无效")


@dataclass(frozen=True, slots=True, kw_only=True)
class AttemptSpec:
    attempt_id: str
    task_id: str
    runtime_id: str
    session_id: str
    goal_id: str
    action_id: str
    kind: AttemptKind
    base_frame: FrameStamp
    expected: ExpectedEffect
    submit_deadline_ns: int
    outcome_deadline_ns: int
    decision_point_id: str | None = None

    def __post_init__(self) -> None:
        if not all(
            (
                self.attempt_id,
                self.task_id,
                self.runtime_id,
                self.session_id,
                self.goal_id,
                self.action_id,
            )
        ):
            raise ValueError("尝试的原身份须完整")
        if self.base_frame.capture_ns >= min(self.submit_deadline_ns, self.outcome_deadline_ns):
            raise ValueError("基帧须早于动作与核验截止")
        if self.kind is AttemptKind.CHOICE and not self.decision_point_id:
            raise ValueError("选择须绑定原决策实例")


@dataclass(frozen=True, slots=True, kw_only=True)
class CoreCorrelation:
    runtime_id: str
    core_instance: int
    producer_id: str
    generation: int
    block_seq: int
    action_id: int

    def __post_init__(self) -> None:
        if (
            not self.runtime_id
            or not self.producer_id
            or min(self.core_instance, self.generation, self.block_seq, self.action_id) < 0
        ):
            raise ValueError("核关联须包含原实例、产出者与动作身份")


class AttemptPhase(StrEnum):
    RESERVED = "reserved"
    AWAITING_RECEIPT = "awaiting-receipt"
    AWAITING_EFFECT = "awaiting-effect"
    PENDING = "pending"
    NOT_EXECUTED = "not-executed"
    VERIFIED = "verified"
    NO_EFFECT = "ordinary-no-effect"
    OPERATOR_RESOLVED = "operator-resolved"


class EffectResult(StrEnum):
    VERIFIED = "verified"
    NO_EFFECT = "no-effect"


@dataclass(frozen=True, slots=True, kw_only=True)
class EffectEvidence:
    result: EffectResult
    frame: FrameStamp


class Resolution(StrEnum):
    CONFIRMED_EXECUTED = "confirmed_executed"
    CONFIRMED_NOT_EXECUTED = "confirmed_not_executed"
    RECONCILED_UNKNOWN = "reconciled_unknown"


type CoreReceipt = Annotated[Executed | Dropped | Uncertain | Rejected, Field(discriminator="type")]


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    spec: AttemptSpec
    phase: AttemptPhase
    core: CoreCorrelation | None
    bound_at_ns: int | None
    receipt: CoreReceipt | None
    effect: EffectEvidence | None
    detail: str | None
    resolution: Resolution | None


@dataclass(frozen=True, slots=True)
class LedgerStatus:
    active_task: TaskAccount | None
    outstanding: AttemptRecord | None


_TASK_REQUEST: TypeAdapter[TaskRequest] = TypeAdapter(TaskRequest)
_TASK_LIMITS: TypeAdapter[TaskLimits] = TypeAdapter(TaskLimits)
_ATTEMPT_SPEC: TypeAdapter[AttemptSpec] = TypeAdapter(AttemptSpec)
_CORE: TypeAdapter[CoreCorrelation] = TypeAdapter(CoreCorrelation)
_RECEIPT: TypeAdapter[CoreReceipt] = TypeAdapter(CoreReceipt)
_EFFECT: TypeAdapter[EffectEvidence] = TypeAdapter(EffectEvidence)

_LIVE = (AttemptPhase.RESERVED, AttemptPhase.AWAITING_RECEIPT, AttemptPhase.AWAITING_EFFECT)
_OUTSTANDING = (*_LIVE, AttemptPhase.PENDING)
_SCHEMA_VERSION = 1
_LOCK_TIMEOUT_S = 0.25

_SCHEMA = (
    """CREATE TABLE owner (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        control_target_id TEXT NOT NULL,
        process_epoch TEXT
    )""",
    """CREATE TABLE tasks (
        task_id TEXT PRIMARY KEY NOT NULL,
        request BLOB NOT NULL,
        limits BLOB NOT NULL,
        opened_at_ns INTEGER NOT NULL,
        last_progress_ns INTEGER NOT NULL,
        calls_spent INTEGER NOT NULL DEFAULT 0 CHECK (calls_spent >= 0),
        inputs_spent INTEGER NOT NULL DEFAULT 0 CHECK (inputs_spent >= 0),
        ended TEXT CHECK (ended IN ('completed', 'revoked', 'expired', 'no-progress', 'restarted'))
    )""",
    "CREATE UNIQUE INDEX active_task ON tasks((1)) WHERE ended IS NULL",
    """CREATE TABLE calls (
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        call_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        PRIMARY KEY (task_id, call_id)
    )""",
    """CREATE TABLE subgoals (
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        progress_id TEXT NOT NULL,
        PRIMARY KEY (task_id, progress_id)
    )""",
    """CREATE TABLE attempts (
        attempt_id TEXT PRIMARY KEY NOT NULL,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        spec BLOB NOT NULL,
        phase TEXT NOT NULL CHECK (phase IN (
            'reserved', 'awaiting-receipt', 'awaiting-effect', 'pending',
            'not-executed', 'verified', 'ordinary-no-effect', 'operator-resolved'
        )),
        core BLOB UNIQUE,
        bound_at_ns INTEGER,
        receipt BLOB,
        effect BLOB,
        detail TEXT,
        resolution TEXT,
        CHECK ((core IS NULL) = (bound_at_ns IS NULL)),
        CHECK (phase NOT IN ('awaiting-receipt', 'awaiting-effect') OR core IS NOT NULL),
        CHECK ((phase = 'operator-resolved') = (resolution IS NOT NULL))
    )""",
    """CREATE UNIQUE INDEX outstanding_attempt ON attempts((1))
        WHERE phase IN ('reserved', 'awaiting-receipt', 'awaiting-effect', 'pending')""",
)


@contextmanager
def _transaction(path: Path) -> Generator[sqlite3.Connection]:
    try:
        # rw 避免在文件消失或指定了错误路径时建立空账。
        with closing(
            sqlite3.connect(
                path.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=_LOCK_TIMEOUT_S
            )
        ) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA synchronous = EXTRA")
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db
    except sqlite3.Error as exc:
        raise LedgerUnavailableError("SQLite 账目事务未成功") from exc


def _owner(db: sqlite3.Connection, control_target_id: str) -> sqlite3.Row:
    if db.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION:
        raise LedgerUnavailableError("任务账目版本不受支持")
    owner = db.execute("SELECT * FROM owner WHERE singleton = 1").fetchone()
    if owner is None or owner["control_target_id"] != control_target_id:
        raise LedgerUnavailableError("任务账目与被控对象不符")
    return owner


def _task(row: sqlite3.Row) -> TaskAccount:
    return TaskAccount(
        request=_TASK_REQUEST.validate_json(row["request"]),
        limits=_TASK_LIMITS.validate_json(row["limits"]),
        opened_at_ns=row["opened_at_ns"],
        last_progress_ns=row["last_progress_ns"],
        calls_spent=row["calls_spent"],
        inputs_spent=row["inputs_spent"],
        end=TaskEnd(row["ended"]) if row["ended"] is not None else None,
    )


def _attempt(row: sqlite3.Row) -> AttemptRecord:
    return AttemptRecord(
        spec=_ATTEMPT_SPEC.validate_json(row["spec"]),
        phase=AttemptPhase(row["phase"]),
        core=_CORE.validate_json(row["core"]) if row["core"] is not None else None,
        bound_at_ns=row["bound_at_ns"],
        receipt=_RECEIPT.validate_json(row["receipt"]) if row["receipt"] is not None else None,
        effect=_EFFECT.validate_json(row["effect"]) if row["effect"] is not None else None,
        detail=row["detail"],
        resolution=Resolution(row["resolution"]) if row["resolution"] is not None else None,
    )


def _find_task(db: sqlite3.Connection, task_id: str) -> TaskAccount | None:
    row = db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    return None if row is None else _task(row)


def _find_attempt(db: sqlite3.Connection, attempt_id: str) -> AttemptRecord | None:
    row = db.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
    return None if row is None else _attempt(row)


def _known_attempt(db: sqlite3.Connection, attempt_id: str) -> AttemptRecord:
    attempt = _find_attempt(db, attempt_id)
    if attempt is None:
        raise LedgerConflictError("未找到原尝试")
    return attempt


def _outstanding(db: sqlite3.Connection) -> AttemptRecord | None:
    row = db.execute("SELECT * FROM attempts WHERE phase IN (?, ?, ?, ?)", _OUTSTANDING).fetchone()
    return None if row is None else _attempt(row)


def _ensure_idle(db: sqlite3.Connection) -> None:
    if _outstanding(db) is not None:
        raise LedgerRefusedError(Refusal.ATTEMPT_OUTSTANDING)


def _expire(db: sqlite3.Connection, task: TaskAccount, now_ns: int) -> TaskAccount:
    if task.end is not None:
        return task
    if now_ns >= task.request.expires_at_ns:
        end = TaskEnd.EXPIRED
    elif now_ns - task.last_progress_ns >= task.limits.no_progress_ns:
        end = TaskEnd.NO_PROGRESS
    else:
        return task
    db.execute("UPDATE tasks SET ended = ? WHERE task_id = ?", (end, task.request.task_id))
    return replace(task, end=end)


def _active(db: sqlite3.Connection, task_id: str, now_ns: int) -> TaskAccount:
    task = _find_task(db, task_id)
    if task is None:
        raise LedgerRefusedError(Refusal.TASK_NOT_ACTIVE)
    task = _expire(db, task, now_ns)
    if task.end is TaskEnd.EXPIRED:
        raise LedgerRefusedError(Refusal.EXPIRED)
    if task.end is TaskEnd.NO_PROGRESS:
        raise LedgerRefusedError(Refusal.NO_PROGRESS)
    if task.end is not None:
        raise LedgerRefusedError(Refusal.TASK_NOT_ACTIVE)
    return task


def _progress(db: sqlite3.Connection, task_id: str, now_ns: int) -> None:
    task = _find_task(db, task_id)
    if task is None:
        raise LedgerUnavailableError("尝试缺少原任务账目")
    if _expire(db, task, now_ns).end is None:
        db.execute("UPDATE tasks SET last_progress_ns = ? WHERE task_id = ?", (now_ns, task_id))


def _set_phase(
    db: sqlite3.Connection, attempt_id: str, phase: AttemptPhase, detail: str | None = None
) -> AttemptRecord:
    db.execute(
        "UPDATE attempts SET phase = ?, detail = ? WHERE attempt_id = ?",
        (phase, detail, attempt_id),
    )
    return _known_attempt(db, attempt_id)


def _expire_attempt(db: sqlite3.Connection, attempt: AttemptRecord, now_ns: int) -> AttemptRecord:
    if attempt.phase in _LIVE and now_ns >= attempt.spec.outcome_deadline_ns:
        return _set_phase(db, attempt.spec.attempt_id, AttemptPhase.PENDING, "outcome-deadline")
    return attempt


def _receipt_phase(attempt: AttemptRecord, receipt: CoreReceipt, now_ns: int) -> AttemptPhase:
    if isinstance(receipt, Executed):
        if (
            attempt.bound_at_ns is not None
            and attempt.bound_at_ns <= receipt.at_ns < attempt.spec.submit_deadline_ns
            and receipt.at_ns <= now_ns
        ):
            return AttemptPhase.AWAITING_EFFECT
    elif (isinstance(receipt, Dropped) and receipt.reason is not DropReason.DUPLICATE) or (
        isinstance(receipt, Rejected) and receipt.reason is not RejectReason.STALE_SEQ
    ):
        return AttemptPhase.NOT_EXECUTED
    return AttemptPhase.PENDING


class TaskLedger:
    """每个主进程、每个被控对象一个实例。同步 SQLite 操作由游戏桥调度。"""

    def __init__(
        self, path: Path, control_target_id: str, epoch: str, clock_ns: Callable[[], int]
    ) -> None:
        self._path = path
        self._control_target_id = control_target_id
        self._epoch = epoch
        self._clock_ns = clock_ns

    @staticmethod
    def provision(path: Path, control_target_id: str) -> None:
        """首次配置时显式建账。调用方负责核对该对象尚无旧账。"""
        if not control_target_id:
            raise ValueError("被控对象身份为空")
        path = path.absolute()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=False)
        with _transaction(path) as db:
            for statement in _SCHEMA:
                db.execute(statement)
            db.execute(
                "INSERT INTO owner (singleton, control_target_id) VALUES (1, ?)",
                (control_target_id,),
            )
            db.execute("PRAGMA user_version = 1")

    @classmethod
    def start(
        cls, path: Path, control_target_id: str, *, clock_ns: Callable[[], int] = perf_counter_ns
    ) -> "TaskLedger":
        """启动时换代，旧任务终止，未完成的尝试保守隔离。"""
        path = path.absolute()
        epoch = uuid4().hex
        with _transaction(path) as db:
            _owner(db, control_target_id)
            db.execute("UPDATE tasks SET ended = ? WHERE ended IS NULL", (TaskEnd.RESTARTED,))
            db.execute(
                "UPDATE attempts SET phase = ?, detail = ? WHERE phase IN (?, ?, ?)",
                (AttemptPhase.PENDING, "process-restarted", *_LIVE),
            )
            db.execute("UPDATE owner SET process_epoch = ? WHERE singleton = 1", (epoch,))
        return cls(path, control_target_id, epoch, clock_ns)

    @contextmanager
    def _current(self) -> Generator[sqlite3.Connection]:
        try:
            with _transaction(self._path) as db:
                if _owner(db, self._control_target_id)["process_epoch"] != self._epoch:
                    raise LedgerUnavailableError("任务账目的进程代已失效")
                yield db
        except ValidationError as exc:
            raise LedgerUnavailableError("任务账目含不完整的记录") from exc

    def status(self) -> LedgerStatus:
        """同一次事务回读任务与挡住新执行的原尝试。"""
        with self._current() as db:
            now_ns = self._clock_ns()
            row = db.execute("SELECT * FROM tasks WHERE ended IS NULL").fetchone()
            task = None if row is None else _expire(db, _task(row), now_ns)
            attempt = _outstanding(db)
            return LedgerStatus(
                task if task is not None and task.end is None else None,
                None if attempt is None else _expire_attempt(db, attempt, now_ns),
            )

    def get_task(self, task_id: str) -> TaskAccount | None:
        with self._current() as db:
            task = _find_task(db, task_id)
            return None if task is None else _expire(db, task, self._clock_ns())

    def get_attempt(self, attempt_id: str) -> AttemptRecord | None:
        with self._current() as db:
            attempt = _find_attempt(db, attempt_id)
            return None if attempt is None else _expire_attempt(db, attempt, self._clock_ns())

    def open_task(
        self, request: TaskRequest, limits: TaskLimits, *, release_verified: bool
    ) -> TaskAccount:
        """已获准的新任务需要核验释放。同 ID 重送保留首次配置的预算。"""
        with self._current() as db:
            now_ns = self._clock_ns()
            existing = _find_task(db, request.task_id)
            if existing is not None:
                if existing.request != request:
                    raise LedgerConflictError("任务 ID 已绑定另一份授权")
                return _expire(db, existing, now_ns)
            if release_verified is not True:
                raise LedgerRefusedError(Refusal.RELEASE_UNVERIFIED)
            _ensure_idle(db)
            current = db.execute("SELECT * FROM tasks WHERE ended IS NULL").fetchone()
            if current is not None and _expire(db, _task(current), now_ns).end is None:
                raise LedgerRefusedError(Refusal.TASK_ALREADY_ACTIVE)
            if now_ns >= request.expires_at_ns:
                raise LedgerRefusedError(Refusal.EXPIRED)
            db.execute(
                """INSERT INTO tasks (task_id, request, limits, opened_at_ns, last_progress_ns)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    request.task_id,
                    _TASK_REQUEST.dump_json(request),
                    _TASK_LIMITS.dump_json(limits),
                    now_ns,
                    now_ns,
                ),
            )
            return TaskAccount(request, limits, now_ns, now_ns, 0, 0, None)

    def close_task(self, task_id: str, end: TaskEnd) -> TaskAccount:
        """完成须结清原尝试。撤权可立即终止执行，原尝试继续记账。"""
        if end not in (TaskEnd.COMPLETED, TaskEnd.REVOKED):
            raise ValueError("只能显式完成或撤权")
        with self._current() as db:
            task = _find_task(db, task_id)
            if task is None:
                raise LedgerConflictError("任务终态冲突")
            task = _expire(db, task, self._clock_ns())
            if task.end is end:
                return task
            if task.end is not None:
                raise LedgerConflictError("任务终态冲突")
            if end is TaskEnd.COMPLETED:
                _ensure_idle(db)
            db.execute("UPDATE tasks SET ended = ? WHERE task_id = ?", (end, task_id))
            return replace(task, end=end)

    def check_goal(self, task_id: str) -> TaskAccount:
        """核对新可执行目标能否记在原任务账上。"""
        with self._current() as db:
            _ensure_idle(db)
            task = _active(db, task_id, self._clock_ns())
            if task.calls_spent >= task.limits.calls:
                raise LedgerRefusedError(Refusal.CALL_BUDGET)
            if task.inputs_spent >= task.limits.inputs:
                raise LedgerRefusedError(Refusal.INPUT_BUDGET)
            return task

    def reserve_call(self, task_id: str, call_id: str, kind: CallKind) -> bool:
        """调用前先扣除额度。仅首次成功提交事务返回 True。"""
        if not call_id:
            raise ValueError("调用身份为空")
        with self._current() as db:
            prior = db.execute(
                "SELECT kind FROM calls WHERE task_id = ? AND call_id = ?", (task_id, call_id)
            ).fetchone()
            if prior is not None:
                if prior["kind"] != kind:
                    raise LedgerConflictError("调用身份已占用")
                return False
            _ensure_idle(db)
            task = _active(db, task_id, self._clock_ns())
            if task.calls_spent >= task.limits.calls:
                raise LedgerRefusedError(Refusal.CALL_BUDGET)
            db.execute(
                "UPDATE tasks SET calls_spent = calls_spent + 1 WHERE task_id = ?", (task_id,)
            )
            db.execute("INSERT INTO calls VALUES (?, ?, ?)", (task_id, call_id, kind))
            return True

    def record_subgoal(self, task_id: str, progress_id: str) -> bool:
        """记录已核验的子目标。重复事实不推进无进展时钟。"""
        if not progress_id:
            raise ValueError("子目标核验身份为空")
        with self._current() as db:
            prior = db.execute(
                "SELECT 1 FROM subgoals WHERE task_id = ? AND progress_id = ?",
                (task_id, progress_id),
            ).fetchone()
            if prior is not None:
                return False
            now_ns = self._clock_ns()
            _active(db, task_id, now_ns)
            _ensure_idle(db)
            db.execute("INSERT INTO subgoals VALUES (?, ?)", (task_id, progress_id))
            _progress(db, task_id, now_ns)
            return True

    def reserve_attempt(self, spec: AttemptSpec) -> bool:
        """可能提交前，原子扣一次额度和锁住原尝试。重复预约返回 False。"""
        with self._current() as db:
            existing = _find_attempt(db, spec.attempt_id)
            if existing is not None:
                if existing.spec != spec:
                    raise LedgerConflictError("尝试 ID 已绑定另一尝试")
                return False
            _ensure_idle(db)
            now_ns = self._clock_ns()
            task = _active(db, spec.task_id, now_ns)
            if task.inputs_spent >= task.limits.inputs:
                raise LedgerRefusedError(Refusal.INPUT_BUDGET)
            if now_ns >= min(spec.submit_deadline_ns, spec.outcome_deadline_ns):
                raise LedgerRefusedError(Refusal.EXPIRED)
            if spec.submit_deadline_ns > task.request.expires_at_ns:
                raise LedgerConflictError("动作截止超出原任务授权")
            db.execute(
                "UPDATE tasks SET inputs_spent = inputs_spent + 1 WHERE task_id = ?",
                (spec.task_id,),
            )
            db.execute(
                "INSERT INTO attempts (attempt_id, task_id, spec, phase) VALUES (?, ?, ?, ?)",
                (
                    spec.attempt_id,
                    spec.task_id,
                    _ATTEMPT_SPEC.dump_json(spec),
                    AttemptPhase.RESERVED,
                ),
            )
            return True

    def bind_core(self, attempt_id: str, core: CoreCorrelation) -> bool:
        """先持久登记原核关联，再由调用方重核许可并至多写出一次。"""
        with self._current() as db:
            attempt = _known_attempt(db, attempt_id)
            if attempt.core is not None:
                if attempt.core != core:
                    raise LedgerConflictError("已关联其它核动作")
                return False
            if (
                attempt.phase is not AttemptPhase.RESERVED
                or core.runtime_id != attempt.spec.runtime_id
            ):
                raise LedgerConflictError("核动作须关联原预约和 Runtime")
            now_ns = self._clock_ns()
            _active(db, attempt.spec.task_id, now_ns)
            if now_ns >= min(attempt.spec.submit_deadline_ns, attempt.spec.outcome_deadline_ns):
                raise LedgerRefusedError(Refusal.EXPIRED)
            key = _CORE.dump_json(core)
            if db.execute("SELECT 1 FROM attempts WHERE core = ?", (key,)).fetchone() is not None:
                raise LedgerConflictError("核关联已属于另一尝试")
            db.execute(
                "UPDATE attempts SET core = ?, bound_at_ns = ?, phase = ? WHERE attempt_id = ?",
                (key, now_ns, AttemptPhase.AWAITING_RECEIPT, attempt_id),
            )
            return True

    def record_not_sent(self, attempt_id: str, *, proof: str) -> AttemptRecord:
        """只在当前进程有写出前的确证时关闭，保存该依据。"""
        if not proof:
            raise ValueError("须记录确证未写出的依据")
        with self._current() as db:
            attempt = _known_attempt(db, attempt_id)
            if (
                attempt.phase is AttemptPhase.NOT_EXECUTED
                and attempt.receipt is None
                and attempt.detail == proof
            ):
                return attempt
            if attempt.phase not in (AttemptPhase.RESERVED, AttemptPhase.AWAITING_RECEIPT):
                raise LedgerConflictError("尝试无法按未写出结案")
            return _set_phase(db, attempt_id, AttemptPhase.NOT_EXECUTED, proof)

    def record_receipt(self, core: CoreCorrelation, receipt: CoreReceipt) -> AttemptRecord:
        """核回执按原关联路由，可在撤权后记录。晚到事实不解除隔离。"""
        with self._current() as db:
            row = db.execute(
                "SELECT * FROM attempts WHERE core = ?", (_CORE.dump_json(core),)
            ).fetchone()
            if row is None or (not isinstance(receipt, Rejected) and receipt.id != core.action_id):
                raise LedgerConflictError("回执与原核关联不符")
            now_ns = self._clock_ns()
            attempt = _expire_attempt(db, _attempt(row), now_ns)
            if attempt.receipt is not None:
                if attempt.receipt != receipt:
                    raise LedgerConflictError("回执与原事实冲突")
                return attempt
            if attempt.phase not in (AttemptPhase.AWAITING_RECEIPT, AttemptPhase.PENDING):
                raise LedgerConflictError("原尝试当前不接纳回执")
            attempt_id = attempt.spec.attempt_id
            db.execute(
                "UPDATE attempts SET receipt = ? WHERE attempt_id = ?",
                (_RECEIPT.dump_json(receipt), attempt_id),
            )
            if attempt.phase is AttemptPhase.PENDING:
                return _known_attempt(db, attempt_id)
            phase = _receipt_phase(attempt, receipt, now_ns)
            detail = "receipt-uncertain" if phase is AttemptPhase.PENDING else None
            return _set_phase(db, attempt_id, phase, detail)

    def record_effect(self, attempt_id: str, evidence: EffectEvidence) -> AttemptRecord:
        """记录原核执行后新帧的核验结果。调用方核对捕获连续性与效果语义。"""
        with self._current() as db:
            now_ns = self._clock_ns()
            attempt = _expire_attempt(db, _known_attempt(db, attempt_id), now_ns)
            if attempt.effect is not None:
                if attempt.effect != evidence:
                    raise LedgerConflictError("效果与原核验事实冲突")
                return attempt
            if attempt.phase is AttemptPhase.PENDING:
                return attempt
            if attempt.phase is not AttemptPhase.AWAITING_EFFECT or not isinstance(
                attempt.receipt, Executed
            ):
                raise LedgerConflictError("效果须对应合时的已执行回执")
            base, observed = attempt.spec.base_frame, evidence.frame
            if (
                observed.number <= base.number
                or observed.transform_version != base.transform_version
                or observed.capture_ns > now_ns
                or not attempt.receipt.at_ns
                < observed.capture_ns
                < attempt.spec.outcome_deadline_ns
            ):
                raise LedgerConflictError("效果帧不属于原尝试的执行后画面")
            db.execute(
                "UPDATE attempts SET effect = ? WHERE attempt_id = ?",
                (_EFFECT.dump_json(evidence), attempt_id),
            )
            if evidence.result is EffectResult.VERIFIED:
                _progress(db, attempt.spec.task_id, now_ns)
                return _set_phase(db, attempt_id, AttemptPhase.VERIFIED)
            if attempt.spec.kind is AttemptKind.ORDINARY and attempt.spec.decision_point_id is None:
                return _set_phase(db, attempt_id, AttemptPhase.NO_EFFECT)
            return _set_phase(db, attempt_id, AttemptPhase.PENDING, "nonordinary-no-effect")

    def mark_pending(self, attempt_id: str, reason: str) -> AttemptRecord:
        """无法可靠确定时隔离当前被控对象。保留第一个已记录的原因。"""
        if not reason:
            raise ValueError("须记录待结案原因")
        with self._current() as db:
            attempt = _known_attempt(db, attempt_id)
            if attempt.phase is AttemptPhase.PENDING:
                return attempt
            if attempt.phase not in _LIVE:
                raise LedgerConflictError("已终结尝试不能转为待结案")
            return _set_phase(db, attempt_id, AttemptPhase.PENDING, reason)

    def resolve_pending(
        self, session_id: str, attempt_id: str, resolution: Resolution, *, release_verified: bool
    ) -> AttemptRecord:
        """接纳已获准的原尝试结案。该事实独立于效果与进展。"""
        with self._current() as db:
            attempt = _known_attempt(db, attempt_id)
            if attempt.spec.session_id != session_id:
                raise LedgerConflictError("结案须对应原会话")
            if attempt.phase is AttemptPhase.OPERATOR_RESOLVED:
                if attempt.resolution != resolution:
                    raise LedgerConflictError("结案与原人工事实冲突")
                return attempt
            if attempt.phase is not AttemptPhase.PENDING:
                raise LedgerConflictError("只接受待结案尝试的人工结案")
            if release_verified is not True:
                raise LedgerRefusedError(Refusal.RELEASE_UNVERIFIED)
            db.execute(
                "UPDATE attempts SET phase = ?, resolution = ? WHERE attempt_id = ?",
                (AttemptPhase.OPERATOR_RESOLVED, resolution, attempt_id),
            )
            return _known_attempt(db, attempt_id)
