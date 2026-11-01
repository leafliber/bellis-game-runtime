"""测试设备：测试许可源加假输入设备，只用于测试，生产代码不导入它。

设计见 `docs/modules/core-protocol.md` 的“测试设备连接”。实时核以假输入后端启动时，作为
`bellis-channel/1` 客户端连到这里（`CoreLinkOptions.test_device` 交给它地址与密钥）：

- 测试许可源：`grant`、`renew`、`stop_renewing`、`revoke` 经 `test_lease.grant`、
  `test_lease.revoke` 授予、续约与吊销输入许可。许可不来自主进程，所以主进程卡死或崩溃时
  测试一方照样能续约或停止续约。
- 假输入设备：收下假后端发来的每个 `fake_input.event`，独立记下“当前按住了什么”（`held`），
  可以再把事件转给合成游戏（`on_event`）。主进程崩溃时，测试一方仍能看到实时核是否发出了抬起。

测试设备运行在自己的线程与事件循环上，与主进程一侧的代码互不阻塞：主进程的事件循环卡住时它照常
续约与收事件。公开的方法可以从任何事件循环里等待；`on_event` 在测试设备的线程上调用。
"""

import asyncio
import contextlib
import logging
import secrets
import threading
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from core_protocol import models
from core_protocol.methods import FAKE_INPUT_EVENT, TEST_LEASE_GRANT, TEST_LEASE_REVOKE
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
from game_runtime.core_link.link import CORE_ROLE

logger = logging.getLogger(__name__)

DEFAULT_LEASE_TTL = 0.5
"""许可的有效期（秒），取 Bellis D-34 的游戏输入许可初值约 500ms。"""
DEFAULT_RENEW_INTERVAL = 0.25
"""续约的间隔（秒），取 Bellis D-34 的每 250ms 内续约。"""

type Held = tuple[str, str | int]
"""按住的一项：`("key", 扫描码)`、`("mouse", 鼠标键)` 或 `("axis", 轴名)`。"""

type FakeEvent = (
    models.FakeKeyDown
    | models.FakeKeyUp
    | models.FakeMouseDown
    | models.FakeMouseUp
    | models.FakeAxisSet
    | models.FakeAxisRelease
    | models.FakeClick
    | models.FakeKeyPress
    | models.FakeScroll
    | models.FakeMouseMove
    | models.FakeReleaseAll
)
"""假输入后端发出的事件之一。"""


@dataclass(frozen=True)
class DeviceEvent:
    """测试设备收到的一个假输入事件。"""

    connection: int
    """第几个连上来的实时核实例，从 1 起。"""
    seq: int
    """事件在实例内的序号。"""
    at_ns: int
    """实时核把它交给假后端的时刻（与 `time.perf_counter_ns()` 同一时钟）。"""
    received_ns: int
    """测试设备收到它的时刻。"""
    event: FakeEvent
    held_after: frozenset[Held]
    """处理完这个事件之后，测试设备记下的按住的项。"""

    @property
    def type(self) -> str:
        return self.event.type


class FakeDevice:
    """测试设备。`start` 之后把 `credentials` 交给 `CoreLinkOptions.test_device`；`close` 关闭。"""

    def __init__(
        self,
        *,
        on_event: Callable[[DeviceEvent], None] | None = None,
        rpc_timeout: float = 2.0,
    ) -> None:
        self._on_event = on_event
        self._rpc_timeout = rpc_timeout
        self._lock = threading.Lock()
        self._events: list[DeviceEvent] = []
        self._held: dict[Held, float | None] = {}
        self._anomalies: list[str] = []
        self._connections = 0
        self._peer: RpcPeer | None = None
        self._token = 0
        self._expires_ns: int | None = None
        self._renewing: tuple[asyncio.Task[None], asyncio.Event] | None = None
        self._renew_errors: list[str] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._credentials: ChannelCredentials | None = None
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._error: BaseException | None = None
        self._thread: threading.Thread | None = None

    # 启动与关闭

    def start(self) -> None:
        """在自己的线程上开始监听，等到可以连接为止。"""
        self._thread = threading.Thread(target=self._run, name="fake-device", daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("测试设备没有在 10 秒内开始监听")
        if self._error is not None:
            raise RuntimeError("测试设备没能开始监听") from self._error

    def close(self) -> None:
        """停止续约、断开实时核、停止监听。可以重复调用。"""
        loop, stop, thread = self._loop, self._stop, self._thread
        if loop is not None and stop is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(stop.set)
        if thread is not None:
            thread.join(10)
            self._thread = None

    def __enter__(self) -> "FakeDevice":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def credentials(self) -> ChannelCredentials:
        """交给实时核的地址与密钥。"""
        if self._credentials is None:
            raise RuntimeError("测试设备还没有开始监听")
        return self._credentials

    # 观察

    def events(self) -> list[DeviceEvent]:
        """到现在为止收到的全部事件。"""
        with self._lock:
            return list(self._events)

    @property
    def held(self) -> frozenset[Held]:
        """测试设备记下的、当前按住的项。"""
        with self._lock:
            return frozenset(self._held)

    @property
    def anomalies(self) -> list[str]:
        """不合预期的事件：抬起没有按下的项、按下已经按住的项。"""
        with self._lock:
            return list(self._anomalies)

    @property
    def connections(self) -> int:
        """连上来过的实时核实例数。"""
        with self._lock:
            return self._connections

    @property
    def renew_errors(self) -> list[str]:
        """续约失败的记录。"""
        with self._lock:
            return list(self._renew_errors)

    @property
    def lease_expires_ns(self) -> int | None:
        """最近一次被接受的授予或续约给出的到期时刻。"""
        with self._lock:
            return self._expires_ns

    async def until[T](self, probe: Callable[["FakeDevice"], T | None], timeout: float = 5.0) -> T:
        """等到 `probe(self)` 不是 None，返回它；超时抛 `TimeoutError`。
        在调用方的事件循环里轮询。"""
        deadline = time.monotonic() + timeout
        while (value := probe(self)) is None:
            if time.monotonic() >= deadline:
                raise TimeoutError("测试设备上等待的条件没有成立")
            await asyncio.sleep(0.002)
        return value

    async def until_connected(self, count: int = 1, timeout: float = 5.0) -> None:
        """等到第 `count` 个实时核实例连上来。"""
        await self.until(lambda device: True if device.connections >= count else None, timeout)

    # 测试许可源

    async def grant(self, *, ttl: float = DEFAULT_LEASE_TTL, token: int | None = None) -> int:
        """授予或续约一次：令牌缺省用新的（比上一个大一），到期时刻为现在加 `ttl` 秒。返回令牌。
        实时核拒绝时抛 `RpcError`（`lease_refused`）。"""
        return await self._call(self._grant(token, ttl))

    async def grant_raw(self, token: int, expires_ns: int) -> None:
        """按给定的令牌与到期时刻发一次 `test_lease.grant`，不改变本地记的令牌。"""
        await self._call(self._request_grant(token, expires_ns))

    async def renew(
        self, *, ttl: float = DEFAULT_LEASE_TTL, interval: float = DEFAULT_RENEW_INTERVAL
    ) -> int:
        """以新的令牌授予，然后每 `interval` 秒续约一次，直到 `stop_renewing` 或 `revoke`。
        返回令牌。实时核换了实例时照样续约：新实例没有许可，同一令牌对它是新的许可。"""
        return await self._call(self._renew(ttl, interval))

    async def stop_renewing(self) -> None:
        """停止续约；已授予的许可照常到期。"""
        await self._call(self._stop_renewing())

    async def revoke(self, token: int | None = None) -> None:
        """停止续约并吊销令牌（缺省为当前令牌）；实时核释放完成后才返回。"""
        await self._call(self._revoke(token))

    async def disconnect(self) -> None:
        """断开当前的实时核：它从此没有许可（`lease_source_lost`）。"""
        await self._call(self._disconnect())

    # 以下都在测试设备的线程上运行。

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except BaseException as error:
            # 交给 start 报告。
            self._error = error
            self._ready.set()

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        with session_credentials(f"dev{secrets.token_hex(4)}") as credentials:
            server: ChannelServer = await listen(
                credentials.path,
                credentials.key,
                on_connection=self._on_connection,
                on_rejected=lambda error: logger.warning("测试设备拒绝了一个连接：%s", error),
                on_error=lambda error: logger.error("测试设备的连接装配失败：%s", error),
            )
            self._credentials = credentials
            self._ready.set()
            try:
                await self._stop.wait()
            finally:
                await self._stop_renewing()
                peer = self._peer
                if peer is not None:
                    await peer.close()
                await server.close()

    async def _call[T](self, coroutine: Coroutine[Any, Any, T]) -> T:
        """在测试设备的事件循环上运行 `coroutine`，在调用方的事件循环里等它。"""
        loop = self._loop
        if loop is None:
            coroutine.close()
            raise RuntimeError("测试设备还没有开始监听")
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coroutine, loop))

    def _on_connection(self, connection: Connection) -> None:
        if connection.role != CORE_ROLE:
            logger.warning("测试设备拒绝了角色为 %s 的连接", connection.role)
            connection.abort()
            return
        with self._lock:
            self._connections += 1
            number = self._connections
        peer = RpcPeer(connection, timeout=self._rpc_timeout)
        peer.on_notification(FAKE_INPUT_EVENT.name, lambda params: self._on_input(number, params))
        self._peer = peer
        logger.info("实时核实例 %d 连上了测试设备", number)

    def _on_input(self, connection: int, params: dict[str, Any]) -> None:
        received = time.perf_counter_ns()
        try:
            message = models.FakeInputEventParams.model_validate(params)
        except ValidationError as error:
            logger.error("测试设备收到不合法的事件：%s", error)
            return
        event = message.event.root
        with self._lock:
            self._apply(event)
            record = DeviceEvent(
                connection, message.seq, message.at_ns, received, event, frozenset(self._held)
            )
            self._events.append(record)
        if self._on_event is not None:
            self._on_event(record)

    def _apply(self, event: FakeEvent) -> None:
        """按事件更新按住的项。须持有 `_lock`。"""
        match event:
            case models.FakeKeyDown(scan_code=code):
                self._press(("key", code), None)
            case models.FakeKeyUp(scan_code=code):
                self._lift(("key", code))
            case models.FakeMouseDown(button=button):
                self._press(("mouse", button.value), None)
            case models.FakeMouseUp(button=button):
                self._lift(("mouse", button.value))
            case models.FakeAxisSet(axis=axis, value=value):
                self._held[("axis", axis)] = value
            case models.FakeAxisRelease(axis=axis):
                self._lift(("axis", axis))
            case _:
                pass

    def _press(self, item: Held, value: float | None) -> None:
        if item in self._held:
            self._anomalies.append(f"按下已经按住的 {item}")
        self._held[item] = value

    def _lift(self, item: Held) -> None:
        if self._held.pop(item, _MISSING) is _MISSING:
            self._anomalies.append(f"抬起没有按住的 {item}")

    def _current(self) -> RpcPeer:
        peer = self._peer
        if peer is None or not peer.connection.is_open:
            raise ChannelError("closed", "没有连着的实时核")
        return peer

    async def _request_grant(self, token: int, expires_ns: int) -> None:
        params = models.LeaseGrantParams(token=token, expires_ns=expires_ns)
        await self._current().request(
            TEST_LEASE_GRANT.name, params.model_dump(mode="json"), timeout=self._rpc_timeout
        )
        with self._lock:
            self._expires_ns = expires_ns

    async def _grant(self, token: int | None, ttl: float) -> int:
        if token is None:
            self._token += 1
            token = self._token
        else:
            self._token = max(self._token, token)
        await self._request_grant(token, time.perf_counter_ns() + round(ttl * 1e9))
        return token

    async def _renew(self, ttl: float, interval: float) -> int:
        await self._stop_renewing()
        token = await self._grant(None, ttl)
        stop = asyncio.Event()
        task = asyncio.get_running_loop().create_task(self._renew_loop(token, ttl, interval, stop))
        self._renewing = (task, stop)
        return token

    async def _renew_loop(
        self, token: int, ttl: float, interval: float, stop: asyncio.Event
    ) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), interval)
            if stop.is_set():
                return
            try:
                await self._grant(token, ttl)
            except (ChannelError, RpcError) as error:
                with self._lock:
                    self._renew_errors.append(str(error))

    async def _stop_renewing(self) -> None:
        """停止续约。正在发的一次续约不打断，等它有了回应：之后 `lease_expires_ns` 与实时核里的
        到期时刻一致。"""
        renewing, self._renewing = self._renewing, None
        if renewing is not None:
            task, stop = renewing
            stop.set()
            await task

    async def _revoke(self, token: int | None) -> None:
        await self._stop_renewing()
        params = models.LeaseRevokeParams(token=self._token if token is None else token)
        await self._current().request(
            TEST_LEASE_REVOKE.name, params.model_dump(mode="json"), timeout=self._rpc_timeout
        )

    async def _disconnect(self) -> None:
        await self._stop_renewing()
        peer = self._peer
        if peer is not None:
            await peer.close()


_MISSING = object()
