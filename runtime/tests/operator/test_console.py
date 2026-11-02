"""操作员控制台：确认与状态机、令牌与续约的时刻、时长上限、按角色的连接、决策点与动作、
SecretStore 里的连接信息、会话记录，以及主进程一侧的 `OperatorClient`。

规则见 `docs/modules/game-bridge.md` 的“独立运行”。控制台的时钟由测试手动推进；扮演实时核与主进程的
两端经真实的 `bellis-channel/1` 连到控制台（Windows 上命名管道，Linux 上 UDS），不拉起实时核：与真实
实时核的联调在 `runtime/tests/core_link/test_operator.py`。
"""

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncGenerator, Callable, Coroutine
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core_protocol.methods import ErrorCode
from core_protocol.models import (
    InputReleasedParams,
    LeaseGrantParams,
    LeaseRevokeParams,
    ReleaseReason,
)
from game_runtime.adapter import GAMES_DIR, Adapter, Precondition, Risk, load_adapter
from game_runtime.channel import ChannelError, RpcError, RpcPeer, connect
from game_runtime.core_link import CORE_ROLE
from game_runtime.operator import (
    MAIN_ROLE,
    PAUSE_ON,
    SUPERVISED,
    ConsoleError,
    ConsoleOptions,
    OperatorAdapterMismatchError,
    OperatorClient,
    OperatorConsole,
    OperatorUnavailableError,
    SessionLog,
    SessionState,
    messages,
    sessions_dir,
)
from game_runtime.secret_store import MemorySecretStore, read_session_info

WAIT = 5.0
START_NS = 1_000_000_000_000
"""手动时钟的起点。"""


async def until[T](probe: Callable[[], T | None], timeout: float = WAIT) -> T:
    """等到 `probe()` 不是 None，返回它（按真实时间轮询）。"""
    deadline = time.monotonic() + timeout
    while (value := probe()) is None:
        assert time.monotonic() < deadline, "等待的条件没有成立"
        await asyncio.sleep(0.002)
    return value


class ManualClock:
    """手动推进的控制台时钟：`advance` 唤醒到期的等待者。"""

    def __init__(self, start_ns: int = START_NS) -> None:
        self.now = start_ns
        self._waiters: list[tuple[int, asyncio.Future[None]]] = []

    def now_ns(self) -> int:
        return self.now

    async def sleep_until(self, deadline_ns: int) -> None:
        if deadline_ns <= self.now:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        waiter = (deadline_ns, future)
        self._waiters.append(waiter)
        try:
            await future
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    @property
    def waiting(self) -> int:
        return len(self._waiters)

    async def advance(self, nanoseconds: int) -> None:
        self.now += nanoseconds
        for deadline, future in list(self._waiters):
            if deadline <= self.now and not future.done():
                future.set_result(None)
        for _ in range(20):
            await asyncio.sleep(0)


class Wall:
    """手动推进的墙上时钟，起点固定，便于核对会话记录的文件名。"""

    def __init__(self) -> None:
        self.now = datetime(2026, 11, 1, 8, 30, 15, 250000, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(milliseconds=1)
        return self.now


@dataclass
class FakeCore:
    """扮演实时核：经操作员许可连接收授予与吊销，可以发 `input.released`。

    `refuse` 为真时拒绝授予。
    """

    peer: RpcPeer
    grants: list[LeaseGrantParams] = field(default_factory=list[LeaseGrantParams])
    revokes: list[LeaseRevokeParams] = field(default_factory=list[LeaseRevokeParams])
    refuse: bool = False

    def released(self, reason: str) -> None:
        params = InputReleasedParams.model_validate(
            {"reason": reason, "producer": None, "released_at_ns": 123}
        )
        self.peer.notify("input.released", params.model_dump(mode="json"))

    def residual(self, cause: str, reason: str | None) -> None:
        params = {
            "cause": cause,
            "reason": reason,
            "items": [{"type": "key", "scan_code": 0x11}],
            "checked_at_ns": 456,
        }
        self.peer.notify("input.residual", params)

    async def close(self) -> None:
        await self.peer.close()


@dataclass
class FakeMain:
    """扮演主进程：收 `operator.session` 与 `operator.decision_resolved`。"""

    peer: RpcPeer
    sessions: list[messages.Session] = field(default_factory=list[messages.Session])
    resolved: list[messages.DecisionResolved] = field(
        default_factory=list[messages.DecisionResolved]
    )

    @property
    def session(self) -> messages.Session | None:
        return self.sessions[-1] if self.sessions else None

    def send(self, method: str, model: messages.Session | Any) -> None:
        self.peer.notify(method, model.params())

    async def close(self) -> None:
        await self.peer.close()


async def connect_core(console: OperatorConsole, role: str = CORE_ROLE) -> FakeCore:
    credentials = console.credentials
    connection = await connect(credentials.path, role, credentials.key)
    peer = RpcPeer(connection, timeout=WAIT)
    core = FakeCore(peer)

    def grant(params: dict[str, Any]) -> object:
        if core.refuse:
            raise RpcError(int(ErrorCode.LEASE_REFUSED), "refused by the fake core")
        core.grants.append(LeaseGrantParams.model_validate(params))
        return {}

    def revoke(params: dict[str, Any]) -> object:
        core.revokes.append(LeaseRevokeParams.model_validate(params))
        return {}

    peer.handle("operator_lease.grant", grant)
    peer.handle("operator_lease.revoke", revoke)
    return core


async def connect_main(console: OperatorConsole, role: str = MAIN_ROLE) -> FakeMain:
    credentials = console.credentials
    connection = await connect(credentials.path, role, credentials.key)
    peer = RpcPeer(connection, timeout=WAIT)
    main = FakeMain(peer)
    peer.on_notification(
        messages.SESSION,
        lambda params: main.sessions.append(messages.Session.model_validate(params)),
    )
    peer.on_notification(
        messages.DECISION_RESOLVED,
        lambda params: main.resolved.append(messages.DecisionResolved.model_validate(params)),
    )
    return main


def synthetic(*preconditions: tuple[str, str]) -> Adapter:
    """合成游戏的适配包，换上给定的前置条件。"""
    adapter = load_adapter(GAMES_DIR / "synthetic-game")
    risk = Risk(preconditions=tuple(Precondition(id=i, text=t) for i, t in preconditions))
    return adapter.model_copy(update={"risk": risk})


TWO_PRECONDITIONS = (("window-visible", "游戏窗口在前台且未被遮挡"), ("sound-on", "游戏声音开着"))


@dataclass
class Bench:
    """一个打开的控制台与它的时钟、存储与数据目录。"""

    console: OperatorConsole
    clock: ManualClock
    store: MemorySecretStore
    data_dir: Path

    async def confirm_all(self) -> None:
        while self.console.pending_confirmation is not None:
            await self.console.answer(True)


@contextlib.asynccontextmanager
async def bench(
    data_dir: Path,
    adapter: Adapter | None = None,
    *,
    options: ConsoleOptions | None = None,
    start_delay: float = 0.0,
) -> AsyncGenerator[Bench]:
    clock = ManualClock()
    store = MemorySecretStore()
    # 缺省不倒数（立即授予），倒数由专门的测试给出 start_delay。
    options = replace(options or ConsoleOptions(), start_delay=start_delay)
    console = OperatorConsole(
        adapter or synthetic(*TWO_PRECONDITIONS),
        store=store,
        data_dir=data_dir,
        options=options,
        clock=clock,
        wall=Wall(),
        pid=4242,
    )
    await console.open()
    try:
        yield Bench(console, clock, store, data_dir)
    finally:
        await console.close()


def run[T](work: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(work())


MS = 1_000_000


def session_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------- 确认与开始


def test_start_needs_every_confirmation_and_the_last_one_is_no_live(data_dir: Path) -> None:
    async def main() -> None:
        async with bench(data_dir) as b:
            console = b.console
            assert console.state is SessionState.WAITING
            items = console.confirmations
            assert [item.id for item in items] == ["window-visible", "sound-on", SUPERVISED]
            assert items[-1].text == "本次是受监督的测试，不直播"
            assert not items[-1].precondition
            with pytest.raises(ConsoleError, match="确认项"):
                await console.start()
            await console.answer(True)
            assert console.confirmed == ("window-visible",)
            await console.answer(True)
            with pytest.raises(ConsoleError):
                await console.start()
            await console.answer(True)
            # “不直播”一条不进 confirmed。
            assert console.confirmed == ("window-visible", "sound-on")
            assert console.pending_confirmation is None
            with pytest.raises(ConsoleError, match="没有待确认"):
                await console.answer(True)
            await console.start()
            assert console.state is SessionState.RUNNING

    run(main)


def test_answering_n_ends_the_session(data_dir: Path) -> None:
    async def main() -> None:
        async with bench(data_dir) as b:
            await b.console.answer(True)
            await b.console.answer(False)
            assert b.console.state is SessionState.ENDED
            assert b.console.end_reason == "declined"
            with pytest.raises(ConsoleError, match="已结束"):
                await b.console.start()

    run(main)


# ---------------------------------------------------------------- 许可


def test_each_start_grants_a_bigger_token_and_renews_every_250ms(data_dir: Path) -> None:
    async def main() -> None:
        async with bench(data_dir) as b:
            core = await connect_core(b.console)
            await until(lambda: True if b.console.core_connected else None)
            await b.confirm_all()
            assert core.grants == []
            start = b.clock.now
            await b.console.start()
            first = await until(lambda: core.grants[0] if core.grants else None)
            assert (first.token, first.expires_ns) == (1, start + 500 * MS)
            for step in (1, 2, 3):
                await b.clock.advance(250 * MS)
                renewal = await until(
                    lambda n=step: core.grants[n] if len(core.grants) > n else None
                )
                assert (renewal.token, renewal.expires_ns) == (1, start + (250 * step + 500) * MS)
            await b.console.pause()
            assert core.revokes == [LeaseRevokeParams(token=1)]
            assert b.console.state is SessionState.PAUSED
            # 暂停后不再续约。
            count = len(core.grants)
            await b.clock.advance(1000 * MS)
            await asyncio.sleep(0.05)
            assert len(core.grants) == count
            # 继续：换发更大的令牌。
            await b.console.start()
            resumed = await until(lambda: core.grants[count] if len(core.grants) > count else None)
            assert resumed.token == 2
            assert resumed.expires_ns == b.clock.now + 500 * MS
            with pytest.raises(ConsoleError, match="已在运行"):
                await b.console.start()
            await core.close()

    run(main)


def test_no_grant_without_a_core_and_a_reconnected_core_gets_a_fresh_token(
    data_dir: Path,
) -> None:
    async def main() -> None:
        async with bench(data_dir) as b:
            await b.confirm_all()
            await b.console.start()
            # 实时核没连上时不授予。
            assert b.console.token == 0
            core = await connect_core(b.console)
            first = await until(lambda: core.grants[0] if core.grants else None)
            assert first.token == 1
            # 实时核断开再连上（重启后的新实例）：换发新令牌，不算交还控制。
            await core.close()
            await until(lambda: True if not b.console.core_connected else None)
            restarted = await connect_core(b.console)
            fresh = await until(lambda: restarted.grants[0] if restarted.grants else None)
            assert fresh.token == 2
            assert b.console.state is SessionState.RUNNING
            await b.clock.advance(250 * MS)
            renewal = await until(
                lambda: restarted.grants[1] if len(restarted.grants) > 1 else None
            )
            assert renewal.token == 2
            assert core.grants == [first]
            await restarted.close()

    run(main)


def test_a_refused_renewal_pauses_the_session(data_dir: Path) -> None:
    async def main() -> None:
        async with bench(data_dir) as b:
            core = await connect_core(b.console)
            await until(lambda: True if b.console.core_connected else None)
            await b.confirm_all()
            await b.console.start()
            await until(lambda: core.grants or None)
            core.refuse = True
            await b.clock.advance(250 * MS)
            await until(lambda: True if b.console.state is SessionState.PAUSED else None)
            await until(lambda: core.revokes or None)
            assert core.revokes == [LeaseRevokeParams(token=1)]
            await core.close()

    run(main)


def test_a_takeover_release_pauses_and_others_do_not(data_dir: Path) -> None:
    """本地撤权的原因（接管、急停、注入被拦截、钩子失效）转为暂停；这里以注入的原因集合核对规则。"""
    options = ConsoleOptions(pause_on=frozenset({ReleaseReason.HEARTBEAT_TIMEOUT}))

    async def main() -> None:
        # 缺省的 PAUSE_ON 是真实后端本地撤权的那几种。
        expected_pause_on = frozenset(
            {
                ReleaseReason.TAKEOVER,
                ReleaseReason.EMERGENCY_STOP,
                ReleaseReason.INJECTION_BLOCKED,
                ReleaseReason.HOOK_LOST,
            }
        )
        assert expected_pause_on == PAUSE_ON
        async with bench(data_dir, options=options) as b:
            core = await connect_core(b.console)
            await until(lambda: True if b.console.core_connected else None)
            await b.confirm_all()
            await b.console.start()
            await until(lambda: core.grants or None)
            core.released("lease_revoked")
            core.released("core_start")
            await asyncio.sleep(0.05)
            assert b.console.state is SessionState.RUNNING
            core.released("heartbeat_timeout")
            await until(lambda: True if b.console.state is SessionState.PAUSED else None)
            await until(lambda: core.revokes or None)
            await core.close()
        log = session_events(b.console.log_path or Path())
        paused = [e for e in log if e["event"] == "paused"]
        assert [e["reason"] for e in paused] == ["released:heartbeat_timeout"]
        released = [e["reason"] for e in log if e["event"] == "input_released"]
        assert released == ["lease_revoked", "core_start", "heartbeat_timeout"]

    run(main)


def test_residual_is_shown_and_logged(data_dir: Path) -> None:
    async def main() -> None:
        async with bench(data_dir) as b:
            core = await connect_core(b.console)
            await until(lambda: True if b.console.core_connected else None)
            await b.confirm_all()
            await b.console.start()
            await until(lambda: core.grants or None)
            core.residual("release", "emergency_stop")
            await until(
                lambda: next(
                    (
                        e
                        for e in session_events(b.console.log_path or Path())
                        if e["event"] == "input_residual"
                    ),
                    None,
                )
            )
            # 残留不改变状态。
            assert b.console.state is SessionState.RUNNING
            await core.close()
        log = session_events(b.console.log_path or Path())
        residual = [e for e in log if e["event"] == "input_residual"]
        assert len(residual) == 1
        assert residual[0]["cause"] == "release"
        assert residual[0]["reason"] == "emergency_stop"
        assert residual[0]["items"] == 1

    run(main)


# ---------------------------------------------------------------- 倒数


def test_start_counts_down_then_grants(data_dir: Path) -> None:
    async def main() -> None:
        async with bench(data_dir, start_delay=3.0) as b:
            core = await connect_core(b.console)
            await until(lambda: True if b.console.core_connected else None)
            await b.confirm_all()
            await b.console.start()
            # 倒数中：状态不变，还没授予，没起算时长上限。
            assert b.console.counting_down
            assert b.console.state is SessionState.WAITING
            assert b.console.ends_at_ns is None
            await asyncio.sleep(0.02)
            assert core.grants == []
            # 倒数结束才授予，时长上限从这一刻起算。
            end = b.clock.now + 3_000 * MS
            await b.clock.advance(3_000 * MS)
            await until(lambda: core.grants or None)
            assert b.console.state is SessionState.RUNNING
            assert not b.console.counting_down
            assert b.console.ends_at_ns == end + round(60 * 60 * 1e9)  # max_minutes 默认 60
            await core.close()

    run(main)


def test_pause_and_end_cancel_the_countdown(data_dir: Path) -> None:
    async def main() -> None:
        async with bench(data_dir, start_delay=3.0) as b:
            core = await connect_core(b.console)
            await until(lambda: True if b.console.core_connected else None)
            await b.confirm_all()
            await b.console.start()
            assert b.console.counting_down
            # 暂停取消倒数，状态不变（仍 waiting），到时也不授予。
            await b.console.pause()
            assert not b.console.counting_down
            assert b.console.state is SessionState.WAITING
            await b.clock.advance(3_000 * MS)
            await asyncio.sleep(0.02)
            assert core.grants == []
            # 可以再开始。
            await b.console.start()
            assert b.console.counting_down
            await b.clock.advance(3_000 * MS)
            await until(lambda: core.grants or None)
            assert b.console.state is SessionState.RUNNING
            # end 也取消倒数。
            await b.console.pause()
            await until(lambda: True if b.console.state is SessionState.PAUSED else None)
            await b.console.start()
            assert b.console.counting_down
            await b.console.end("operator")
            assert not b.console.counting_down
            assert b.console.ended
            await core.close()

    run(main)


def test_a_reconnected_core_while_running_grants_without_a_countdown(data_dir: Path) -> None:
    async def main() -> None:
        async with bench(data_dir, start_delay=3.0) as b:
            core = await connect_core(b.console)
            await until(lambda: True if b.console.core_connected else None)
            await b.confirm_all()
            await b.console.start()
            await b.clock.advance(3_000 * MS)
            first = await until(lambda: core.grants[0] if core.grants else None)
            assert first.token == 1
            # 实时核重连（重启后的新实例）：立即授予，不再倒数。
            await core.close()
            await until(lambda: True if not b.console.core_connected else None)
            restarted = await connect_core(b.console)
            fresh = await until(lambda: restarted.grants[0] if restarted.grants else None)
            assert fresh.token == 2
            assert not b.console.counting_down
            assert b.console.state is SessionState.RUNNING
            await restarted.close()

    run(main)


def test_the_time_cap_counts_from_the_first_start_and_ends_the_session(data_dir: Path) -> None:
    options = ConsoleOptions(max_minutes=1)

    async def main() -> None:
        async with bench(data_dir, options=options) as b:
            core = await connect_core(b.console)
            await until(lambda: True if b.console.core_connected else None)
            await b.confirm_all()
            await b.clock.advance(30_000 * MS)  # 还没开始：不计时
            assert b.console.ends_at_ns is None
            start = b.clock.now
            await b.console.start()
            assert b.console.ends_at_ns == start + 60_000 * MS
            await until(lambda: core.grants or None)
            await b.console.pause()
            # 暂停中照样计时；继续后到时即吊销并结束。
            await b.clock.advance(20_000 * MS)
            await b.console.start()
            revokes = len(core.revokes)
            await b.clock.advance(39_999 * MS)
            assert b.console.state is SessionState.RUNNING
            await b.clock.advance(1 * MS)
            await until(lambda: True if b.console.ended else None)
            assert b.console.end_reason == "time_cap"
            assert len(core.revokes) == revokes + 1
            assert core.revokes[-1].token == b.console.token == 2
            with pytest.raises(ConsoleError, match="已结束"):
                await b.console.start()
            await core.close()

    run(main)


# ---------------------------------------------------------------- 连接


def test_one_connection_per_role_and_other_roles_are_rejected(data_dir: Path) -> None:
    async def main() -> None:
        async with bench(data_dir) as b:
            core = await connect_core(b.console)
            main_side = await connect_main(b.console)
            await until(
                lambda: True if b.console.core_connected and b.console.main_connected else None
            )
            second_core = await connect_core(b.console)
            second_main = await connect_main(b.console)
            stranger = await connect_main(b.console, role="game-runtime.other")
            for rejected in (second_core.peer, second_main.peer, stranger.peer):
                await asyncio.wait_for(rejected.closed, WAIT)
            assert core.peer.connection.is_open
            assert main_side.peer.connection.is_open
            # 前一个断开后可以再连。
            await main_side.close()
            await until(lambda: True if not b.console.main_connected else None)
            again = await connect_main(b.console)
            session = await until(lambda: again.session)
            assert session.state is SessionState.WAITING
            await again.close()
            await core.close()
        log = session_events(b.console.log_path or Path())
        rejected_roles = [e["role"] for e in log if e["event"] == "rejected"]
        assert sorted(rejected_roles) == sorted([CORE_ROLE, MAIN_ROLE, "game-runtime.other"])
        connected = [e["role"] for e in log if e["event"] == "connected"]
        assert connected == ["core", "main", "main"]

    run(main)


def test_the_main_process_gets_the_session_on_connect_and_on_every_change(
    data_dir: Path,
) -> None:
    async def main() -> None:
        async with bench(data_dir) as b:
            main_side = await connect_main(b.console)
            first = await until(lambda: main_side.session)
            assert first == messages.Session(
                adapter="synthetic-game",
                state=SessionState.WAITING,
                confirmed=(),
                goal=None,
                ends_at_ns=None,
            )
            await b.console.answer(True)
            await until(lambda: True if main_side.session and main_side.session.confirmed else None)
            await b.console.answer(True)
            await b.console.answer(True)
            await b.console.start()
            running = await until(
                lambda: (
                    main_side.session
                    if main_side.session and main_side.session.state is SessionState.RUNNING
                    else None
                )
            )
            assert running.confirmed == ("window-visible", "sound-on")
            assert running.ends_at_ns == b.console.ends_at_ns
            b.console.set_goal("  推进到第一次选择  ")
            await until(
                lambda: (
                    True
                    if main_side.session and main_side.session.goal == "推进到第一次选择"
                    else None
                )
            )
            b.console.set_goal("")
            await until(
                lambda: True if main_side.session and main_side.session.goal is None else None
            )
            await b.console.end("operator")
            await until(
                lambda: (
                    True
                    if main_side.session and main_side.session.state is SessionState.ENDED
                    else None
                )
            )
            await main_side.close()

    run(main)


def test_decisions_are_chosen_by_index_and_actions_are_recorded(data_dir: Path) -> None:
    async def main() -> None:
        async with bench(data_dir) as b:
            main_side = await connect_main(b.console)
            await until(lambda: main_side.session)
            with pytest.raises(ConsoleError, match="没有打开的决策点"):
                b.console.choose(0)
            decision = messages.DecisionOpened(
                id="choice-1",
                prompt="去哪里？",
                options=(
                    messages.DecisionOption(index=0, text="钟楼"),
                    messages.DecisionOption(index=1, text="商店"),
                ),
                candidates=(1, 0),
                deadline_ns=None,
            )
            main_side.send(messages.DECISION_OPENED, decision)
            current = await until(lambda: b.console.current_decision)
            assert current == decision
            # 重复的 id 不算新的决策点。
            main_side.send(messages.DECISION_OPENED, decision)
            with pytest.raises(ConsoleError, match="没有序号 7"):
                b.console.choose(7)
            b.console.choose(1)
            resolved = await until(lambda: main_side.resolved or None)
            assert resolved == [messages.DecisionResolved(id="choice-1", index=1)]
            assert b.console.resolved("choice-1") == 1
            with pytest.raises(ConsoleError, match="已给出过选择"):
                b.console.choose(0)
            main_side.send(
                messages.DECISION_CLOSED,
                messages.DecisionClosed(id="choice-1", index=1, by="operator"),
            )
            main_side.send(
                messages.ACTION, messages.Action(category="select", summary="点击“商店”")
            )
            await until(lambda: True if b.console.current_decision is None else None)
            # 不合法的消息被忽略。
            main_side.peer.notify(messages.ACTION, {"category": "jump", "summary": "x"})
            main_side.peer.notify(
                messages.DECISION_CLOSED, {"id": "nope", "index": None, "by": "withdrawn"}
            )
            await asyncio.sleep(0.05)
            await main_side.close()
        log = session_events(b.console.log_path or Path())
        kinds = [e["event"] for e in log]
        assert kinds.count("decision_opened") == 1
        assert kinds.count("action") == 1
        closed = next(e for e in log if e["event"] == "decision_closed")
        assert (closed["id"], closed["index"], closed["by"]) == ("choice-1", 1, "operator")
        chosen = next(e for e in log if e["event"] == "decision_chosen")
        assert (chosen["id"], chosen["index"]) == ("choice-1", 1)

    run(main)


# ---------------------------------------------------------------- SecretStore 与会话记录


def test_the_connection_info_is_in_the_secret_store_while_open(data_dir: Path) -> None:
    async def main() -> None:
        async with bench(data_dir) as b:
            info = read_session_info(b.store)
            assert info is not None
            assert info.credentials() == b.console.credentials
            assert info.launcher_pid == 4242
            assert info.session_id.startswith("op")
            assert info.started_at == "2026-11-01T08:30:15.251Z"
            store = b.store
        assert read_session_info(store) is None

    run(main)


def test_closing_leaves_the_connection_info_of_a_later_console(data_dir: Path) -> None:
    async def main() -> None:
        async with bench(data_dir) as first:
            later = OperatorConsole(
                synthetic(*TWO_PRECONDITIONS),
                store=first.store,
                data_dir=data_dir,
                clock=first.clock,
                wall=Wall(),
                pid=4343,
            )
            await later.open()
            try:
                await first.console.close()
                info = read_session_info(first.store)
                assert info is not None
                assert info.credentials() == later.credentials
            finally:
                await later.close()
            assert read_session_info(first.store) is None

    run(main)


def test_the_session_log_records_the_session(data_dir: Path) -> None:
    async def main() -> Path:
        async with bench(data_dir) as b:
            core = await connect_core(b.console)
            main_side = await connect_main(b.console)
            await until(
                lambda: True if b.console.core_connected and b.console.main_connected else None
            )
            await b.confirm_all()
            await b.console.start()
            await until(lambda: core.grants or None)
            core.released("lease_revoked")
            await until(lambda: True if b.console.token else None)
            await b.console.pause()
            await b.console.start()
            await until(
                lambda: True if len(core.grants) >= 2 and core.grants[-1].token == 2 else None
            )
            main_side.send(messages.ACTION, messages.Action(category="advance", summary="回车"))
            await asyncio.sleep(0.05)
            await main_side.close()
            await core.close()
            await until(lambda: True if not b.console.core_connected else None)
            path = b.console.log_path
            assert path is not None
        return path

    path = run(main)
    assert path.parent == sessions_dir(data_dir, "synthetic-game")
    assert path.name == "20261101T083015Z.jsonl"
    log = session_events(path)
    kinds = [e["event"] for e in log]
    assert kinds[0] == "session_started"
    assert kinds[-1] == "session_ended"
    for expected in (
        "confirmation",
        "started",
        "resumed",
        "paused",
        "connected",
        "disconnected",
        "lease_granted",
        "input_released",
        "action",
    ):
        assert expected in kinds, expected
    started = log[0]
    assert started["adapter"] == "synthetic-game"
    assert started["pid"] == 4242
    assert started["max_minutes"] == 60
    assert [c["id"] for c in started["confirmations"]] == ["window-visible", "sound-on", SUPERVISED]
    confirmations = [(e["id"], e["answer"]) for e in log if e["event"] == "confirmation"]
    assert confirmations == [("window-visible", "y"), ("sound-on", "y"), (SUPERVISED, "y")]
    assert [e["token"] for e in log if e["event"] == "lease_granted"] == [1, 2]
    assert next(e for e in log if e["event"] == "paused")["reason"] == "operator"
    ended = log[-1]
    assert ended["reason"] == "closed"
    assert ended["counts"]["confirmation"] == 3
    assert ended["counts"]["started"] == 1
    assert ended["counts"]["resumed"] == 1
    assert ended["counts"]["input_released"] == 1
    assert ended["counts"]["action"] == 1
    for entry in log:
        assert entry["at"].endswith("Z")
        assert isinstance(entry["mono_ns"], int)
    # 会话记录里没有密钥。
    assert "channelKey" not in path.read_text(encoding="utf-8")


def test_a_second_session_in_the_same_second_gets_its_own_log(data_dir: Path) -> None:
    started = datetime(2026, 11, 1, 8, 0, 0, tzinfo=UTC)
    directory = sessions_dir(data_dir, "synthetic-game")
    first = SessionLog.create(directory, started)
    second = SessionLog.create(directory, started)
    first.close()
    second.close()
    assert [first.path.name, second.path.name] == [
        "20261101T080000Z.jsonl",
        "20261101T080000Z-2.jsonl",
    ]


# ---------------------------------------------------------------- 主进程一侧


def test_the_client_tracks_the_session_and_may_autoplay_only_while_running(
    data_dir: Path,
) -> None:
    async def main() -> None:
        async with bench(data_dir) as b:
            resolved: list[messages.DecisionResolved] = []
            ended: list[str] = []
            client = OperatorClient(
                synthetic(*TWO_PRECONDITIONS),
                store=b.store,
                on_decision_resolved=resolved.append,
                on_ended=ended.append,
            )
            await client.connect()
            assert client.credentials == b.console.credentials
            session = client.session
            assert session is not None
            assert session.state is SessionState.WAITING
            assert not client.may_autoplay
            await b.confirm_all()
            await b.console.start()
            await until(lambda: True if client.may_autoplay else None)
            await b.console.pause()
            await until(lambda: True if not client.may_autoplay else None)
            await b.console.start()
            await until(lambda: True if client.may_autoplay else None)
            # 决策点与动作经客户端发出，选择交回调用方。
            client.open_decision(
                messages.DecisionOpened(
                    id="d1",
                    prompt="选哪个？",
                    options=(messages.DecisionOption(index=3, text="甲"),),
                    candidates=(3,),
                    deadline_ns=b.clock.now + 5_000 * MS,
                )
            )
            await until(lambda: b.console.current_decision)
            b.console.choose(3)
            await until(lambda: resolved or None)
            assert resolved == [messages.DecisionResolved(id="d1", index=3)]
            client.close_decision(messages.DecisionClosed(id="d1", index=3, by="operator"))
            client.report_action(messages.Action(category="select", summary="甲"))
            await until(lambda: True if b.console.current_decision is None else None)
            # 控制台结束会话并退出：按会话结束处理。
            await b.console.end("operator")
            await until(lambda: ended or None)
            assert not client.may_autoplay
            assert ended == ["ended"]
            await b.console.close()
            await until(lambda: True if not client.connected else None)
            with pytest.raises(ChannelError):
                client.report_action(messages.Action(category="advance", summary="x"))
            await client.close()

    run(main)


def test_the_client_requires_its_own_preconditions_to_be_confirmed(data_dir: Path) -> None:
    """主进程只按自己适配包的前置条件判断：控制台确认的不含它们时不进入自动游玩。"""

    async def main() -> None:
        async with bench(data_dir, synthetic()) as b:
            client = OperatorClient(synthetic(*TWO_PRECONDITIONS), store=b.store)
            await client.connect()
            await b.confirm_all()
            await b.console.start()
            await until(
                lambda: (
                    True
                    if client.session is not None and client.session.state is SessionState.RUNNING
                    else None
                )
            )
            assert not client.may_autoplay
            await client.close()
            assert client.ended == "closed"

    run(main)


def test_a_disconnect_counts_as_the_session_ending(data_dir: Path) -> None:
    async def main() -> None:
        async with bench(data_dir) as b:
            ended: list[str] = []
            client = OperatorClient(
                synthetic(*TWO_PRECONDITIONS), store=b.store, on_ended=ended.append
            )
            await client.connect()
            await b.confirm_all()
            await b.console.start()
            await until(lambda: True if client.may_autoplay else None)
            # 控制台的连接断开（这里由控制台一侧关闭这条连接）。
            peer = b.console._main  # pyright: ignore[reportPrivateUsage]
            assert peer is not None
            await peer.close()
            await until(lambda: ended or None)
            assert ended == ["disconnected"]
            assert not client.may_autoplay

    run(main)


def test_the_client_disconnects_from_a_console_of_another_adapter(data_dir: Path) -> None:
    async def main() -> None:
        other = synthetic().model_copy(update={"id": "another-game"})
        async with bench(data_dir, other) as b:
            client = OperatorClient(synthetic(), store=b.store)
            with pytest.raises(OperatorAdapterMismatchError, match="another-game"):
                await client.connect()
            assert client.ended == "adapter_mismatch"
            assert not client.may_autoplay
            await until(lambda: True if not b.console.main_connected else None)

    run(main)


def test_the_client_needs_the_connection_info(tmp_path: Path) -> None:
    async def main() -> None:
        client = OperatorClient(synthetic(), store=MemorySecretStore())
        with pytest.raises(OperatorUnavailableError, match="uv run operate synthetic-game"):
            await client.connect()

    run(main)
