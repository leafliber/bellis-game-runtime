"""主进程一侧的核连接：拉起实时核、握手、控制产出者心跳、`CoreControl` 与停止。

设计见 `docs/modules/realtime-core.md` 的“启动与生命线”。每次启动都是一个新的实时核实例：
新的会话地址与密钥、新的监听与子进程。等输入已释放的确认跨实例保留，按以下任一事件完成：

- `producer.revoke` 的回应：该代次及更早代次的输入已释放；
- 新实例的 `input.released(core_start)`：新实例启动时已释放全部输入，此前的等待全部完成；
- 实例以退出码 0 退出：实时核只在释放全部输入之后以 0 退出（`core.stop`、核连接断开或 stdin EOF）；
  此后没有实例在运行时，新的等待也立即完成。

实例被强制终止或崩溃（退出码非 0）时没有进程发出按键抬起，等待保留到下一个实例的 `core_start`。

配置了采集（`CoreLinkOptions.capture`）时，第一次 `start` 创建帧环；每个实例就绪后先挂接帧环，
再设定采集目标，然后才开始发心跳。帧环跨实例保留，`stop` 在实时核退出之后销毁它。同一时刻只有
一个实例：上一个实例退出之后才能再 `start`，所以帧环只挂接给一个实时核，新实例从帧环头部的
`write_seq` 续写帧号。
"""

import asyncio
import contextlib
import logging
import math
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from core_protocol.methods import (
    CAPTURE_SET_TARGET,
    CORE_HEALTH,
    CORE_HELLO,
    CORE_STOP,
    FRAME_RING_ATTACH,
    INPUT_RELEASED,
    PRODUCER_HEARTBEAT,
    PRODUCER_REVOKE,
    PROTOCOL_VERSION,
    ErrorCode,
)
from core_protocol.models import (
    CoreHealthParams,
    CoreHelloParams,
    CoreHelloResult,
    InputReleasedParams,
    Producer,
    ProducerHeartbeatParams,
    ProducerRevokeParams,
    ReleaseReason,
)
from game_runtime.channel import (
    ChannelError,
    ChannelServer,
    Connection,
    RpcError,
    RpcErrorCode,
    RpcPeer,
    listen,
    session_credentials,
)
from game_runtime.core_link.capture import CaptureConfig
from game_runtime.core_link.process import ManagedProcess, StopResult, StopStage, StopTimeouts
from game_runtime.core_link.process import base_environment as _base_environment
from game_runtime.frame_ring import FrameRing
from game_runtime.kernel import ControlGrant, Kernel

logger = logging.getLogger(__name__)

CORE_ROLE = "game-runtime.core"
"""实时核连接主进程时使用的角色。"""
ENV_CHANNEL_PATH = "BELLIS_CHANNEL_PATH"
ENV_CHANNEL_KEY = "BELLIS_CHANNEL_KEY"
ENV_ROLE = "BELLIS_ROLE"
"""凭据经这三个环境变量交给子进程，与 Bellis 的 `ChildEnv` 相同。"""


class CoreStartError(RuntimeError):
    """实时核没能就绪：启动失败、握手失败、就绪前退出或超时。"""


@dataclass(frozen=True, slots=True)
class CoreLinkOptions:
    """核连接的配置，时限单位为秒。缺省值的依据见模块文档。"""

    executable: Path
    """`realtime-core` 可执行文件。"""
    heartbeat_interval: float = 0.1
    """控制产出者心跳的周期。"""
    heartbeat_timeout: float = 0.5
    """实时核判定心跳超时的时限，经命令行交给实时核；须大于心跳周期。"""
    health_interval: float = 1.0
    """实时核发 `core.health` 的周期，经命令行交给实时核。"""
    health_timeout: float = 3.0
    """超过这么久没有收到 `core.health` 即判定实时核失联并记录告警。"""
    start_timeout: float = 10.0
    """从拉起到收到 `core.hello` 与 `input.released(core_start)` 的时限。"""
    rpc_timeout: float = 2.0
    """发给实时核的请求（`producer.revoke`、`frame_ring.attach`、`capture.set_target`）的截止时间。"""
    stop: StopTimeouts = field(default_factory=StopTimeouts)
    """停止各步的时限。"""
    capture: CaptureConfig | None = None
    """帧环与采集目标；`None` 时不建帧环，也不设定采集目标。"""

    def __post_init__(self) -> None:
        timeouts = (
            self.heartbeat_interval,
            self.heartbeat_timeout,
            self.health_interval,
            self.health_timeout,
            self.start_timeout,
            self.rpc_timeout,
        )
        if min(timeouts) <= 0:
            raise ValueError("时限与周期必须为正")
        if self.heartbeat_timeout <= self.heartbeat_interval:
            raise ValueError("心跳超时须大于心跳周期")
        if self.health_timeout <= self.health_interval:
            raise ValueError("健康超时须大于健康周期")

    def argv(self) -> list[str]:
        """拉起实时核的参数列表。"""
        return [
            str(self.executable),
            "--heartbeat-timeout-ms",
            str(_millis(self.heartbeat_timeout)),
            "--health-interval-ms",
            str(_millis(self.health_interval)),
        ]


def _millis(seconds: float) -> int:
    return max(1, math.ceil(seconds * 1000))


@dataclass(eq=False)
class _Instance:
    """一次拉起的实时核。"""

    number: int
    process: ManagedProcess
    server: ChannelServer
    credentials: contextlib.ExitStack
    ready: asyncio.Future[None]
    """收到 `input.released(core_start)` 时完成。"""
    peer: RpcPeer | None = None
    hello_at_ns: int | None = None
    """接受 `core.hello`、回应之前的 `time.perf_counter_ns()`。"""
    tasks: list[asyncio.Task[None]] = field(default_factory=list[asyncio.Task[None]])
    closing: asyncio.Task[None] | None = None


class CoreLink:
    """主进程与实时核之间的核连接，也是插件内核的 `CoreControl`。

    用法：`link.bind_control(lambda: kernel.active_control)` 让心跳跟随当前的提交资格；
    `start` 拉起实时核并等到就绪；停止主进程时先停插件内核再 `stop`（见 `shutdown_runtime`）。
    实例退出后可以再 `start`。
    """

    def __init__(
        self,
        options: CoreLinkOptions,
        *,
        on_released: Callable[[InputReleasedParams], None] | None = None,
        on_health: Callable[[CoreHealthParams], None] | None = None,
    ) -> None:
        self._options = options
        self._on_released = on_released
        self._on_health = on_health
        self._grant_source: Callable[[], ControlGrant | None] | None = None
        self._instance: _Instance | None = None
        self._instances = 0
        self._clean = True
        self._released: dict[int, asyncio.Future[None]] = {}
        self._heartbeat_seq = 0
        self._last_health: CoreHealthParams | None = None
        self._last_health_at_ns: int | None = None
        self._last_returncode: int | None = None
        self._exited: _Instance | None = None
        """最近一个已退出的实例；`stop` 等它的善后做完。"""
        self._frames: FrameRing | None = None

    def bind_control(self, source: Callable[[], ControlGrant | None]) -> None:
        """给出当前的提交资格，通常是 `lambda: kernel.active_control`。资格有效时才发心跳。"""
        self._grant_source = source

    # 状态

    @property
    def pid(self) -> int | None:
        """运行中的实时核的进程号。"""
        return None if self._instance is None else self._instance.process.pid

    @property
    def running(self) -> bool:
        return self._instance is not None

    @property
    def hello_at_ns(self) -> int | None:
        """运行中的实例握手时（回应之前）主进程的 `time.perf_counter_ns()`。"""
        return None if self._instance is None else self._instance.hello_at_ns

    @property
    def last_health(self) -> CoreHealthParams | None:
        return self._last_health

    @property
    def frames(self) -> FrameRing | None:
        """帧环，供宿主代码读帧（`read_latest`、`latest_frame_number`，帧龄用 `frame_age_ns`）。

        配置了采集时由第一次 `start` 创建，跨实时核重启保留，`stop` 在实时核退出后销毁；其余时候
        为 None。帧环归 `CoreLink` 所有，宿主只读，不关闭它。
        """
        return self._frames

    @property
    def healthy(self) -> bool:
        """`health_timeout` 内收到过运行中实例的 `core.health`。"""
        at = self._last_health_at_ns
        timeout_ns = self._options.health_timeout * 1e9
        return self.running and at is not None and time.perf_counter_ns() - at <= timeout_ns

    # 启动与停止

    async def start(self) -> None:
        """拉起实时核，等到它握手并发出 `input.released(core_start)`；配置了采集时再挂接帧环、
        设定采集目标。

        失败时已终止子进程并清理（帧环保留到 `stop`），抛 `CoreStartError`。
        """
        # 上一个实例退出之后 `_instance` 才清空：帧环同一时刻只挂接给一个实时核。
        if self._instance is not None:
            raise RuntimeError("实时核已在运行")
        capture = self._options.capture
        if capture is not None and self._frames is None:
            self._frames = FrameRing.create(
                capture.max_width, capture.max_height, slot_count=capture.slot_count
            )
            logger.info(
                "已创建帧环 %s：最大 %d×%d，%d 个槽",
                self._frames.name,
                capture.max_width,
                capture.max_height,
                capture.slot_count,
            )
        self._instances += 1
        number = self._instances
        loop = asyncio.get_running_loop()
        credentials = contextlib.ExitStack()
        try:
            created = credentials.enter_context(session_credentials(f"core{secrets.token_hex(4)}"))
            ready: asyncio.Future[None] = loop.create_future()
            holder: list[_Instance] = []
            server = await listen(
                created.path,
                created.key,
                on_connection=lambda connection: self._on_connection(holder[0], connection),
                on_rejected=lambda error: logger.warning("拒绝了一个核连接：%s", error),
                on_error=lambda error: logger.error("核连接装配失败：%s", error),
            )
            env = {
                **_base_environment(),
                ENV_CHANNEL_PATH: created.path,
                ENV_CHANNEL_KEY: created.encoded_key,
                ENV_ROLE: CORE_ROLE,
            }
            try:
                process = ManagedProcess.start("realtime-core", self._options.argv(), env)
            except BaseException:
                await server.close()
                raise
        except BaseException:
            credentials.close()
            raise
        instance = _Instance(number, process, server, credentials, ready)
        holder.append(instance)
        self._instance = instance
        self._clean = False
        process.exited.add_done_callback(lambda done: self._handle_exit(instance, done.result()))
        logger.info("实时核 %d 已拉起，进程号 %d", number, process.pid)
        try:
            async with asyncio.timeout(self._options.start_timeout):
                await asyncio.wait({ready, process.exited}, return_when=asyncio.FIRST_COMPLETED)
        except TimeoutError:
            pass
        except BaseException:
            # 启动被取消：不留下子进程，善后由退出回调完成。
            process.kill()
            raise
        if not ready.done():
            if process.exited.done():
                reason = f"实时核 {number} 在就绪前退出，退出码 {process.exited.result()}"
            else:
                reason = f"实时核 {number} 未在 {self._options.start_timeout:g} 秒内就绪"
            await process.stop(timeouts=self._options.stop)
            await self._finish(instance)
            raise CoreStartError(reason)
        if capture is not None and self._frames is not None:
            try:
                await self._attach_capture(instance, capture, self._frames)
            except (ChannelError, RpcError) as error:
                await self._stop_instance(instance)
                raise CoreStartError(f"实时核 {number} 没有接受帧环或采集目标：{error}") from error
        instance.tasks.append(loop.create_task(self._send_heartbeats(instance)))
        instance.tasks.append(loop.create_task(self._watch_health(instance)))

    async def stop(self) -> StopResult:
        """按 R-06 的顺序停止实时核：`core.stop` → 关 stdin → 强制终止；实时核退出后销毁帧环。

        没有在运行时返回上次的退出码。强制终止后仍未确认退出时保留帧环。返回时已退出实例的连接、
        监听与凭据都已关闭。
        """
        instance = self._instance
        if instance is None:
            if self._exited is not None:
                await self._finish(self._exited)
            result = StopResult(StopStage.ALREADY_EXITED, self._last_returncode)
        else:
            result = await self._stop_instance(instance)
        if self._instance is None and self._frames is not None:
            self._frames.close()
            self._frames = None
        return result

    async def _stop_instance(self, instance: _Instance) -> StopResult:
        for task in instance.tasks:
            task.cancel()
        peer = instance.peer

        async def shutdown() -> None:
            if peer is None or not peer.connection.is_open:
                raise ChannelError("closed", "核连接未建立")
            await peer.request(CORE_STOP.name, {}, timeout=self._options.stop.shutdown)

        result = await instance.process.stop(shutdown, self._options.stop)
        if result.returncode is not None:
            await self._finish(instance)
        return result

    async def _attach_capture(
        self, instance: _Instance, capture: CaptureConfig, ring: FrameRing
    ) -> None:
        """挂接帧环，再设定采集目标。实时核从帧环头部的 `write_seq` 续写帧号。"""
        peer = instance.peer
        if peer is None or not peer.connection.is_open:
            raise ChannelError("closed", "核连接未建立")
        timeout = self._options.rpc_timeout
        await peer.request(FRAME_RING_ATTACH.name, _dump(ring.attach_params()), timeout=timeout)
        params = capture.set_target_params()
        await peer.request(CAPTURE_SET_TARGET.name, _dump(params), timeout=timeout)
        logger.info(
            "实时核 %d 已挂接帧环 %s，采集目标：%s",
            instance.number,
            ring.name,
            params.target.root.type,
        )

    # CoreControl

    async def invalidate_generation(self, grant: ControlGrant) -> None:
        """发 `producer.revoke`，返回表示请求已写出；回应由 `wait_released` 等待。"""
        released = self._released_future(grant.generation)
        instance = self._instance
        peer = None if instance is None else instance.peer
        if peer is None or not peer.connection.is_open:
            raise ChannelError("closed", "核连接未建立，无法发出 producer.revoke")
        task = asyncio.get_running_loop().create_task(self._revoke(peer, grant.generation))
        if instance is not None:
            instance.tasks.append(task)
        # 让出一次，请求任务先跑到等回应处：RpcPeer.request 在第一次等待之前就已写出请求。
        # 回应只能在之后的轮次到达，所以此时任务已结束即写出失败（_revoke 已记录）。
        await asyncio.sleep(0)
        if task.done() and not released.done():
            raise ChannelError("closed", f"producer.revoke({grant.generation}) 没有写出")

    async def wait_released(self, grant: ControlGrant) -> None:
        """等到该代次的输入已释放，见模块说明中的三种事件。"""
        released = self._released_future(grant.generation)
        if not released.done() and self._instance is None and self._clean:
            released.set_result(None)
        await asyncio.shield(released)

    def _released_future(self, generation: int) -> asyncio.Future[None]:
        future = self._released.get(generation)
        if future is None:
            future = asyncio.get_running_loop().create_future()
            self._released[generation] = future
        return future

    def _release_through(self, generation: int | None) -> None:
        """完成代次不大于 `generation` 的等待；None 表示全部。完成过的保留，之后再等立即返回。"""
        for number, future in self._released.items():
            if not future.done() and (generation is None or number <= generation):
                future.set_result(None)

    async def _revoke(self, peer: RpcPeer, generation: int) -> None:
        params = ProducerRevokeParams(generation=generation)
        try:
            await peer.request(
                PRODUCER_REVOKE.name, _dump(params), timeout=self._options.rpc_timeout
            )
        except (ChannelError, RpcError) as error:
            # 等待不就此失败：实时核退出或重启时同样完成，见模块说明。
            logger.warning("producer.revoke(%d) 未得到回应：%s", generation, error)
            return
        logger.info("实时核确认代次 %d 及更早代次的输入已释放", generation)
        self._release_through(generation)

    # 连接与消息

    def _on_connection(self, instance: _Instance, connection: Connection) -> None:
        if self._instance is not instance or instance.peer is not None:
            logger.warning("拒绝了实例 %d 的多余连接", instance.number)
            connection.abort()
            return
        if connection.role != CORE_ROLE:
            logger.warning("拒绝了角色为 %s 的核连接", connection.role)
            connection.abort()
            return
        peer = RpcPeer(connection, timeout=self._options.rpc_timeout)
        peer.handle(CORE_HELLO.name, lambda params: self._on_hello(instance, peer, params))
        peer.on_notification(
            INPUT_RELEASED.name, lambda params: self._on_released_message(instance, params)
        )
        peer.on_notification(CORE_HEALTH.name, lambda params: self._on_health_message(params))
        instance.peer = peer
        peer.closed.add_done_callback(
            lambda _: logger.info("实时核 %d 的核连接已断开", instance.number)
        )

    def _on_hello(self, instance: _Instance, peer: RpcPeer, params: dict[str, Any]) -> object:
        if instance.hello_at_ns is not None:
            raise RpcError(RpcErrorCode.INVALID_REQUEST, "重复的 core.hello")
        try:
            hello = CoreHelloParams.model_validate(params)
        except ValidationError as error:
            raise RpcError(
                RpcErrorCode.INVALID_PARAMS, f"core.hello 的参数不合法：{error}"
            ) from None
        if hello.protocol_version != PROTOCOL_VERSION:
            logger.error(
                "实时核的协议版本 %d 与主进程的 %d 不相等，断开",
                hello.protocol_version,
                PROTOCOL_VERSION,
            )
            # 先让错误回应写出，再断开。
            asyncio.get_running_loop().call_soon(_close_later, peer)
            raise RpcError(
                int(ErrorCode.PROTOCOL_VERSION_MISMATCH),
                f"主进程的协议版本是 {PROTOCOL_VERSION}，实时核是 {hello.protocol_version}",
            )
        instance.hello_at_ns = time.perf_counter_ns()
        logger.info("实时核 %d 已握手，程序版本 %s", instance.number, hello.core_version)
        return _dump(CoreHelloResult(protocol_version=PROTOCOL_VERSION))

    def _on_released_message(self, instance: _Instance, params: dict[str, Any]) -> None:
        released = _parse(InputReleasedParams, INPUT_RELEASED.name, params)
        if released is None or instance.hello_at_ns is None:
            return
        producer = released.producer
        logger.info(
            "实时核 %d 释放了全部输入：%s（产出者 %s）",
            instance.number,
            released.reason,
            "无" if producer is None else f"{producer.id}#{producer.generation}",
        )
        if released.reason is ReleaseReason.CORE_START:
            # 新实例启动时已释放全部输入：此前的等待全部完成。
            self._release_through(None)
            if not instance.ready.done():
                instance.ready.set_result(None)
        if self._on_released is not None:
            self._on_released(released)

    def _on_health_message(self, params: dict[str, Any]) -> None:
        health = _parse(CoreHealthParams, CORE_HEALTH.name, params)
        if health is None:
            return
        self._last_health = health
        self._last_health_at_ns = time.perf_counter_ns()
        if self._on_health is not None:
            self._on_health(health)

    async def _send_heartbeats(self, instance: _Instance) -> None:
        """当前提交资格有效时按固定周期发 `producer.heartbeat`；资格收回即停发。"""
        while True:
            grant = None if self._grant_source is None else self._grant_source()
            peer = instance.peer
            if grant is not None and grant.is_open and peer is not None and peer.connection.is_open:
                self._heartbeat_seq += 1
                heartbeat = ProducerHeartbeatParams(
                    producer=Producer(id=grant.producer, generation=grant.generation),
                    seq=self._heartbeat_seq,
                )
                with contextlib.suppress(ChannelError):
                    peer.notify(PRODUCER_HEARTBEAT.name, _dump(heartbeat))
            await asyncio.sleep(self._options.heartbeat_interval)

    async def _watch_health(self, instance: _Instance) -> None:
        """`core.health` 超时与恢复时各记录一次。"""
        stale = False
        while True:
            await asyncio.sleep(self._options.health_interval / 2)
            if self._instance is not instance:
                return
            healthy = self.healthy
            if not healthy and not stale:
                logger.warning(
                    "%g 秒内没有收到实时核 %d 的 core.health",
                    self._options.health_timeout,
                    instance.number,
                )
            elif healthy and stale:
                logger.info("实时核 %d 的 core.health 已恢复", instance.number)
            stale = not healthy

    # 退出

    def _handle_exit(self, instance: _Instance, returncode: int) -> None:
        if instance.closing is not None:
            return
        level = logging.INFO if returncode == 0 else logging.WARNING
        logger.log(level, "实时核 %d 已退出，退出码 %d", instance.number, returncode)
        if returncode == 0:
            # 实时核只在释放全部输入后以 0 退出。
            self._release_through(None)
        if self._instance is instance:
            self._instance = None
            self._exited = instance
            self._clean = returncode == 0
            self._last_returncode = returncode
            self._last_health_at_ns = None
        for task in instance.tasks:
            task.cancel()
        instance.closing = asyncio.get_running_loop().create_task(self._close(instance))

    async def _close(self, instance: _Instance) -> None:
        if instance.peer is not None:
            await instance.peer.close()
        await instance.server.close()
        instance.credentials.close()
        if instance.tasks:
            await asyncio.gather(*instance.tasks, return_exceptions=True)

    async def _finish(self, instance: _Instance) -> None:
        """等实例退出的善后做完：连接、监听与凭据都已关闭。"""
        returncode = await asyncio.shield(instance.process.exited)
        self._handle_exit(instance, returncode)
        if instance.closing is not None:
            await asyncio.shield(instance.closing)


async def shutdown_runtime(kernel: Kernel, link: CoreLink) -> StopResult:
    """主进程的停止顺序：先停插件内核，控制产出者走完 QUIESCING（等确认超时就上报后继续），
    再按 R-06 停实时核；实时核停止时也会释放全部输入，退出后帧环随之销毁。"""
    try:
        await kernel.shutdown()
    finally:
        result = await link.stop()
    return result


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _parse[T: BaseModel](model: type[T], method: str, params: dict[str, Any]) -> T | None:
    try:
        return model.model_validate(params)
    except ValidationError as error:
        logger.warning("忽略了参数不合法的 %s：%s", method, error)
        return None


def _close_later(peer: RpcPeer) -> None:
    task = asyncio.get_running_loop().create_task(peer.close())
    _background.add(task)
    task.add_done_callback(_background.discard)


_background: set[asyncio.Task[None]] = set()
