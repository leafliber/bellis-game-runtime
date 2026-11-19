"""主进程一侧的核连接：拉起实时核、握手、控制产出者心跳、`CoreControl` 与停止。

设计见 `docs/modules/realtime-core.md` 的“启动与生命线”。每次启动都是一个新的实时核实例：
新的会话地址与密钥、新的监听与子进程。等输入已释放的确认跨实例保留，按以下任一事件完成：

- 真实输入等待同实例内 `producer.revoke` 对应的 `verified`，或该实例核验后以 0 退出。
- 新实例的启动核验成功后，完成旧实例仍未收到确认的等待。
- 假后端以请求回应、启动释放回执或正常退出作为假输入释放依据。

已知抬起或核验失败时，主进程停止动作与心跳，保留等待并停止自动重启。

实例被强制终止或崩溃（退出码非 0）时没有进程发出按键抬起，等待保留到下一个实例的 `core_start`。

配置了采集（`CoreLinkOptions.capture`）时，第一次 `start` 创建帧环；每个实例就绪后先挂接帧环，
再设定采集目标。配置了音频（`CoreLinkOptions.audio`）时同样创建音频环，接着挂接音频环、
设定音频目标。之后才开始发心跳。两个环都跨实例保留，`stop` 在实时核退出之后销毁它们。
同一时刻只有一个实例：上一个实例退出之后才能再 `start`，所以环只挂接给一个实时核，
新实例从头部的 `write_seq` 续写帧号与块号。
插件经 `audio_reader`（交给插件内核，即 `ctx.audio`）读音频环。

`submit` 是 `CoreControl` 的第三项：控制产出者经提交资格（`ControlGrant.submit`）提交动作块，见
`docs/modules/plugin-kernel.md` 的“提交动作块”。
"""

import asyncio
import contextlib
import logging
import math
import re
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from core_protocol import models
from core_protocol.methods import (
    ACTION_RECEIPT,
    ACTION_SUBMIT,
    AUDIO_RING_ATTACH,
    AUDIO_SET_TARGET,
    CAPTURE_SET_TARGET,
    CORE_HEALTH,
    CORE_HELLO,
    CORE_STOP,
    FRAME_RING_ATTACH,
    INPUT_CONFIGURE,
    INPUT_RELEASED,
    INPUT_RESIDUAL,
    INPUT_VERIFICATION,
    PRODUCER_HEARTBEAT,
    PRODUCER_REVOKE,
    PROTOCOL_VERSION,
    ErrorCode,
)
from core_protocol.models import (
    CoreHealthParams,
    CoreHelloParams,
    CoreHelloResult,
    InputPolicy,
    InputReleasedParams,
    InputResidualParams,
    InputVerificationParams,
    Producer,
    ProducerHeartbeatParams,
    ProducerRevokeParams,
    ProducerRevokeResult,
    ReleaseReason,
    ReleaseVerification,
    VerificationResult,
)
from game_runtime.audio_ring import AudioRead, AudioReader, AudioRing
from game_runtime.channel import (
    ChannelCredentials,
    ChannelError,
    ChannelServer,
    Connection,
    RpcError,
    RpcErrorCode,
    RpcPeer,
    listen,
    session_credentials,
)
from game_runtime.core_link.audio import AudioConfig
from game_runtime.core_link.capture import CaptureConfig
from game_runtime.core_link.process import ManagedProcess, StopResult, StopStage, StopTimeouts
from game_runtime.core_link.process import base_environment as _base_environment
from game_runtime.frame_ring import FrameRing
from game_runtime.kernel import ControlGrant, Kernel, Submission

logger = logging.getLogger(__name__)

CORE_ROLE = "game-runtime.core"
"""实时核连接主进程时使用的角色。"""
ENV_CHANNEL_PATH = "BELLIS_CHANNEL_PATH"
ENV_CHANNEL_KEY = "BELLIS_CHANNEL_KEY"
ENV_ROLE = "BELLIS_ROLE"
"""凭据经这三个环境变量交给子进程，与 Bellis 的 `ChildEnv` 相同。"""
ENV_TEST_DEVICE_PATH = "GAME_RUNTIME_TEST_DEVICE_PATH"
ENV_TEST_DEVICE_KEY = "GAME_RUNTIME_TEST_DEVICE_KEY"
"""只用于测试：测试设备连接的地址与密钥经这两个环境变量交给以假输入后端启动的实时核。"""
ENV_OPERATOR_PATH = "GAME_RUNTIME_OPERATOR_PATH"
ENV_OPERATOR_KEY = "GAME_RUNTIME_OPERATOR_KEY"
"""操作员许可连接：操作员控制台的地址与密钥经这两个环境变量交给实时核。"""


class CoreStartError(RuntimeError):
    """实时核没能就绪：启动失败、握手失败、就绪前退出或超时。"""


class ActionDeadlineError(RuntimeError):
    """动作的绝对截止时刻已到，动作块未发送。"""


DEFAULT_RESTART_BACKOFF: tuple[float, ...] = (1.0, 2.0, 4.0)
"""GR-35 自动重启的退避（秒），至多这么多次。"""
DEFAULT_RESTART_RESET: float = 60.0
"""就绪后连续运行满这么久，重启次数清零（秒）。"""


@dataclass(frozen=True, slots=True)
class RestartPolicy:
    """GR-35 崩溃后的自动重启。非 0 退出（panic、被强杀、运行中失败）后按 `backoff` 退避重启，至多
    `len(backoff)` 次；就绪后连续运行满 `reset_after` 秒，次数清零。主进程要求的停止、0 退出与
    参数不合法（2）不重启。"""

    backoff: tuple[float, ...] = DEFAULT_RESTART_BACKOFF
    reset_after: float = DEFAULT_RESTART_RESET

    def __post_init__(self) -> None:
        if not self.backoff or any(delay < 0 for delay in self.backoff):
            raise ValueError("退避须非空且非负")
        if self.reset_after <= 0:
            raise ValueError("清零时长须为正")


@dataclass(frozen=True, slots=True)
class BellisCoreOptions:
    """只交给实时核运行身份。实时核在自己的进程里读取 Bellis 会话。"""

    session_id: str
    runtime_id: str
    test_session_file: Path | None = None

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z0-9]{1,16}", self.session_id) is None:
            raise ValueError("Bellis 会话号无效")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", self.runtime_id) is None:
            raise ValueError("Runtime 运行编号无效")
        if self.test_session_file is not None and not self.test_session_file.is_absolute():
            raise ValueError("Bellis 测试会话文件须为绝对路径")


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
    """发给实时核的请求（`producer.revoke`、`action.submit`、两个环的挂接与两个目标的设定）的截止时间。"""
    max_frame_age: float = 0.5
    """动作允许的帧龄，经命令行交给实时核：最新写入帧的帧龄超过它，实时核即不发输入并释放。"""
    stop: StopTimeouts = field(default_factory=StopTimeouts)
    """停止各步的时限。"""
    capture: CaptureConfig | None = None
    """帧环与采集目标；`None` 时不建帧环，也不设定采集目标。"""
    audio: AudioConfig | None = None
    """音频环与音频目标；`None` 时不建音频环，也不设定音频目标。"""
    test_device: ChannelCredentials | None = None
    """只用于测试：测试设备（`game_runtime.core_link.fake_device.FakeDevice`）的地址与密钥。给出时以
    假输入后端（`--input-backend fake`）拉起实时核；没有 `operator` 时许可来自测试设备。生产中为
    None。"""
    operator: ChannelCredentials | None = None
    """操作员控制台本次会话的地址与密钥（`game_runtime.operator.OperatorClient.credentials`）。给出
    时实时核连到控制台，许可只来自它。实时核须有输入后端，否则以参数不合法退出。"""
    real_input: bool = False
    """真实输入后端（SendInput）。为真时另加 `--input-backend sendinput` 与 `--emergency-hotkey`，
    须同时给出操作员或 Bellis 许可源，且不给 `test_device`。只在 Windows 上。"""
    emergency_hotkey: str | None = None
    """急停组合键（如 `ctrl+shift+f12`、`ctrl+alt+end`）；只在 `real_input` 为真时交给实时核。
    缺省由实时核取 Ctrl+Shift+F12。"""
    input_policy: InputPolicy | None = None
    """输入策略（动作白名单与静态禁区），在设定采集目标之后、第一次心跳之前以 `input.configure`
    交出，重启的实例也再交一次。`None` 时不交（假后端的测试照旧不限制）。"""
    restart: RestartPolicy | None = field(default_factory=RestartPolicy)
    """GR-35 自动重启，缺省退避 1、2、4 秒；`None` 时不自动重启（宿主自己 `start`，只用于自己驱动
    重启的测试）。"""
    bellis: BellisCoreOptions | None = None

    def __post_init__(self) -> None:
        timeouts = (
            self.heartbeat_interval,
            self.heartbeat_timeout,
            self.health_interval,
            self.health_timeout,
            self.start_timeout,
            self.rpc_timeout,
            self.max_frame_age,
        )
        if min(timeouts) <= 0:
            raise ValueError("时限与周期必须为正")
        if self.heartbeat_timeout <= self.heartbeat_interval:
            raise ValueError("心跳超时须大于心跳周期")
        if self.health_timeout <= self.health_interval:
            raise ValueError("健康超时须大于健康周期")
        if self.bellis is not None:
            if self.operator is not None:
                raise ValueError("操作员许可与 Bellis 许可只能选一个")
            if not self.real_input and self.test_device is None:
                raise ValueError("Bellis 控制连接需要明确的输入后端")
            if self.bellis.test_session_file is not None and self.test_device is None:
                raise ValueError("Bellis 测试会话文件只与假输入设备一起使用")
        if self.real_input:
            if self.operator is None and self.bellis is None:
                raise ValueError("真实输入后端须与操作员许可连接或 Bellis 控制连接一起给出")
            if self.test_device is not None:
                raise ValueError("真实输入后端不能同时给测试设备的凭据")
        elif self.emergency_hotkey is not None:
            raise ValueError("急停组合键只与真实输入后端一起给出")

    def argv(self) -> list[str]:
        """拉起实时核的参数列表。"""
        argv = [
            str(self.executable),
            "--heartbeat-timeout-ms",
            str(_millis(self.heartbeat_timeout)),
            "--health-interval-ms",
            str(_millis(self.health_interval)),
            "--max-frame-age-ms",
            str(_millis(self.max_frame_age)),
        ]
        if self.real_input:
            argv += ["--input-backend", "sendinput"]
            if self.emergency_hotkey is not None:
                argv += ["--emergency-hotkey", self.emergency_hotkey]
        elif self.test_device is not None:
            argv += ["--input-backend", "fake"]
        if self.bellis is not None:
            argv += ["--bellis-control", self.bellis.runtime_id]
        return argv

    def child_env(self) -> dict[str, str]:
        """除系统必需的变量与核连接的凭据外，交给实时核的环境变量：测试设备与操作员控制台的地址与
        密钥。"""
        env: dict[str, str] = {}
        if self.test_device is not None:
            env[ENV_TEST_DEVICE_PATH] = self.test_device.path
            env[ENV_TEST_DEVICE_KEY] = self.test_device.encoded_key
        if self.operator is not None:
            env[ENV_OPERATOR_PATH] = self.operator.path
            env[ENV_OPERATOR_KEY] = self.operator.encoded_key
        if self.bellis is not None:
            env["BELLIS_SESSION_ID"] = self.bellis.session_id
            if self.bellis.test_session_file is not None:
                env["GAME_RUNTIME_TEST_BELLIS_SESSION_FILE"] = str(self.bellis.test_session_file)
        return env


def _millis(seconds: float) -> int:
    return max(1, math.ceil(seconds * 1000))


def _nanos(seconds: float) -> int:
    return round(seconds * 1e9)


class _Submission:
    """`CoreLink.submit` 交出的一块动作：实时核的回应与回执里各一次性动作的结果。"""

    def __init__(self, block: models.ActionBlock, done: asyncio.Future[None]) -> None:
        self.block = block
        self._expected: set[int] = {
            action.root.id for step in block.steps for action in step.actions
        }
        self._outcomes: dict[int, models.Executed | models.Dropped | models.Uncertain] = {}
        self._result: models.Accepted | models.Rejected | None = None
        self._done = done

    @property
    def result(self) -> models.Accepted | models.Rejected:
        if self._result is None:
            raise RuntimeError("还没有收到 action.submit 的回应")
        return self._result

    async def outcomes(self) -> Mapping[int, models.Executed | models.Dropped | models.Uncertain]:
        await asyncio.shield(self._done)
        return dict(self._outcomes)

    def resolve(self, result: models.Accepted | models.Rejected) -> None:
        self._result = result
        if isinstance(result, models.Rejected):
            # 被拒绝的块不执行任何动作，也没有回执。
            self._expected = set()
        self._settle()

    def record(self, outcomes: Sequence[models.ActionOutcome]) -> None:
        for outcome in outcomes:
            self._outcomes.setdefault(outcome.root.id, outcome.root)
        self._settle()

    def fail(self, error: BaseException) -> None:
        if self._done.done():
            return
        if isinstance(error, asyncio.CancelledError):
            self._done.cancel()
            return
        self._done.set_exception(error)
        # 没人等的时候不报“未取回的异常”。
        self._done.exception()

    @property
    def settled(self) -> bool:
        return self._done.done()

    def _settle(self) -> None:
        settled = self._result is not None and self._expected <= self._outcomes.keys()
        if settled and not self._done.done():
            self._done.set_result(None)


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
    ready_at_ns: int | None = None
    """收到 `input.released(core_start)`、实例就绪的 `time.perf_counter_ns()`；用于自动重启清零。"""
    tasks: list[asyncio.Task[None]] = field(default_factory=list[asyncio.Task[None]])
    closing: asyncio.Task[None] | None = None
    release_failure: str | None = None
    startup_release_id: int | None = None
    latest_verification: InputVerificationParams | None = None
    verification_changed: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True)
class _ReleaseWait:
    instance_number: int
    done: asyncio.Future[None]


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
        on_residual: Callable[[InputResidualParams], None] | None = None,
        on_restart: Callable[[int], None] | None = None,
        on_verification: Callable[[InputVerificationParams], None] | None = None,
        action_deadline: Callable[[], int | None] | None = None,
    ) -> None:
        self._options = options
        self._on_released = on_released
        self._on_health = on_health
        self._on_residual = on_residual
        self._on_restart = on_restart
        self._on_verification = on_verification
        self._action_deadline = action_deadline
        self._grant_source: Callable[[], ControlGrant | None] | None = None
        self._instance: _Instance | None = None
        self._instances = 0
        self._clean = True
        self._stopping = False
        """已请求停止或已知输入释放失败，此后不自动重启。"""
        self._restart_attempt = 0
        """连续重启的次数，就绪满 `reset_after` 秒后清零。"""
        self._restart_task: asyncio.Task[None] | None = None
        self._released: dict[int, _ReleaseWait] = {}
        self._heartbeat_seq = 0
        self._heartbeat_generation: int | None = None
        """运行中的实例最近一次心跳的代次。"""
        self._block_seq = 0
        """动作块序号，在主进程运行期间全局递增，因而在每个代次内严格递增。"""
        self._submissions: dict[tuple[int, int], _Submission] = {}
        """还在等回应或回执的动作块，按（代次，序号）索引。"""
        self._last_health: CoreHealthParams | None = None
        self._last_health_at_ns: int | None = None
        self._last_returncode: int | None = None
        self._exited: _Instance | None = None
        """最近一个已退出的实例；`stop` 等它的善后做完。"""
        self._frames: FrameRing | None = None
        self._audio: AudioRing | None = None
        self._audio_reader = _LinkAudio(self)

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
    def audio(self) -> AudioRing | None:
        """音频环，供宿主代码读声音（`read_recent`）。

        配置了音频时由第一次 `start` 创建，跨实时核重启保留，`stop` 在实时核退出后销毁；其余时候
        为 None。音频环归 `CoreLink` 所有，宿主只读，不关闭它。
        """
        return self._audio

    @property
    def audio_reader(self) -> AudioReader:
        """交给插件内核的读音频接口（`Kernel(audio=...)`，即插件的 `ctx.audio`）。它总是读当前的
        音频环，没有音频环时返回 None。"""
        return self._audio_reader

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
        # 新的一次启动：再次打开自动重启（上次 stop 关掉了它）。
        self._stopping = False
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
        audio = self._options.audio
        if audio is not None and self._audio is None:
            self._audio = AudioRing.create(
                audio.sample_rate,
                audio.channels,
                block_capacity=audio.block_capacity,
                block_count=audio.block_count,
            )
            logger.info(
                "已创建音频环 %s：%d Hz、%d 声道，%d 个块槽，每块至多 %d 个采样点",
                self._audio.name,
                audio.sample_rate,
                audio.channels,
                audio.block_count,
                audio.block_capacity,
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
                **self._options.child_env(),
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
        if not ready.done() or instance.release_failure is not None:
            if instance.release_failure is not None:
                reason = instance.release_failure
            elif process.exited.done():
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
        if audio is not None and self._audio is not None:
            try:
                await self._attach_audio(instance, audio, self._audio)
            except (ChannelError, RpcError) as error:
                await self._stop_instance(instance)
                raise CoreStartError(
                    f"实时核 {number} 没有接受音频环或音频目标：{error}"
                ) from error
        if self._options.input_policy is not None:
            try:
                await self._configure_input(instance, self._options.input_policy)
            except (ChannelError, RpcError) as error:
                await self._stop_instance(instance)
                raise CoreStartError(f"实时核 {number} 没有接受输入策略：{error}") from error
        if instance.release_failure is not None:
            await self._stop_instance(instance)
            raise CoreStartError(instance.release_failure)
        instance.tasks.append(loop.create_task(self._send_heartbeats(instance)))
        instance.tasks.append(loop.create_task(self._watch_health(instance)))

    async def _configure_input(self, instance: _Instance, policy: InputPolicy) -> None:
        """在设定采集目标之后、第一次心跳之前交出输入策略（`input.configure`）。"""
        peer = instance.peer
        if peer is None or not peer.connection.is_open:
            raise ChannelError("closed", "核连接未建立")
        await peer.request(INPUT_CONFIGURE.name, _dump(policy), timeout=self._options.rpc_timeout)
        logger.info("实时核 %d 已接受输入策略", instance.number)

    async def stop(self) -> StopResult:
        """按 R-06 的顺序停止实时核：`core.stop` → 关 stdin → 强制终止；
        实时核退出后销毁帧环与音频环。

        没有在运行时返回上次的退出码。强制终止后仍未确认退出时保留两个环。返回时已退出实例的连接、
        监听与凭据都已关闭。此后不再自动重启。
        """
        self._stopping = True
        if self._restart_task is not None:
            self._restart_task.cancel()
            self._restart_task = None
        instance = self._instance
        if instance is None:
            if self._exited is not None:
                await self._finish(self._exited)
            result = StopResult(StopStage.ALREADY_EXITED, self._last_returncode)
        else:
            result = await self._stop_instance(instance)
        if self._instance is None:
            if self._frames is not None:
                self._frames.close()
                self._frames = None
            if self._audio is not None:
                self._audio.close()
                self._audio = None
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

    async def _attach_audio(self, instance: _Instance, audio: AudioConfig, ring: AudioRing) -> None:
        """挂接音频环，再设定音频目标。实时核从音频环头部的 `write_seq` 续写块号。"""
        peer = instance.peer
        if peer is None or not peer.connection.is_open:
            raise ChannelError("closed", "核连接未建立")
        timeout = self._options.rpc_timeout
        await peer.request(AUDIO_RING_ATTACH.name, _dump(ring.attach_params()), timeout=timeout)
        params = audio.set_target_params()
        await peer.request(AUDIO_SET_TARGET.name, _dump(params), timeout=timeout)
        logger.info(
            "实时核 %d 已挂接音频环 %s，音频目标：%s",
            instance.number,
            ring.name,
            params.target.root.type,
        )

    # CoreControl

    async def invalidate_generation(self, grant: ControlGrant) -> None:
        """发 `producer.revoke`，返回表示请求已写出；回应由 `wait_released` 等待。"""
        released = self._released_future(grant.generation)
        instance = self._instance
        if instance is None or instance.peer is None or not instance.peer.connection.is_open:
            raise ChannelError("closed", "核连接未建立，无法发出 producer.revoke")
        task = asyncio.get_running_loop().create_task(
            self._revoke(instance, instance.peer, grant.generation)
        )
        instance.tasks.append(task)
        # 让出一次，请求任务先跑到等回应处：RpcPeer.request 在第一次等待之前就已写出请求。
        # 回应只能在之后的轮次到达，所以此时任务已结束即写出失败（_revoke 已记录）。
        await asyncio.sleep(0)
        if task.done() and not released.done():
            raise ChannelError("closed", f"producer.revoke({grant.generation}) 没有写出")

    async def wait_released(self, grant: ControlGrant) -> None:
        """等到该代次的输入释放确认，见模块说明。"""
        released = self._released_future(grant.generation)
        if not released.done() and self._instance is None and self._clean:
            released.set_result(None)
        await asyncio.shield(released)

    def _released_future(self, generation: int) -> asyncio.Future[None]:
        wait = self._released.get(generation)
        if wait is None:
            wait = _ReleaseWait(self._instances, asyncio.get_running_loop().create_future())
            self._released[generation] = wait
        return wait.done

    async def submit(
        self,
        grant: ControlGrant,
        steps: Sequence[models.ControlStep],
        *,
        base_frame: int,
        valid_for: float,
        start_delay: float = 0.0,
    ) -> Submission:
        """以 `grant` 的身份与代次提交一块动作（`action.submit`），返回时已收到回应。

        起始时刻与有效截止时刻以 `time.perf_counter_ns()` 生成，与实时核同一时钟；序号由本对象管理；
        `base_frame` 是产出者做决定时看的那一帧的帧号（读帧环得到）。这一代次还没发过心跳时
        先发一次，让实时核在执行第一步之前就知道产出者在。发送前调用 `grant.check()`。
        """
        if not math.isfinite(valid_for) or valid_for <= 0:
            raise ValueError(f"valid_for 须为正数：{valid_for}")
        if not math.isfinite(start_delay) or start_delay < 0:
            raise ValueError(f"start_delay 不能为负：{start_delay}")
        grant.check()
        instance = self._instance
        if instance is not None and instance.release_failure is not None:
            raise ChannelError("closed", instance.release_failure)
        peer = None if instance is None else instance.peer
        if peer is None or not peer.connection.is_open:
            raise ChannelError("closed", "核连接未建立，无法提交动作块")
        if self._heartbeat_generation != grant.generation:
            self._send_heartbeat(peer, grant)
        self._block_seq += 1
        start_ns = time.perf_counter_ns() + _nanos(start_delay)
        deadline_ns = start_ns + _nanos(valid_for)
        if self._action_deadline is not None:
            inclusive_bound = self._action_deadline()
            if inclusive_bound is None or inclusive_bound <= start_ns:
                raise ActionDeadlineError("动作的绝对截止时刻已到")
            deadline_ns = min(deadline_ns, inclusive_bound)
        block = models.ActionBlock(
            producer=Producer(id=grant.producer, generation=grant.generation),
            seq=self._block_seq,
            base_frame=base_frame,
            start_ns=start_ns,
            deadline_ns=deadline_ns,
            steps=list(steps),
        )
        submission = _Submission(block, asyncio.get_running_loop().create_future())
        key = (grant.generation, block.seq)
        # 回执可能先于回应到达：先登记再发。
        self._submissions[key] = submission
        try:
            grant.check()
            reply = await peer.request(
                ACTION_SUBMIT.name, _dump(block), timeout=self._options.rpc_timeout
            )
            result = models.ActionSubmitResult.model_validate(reply).root
        except BaseException as error:
            self._submissions.pop(key, None)
            submission.fail(error)
            raise
        submission.resolve(result)
        if isinstance(result, models.Rejected):
            logger.warning(
                "实时核拒绝了动作块 %s#%d seq %d：%s（%s）",
                grant.producer,
                grant.generation,
                block.seq,
                result.reason,
                result.message,
            )
        if submission.settled:
            self._submissions.pop(key, None)
        return submission

    def _on_receipt_message(self, params: dict[str, Any]) -> None:
        receipt = _parse(models.ActionReceiptParams, ACTION_RECEIPT.name, params)
        if receipt is None:
            return
        key = (receipt.producer.generation, receipt.block_seq)
        submission = self._submissions.get(key)
        if submission is None:
            logger.debug("收到不认识的动作块的回执：%s", key)
            return
        submission.record(receipt.outcomes)
        if submission.settled:
            self._submissions.pop(key, None)

    def _fail_submissions(self, error: BaseException) -> None:
        """实例退出：还没等到的回执不会再来。"""
        for submission in self._submissions.values():
            submission.fail(error)
        self._submissions.clear()

    def _release_through(
        self, generation: int | None, *, before_instance: int | None = None
    ) -> None:
        """完成确认覆盖的等待；启动核验只覆盖旧实例的等待。"""
        for number, wait in self._released.items():
            if (
                not wait.done.done()
                and (generation is None or number <= generation)
                and (before_instance is None or wait.instance_number < before_instance)
            ):
                wait.done.set_result(None)

    async def _revoke(self, instance: _Instance, peer: RpcPeer, generation: int) -> None:
        params = ProducerRevokeParams(generation=generation)
        previous_id = None
        while self._instance is instance and instance.release_failure is None:
            try:
                reply = await peer.request(
                    PRODUCER_REVOKE.name, _dump(params), timeout=self._options.rpc_timeout
                )
                revoked = ProducerRevokeResult.model_validate(reply)
            except (ChannelError, RpcError, ValidationError) as error:
                logger.warning("producer.revoke(%d) 未得到有效回应：%s", generation, error)
                return
            if self._instance is not instance or instance.release_failure is not None:
                return
            if not self._options.real_input:
                self._release_through(generation)
                return
            release_id = revoked.release_id
            if release_id is None or release_id == previous_id:
                return
            previous_id = release_id
            verification = await self._wait_verification(instance, release_id)
            if verification is None:
                return
            if (
                verification.release_id == release_id
                and verification.result is VerificationResult.VERIFIED
            ):
                self._release_through(generation)
                return
            # 有更新的释放时再次幂等作废，请实时核返回仍可关联的编号。

    async def _wait_verification(
        self, instance: _Instance, release_id: int
    ) -> InputVerificationParams | None:
        while self._instance is instance and instance.release_failure is None:
            observed = instance.latest_verification
            if observed is not None and observed.release_id >= release_id:
                return observed
            await instance.verification_changed.wait()
        return None

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
        peer.on_notification(ACTION_RECEIPT.name, self._on_receipt_message)
        peer.on_notification(
            INPUT_RESIDUAL.name, lambda params: self._on_residual_message(instance, params)
        )
        peer.on_notification(
            INPUT_VERIFICATION.name,
            lambda params: self._on_verification_message(instance, params),
        )
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
        if released is None or instance.hello_at_ns is None or instance.number != self._instances:
            return
        producer = released.producer
        logger.info(
            "实时核 %d 释放回执 #%d %s，状态 %s（产出者 %s）",
            instance.number,
            released.release_id,
            released.reason,
            released.verification,
            "无" if producer is None else f"{producer.id}#{producer.generation}",
        )
        if released.verification is ReleaseVerification.UP_FAILED:
            self._fail_release(instance, released.release_id, "系统拒绝抬起")
        if released.reason is ReleaseReason.CORE_START and not instance.ready.done():
            instance.startup_release_id = released.release_id
            if instance.release_failure is None:
                instance.ready_at_ns = time.perf_counter_ns()
                if not self._options.real_input:
                    self._release_through(None)
            instance.ready.set_result(None)
        if self._on_released is not None:
            self._on_released(released)

    def _on_residual_message(self, instance: _Instance, params: dict[str, Any]) -> None:
        residual = _parse(InputResidualParams, INPUT_RESIDUAL.name, params)
        if residual is None or instance.hello_at_ns is None or instance.number != self._instances:
            return
        logger.info(
            "实时核 %d 释放 #%d 核验出残留 %d 项，补发抬起全被接受=%s",
            instance.number,
            residual.release_id,
            len(residual.items),
            residual.repair_accepted,
        )
        if not residual.repair_accepted:
            self._fail_release(instance, residual.release_id, "系统拒绝补发抬起")
        if self._on_residual is not None:
            self._on_residual(residual)

    def _on_verification_message(self, instance: _Instance, params: dict[str, Any]) -> None:
        verification = _parse(InputVerificationParams, INPUT_VERIFICATION.name, params)
        if (
            verification is None
            or instance.hello_at_ns is None
            or instance.number != self._instances
        ):
            return
        logger.info(
            "实时核 %d 释放 #%d 物理核验 %s",
            instance.number,
            verification.release_id,
            verification.result,
        )
        if verification.result in (VerificationResult.RESIDUAL, VerificationResult.UNAVAILABLE):
            self._fail_release(instance, verification.release_id, verification.result.value)
        if (
            verification.release_id == instance.startup_release_id
            and verification.result is VerificationResult.VERIFIED
            and instance.release_failure is None
        ):
            self._release_through(None, before_instance=instance.number)
        if (
            instance.latest_verification is None
            or verification.release_id > instance.latest_verification.release_id
        ):
            instance.latest_verification = verification
            changed, instance.verification_changed = instance.verification_changed, asyncio.Event()
            changed.set()
        if self._on_verification is not None:
            self._on_verification(verification)

    def _fail_release(self, instance: _Instance, release_id: int, reason: str) -> None:
        if instance.release_failure is None:
            instance.release_failure = (
                f"实时核 {instance.number} 输入释放 #{release_id} 失败（{reason}）"
            )
            instance.verification_changed.set()
            logger.error("%s，停止动作提交与心跳", instance.release_failure)
        if self._instance is instance:
            self._stopping = True

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
        while instance.release_failure is None:
            grant = None if self._grant_source is None else self._grant_source()
            peer = instance.peer
            if grant is not None and grant.is_open and peer is not None and peer.connection.is_open:
                self._send_heartbeat(peer, grant)
            await asyncio.sleep(self._options.heartbeat_interval)

    def _send_heartbeat(self, peer: RpcPeer, grant: ControlGrant) -> None:
        self._heartbeat_seq += 1
        heartbeat = ProducerHeartbeatParams(
            producer=Producer(id=grant.producer, generation=grant.generation),
            seq=self._heartbeat_seq,
        )
        with contextlib.suppress(ChannelError):
            peer.notify(PRODUCER_HEARTBEAT.name, _dump(heartbeat))
            self._heartbeat_generation = grant.generation

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
        if self._instance is instance:
            self._instance = None
            self._exited = instance
            self._clean = returncode == 0 and instance.release_failure is None
            if self._clean:
                self._release_through(None)
            self._last_returncode = returncode
            self._last_health_at_ns = None
            self._heartbeat_generation = None
            self._fail_submissions(ChannelError("closed", f"实时核 {instance.number} 已退出"))
            self._maybe_restart(instance, returncode)
        for task in instance.tasks:
            task.cancel()
        instance.closing = asyncio.get_running_loop().create_task(self._close(instance))

    def _maybe_restart(self, instance: _Instance, returncode: int) -> None:
        """GR-35：运行中（已就绪）的实例非 0 退出，且不是主进程要求的停止、参数不合法（2）时，按退避
        自动重启。就绪后连续运行满 `reset_after` 秒即先清零重启次数。就绪之前的失败由 `start` 以
        `CoreStartError` 交给调用方，不在这里重启。"""
        policy = self._options.restart
        if policy is None or self._stopping or returncode in (0, 2):
            return
        if instance.ready_at_ns is None:
            return
        if time.perf_counter_ns() - instance.ready_at_ns >= policy.reset_after * 1e9:
            self._restart_attempt = 0
        self._schedule_restart(f"实时核 {instance.number} 非 0 退出（{returncode}）")

    def _schedule_restart(self, why: str) -> None:
        """按下一档退避排一次重启；次数用尽即停止并上报。"""
        policy = self._options.restart
        if policy is None:
            return
        if self._restart_attempt >= len(policy.backoff):
            logger.error("%s，自动重启已用尽 %d 次，停止并上报", why, len(policy.backoff))
            return
        delay = policy.backoff[self._restart_attempt]
        self._restart_attempt += 1
        attempt = self._restart_attempt
        logger.warning("%s，%g 秒后第 %d 次自动重启", why, delay, attempt)
        self._restart_task = asyncio.get_running_loop().create_task(self._restart(delay, attempt))

    async def _restart(self, delay: float, attempt: int) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if self._stopping or self._instance is not None:
            return
        if self._on_restart is not None:
            try:
                self._on_restart(attempt)
            except Exception:
                logger.exception("on_restart 出错")
        try:
            await self.start()
        except CoreStartError as error:
            # 这次没能就绪：接着排下一档退避（就绪前的失败不经 _maybe_restart）。期间已另排了一次
            # （例如就绪后又被杀）时不重复。
            if not self._stopping and self._restart_task is asyncio.current_task():
                self._schedule_restart(f"自动重启第 {attempt} 次没能就绪：{error}")

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


class _LinkAudio:
    """`CoreLink.audio_reader`：总是读 `CoreLink` 当前的音频环，没有时返回 None。"""

    def __init__(self, link: CoreLink) -> None:
        self._link = link

    def read_recent(self, duration_ns: int, *, after: int = 0) -> AudioRead | None:
        ring = self._link.audio
        if ring is None:
            return None
        return ring.read_recent(duration_ns, after=after)


async def shutdown_runtime(kernel: Kernel, link: CoreLink) -> StopResult:
    """主进程的停止顺序：先停插件内核，控制产出者走完 QUIESCING（等确认超时就上报后继续），
    再按 R-06 停实时核；实时核停止时也会释放全部输入，退出后帧环随之销毁。"""
    try:
        await kernel.shutdown()
    finally:
        result = await link.stop()
    return result


def _dump(model: BaseModel) -> dict[str, Any]:
    if isinstance(model, models.ActionBlock) and model.local_dialogue is None:
        return model.model_dump(mode="json", exclude={"local_dialogue"})
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
