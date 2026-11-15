"""操作员控制台与真实的 `realtime-core`：测试会话即测试许可（GR-36）。

拓扑：操作员控制台（`OperatorConsole`，在测试进程里，SecretStore 用内存实现）是 `bellis-channel/1`
的服务端；主进程一侧（`OperatorClient` 与 `CoreLink`）读它的连接信息、连上操作员连接，再把同一地址与
密钥交给实时核；实时核连上操作员许可连接，许可只来自控制台。实时核以假输入后端启动，测试设备
（`FakeDevice`）只收假输入事件，从不授予许可。帧由测试一方写进假显示面，经实时核进入帧环。

核对：只有会话在运行时动作才执行；暂停即 `lease_revoked` 释放且不留按住的键；控制台退出即
`lease_source_lost`；到了时长上限即吊销并结束；有操作员许可连接时 `test_lease.*` 是“方法不存在”；
重启的实时核得到更大的新令牌；门禁以真实的程序核对；最后以脚本化的产出者在合成游戏上推进对白，只在
运行时推进。打印的时延只是假后端上的数值，不代表真实输入（G2 实测）。
"""

import asyncio
import contextlib
import dataclasses
import sys
import threading
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from core_protocol import models
from core_protocol.models import ReleaseReason
from game_runtime.adapter import GAMES_DIR, Adapter, load_adapter
from game_runtime.channel import ChannelError, RpcError, RpcErrorCode, session_credentials
from game_runtime.core_link import (
    CORE_ROLE,
    ENV_CHANNEL_KEY,
    ENV_CHANNEL_PATH,
    ENV_OPERATOR_KEY,
    ENV_OPERATOR_PATH,
    ENV_ROLE,
    ENV_TEST_DEVICE_KEY,
    ENV_TEST_DEVICE_PATH,
    CaptureConfig,
    CoreLink,
    CoreLinkOptions,
    ManagedProcess,
    base_environment,
)
from game_runtime.core_link.fake_device import DeviceEvent, FakeDevice
from game_runtime.frame_ring import RingFrame
from game_runtime.frame_ring.fake import FakeDisplay
from game_runtime.kernel import ControlGrant, Submission
from game_runtime.operator import (
    ConsoleError,
    ConsoleEvent,
    ConsoleOptions,
    EventKind,
    OperatorClient,
    OperatorConsole,
    SessionState,
    messages,
)
from game_runtime.secret_store import MemorySecretStore
from synthetic_game import Click as GameClick
from synthetic_game import (
    Effect,
    EffectKind,
    GameConfig,
    Key,
    ManualClock,
    Press,
    SceneType,
    SyntheticGame,
)
from synthetic_game import MouseButton as GameButton

WAIT = 5.0
HEARTBEAT_INTERVAL = 0.05
HEARTBEAT_TIMEOUT = 0.3
MAX_FRAME_AGE = 0.2
PRESENT_INTERVAL = 0.02
W = 0x11
"""按住的键：W 的扫描码。"""
ENTER = 0x1C


async def until[T](probe: Callable[[], T | None], timeout: float = WAIT) -> T:
    deadline = time.monotonic() + timeout
    while (value := probe()) is None:
        assert time.monotonic() < deadline, "等待的条件没有成立"
        await asyncio.sleep(0.002)
    return value


def ms(nanoseconds: int) -> float:
    return nanoseconds / 1e6


type FrameSource = Callable[[], tuple[bytes, int, int]]


class Presenter:
    """在自己的线程上按固定间隔把画面写进假显示面，相当于游戏持续绘制。"""

    def __init__(self, display: FakeDisplay, source: FrameSource) -> None:
        self._display = display
        self._source = source
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name="presenter", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._thread.join(5)

    def present(self) -> None:
        with self._lock:
            pixels, width, height = self._source()
            self._display.present(pixels, width, height)

    def _run(self) -> None:
        while not self._stopped.wait(PRESENT_INTERVAL):
            self.present()


def solid(width: int, height: int) -> FrameSource:
    pixels = bytes(width * height * 4)
    return lambda: (pixels, width, height)


def held_key(scan_code: int) -> models.HeldInput:
    return models.HeldInput(root=models.HeldKey(scan_code=scan_code))


def key_press(action_id: int, scan_code: int) -> models.Action:
    return models.Action(root=models.KeyPress(id=action_id, scan_code=scan_code))


def click_at(action_id: int, x: int, y: int) -> models.Action:
    return models.Action(root=models.Click(id=action_id, x=x, y=y, button=models.MouseButton.LEFT))


def step(
    held: list[models.HeldInput] | None = None, actions: list[models.Action] | None = None
) -> models.ControlStep:
    return models.ControlStep(offset_ns=0, held=held or [], actions=actions or [])


def adapter() -> Adapter:
    return load_adapter(GAMES_DIR / "synthetic-game")


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """临时的本地数据目录（会话记录写在这里）。Windows 上加扩展长度前缀：临时目录较深而系统没有开
    长路径支持时，会话记录的路径会超过 MAX_PATH。"""
    if sys.platform == "win32":
        return Path("\\\\?\\" + str(tmp_path.resolve())) / "data"
    return tmp_path / "data"


@dataclass
class Rig:
    """控制台、主进程一侧（操作员客户端与核连接）、测试设备与假显示面。"""

    console: OperatorConsole
    client: OperatorClient
    link: CoreLink
    device: FakeDevice
    display: FakeDisplay
    presenter: Presenter
    grant: ControlGrant
    console_events: list[ConsoleEvent] = field(default_factory=list[ConsoleEvent])
    released: list[models.InputReleasedParams] = field(
        default_factory=list[models.InputReleasedParams]
    )
    residuals: list[models.InputResidualParams] = field(
        default_factory=list[models.InputResidualParams]
    )
    health: list[models.CoreHealthParams] = field(default_factory=list[models.CoreHealthParams])
    action_ids: int = 0

    def latest_frame(self) -> RingFrame:
        ring = self.link.frames
        assert ring is not None
        frame = ring.read_latest()
        assert frame is not None
        return frame

    async def submit(self, *steps: models.ControlStep, valid_for: float = 1.0) -> Submission:
        frame = self.latest_frame()
        return await self.grant.submit(
            list(steps), base_frame=frame.frame_number, valid_for=valid_for
        )

    def next_id(self) -> int:
        self.action_ids += 1
        return self.action_ids

    async def lease(self, status: models.LeaseStatus, after: int = 0) -> None:
        """等到一条（第 `after` 条之后的）`core.health`：产出者是这份资格的，许可是 `status`，本实例
        已写过帧。"""
        producer = models.Producer(id=self.grant.producer, generation=self.grant.generation)
        await until(
            lambda: next(
                (
                    True
                    for h in self.health[after:]
                    if h.producer == producer and h.lease is status and h.latest_frame is not None
                ),
                None,
            )
        )

    async def confirm_and_start(self) -> None:
        while self.console.pending_confirmation is not None:
            await self.console.answer(True)
        healths = len(self.health)
        await self.console.start()
        await self.lease(models.LeaseStatus.VALID, after=healths)
        await until(lambda: True if self.client.may_autoplay else None)

    async def hold(self, scan_code: int = W) -> None:
        submission = await self.submit(step(held=[held_key(scan_code)]), valid_for=30)
        assert isinstance(submission.result, models.Accepted), submission.result
        await self.device.until(lambda d: True if ("key", scan_code) in d.held else None)

    async def press(self) -> models.Executed | models.Dropped | models.Uncertain:
        action = self.next_id()
        submission = await self.submit(step(actions=[key_press(action, ENTER)]))
        assert isinstance(submission.result, models.Accepted), submission.result
        return (await asyncio.wait_for(submission.outcomes(), WAIT))[action]

    async def device_released(self, reason: ReleaseReason, after: int = 0) -> DeviceEvent:
        return await self.device.until(
            lambda d: next(
                (
                    e
                    for e in d.events()[after:]
                    if isinstance(e.event, models.FakeReleaseAll) and e.event.reason is reason
                ),
                None,
            )
        )

    def console_released(self) -> list[ReleaseReason]:
        return [
            ReleaseReason(e.fields["reason"])
            for e in self.console_events
            if e.kind is EventKind.INPUT_RELEASED
        ]


def assert_lifted_before(events: list[DeviceEvent], release: DeviceEvent) -> None:
    """`release` 之前测试设备收到了 W 的抬起，`release` 时它记下的按住的项为空。"""
    ups = [
        e
        for e in events
        if e.connection == release.connection
        and e.seq < release.seq
        and isinstance(e.event, models.FakeKeyUp)
        and e.event.scan_code == W
    ]
    assert ups, [e.type for e in events]
    assert release.held_after == frozenset(), release.held_after


@contextlib.asynccontextmanager
async def rig(
    core_exe: Path,
    data_dir: Path,
    *,
    options: ConsoleOptions | None = None,
    size: tuple[int, int] = (64, 48),
    source: FrameSource | None = None,
    on_event: Callable[[DeviceEvent], None] | None = None,
    input_policy: models.InputPolicy | None = None,
) -> AsyncGenerator[Rig]:
    """拉起一套联调环境：控制台已打开、在等确认；主进程连上了操作员连接；实时核连上了控制台、测试设备与
    主进程，帧在流入帧环，产出者 step 代次 1 在发心跳。"""
    width, height = size
    store = MemorySecretStore()
    # 联调测试不测倒数（倒数在 test_console 里单测）：立即授予。
    options = dataclasses.replace(options or ConsoleOptions(), start_delay=0.0)
    console = OperatorConsole(adapter(), store=store, data_dir=data_dir, options=options)
    holder: list[Rig] = []
    console.add_listener(lambda event: holder[0].console_events.append(event) if holder else None)
    await console.open()
    link: CoreLink | None = None
    client = OperatorClient(adapter(), store=store)
    try:
        await client.connect()
        with FakeDevice(on_event=on_event) as device, FakeDisplay.create(width, height) as display:
            presenter = Presenter(display, source or solid(width, height))
            presenter.start()
            try:
                surface = display.target.surface
                link = CoreLink(
                    CoreLinkOptions(
                        core_exe,
                        heartbeat_interval=HEARTBEAT_INTERVAL,
                        heartbeat_timeout=HEARTBEAT_TIMEOUT,
                        health_interval=0.05,
                        max_frame_age=MAX_FRAME_AGE,
                        capture=CaptureConfig(
                            target=display.target,
                            max_width=surface.max_width,
                            max_height=surface.max_height,
                        ),
                        test_device=device.credentials,
                        operator=client.credentials,
                        input_policy=input_policy,
                    ),
                    on_released=lambda r: holder[0].released.append(r),
                    on_health=lambda h: holder[0].health.append(h),
                    on_residual=lambda r: holder[0].residuals.append(r),
                )
                grant = ControlGrant("step", "step#1", 1, lambda: True, link.submit)
                current = Rig(console, client, link, device, display, presenter, grant)
                holder.append(current)
                link.bind_control(lambda: current.grant)
                await link.start()
                await device.until_connected()
                await current.lease(models.LeaseStatus.NOT_GRANTED)
                yield current
            finally:
                if link is not None:
                    await link.stop()
                presenter.stop()
    finally:
        await client.close()
        await console.close()


# ---------------------------------------------------------------- 会话即许可


@pytest.mark.parametrize("event", ["release", "residual", "unavailable"])
def test_input_release_failure_stops_submissions_and_lease(
    core_exe: Path, data_dir: Path, event: str
) -> None:
    async def main() -> None:
        async with rig(core_exe, data_dir) as r:
            await r.confirm_and_start()
            await r.hold()
            instance = r.link._instance  # pyright: ignore[reportPrivateUsage]
            peer = r.console._core  # pyright: ignore[reportPrivateUsage]
            assert instance is not None
            assert peer is not None
            at = time.perf_counter_ns()
            if event == "release":
                params = models.InputReleasedParams(
                    release_id=2,
                    reason=ReleaseReason.DEADLINE_EXPIRED,
                    producer=models.Producer(id="step", generation=1),
                    released_at_ns=at,
                    verification=models.ReleaseVerification.UP_FAILED,
                ).model_dump(mode="json")
                r.link._on_released_message(instance, params)  # pyright: ignore[reportPrivateUsage]
                r.console._on_released(peer, params)  # pyright: ignore[reportPrivateUsage]
            elif event == "residual":
                params = models.InputResidualParams(
                    release_id=2,
                    cause=models.ResidualCause.RELEASE,
                    reason=ReleaseReason.DEADLINE_EXPIRED,
                    items=[held_key(W)],
                    repair_accepted=False,
                    checked_at_ns=at,
                ).model_dump(mode="json")
                r.link._on_residual_message(instance, params)  # pyright: ignore[reportPrivateUsage]
                r.console._on_residual(peer, params)  # pyright: ignore[reportPrivateUsage]
            else:
                params = models.InputVerificationParams(
                    release_id=2,
                    result=models.VerificationResult.UNAVAILABLE,
                    checked_at_ns=at,
                ).model_dump(mode="json")
                r.link._on_verification_message(instance, params)  # pyright: ignore[reportPrivateUsage]
                r.console._on_verification(peer, params)  # pyright: ignore[reportPrivateUsage]

            with pytest.raises(ChannelError, match="输入释放"):
                await r.press()
            await asyncio.wait_for(r.console.wait_ended(), WAIT)
            assert r.console.end_reason == "input_release_failed"
            assert await r.device_released(ReleaseReason.LEASE_REVOKED)
            assert not r.device.held

    asyncio.run(main())


def test_actions_run_only_while_the_session_is_running(core_exe: Path, data_dir: Path) -> None:
    async def main() -> None:
        async with rig(core_exe, data_dir) as r:
            # 控制台在连上时就收到了实时核启动时的释放。
            await until(lambda: True if ReleaseReason.CORE_START in r.console_released() else None)
            # 还在等确认：没有许可，什么也不发。
            assert r.console.state is SessionState.WAITING
            assert not r.client.may_autoplay
            outcome = await r.press()
            assert outcome == models.Dropped(id=outcome.id, reason=models.DropReason.RELEASED)
            assert not [e for e in r.device.events() if isinstance(e.event, models.FakeKeyPress)]

            await r.confirm_and_start()
            assert isinstance(await r.press(), models.Executed)
            await r.hold()

            # 暂停：吊销令牌，实时核释放全部输入（lease_revoked），不留按住的键。
            seen = len(r.device.events())
            pausing = time.perf_counter_ns()
            await r.console.pause()
            release = await r.device_released(ReleaseReason.LEASE_REVOKED, after=seen)
            assert_lifted_before(r.device.events(), release)
            print(f"暂停 → 释放全部：{ms(release.at_ns - pausing):.1f} ms（假后端）")
            assert r.device.held == frozenset()
            await until(
                lambda: True if ReleaseReason.LEASE_REVOKED in r.console_released() else None
            )
            await until(
                lambda: next(
                    (True for x in r.released if x.reason is ReleaseReason.LEASE_REVOKED), None
                )
            )
            assert not r.client.may_autoplay
            outcome = await r.press()
            assert outcome == models.Dropped(id=outcome.id, reason=models.DropReason.RELEASED)

            # 继续：换发更大的令牌，动作照常执行。
            healths = len(r.health)
            await r.console.start()
            await r.lease(models.LeaseStatus.VALID, after=healths)
            assert r.console.token == 2
            assert isinstance(await r.press(), models.Executed)
            assert r.device.anomalies == []

    asyncio.run(main())


def test_closing_the_console_withdraws_the_lease(core_exe: Path, data_dir: Path) -> None:
    async def main() -> None:
        async with rig(core_exe, data_dir) as r:
            await r.confirm_and_start()
            await r.hold()
            seen = len(r.device.events())
            closing = time.perf_counter_ns()
            await r.console.close()
            release = await r.device_released(ReleaseReason.LEASE_SOURCE_LOST, after=seen)
            # 控制台退出前先结束会话：吊销在前，连接断开在后。
            revoked = next(
                (
                    e
                    for e in r.device.events()[seen:]
                    if isinstance(e.event, models.FakeReleaseAll)
                    and e.event.reason is ReleaseReason.LEASE_REVOKED
                ),
                None,
            )
            assert revoked is not None
            assert revoked.seq < release.seq
            print(f"控制台退出 → 吊销：{ms(revoked.at_ns - closing):.1f} ms（假后端）")
            assert r.device.held == frozenset()
            assert r.client.ended in {"ended", "disconnected"}
            assert not r.client.may_autoplay
            await until(
                lambda: (
                    True
                    if r.health[-1].lease is models.LeaseStatus.SOURCE_LOST
                    and not r.health[-1].inputs_held
                    else None
                )
            )
            # 实时核照常运行，但没有许可，不发任何输入，也不重连控制台。
            assert r.link.running
            outcome = await r.press()
            assert outcome == models.Dropped(id=outcome.id, reason=models.DropReason.RELEASED)

    asyncio.run(main())


def test_the_time_cap_revokes_and_ends_the_session(core_exe: Path, data_dir: Path) -> None:
    options = ConsoleOptions(max_minutes=1.5 / 60)  # 1.5 秒

    async def main() -> None:
        async with rig(core_exe, data_dir, options=options) as r:
            await r.confirm_and_start()
            await r.hold()
            seen = len(r.device.events())
            await asyncio.wait_for(r.console.wait_ended(), WAIT)
            assert r.console.end_reason == "time_cap"
            release = await r.device_released(ReleaseReason.LEASE_REVOKED, after=seen)
            assert_lifted_before(r.device.events(), release)
            ends_at = r.console.ends_at_ns
            assert ends_at is not None
            print(f"到时 → 释放全部：{ms(release.at_ns - ends_at):.1f} ms（假后端）")
            await until(lambda: True if r.client.ended == "ended" else None)
            assert not r.client.may_autoplay
            with pytest.raises(ConsoleError, match="已结束"):
                await r.console.start()
            outcome = await r.press()
            assert outcome == models.Dropped(id=outcome.id, reason=models.DropReason.RELEASED)

    asyncio.run(main())


def test_the_test_lease_source_is_not_there_with_the_operator(
    core_exe: Path, data_dir: Path
) -> None:
    """有操作员许可连接时测试许可源不构造：测试设备的授予与吊销都是“方法不存在”。"""

    async def main() -> None:
        async with rig(core_exe, data_dir) as r:
            with pytest.raises(RpcError) as refused:
                await r.device.grant()
            assert refused.value.code == RpcErrorCode.METHOD_NOT_FOUND
            with pytest.raises(RpcError) as refused:
                await r.device.revoke()
            assert refused.value.code == RpcErrorCode.METHOD_NOT_FOUND
            outcome = await r.press()
            assert outcome == models.Dropped(id=outcome.id, reason=models.DropReason.RELEASED)
            assert r.health[-1].lease is models.LeaseStatus.NOT_GRANTED
            # 测试设备照样收到假输入事件。
            await r.confirm_and_start()
            assert isinstance(await r.press(), models.Executed)
            await r.device.until(
                lambda d: next(
                    (True for e in d.events() if isinstance(e.event, models.FakeKeyPress)), None
                )
            )

    asyncio.run(main())


def test_a_restarted_core_gets_a_bigger_fresh_token(core_exe: Path, data_dir: Path) -> None:
    """GR-35 的自动重启还没有实现：手动停掉再拉起实时核，相当于新实例连上控制台。"""

    async def main() -> None:
        async with rig(core_exe, data_dir) as r:
            await r.confirm_and_start()
            first = r.console.token
            assert first == 1
            await r.link.stop()
            await until(lambda: True if not r.console.core_connected else None)
            assert ReleaseReason.STOP in r.console_released()
            starts = r.console_released().count(ReleaseReason.CORE_START)
            healths = len(r.health)
            await r.link.start()
            await r.device.until_connected(2)
            # 新实例先释放全部输入（core_start），控制台换发更大的令牌授予它。
            await until(
                lambda: (
                    True
                    if r.console_released().count(ReleaseReason.CORE_START) == starts + 1
                    else None
                )
            )
            await r.lease(models.LeaseStatus.VALID, after=healths)
            assert r.console.token == first + 1
            assert r.console.state is SessionState.RUNNING
            await r.hold()
            granted = [
                e.fields["token"] for e in r.console_events if e.kind is EventKind.LEASE_GRANTED
            ]
            assert granted == [1, 2]

    asyncio.run(main())


# ---------------------------------------------------------------- 门禁


def test_the_operator_lease_connection_gate(core_exe: Path, tmp_path: Path) -> None:
    """操作员许可连接离不开输入后端（2）；假后端离不开测试设备（2）；连不上控制台即以 1 退出。

    Windows 上另以真实后端启动：它先装上低层钩子再连控制台，所以连不上时报的是连不上控制台，而不是
    装不上钩子。没有许可也没有输入策略，不发任何输入。
    """

    async def exit_code(argv: list[str], *, device: bool) -> tuple[int, str]:
        with (
            session_credentials("gate") as core,
            session_credentials("gatedev") as dev,
            session_credentials("gateop") as operator,
        ):
            env = {
                **base_environment(),
                ENV_CHANNEL_PATH: core.path,
                ENV_CHANNEL_KEY: core.encoded_key,
                ENV_ROLE: CORE_ROLE,
                ENV_OPERATOR_PATH: operator.path,
                ENV_OPERATOR_KEY: operator.encoded_key,
            }
            if device:
                env |= {ENV_TEST_DEVICE_PATH: dev.path, ENV_TEST_DEVICE_KEY: dev.encoded_key}
            log_path = tmp_path / "gate.log"
            with log_path.open("wb") as log:
                process = ManagedProcess.start(
                    "realtime-core", [str(core_exe), *argv], env, stderr=log
                )
            try:
                code = await asyncio.wait_for(asyncio.shield(process.exited), WAIT)
            finally:
                process.kill()
            return code, log_path.read_text(encoding="utf-8", errors="replace")

    async def main() -> None:
        for device in (False, True):
            code, log = await exit_code([], device=device)
            assert code == 2, log
            assert "needs an input backend" in log
        code, log = await exit_code(["--input-backend", "fake"], device=False)
        assert code == 2, log
        assert ENV_TEST_DEVICE_PATH in log
        # 凭据齐全，但没有控制台在听。
        code, log = await exit_code(["--input-backend", "fake"], device=True)
        assert code == 1, log
        assert "cannot connect to the operator console" in log
        if sys.platform == "win32":
            code, log = await exit_code(["--input-backend", "sendinput"], device=False)
            assert code == 1, log
            assert "low-level hooks" not in log, log
            assert "cannot connect to the operator console" in log, log

    asyncio.run(main())


def test_options_pass_the_operator_credentials() -> None:
    with session_credentials("opt") as device, session_credentials("optop") as operator:
        options = CoreLinkOptions(Path("core"), test_device=device, operator=operator)
        assert options.child_env() == {
            ENV_TEST_DEVICE_PATH: device.path,
            ENV_TEST_DEVICE_KEY: device.encoded_key,
            ENV_OPERATOR_PATH: operator.path,
            ENV_OPERATOR_KEY: operator.encoded_key,
        }
        only = CoreLinkOptions(Path("core"), operator=operator)
        # 后端由实时核的门禁核对，主进程不替它选：没有后端时实时核以 2 退出。
        assert "--input-backend" not in only.argv()
        assert only.child_env() == {
            ENV_OPERATOR_PATH: operator.path,
            ENV_OPERATOR_KEY: operator.encoded_key,
        }


# ---------------------------------------------------------------- 合成游戏


def game_input(event: DeviceEvent) -> GameClick | Press | None:
    match event.event:
        case models.FakeClick(x=x, y=y, button=button):
            return GameClick(x, y, GameButton(button.value))
        case models.FakeKeyPress(scan_code=code) if code == ENTER:
            return Press(Key.ENTER)
        case _:
            return None


def test_a_scripted_producer_advances_the_dialogue_only_while_running(
    core_exe: Path, data_dir: Path
) -> None:
    """G2 第 1 步：控制台在假后端与合成游戏上测通。产出者每 50 ms 看一次画面的真值（测试的
    真值，不冒充感知），只在 `may_autoplay` 时提交：菜单上点“开始”，对话里按回车；每个动作都报给
    控制台。"""
    game = SyntheticGame(GameConfig(320, 180), ManualClock())
    lock = threading.Lock()
    effects: list[Effect] = []
    presenter: list[Presenter] = []

    def frame() -> tuple[bytes, int, int]:
        with lock:
            rendered = game.frame()
        return rendered.data, rendered.width, rendered.height

    def on_event(event: DeviceEvent) -> None:
        item = game_input(event)
        if item is None:
            return
        with lock:
            effects.append(game.handle(item))
        presenter[0].present()

    def advanced() -> int:
        with lock:
            return sum(1 for e in effects if e.changed and e.kind is not EffectKind.MISS)

    async def produce(r: Rig, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if r.client.may_autoplay:
                with lock:
                    truth = game.truth()
                if truth.scene is SceneType.MENU:
                    target = next(t for t in truth.targets if t.id == "menu.start")
                    x, y = target.rect.center
                    action, summary = click_at(r.next_id(), x, y), "点击“开始”"
                    category = "navigate"
                else:
                    action, summary, category = key_press(r.next_id(), ENTER), "回车", "advance"
                submission = await r.submit(step(actions=[action]), valid_for=0.5)
                if isinstance(submission.result, models.Accepted):
                    r.client.report_action(messages.Action(category=category, summary=summary))
            await asyncio.sleep(0.05)

    async def main() -> None:
        async with rig(core_exe, data_dir, size=(320, 180), source=frame, on_event=on_event) as r:
            presenter.append(r.presenter)
            stop = asyncio.Event()
            producer = asyncio.ensure_future(produce(r, stop))
            try:
                # 等确认：产出者不动作，游戏停在菜单。
                await asyncio.sleep(0.5)
                assert advanced() == 0
                with lock:
                    assert game.truth().scene is SceneType.MENU

                await r.confirm_and_start()
                await until(lambda: True if advanced() >= 5 else None)
                with lock:
                    assert game.truth().scene is SceneType.DIALOGUE

                # 暂停：令牌吊销，对白不再推进。
                await r.console.pause()
                await until(lambda: True if not r.client.may_autoplay else None)
                await asyncio.sleep(0.3)  # 已在路上的一块动作执行完或被丢弃
                paused_at = advanced()
                await asyncio.sleep(0.5)
                assert advanced() == paused_at

                # 继续：接着推进。
                await r.console.start()
                await until(lambda: True if advanced() >= paused_at + 3 else None)

                # 结束：不再推进。
                await r.console.end("operator")
                await until(lambda: True if not r.client.may_autoplay else None)
                await asyncio.sleep(0.3)
                ended_at = advanced()
                await asyncio.sleep(0.5)
                assert advanced() == ended_at
            finally:
                stop.set()
                await producer
            actions = [e for e in r.console_events if e.kind is EventKind.ACTION]
            assert actions
            assert {e.fields["category"] for e in actions} <= {"navigate", "advance"}
            assert r.device.anomalies == []

    asyncio.run(main())


# ---------------------------------------------------------------- 输入策略


def norm_rect(left: float, top: float, right: float, bottom: float) -> models.NormRect:
    return models.NormRect(left=left, top=top, right=right, bottom=bottom)


def policy_for_tests() -> models.InputPolicy:
    """只允许左键点击与短按回车、滚动，落点限客户区左上 1/4，按住 50 ms。"""
    return models.InputPolicy.model_validate(
        {
            "click_buttons": ["left"],
            "press_keys": [ENTER],
            "scroll": True,
            "click_regions": [norm_rect(0.0, 0.0, 0.5, 0.5).model_dump()],
            "forbidden_zones": [],
            "press_duration_ms": 50,
            "max_rate": 10.0,
            "hold_keys": [W],
            "hold_buttons": [],
            "max_hold_ms": 2000,
        }
    )


def test_input_policy_is_enforced_through_the_real_core(core_exe: Path, data_dir: Path) -> None:
    """主进程经 input.configure 交出输入策略，真实的实时核据它把关：提交时核对白名单，执行时核对
    落点。假后端上没有残留（input.residual 只在真实后端）。"""

    async def main() -> None:
        async with rig(core_exe, data_dir, size=(100, 100), input_policy=policy_for_tests()) as r:
            await r.confirm_and_start()
            # 白名单外的键（右键点击的身份不在策略里）：提交即整块拒绝 not_allowed。
            right = models.Action(
                root=models.Click(id=r.next_id(), x=10, y=10, button=models.MouseButton.RIGHT)
            )
            submission = await r.submit(step(actions=[right]))
            assert isinstance(submission.result, models.Rejected)
            assert submission.result.reason is models.RejectReason.NOT_ALLOWED

            # 落点在允许区外（右下）：执行时丢弃 not_allowed，不释放。
            far = click_at(r.next_id(), 90, 90)
            submission = await r.submit(step(actions=[far]))
            assert isinstance(submission.result, models.Accepted), submission.result
            outcome = (await asyncio.wait_for(submission.outcomes(), WAIT))[far.root.id]
            assert outcome == models.Dropped(id=far.root.id, reason=models.DropReason.NOT_ALLOWED)

            # 落点在允许区内：执行（假后端上点击作瞬时按住，先发 mouse_down）。
            near = click_at(r.next_id(), 10, 10)
            submission = await r.submit(step(actions=[near]))
            assert isinstance(submission.result, models.Accepted), submission.result
            got = (await asyncio.wait_for(submission.outcomes(), WAIT))[near.root.id]
            assert isinstance(got, models.Executed)
            await r.device.until(
                lambda d: next(
                    (True for e in d.events() if isinstance(e.event, models.FakeMouseDown)), None
                )
            )
            # 假后端不做 GetAsyncKeyState 核验：没有 input.residual。
            assert r.residuals == []
            assert r.device.anomalies == []

    asyncio.run(main())
