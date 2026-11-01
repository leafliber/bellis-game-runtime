"""操作员控制台的核心：测试会话、许可源与主进程的对端（GR-36）。

设计见 `docs/modules/game-bridge.md` 的“独立运行”。控制台是 `bellis-channel/1` 的服务端，一个地址与
密钥，按角色区分两条连接：

- 操作员连接：主进程，角色 `game-runtime.main`。控制台发 `operator.session`、
  `operator.decision_resolved`，收 `operator.decision_opened`、`operator.decision_closed`、
  `operator.action`（`messages`）；
- 操作员许可连接：实时核，角色 `game-runtime.core`。控制台以 `operator_lease.grant` 授予与续约、以
  `operator_lease.revoke` 吊销输入许可，收 `input.released`（`docs/modules/core-protocol.md` 的
  “操作员许可连接”）。

每种角色同时只接受一个连接；前一个断开后可以再连（实时核重启后的新实例）。

测试会话即测试许可：`waiting`（逐条确认前置条件）→ `running` ⇄ `paused` → `ended`。每次开始或继续都
换发更大的令牌，以“现在 + 500 ms”授予，此后每 250 ms 续约；暂停与结束时吊销。运行中实时核重新连上时
换发新令牌授予它。时长上限从第一次开始算起。每次会话一个 JSON Lines 的会话记录。

时刻读 `Clock`（缺省 `time.perf_counter_ns()`，与帧环同一时钟），测试注入手动推进的时钟；SecretStore
与本地数据目录也由调用方给出。终端界面在 `game_runtime.operator.terminal`，这里只有状态与连接。
"""

import asyncio
import contextlib
import json
import logging
import os
import secrets
import time
from collections import Counter
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import IO, Any, Final, Protocol

from pydantic import BaseModel, ValidationError

from core_protocol.methods import (
    OPERATOR_INPUT_RELEASED,
    OPERATOR_LEASE_GRANT,
    OPERATOR_LEASE_REVOKE,
)
from core_protocol.models import (
    InputReleasedParams,
    LeaseGrantParams,
    LeaseRevokeParams,
    ReleaseReason,
)
from game_runtime.adapter import Adapter
from game_runtime.channel import (
    ChannelCredentials,
    ChannelError,
    ChannelServer,
    Connection,
    RpcError,
    RpcPeer,
    listen,
    session_credentials,
)
from game_runtime.core_link import CORE_ROLE
from game_runtime.operator import messages
from game_runtime.operator.messages import SessionState
from game_runtime.secret_store import (
    SecretStore,
    SecretStoreError,
    SessionInfo,
    delete_session_info,
    write_session_info,
)

logger = logging.getLogger(__name__)

MAIN_ROLE: Final = "game-runtime.main"
"""主进程连接控制台时使用的角色。"""
DEFAULT_MAX_MINUTES: Final = 60.0
"""会话时长上限的缺省值（分钟），从第一次开始算起。"""
LEASE_TTL_NS: Final = 500_000_000
"""授予与续约时的有效期：现在 + 500 ms（Bellis D-34 的初值）。"""
RENEW_INTERVAL_NS: Final = 250_000_000
"""续约的间隔：250 ms（Bellis D-34 的初值）。"""
PAUSE_ON: Final[frozenset[ReleaseReason]] = frozenset()
"""收到这些原因的 `input.released` 时转为暂停并吊销令牌：真人接管与急停。它们在 G2 第 2 步接真实输入
后才有，现在为空。"""
SUPERVISED: Final = "console.supervised-test"
"""最后一条确认项的 id。它不是适配包的前置条件，不进 `operator.session` 的 `confirmed`；id 里的 `.`
不会与适配包的标识重名。"""
SUPERVISED_TEXT: Final = "本次是受监督的测试，不直播"


class ConsoleError(Exception):
    """命令在当前状态下不能执行；说明给操作员看。"""


@dataclass(frozen=True)
class Confirmation:
    """会话开始前要确认的一项。"""

    id: str
    text: str
    precondition: bool
    """是不是适配包的前置条件（否则是最后一条“不直播”）。"""


class Clock(Protocol):
    """控制台的时钟，与帧环同一单调时钟的纳秒数。"""

    def now_ns(self) -> int: ...

    async def sleep_until(self, deadline_ns: int) -> None:
        """等到 `now_ns()` 不早于 `deadline_ns`。"""
        ...


class MonotonicClock:
    """真实时钟：`time.perf_counter_ns()`，等待用 asyncio。"""

    def now_ns(self) -> int:
        return time.perf_counter_ns()

    async def sleep_until(self, deadline_ns: int) -> None:
        # 事件循环的计时比 perf_counter 粗，可能早醒：醒来后再核对一次。
        while (remaining := deadline_ns - time.perf_counter_ns()) > 0:
            await asyncio.sleep(remaining / 1e9)


class EventKind(StrEnum):
    """控制台事件，也是会话记录每行的 `event`。"""

    SESSION_STARTED = "session_started"
    CONFIRMATION = "confirmation"
    STARTED = "started"
    RESUMED = "resumed"
    PAUSED = "paused"
    LEASE_GRANTED = "lease_granted"
    LEASE_REFUSED = "lease_refused"
    GOAL = "goal"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    REJECTED = "rejected"
    INPUT_RELEASED = "input_released"
    DECISION_OPENED = "decision_opened"
    DECISION_CHOSEN = "decision_chosen"
    DECISION_CLOSED = "decision_closed"
    ACTION = "action"
    SESSION_ENDED = "session_ended"


@dataclass(frozen=True)
class ConsoleEvent:
    """控制台上发生的一件事：写进会话记录，并交给监听者（终端界面）。"""

    kind: EventKind
    at_ns: int
    """`Clock.now_ns()`。"""
    fields: Mapping[str, Any]
    """会话记录这一行的其余字段，只含 JSON 的值。"""


type Listener = Callable[[ConsoleEvent], None]


@dataclass(frozen=True)
class ConsoleOptions:
    """控制台的参数；时刻单位为纳秒，`rpc_timeout` 为秒。"""

    max_minutes: float = DEFAULT_MAX_MINUTES
    lease_ttl_ns: int = LEASE_TTL_NS
    renew_interval_ns: int = RENEW_INTERVAL_NS
    rpc_timeout: float = 2.0
    pause_on: frozenset[ReleaseReason] = PAUSE_ON

    def __post_init__(self) -> None:
        if not self.max_minutes > 0:
            raise ValueError(f"时长上限须为正：{self.max_minutes}")
        if self.renew_interval_ns <= 0 or self.lease_ttl_ns <= self.renew_interval_ns:
            raise ValueError("续约间隔须为正，且短于有效期")
        if self.rpc_timeout <= 0:
            raise ValueError("请求的时限须为正")


def sessions_dir(data_dir: Path, adapter_id: str) -> Path:
    """一个适配包的会话记录所在的目录。"""
    return data_dir / "operator" / "sessions" / adapter_id


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class SessionLog:
    """会话记录：一个 JSON Lines 文件，以开始时刻（UTC）命名，逐行写出、逐行落盘。"""

    def __init__(self, path: Path, file: IO[str]) -> None:
        self.path = path
        self._file: IO[str] | None = file

    @classmethod
    def create(cls, directory: Path, started: datetime) -> "SessionLog":
        """在 `directory` 下新建 `<UTC 开始时刻>.jsonl`；同一秒已有时加 `-2`、`-3`……"""
        directory.mkdir(parents=True, exist_ok=True)
        stem = started.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        for number in range(1, 1000):
            path = directory / (f"{stem}.jsonl" if number == 1 else f"{stem}-{number}.jsonl")
            try:
                file = path.open("x", encoding="utf-8", newline="\n")
            except FileExistsError:
                continue
            return cls(path, file)
        raise FileExistsError(f"{directory} 里 {stem} 的会话记录太多")

    def write(self, record: Mapping[str, Any]) -> None:
        file = self._file
        if file is None:
            return
        file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


class OperatorConsole:
    """操作员控制台的核心。用法：`await open()`，交出 `credentials`（已写进 SecretStore）；
    按命令调用 `answer`、`start`、`pause`、`set_goal`、`choose`、`end`；`await close()` 断开、
    删除连接信息。

    命令在当前状态下不能执行时抛 `ConsoleError`。监听者经 `add_listener` 收到每个 `ConsoleEvent`。
    """

    def __init__(
        self,
        adapter: Adapter,
        *,
        store: SecretStore,
        data_dir: Path,
        options: ConsoleOptions | None = None,
        clock: Clock | None = None,
        wall: Callable[[], datetime] | None = None,
        pid: int | None = None,
    ) -> None:
        self.adapter = adapter
        self.options = options or ConsoleOptions()
        self._store = store
        self._data_dir = data_dir
        self._clock: Clock = clock or MonotonicClock()
        self._wall = wall or (lambda: datetime.now(UTC))
        self._pid = os.getpid() if pid is None else pid
        self._items = (
            *(Confirmation(p.id, p.text, precondition=True) for p in adapter.risk.preconditions),
            Confirmation(SUPERVISED, SUPERVISED_TEXT, precondition=False),
        )
        self._answered = 0
        self._state = SessionState.WAITING
        self._goal: str | None = None
        self._ends_at_ns: int | None = None
        self._token = 0
        self._granted: int | None = None
        """已授予给运行中的实时核、还没吊销的令牌。"""
        self._end_reason: str | None = None
        self._ended = asyncio.Event()
        self._transition = asyncio.Lock()
        self._listeners: list[Listener] = []
        self._counts: Counter[str] = Counter()
        self._core: RpcPeer | None = None
        self._main: RpcPeer | None = None
        self._open: dict[str, messages.DecisionOpened] = {}
        self._seen: set[str] = set()
        self._resolved: dict[str, int] = {}
        self._lease_task: asyncio.Task[None] | None = None
        self._cap_task: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[None]] = set()
        self._server: ChannelServer | None = None
        self._credentials: ChannelCredentials | None = None
        self._credentials_scope = contextlib.ExitStack()
        self._log: SessionLog | None = None
        self._info_written = False
        self._closed = False

    # 状态

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def ended(self) -> bool:
        return self._state is SessionState.ENDED

    @property
    def end_reason(self) -> str | None:
        return self._end_reason

    @property
    def confirmations(self) -> tuple[Confirmation, ...]:
        """要确认的全部项，按顺序：适配包的前置条件，最后是“不直播”。"""
        return self._items

    @property
    def pending_confirmation(self) -> Confirmation | None:
        """下一条待确认的项；全部确认后、或不在 `waiting` 时为 None。"""
        if self._state is not SessionState.WAITING or self._answered >= len(self._items):
            return None
        return self._items[self._answered]

    @property
    def confirmed(self) -> tuple[str, ...]:
        """已确认的适配包前置条件的 id。"""
        return tuple(item.id for item in self._items[: self._answered] if item.precondition)

    @property
    def goal(self) -> str | None:
        return self._goal

    @property
    def ends_at_ns(self) -> int | None:
        return self._ends_at_ns

    @property
    def token(self) -> int:
        """最近换发的令牌；还没换发过时为 0。"""
        return self._token

    @property
    def core_connected(self) -> bool:
        return self._core is not None

    @property
    def main_connected(self) -> bool:
        return self._main is not None

    @property
    def open_decisions(self) -> tuple[messages.DecisionOpened, ...]:
        """主进程打开、还没关闭的决策点，按打开的顺序。"""
        return tuple(self._open.values())

    @property
    def current_decision(self) -> messages.DecisionOpened | None:
        """`<序号>` 作用的决策点：最近打开、还没关闭的那个。"""
        return next(reversed(self._open.values()), None)

    def resolved(self, decision_id: str) -> int | None:
        """操作员给这个决策点的选择；还没给时为 None。"""
        return self._resolved.get(decision_id)

    @property
    def credentials(self) -> ChannelCredentials:
        """本次会话的通道地址与密钥。"""
        if self._credentials is None:
            raise RuntimeError("控制台还没有打开")
        return self._credentials

    @property
    def log_path(self) -> Path | None:
        return None if self._log is None else self._log.path

    def now_ns(self) -> int:
        return self._clock.now_ns()

    def session(self) -> messages.Session:
        """`operator.session` 的内容。"""
        return messages.Session(
            adapter=self.adapter.id,
            state=self._state,
            confirmed=self.confirmed,
            goal=self._goal,
            ends_at_ns=self._ends_at_ns,
        )

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    async def wait_ended(self) -> None:
        await self._ended.wait()

    # 打开与关闭

    async def open(self) -> None:
        """开始监听，新建会话记录，把连接信息写进 SecretStore。失败时已清理，异常照常抛出。"""
        if self._server is not None or self._closed:
            raise RuntimeError("控制台只能打开一次")
        session_id = f"op{secrets.token_hex(4)}"
        started = self._wall()
        try:
            credentials = self._credentials_scope.enter_context(session_credentials(session_id))
            self._server = await listen(
                credentials.path,
                credentials.key,
                on_connection=self._on_connection,
                on_rejected=lambda error: self._reject("unauthenticated", str(error)),
                on_error=lambda error: logger.error("控制台的连接装配失败：%s", error),
            )
            self._credentials = credentials
            self._log = SessionLog.create(sessions_dir(self._data_dir, self.adapter.id), started)
            info = SessionInfo.create(
                credentials, session_id, launcher_pid=self._pid, started_at=started
            )
            write_session_info(self._store, info)
            self._info_written = True
        except BaseException:
            await self._teardown()
            raise
        self._emit(
            EventKind.SESSION_STARTED,
            adapter=self.adapter.id,
            session_id=session_id,
            pid=self._pid,
            max_minutes=self.options.max_minutes,
            confirmations=[{"id": item.id, "text": item.text} for item in self._items],
        )

    async def close(self, reason: str = "closed") -> None:
        """结束会话（还没结束时，原因为 `reason`），断开两条连接、停止监听，删除 SecretStore 里的
        连接信息，关闭会话记录。可以重复调用。"""
        if self._closed:
            return
        await self.end(reason)
        self._closed = True
        await self._teardown()

    async def _teardown(self) -> None:
        self._stop_lease()
        for task in (self._cap_task, *self._background):
            if task is not None:
                task.cancel()
        for peer in (self._core, self._main):
            if peer is not None:
                await peer.close()
        if self._server is not None:
            await self._server.close()
        if self._info_written:
            try:
                delete_session_info(self._store)
            except SecretStoreError as error:
                logger.error("没能删除 SecretStore 里的会话信息：%s", error)
            self._info_written = False
        self._credentials_scope.close()
        if self._log is not None:
            self._log.close()

    # 命令

    async def answer(self, yes: bool) -> None:
        """回答当前的确认项：`y` 确认，`n` 即结束会话。"""
        item = self.pending_confirmation
        if item is None:
            raise ConsoleError("没有待确认的项")
        self._emit(EventKind.CONFIRMATION, id=item.id, text=item.text, answer="y" if yes else "n")
        if not yes:
            await self.end("declined")
            return
        self._answered += 1
        if item.precondition:
            self._publish()

    async def start(self) -> None:
        """开始或继续：换发新令牌并开始续约。第一次开始时起算时长上限。"""
        async with self._transition:
            match self._state:
                case SessionState.ENDED:
                    raise ConsoleError("会话已结束，不能再开始")
                case SessionState.RUNNING:
                    raise ConsoleError("已在运行")
                case SessionState.WAITING if self.pending_confirmation is not None:
                    raise ConsoleError("还有确认项没有回答")
                case _:
                    pass
            first = self._state is SessionState.WAITING
            if first:
                self._ends_at_ns = self._clock.now_ns() + round(self.options.max_minutes * 60e9)
                self._cap_task = asyncio.get_running_loop().create_task(
                    self._time_cap(self._ends_at_ns)
                )
            self._state = SessionState.RUNNING
            self._emit(
                EventKind.STARTED if first else EventKind.RESUMED, ends_at_ns=self._ends_at_ns
            )
            self._publish()
            self._begin_lease()

    async def pause(self) -> None:
        """暂停：吊销当前令牌。"""
        if self._state is not SessionState.RUNNING:
            raise ConsoleError("没有在运行")
        await self._pause("operator")

    def set_goal(self, text: str | None) -> None:
        """设定（`text` 非空）或清除当前目标。"""
        if self.ended:
            raise ConsoleError("会话已结束")
        goal = None if text is None else text.strip() or None
        if goal is not None and len(goal) > 2000:
            raise ConsoleError("目标太长（至多 2000 字）")
        self._goal = goal
        self._emit(EventKind.GOAL, goal=goal)
        self._publish()

    def choose(self, index: int) -> messages.DecisionResolved:
        """给出当前决策点的选择，发给主进程。"""
        decision = self.current_decision
        if decision is None:
            raise ConsoleError("没有打开的决策点")
        if decision.id in self._resolved:
            raise ConsoleError(f"决策点 {decision.id} 已给出过选择 {self._resolved[decision.id]}")
        if decision.option(index) is None:
            raise ConsoleError(f"决策点 {decision.id} 没有序号 {index}")
        peer = self._main
        if peer is None:
            raise ConsoleError("主进程没有连上")
        resolved = messages.DecisionResolved(id=decision.id, index=index)
        try:
            peer.notify(messages.DECISION_RESOLVED, resolved.params())
        except ChannelError as error:
            raise ConsoleError(f"没能发给主进程：{error}") from None
        self._resolved[decision.id] = index
        self._emit(EventKind.DECISION_CHOSEN, id=decision.id, index=index)
        return resolved

    async def end(self, reason: str) -> None:
        """结束会话：吊销令牌，此后不能再开始。已结束时什么也不做。"""
        async with self._transition:
            if self.ended:
                return
            running = self._state is SessionState.RUNNING
            self._stop_lease()
            self._state = SessionState.ENDED
            self._end_reason = reason
            if self._cap_task is not None and self._cap_task is not asyncio.current_task():
                self._cap_task.cancel()
            self._publish()
            if running:
                await self._revoke()
            counts = {
                kind: count
                for kind, count in sorted(self._counts.items())
                if kind != EventKind.SESSION_STARTED
            }
            self._emit(EventKind.SESSION_ENDED, reason=reason, counts=counts)
            self._ended.set()

    # 许可

    async def _pause(self, reason: str) -> None:
        async with self._transition:
            if self._state is not SessionState.RUNNING:
                return
            self._state = SessionState.PAUSED
            self._stop_lease()
            self._emit(EventKind.PAUSED, reason=reason, token=self._granted)
            self._publish()
            await self._revoke()

    def _begin_lease(self) -> None:
        """运行中且实时核连着时，换发更大的令牌并开始授予与续约。"""
        self._stop_lease()
        peer = self._core
        if peer is None or self._state is not SessionState.RUNNING:
            return
        self._token += 1
        self._lease_task = asyncio.get_running_loop().create_task(
            self._lease_loop(peer, self._token)
        )

    def _stop_lease(self) -> None:
        task, self._lease_task = self._lease_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _lease_loop(self, peer: RpcPeer, token: int) -> None:
        """以 `token` 授予，之后每 `renew_interval_ns` 续约，每次到期时刻为“现在 + 有效期”。"""
        options = self.options
        next_at = self._clock.now_ns()
        first = True
        while True:
            expires_ns = self._clock.now_ns() + options.lease_ttl_ns
            params = LeaseGrantParams(token=token, expires_ns=expires_ns)
            if first:
                # 记下的是发出授予的一刻；实时核不接受时另有 `lease_refused`。
                first = False
                self._granted = token
                self._emit(EventKind.LEASE_GRANTED, token=token, expires_ns=expires_ns)
            try:
                await peer.request(
                    OPERATOR_LEASE_GRANT.name, _dump(params), timeout=options.rpc_timeout
                )
            except RpcError as error:
                self._spawn(self._lease_refused(token, error))
                return
            except ChannelError as error:
                if not peer.connection.is_open:
                    return
                logger.warning("续约令牌 %d 没有得到回应：%s", token, error)
            next_at += options.renew_interval_ns
            await self._clock.sleep_until(next_at)

    async def _lease_refused(self, token: int, error: RpcError) -> None:
        """实时核拒绝了授予或续约（例如许可已过期，过期的令牌不能续约）：转为暂停，由操作员决定继续。"""
        if token != self._token or self._state is not SessionState.RUNNING:
            return
        self._emit(EventKind.LEASE_REFUSED, token=token, code=error.code, message=error.message)
        await self._pause("lease_refused")

    async def _revoke(self) -> None:
        """吊销当前令牌；实时核释放完成后才回应。实时核没连着时无事可做。"""
        token, self._granted = self._token, None
        peer = self._core
        if peer is None or token == 0:
            return
        try:
            await peer.request(
                OPERATOR_LEASE_REVOKE.name,
                _dump(LeaseRevokeParams(token=token)),
                timeout=self.options.rpc_timeout,
            )
        except (ChannelError, RpcError) as error:
            logger.warning("吊销令牌 %d 没有得到确认：%s", token, error)

    async def _time_cap(self, ends_at_ns: int) -> None:
        await self._clock.sleep_until(ends_at_ns)
        await self.end("time_cap")

    # 连接

    def _on_connection(self, connection: Connection) -> None:
        role = connection.role
        if self._closed or self.ended:
            self._reject(role, "会话已结束", connection)
        elif role == CORE_ROLE:
            self._attach_core(connection)
        elif role == MAIN_ROLE:
            self._attach_main(connection)
        else:
            self._reject(role, "角色不认识", connection)

    def _reject(self, role: str, reason: str, connection: Connection | None = None) -> None:
        if connection is not None:
            connection.abort()
        logger.warning("控制台拒绝了一个连接（%s）：%s", role, reason)
        self._emit(EventKind.REJECTED, role=role, reason=reason)

    def _attach_core(self, connection: Connection) -> None:
        if self._core is not None and self._core.connection.is_open:
            self._reject(connection.role, "已有实时核连着", connection)
            return
        peer = RpcPeer(connection, timeout=self.options.rpc_timeout)
        peer.on_notification(
            OPERATOR_INPUT_RELEASED.name, lambda params: self._on_released(peer, params)
        )
        self._core = peer
        peer.closed.add_done_callback(lambda _: self._detach_core(peer))
        self._emit(EventKind.CONNECTED, role="core")
        # 运行中实时核（重新）连上：换发新令牌授予它。
        self._begin_lease()

    def _detach_core(self, peer: RpcPeer) -> None:
        if self._core is not peer:
            return
        self._core = None
        self._granted = None
        self._stop_lease()
        self._emit(EventKind.DISCONNECTED, role="core")

    def _attach_main(self, connection: Connection) -> None:
        if self._main is not None and self._main.connection.is_open:
            self._reject(connection.role, "已有主进程连着", connection)
            return
        peer = RpcPeer(connection, timeout=self.options.rpc_timeout)
        handlers: Sequence[tuple[str, Callable[[dict[str, Any]], None]]] = (
            (messages.DECISION_OPENED, lambda params: self._on_decision_opened(peer, params)),
            (messages.DECISION_CLOSED, lambda params: self._on_decision_closed(peer, params)),
            (messages.ACTION, lambda params: self._on_action(peer, params)),
        )
        for method, handler in handlers:
            peer.on_notification(method, handler)
        self._main = peer
        peer.closed.add_done_callback(lambda _: self._detach_main(peer))
        self._emit(EventKind.CONNECTED, role="main")
        self._publish()

    def _detach_main(self, peer: RpcPeer) -> None:
        if self._main is not peer:
            return
        self._main = None
        dropped = list(self._open)
        self._open.clear()
        self._emit(EventKind.DISCONNECTED, role="main", open_decisions=dropped)

    def _publish(self) -> None:
        """把 `operator.session` 发给主进程（连着时）。"""
        peer = self._main
        if peer is None or not peer.connection.is_open:
            return
        with contextlib.suppress(ChannelError):
            peer.notify(messages.SESSION, self.session().params())

    # 收到的消息

    def _on_released(self, peer: RpcPeer, params: dict[str, Any]) -> None:
        if self._core is not peer:
            return
        released = _parse(InputReleasedParams, OPERATOR_INPUT_RELEASED.name, params)
        if released is None:
            return
        self._emit(EventKind.INPUT_RELEASED, **_dump(released))
        if released.reason in self.options.pause_on and self._state is SessionState.RUNNING:
            # 真人接管或急停：交还控制只能由操作员按“开始”。
            self._spawn(self._pause(f"released:{released.reason.value}"))

    def _on_decision_opened(self, peer: RpcPeer, params: dict[str, Any]) -> None:
        if self._main is not peer:
            return
        decision = _parse(messages.DecisionOpened, messages.DECISION_OPENED, params)
        if decision is None:
            return
        if decision.id in self._seen:
            logger.warning("忽略了重复的决策点 %s", decision.id)
            return
        self._seen.add(decision.id)
        self._open[decision.id] = decision
        self._emit(EventKind.DECISION_OPENED, **decision.params())

    def _on_decision_closed(self, peer: RpcPeer, params: dict[str, Any]) -> None:
        if self._main is not peer:
            return
        closed = _parse(messages.DecisionClosed, messages.DECISION_CLOSED, params)
        if closed is None:
            return
        if self._open.pop(closed.id, None) is None:
            logger.warning("忽略了不认识或已关闭的决策点 %s", closed.id)
            return
        self._emit(EventKind.DECISION_CLOSED, **closed.params())

    def _on_action(self, peer: RpcPeer, params: dict[str, Any]) -> None:
        if self._main is not peer:
            return
        action = _parse(messages.Action, messages.ACTION, params)
        if action is not None:
            self._emit(EventKind.ACTION, **action.params())

    # 记录

    def _emit(self, kind: EventKind, **fields: Any) -> None:
        at_ns = self._clock.now_ns()
        self._counts[kind] += 1
        if self._log is not None:
            self._log.write({"at": _iso(self._wall()), "mono_ns": at_ns, "event": kind, **fields})
        event = ConsoleEvent(kind, at_ns, fields)
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                logger.exception("控制台事件的监听者出错")

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        task: asyncio.Task[None] = asyncio.get_running_loop().create_task(work)
        self._background.add(task)
        task.add_done_callback(self._background.discard)


def _parse[T: BaseModel](model: type[T], method: str, params: dict[str, Any]) -> T | None:
    try:
        return model.model_validate(params)
    except ValidationError as error:
        logger.warning("忽略了参数不合法的 %s：%s", method, error)
        return None
