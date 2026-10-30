"""真实的 `realtime-core`：握手、生命线、控制产出者心跳、时钟、帧环挂接与采集目标。

`FakeMain` 由测试扮演主进程，按脚本收发核协议的消息，用来核对实时核一侧的规则；主进程一侧的实现
（`CoreLink`、插件内核与停止顺序）见 `test_link.py` 与 `test_capture.py`。Windows 上走命名管道，
其它平台走 UDS。
"""

import asyncio
import contextlib
import secrets
import sys
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from core_protocol import models
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
    CaptureState,
    CoreHealthParams,
    CoreHelloParams,
    InputReleasedParams,
    Producer,
    ReleaseReason,
)
from game_runtime.channel import (
    ChannelServer,
    Connection,
    RpcError,
    RpcErrorCode,
    RpcPeer,
    listen,
    session_credentials,
)
from game_runtime.core_link import (
    CORE_ROLE,
    ENV_CHANNEL_KEY,
    ENV_CHANNEL_PATH,
    ENV_ROLE,
    ManagedProcess,
    StopStage,
    StopTimeouts,
    base_environment,
)
from game_runtime.frame_ring import FrameRing
from game_runtime.frame_ring.fake import FakeDisplay

HEARTBEAT_TIMEOUT = 0.3
HEALTH_INTERVAL = 0.05
WAIT = 5.0

type Hello = Callable[["FakeMain", dict[str, Any]], object]
type Script = Callable[["FakeMain"], Awaitable[None]]


class FakeMain:
    """测试扮演的主进程：监听本机通道、拉起真实的实时核，收下它发来的通知。"""

    def __init__(self, exe: Path, log: Path, hello: Hello | None) -> None:
        self.exe = exe
        self.log_path = log
        self._hello = hello
        self.peer: RpcPeer | None = None
        self.process: ManagedProcess | None = None
        self.hello_at_ns = 0
        self.released: asyncio.Queue[tuple[InputReleasedParams, int]] = asyncio.Queue()
        self.health: asyncio.Queue[tuple[CoreHealthParams, int]] = asyncio.Queue()
        self.events: list[str] = []
        self._connected: asyncio.Future[RpcPeer] = asyncio.get_running_loop().create_future()

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncGenerator["FakeMain"]:
        with session_credentials(f"t{secrets.token_hex(4)}") as credentials:
            server: ChannelServer = await listen(
                credentials.path,
                credentials.key,
                on_connection=self._on_connection,
                on_rejected=lambda error: self.events.append(f"rejected {error.code}"),
                on_error=lambda error: self.events.append(f"error {error}"),
            )
            env = {
                **base_environment(),
                ENV_CHANNEL_PATH: credentials.path,
                ENV_CHANNEL_KEY: credentials.encoded_key,
                ENV_ROLE: CORE_ROLE,
            }
            argv = [
                str(self.exe),
                "--heartbeat-timeout-ms",
                str(round(HEARTBEAT_TIMEOUT * 1000)),
                "--health-interval-ms",
                str(round(HEALTH_INTERVAL * 1000)),
            ]
            try:
                with self.log_path.open("wb") as log:
                    self.process = ManagedProcess.start("realtime-core", argv, env, stderr=log)
                yield self
            finally:
                if self.process is not None:
                    self.process.kill()
                    await asyncio.wait_for(asyncio.shield(self.process.exited), 10)
                if self.peer is not None:
                    await self.peer.close()
                await server.close()

    def _on_connection(self, connection: Connection) -> None:
        assert connection.role == CORE_ROLE
        peer = RpcPeer(connection, timeout=WAIT)
        peer.handle(CORE_HELLO.name, self._on_hello)
        peer.on_notification(INPUT_RELEASED.name, self._on_released)
        peer.on_notification(CORE_HEALTH.name, self._on_health)
        self.peer = peer
        self._connected.set_result(peer)

    def _on_hello(self, params: dict[str, Any]) -> object:
        hello = CoreHelloParams.model_validate(params)
        assert hello.protocol_version == PROTOCOL_VERSION
        self.events.append("hello")
        self.hello_at_ns = time.perf_counter_ns()
        if self._hello is not None:
            return self._hello(self, params)
        return {"protocol_version": PROTOCOL_VERSION}

    def _on_released(self, params: dict[str, Any]) -> None:
        released = InputReleasedParams.model_validate(params)
        self.events.append(f"released {released.reason}")
        self.released.put_nowait((released, time.perf_counter_ns()))

    def _on_health(self, params: dict[str, Any]) -> None:
        self.health.put_nowait((CoreHealthParams.model_validate(params), time.perf_counter_ns()))

    async def connected(self) -> RpcPeer:
        return await asyncio.wait_for(asyncio.shield(self._connected), WAIT)

    async def next_released(self) -> InputReleasedParams:
        released, _ = await asyncio.wait_for(self.released.get(), WAIT)
        return released

    async def started(self) -> RpcPeer:
        """等到握手完成与 `core_start`。"""
        peer = await self.connected()
        released = await self.next_released()
        assert (released.reason, released.producer) == (ReleaseReason.CORE_START, None)
        return peer

    async def snapshot(self) -> CoreHealthParams:
        """实时核处理完此前发出的全部消息之后的状态。

        先发一个实时核不认识的请求作屏障：读线程按顺序处理消息，它的回应到达时此前的心跳都已处理。
        再等一条在收到回应之后才读取状态的 `core.health`：它的 `now_ns` 与主进程同一时钟。
        """
        assert self.peer is not None
        with pytest.raises(RpcError) as barrier:
            await self.peer.request("test.barrier", {})
        assert barrier.value.code == RpcErrorCode.METHOD_NOT_FOUND
        settled = time.perf_counter_ns()
        while True:
            health, _ = await asyncio.wait_for(self.health.get(), WAIT)
            if health.now_ns >= settled:
                return health

    async def until_health(self, condition: Callable[[CoreHealthParams], bool]) -> CoreHealthParams:
        """等到一条满足条件的 `core.health`。"""
        deadline = time.monotonic() + WAIT
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0, "没有等到满足条件的 core.health"
            health, _ = await asyncio.wait_for(self.health.get(), remaining)
            if condition(health):
                return health

    async def request(self, method: str, params: dict[str, Any]) -> object:
        assert self.peer is not None
        return await self.peer.request(method, params)

    async def refused(self, method: str, params: dict[str, Any]) -> RpcError:
        """发出请求，它须以错误回应；返回那个错误。"""
        with pytest.raises(RpcError) as refused:
            await self.request(method, params)
        return refused.value

    def heartbeat(self, producer: str, generation: int, seq: int) -> None:
        assert self.peer is not None
        self.peer.notify(
            PRODUCER_HEARTBEAT.name,
            {"producer": {"id": producer, "generation": generation}, "seq": seq},
        )

    async def revoke(self, generation: int) -> None:
        assert self.peer is not None
        assert await self.peer.request(PRODUCER_REVOKE.name, {"generation": generation}) == {}
        self.events.append(f"revoke {generation} answered")

    async def exit_code(self) -> int:
        assert self.process is not None
        return await asyncio.wait_for(asyncio.shield(self.process.exited), WAIT)

    def log(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace")


def run_with_core(exe: Path, tmp_path: Path, script: Script, hello: Hello | None = None) -> None:
    async def main() -> None:
        fake = FakeMain(exe, tmp_path / "core.log", hello)
        async with fake.running():
            await script(fake)

    asyncio.run(main())


def producer(producer_id: str, generation: int) -> Producer:
    return Producer(id=producer_id, generation=generation)


# 握手与启动


def test_core_start_comes_before_any_producer_message(core_exe: Path, tmp_path: Path) -> None:
    def hello(fake: FakeMain, _: dict[str, Any]) -> object:
        # 回应握手后立刻发心跳：实时核须先发 core_start，再处理这些心跳。
        def beat() -> None:
            fake.heartbeat("a", 1, 1)
            fake.heartbeat("b", 2, 1)

        asyncio.get_running_loop().call_soon(beat)
        return {"protocol_version": PROTOCOL_VERSION}

    async def script(fake: FakeMain) -> None:
        await fake.started()
        released = await fake.next_released()
        assert (released.reason, released.producer) == (
            ReleaseReason.PRODUCER_CHANGED,
            producer("a", 1),
        )
        health = await fake.snapshot()
        assert health.producer == producer("b", 2)
        assert health.inputs_held is False
        assert health.capture is CaptureState.IDLE

    run_with_core(core_exe, tmp_path, script, hello)


def test_protocol_version_mismatch_stops_the_core(core_exe: Path, tmp_path: Path) -> None:
    def hello(_: FakeMain, __: dict[str, Any]) -> object:
        raise RpcError(int(ErrorCode.PROTOCOL_VERSION_MISMATCH), "版本不符")

    async def script(fake: FakeMain) -> None:
        assert await fake.exit_code() == 1
        assert "core.hello failed" in fake.log()
        assert fake.released.empty()

    run_with_core(core_exe, tmp_path, script, hello)


# 生命线


def test_stop_releases_answers_and_exits_zero(core_exe: Path, tmp_path: Path) -> None:
    async def script(fake: FakeMain) -> None:
        peer = await fake.started()
        fake.heartbeat("a", 1, 1)
        assert (await fake.snapshot()).producer == producer("a", 1)
        assert await peer.request(CORE_STOP.name, {}) == {}
        fake.events.append("stop answered")
        released = await fake.next_released()
        assert (released.reason, released.producer) == (ReleaseReason.STOP, producer("a", 1))
        # 先释放、发通知，再回应，回应之后才退出。
        assert fake.events[-2:] == ["released stop", "stop answered"]
        assert await fake.exit_code() == 0
        assert "core.stop; released all inputs, exiting" in fake.log()

    run_with_core(core_exe, tmp_path, script)


def test_connection_loss_releases_and_exits(core_exe: Path, tmp_path: Path) -> None:
    async def script(fake: FakeMain) -> None:
        peer = await fake.started()
        fake.heartbeat("a", 1, 1)
        assert (await fake.snapshot()).producer == producer("a", 1)
        dropped = time.monotonic()
        peer.connection.abort()
        assert await fake.exit_code() == 0
        assert time.monotonic() - dropped < 2
        log = fake.log()
        assert "core connection" in log
        assert "released all inputs, exiting" in log

    run_with_core(core_exe, tmp_path, script)


def test_stdin_eof_releases_and_exits(core_exe: Path, tmp_path: Path) -> None:
    async def skip_rpc() -> object:
        raise ConnectionError("跳过第 1 步")

    async def script(fake: FakeMain) -> None:
        await fake.started()
        assert fake.process is not None
        closed = time.monotonic()
        # 不发 core.stop，连接仍在，只关 stdin。
        result = await fake.process.stop(skip_rpc, StopTimeouts(shutdown=0.01, stdin=WAIT))
        assert (result.stage, result.returncode) == (StopStage.STDIN_EOF, 0)
        assert time.monotonic() - closed < 2
        assert "stdin closed; released all inputs, exiting" in fake.log()

    run_with_core(core_exe, tmp_path, script)


# 心跳与代次


def test_heartbeat_timeout_releases(core_exe: Path, tmp_path: Path) -> None:
    async def script(fake: FakeMain) -> None:
        await fake.started()
        last = 0
        # 心跳持续的 0.6 秒（两倍超时）内不超时。
        for seq in range(1, 13):
            fake.heartbeat("a", 1, seq)
            last = time.perf_counter_ns()
            await asyncio.sleep(0.05)
        assert fake.released.empty()
        released, at = await asyncio.wait_for(fake.released.get(), WAIT)
        assert (released.reason, released.producer) == (
            ReleaseReason.HEARTBEAT_TIMEOUT,
            producer("a", 1),
        )
        # 停发后在超时加余量内释放。
        waited = (at - last) / 1e9
        assert HEARTBEAT_TIMEOUT - 0.05 <= waited < HEARTBEAT_TIMEOUT + 0.5, waited
        # 待命：产出者仍是当前的，不重复释放。
        assert (await fake.snapshot()).producer == producer("a", 1)
        assert fake.released.empty()

    run_with_core(core_exe, tmp_path, script)


def test_non_increasing_seq_does_not_count(core_exe: Path, tmp_path: Path) -> None:
    async def script(fake: FakeMain) -> None:
        await fake.started()
        fake.heartbeat("a", 1, 5)
        counted = time.perf_counter_ns()
        timed_out = asyncio.ensure_future(fake.released.get())
        # 继续发 seq 不递增的心跳：它们不刷新截止时刻。
        stale = [5, 4]
        while not timed_out.done():
            fake.heartbeat("a", 1, stale[0])
            stale.reverse()
            await asyncio.sleep(0.05)
            assert time.perf_counter_ns() - counted < WAIT * 1e9
        released, at = timed_out.result()
        assert released.reason is ReleaseReason.HEARTBEAT_TIMEOUT
        assert (at - counted) / 1e9 < HEARTBEAT_TIMEOUT + 0.5
        assert "StaleSeq" in fake.log()

    run_with_core(core_exe, tmp_path, script)


def test_stale_and_revoked_generations_are_rejected(core_exe: Path, tmp_path: Path) -> None:
    async def script(fake: FakeMain) -> None:
        await fake.started()
        fake.heartbeat("a", 2, 1)
        assert (await fake.snapshot()).producer == producer("a", 2)
        # 旧代次与同代次的其它身份：不算数，当前产出者不变。
        fake.heartbeat("a", 1, 100)
        fake.heartbeat("b", 2, 100)
        assert (await fake.snapshot()).producer == producer("a", 2)

        await fake.revoke(2)
        released = await fake.next_released()
        assert (released.reason, released.producer) == (
            ReleaseReason.GENERATION_REVOKED,
            producer("a", 2),
        )
        # 已作废的代次与更早的代次都被拒绝。
        fake.heartbeat("a", 2, 101)
        fake.heartbeat("c", 1, 1)
        assert (await fake.snapshot()).producer is None
        # 更新的代次照常接受；没有当前产出者时不需要释放。
        fake.heartbeat("c", 3, 1)
        assert (await fake.snapshot()).producer == producer("c", 3)
        assert fake.released.empty()
        log = fake.log()
        for rejection in ("StaleGeneration", "ProducerMismatch", "Revoked"):
            assert rejection in log

    run_with_core(core_exe, tmp_path, script)


def test_higher_generation_releases_before_switching(core_exe: Path, tmp_path: Path) -> None:
    async def script(fake: FakeMain) -> None:
        await fake.started()
        fake.heartbeat("step", 1, 1)
        fake.heartbeat("vla", 2, 1)
        released = await fake.next_released()
        assert (released.reason, released.producer) == (
            ReleaseReason.PRODUCER_CHANGED,
            producer("step", 1),
        )
        assert (await fake.snapshot()).producer == producer("vla", 2)

    run_with_core(core_exe, tmp_path, script)


def test_revoke_answers_after_release(core_exe: Path, tmp_path: Path) -> None:
    async def script(fake: FakeMain) -> None:
        await fake.started()
        fake.heartbeat("a", 7, 1)
        assert (await fake.snapshot()).producer == producer("a", 7)
        await fake.revoke(7)
        assert fake.events[-2:] == ["released generation_revoked", "revoke 7 answered"]
        # 作废不是当前的代次：没有可释放的，直接回应。
        await fake.revoke(3)
        assert fake.events[-1] == "revoke 3 answered"
        assert fake.released.qsize() == 1

    run_with_core(core_exe, tmp_path, script)


# 时钟


def test_core_reads_the_same_clock_as_perf_counter_ns(core_exe: Path, tmp_path: Path) -> None:
    async def script(fake: FakeMain) -> None:
        await fake.started()
        # 实时核收到握手回应之后才发第一条 core.health：读数落在主进程回应之前与收到之间。
        health, received = await asyncio.wait_for(fake.health.get(), WAIT)
        assert fake.hello_at_ns <= health.now_ns <= received
        # 请求前后各读一次：作废代次的释放时刻落在两次读数之间。
        fake.heartbeat("a", 1, 1)
        assert (await fake.snapshot()).producer == producer("a", 1)
        before = time.perf_counter_ns()
        await fake.revoke(1)
        after = time.perf_counter_ns()
        released = await fake.next_released()
        assert before <= released.released_at_ns <= after
        # 之后的每条 core.health 都不早于上一条，也不晚于收到它的时刻。
        last = health.now_ns
        for _ in range(5):
            health, received = await asyncio.wait_for(fake.health.get(), WAIT)
            assert last <= health.now_ns <= received
            last = health.now_ns

    run_with_core(core_exe, tmp_path, script)


# 帧环与采集


def attach_params(ring: FrameRing) -> dict[str, Any]:
    return ring.attach_params().model_dump(mode="json")


def target_params(target: models.WindowTarget | models.FakeTarget) -> dict[str, Any]:
    params = models.CaptureSetTargetParams(target=models.CaptureTarget(root=target))
    return params.model_dump(mode="json")


def solid(width: int, height: int, value: int) -> bytes:
    """每个字节都是 `value` 的 BGRA8 画面。"""
    return bytes([value]) * (width * height * 4)


def test_capture_goes_from_idle_to_capturing_and_fills_the_ring(
    core_exe: Path, tmp_path: Path
) -> None:
    async def script(fake: FakeMain) -> None:
        await fake.started()
        with FrameRing.create(32, 24) as ring, FakeDisplay.create(32, 24) as display:
            assert (await fake.snapshot()).capture is CaptureState.IDLE
            # 只挂接了帧环、还没有采集目标：仍是 idle，没有写过帧。
            assert await fake.request(FRAME_RING_ATTACH.name, attach_params(ring)) == {}
            health = await fake.snapshot()
            assert (health.capture, health.latest_frame) == (CaptureState.IDLE, None)

            assert await fake.request(CAPTURE_SET_TARGET.name, target_params(display.target)) == {}
            await fake.until_health(lambda h: h.capture is CaptureState.CAPTURING)
            pixels = solid(32, 24, 0x5A)
            display.present(pixels, 32, 24, capture_ns=12_345, transform_version=3)
            health = await fake.until_health(lambda h: h.latest_frame is not None)
            frame = ring.read_latest()
            assert frame is not None
            assert health.latest_frame == models.LatestFrame(frame_number=1, capture_ns=12_345)
            assert (frame.frame_number, frame.capture_ns, frame.transform_version) == (1, 12_345, 3)
            assert frame.pixels == pixels

    run_with_core(core_exe, tmp_path, script)


def test_a_new_target_replaces_the_old_source(core_exe: Path, tmp_path: Path) -> None:
    async def script(fake: FakeMain) -> None:
        await fake.started()
        with (
            FrameRing.create(16, 12) as ring,
            FakeDisplay.create(16, 12) as first,
            FakeDisplay.create(16, 12) as second,
        ):
            await fake.request(FRAME_RING_ATTACH.name, attach_params(ring))
            await fake.request(CAPTURE_SET_TARGET.name, target_params(first.target))
            first.present(solid(16, 12, 1), 16, 12)
            await fake.until_health(lambda h: h.latest_frame is not None)

            await fake.request(CAPTURE_SET_TARGET.name, target_params(second.target))
            second.present(solid(16, 12, 2), 16, 12)
            health = await fake.until_health(
                lambda h: h.latest_frame is not None and h.latest_frame.frame_number > 1
            )
            frame = ring.read_latest()
            assert frame is not None
            assert frame.pixels == solid(16, 12, 2)
            # 旧的采集源已停掉：旧显示面上的新帧不再进入帧环。
            first.present(solid(16, 12, 3), 16, 12)
            await asyncio.sleep(0.3)
            assert (await fake.snapshot()).latest_frame == health.latest_frame
            assert ring.latest_frame_number() == frame.frame_number

    run_with_core(core_exe, tmp_path, script)


def test_mismatched_rings_and_bad_targets_are_refused(core_exe: Path, tmp_path: Path) -> None:
    async def script(fake: FakeMain) -> None:
        await fake.started()
        with FrameRing.create(16, 12) as ring, FakeDisplay.create(16, 12) as display:
            params = attach_params(ring)
            for bad in (
                {**params, "max_width": 15},
                {**params, "name": params["name"] + "x"},
                {**params, "layout_version": 2},
            ):
                error = await fake.refused(FRAME_RING_ATTACH.name, bad)
                assert error.code == ErrorCode.FRAME_RING_MISMATCH, bad
            # 假显示面按同样的规则核对。
            surface = {**display.target.surface.model_dump(mode="json"), "slot_count": 2}
            error = await fake.refused(
                CAPTURE_SET_TARGET.name, {"target": {"type": "fake", "surface": surface}}
            )
            assert error.code == ErrorCode.FRAME_RING_MISMATCH
            # 窗口识别规则须给出窗口类或标题。
            vague = models.WindowTarget(process_name="game.exe", window_class=None, title=None)
            error = await fake.refused(CAPTURE_SET_TARGET.name, target_params(vague))
            assert error.code == RpcErrorCode.INVALID_PARAMS
            # 被拒绝的请求不改变什么。
            assert (await fake.snapshot()).capture is CaptureState.IDLE
            assert ring.latest_frame_number() == 0
        log = fake.log()
        assert "frame_ring.attach" in log
        assert "header max_width is 16" in log

    run_with_core(core_exe, tmp_path, script)


def test_window_targets_search_on_windows_and_are_unsupported_elsewhere(
    core_exe: Path, tmp_path: Path
) -> None:
    window = models.WindowTarget(
        process_name="no-such-process-7f3a9c.exe", window_class=None, title="Bellis Test"
    )

    async def script(fake: FakeMain) -> None:
        await fake.started()
        with FrameRing.create(16, 12) as ring:
            await fake.request(FRAME_RING_ATTACH.name, attach_params(ring))
            if sys.platform == "win32":
                assert await fake.request(CAPTURE_SET_TARGET.name, target_params(window)) == {}
                health = await fake.until_health(lambda h: h.capture is not CaptureState.IDLE)
                assert (health.capture, health.latest_frame) == (CaptureState.SEARCHING, None)
                assert "no window matches" in fake.log()
            else:
                error = await fake.refused(CAPTURE_SET_TARGET.name, target_params(window))
                assert error.code == ErrorCode.CAPTURE_UNSUPPORTED
                assert "only available on Windows" in error.message
                assert (await fake.snapshot()).capture is CaptureState.IDLE

    run_with_core(core_exe, tmp_path, script)
