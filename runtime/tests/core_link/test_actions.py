"""动作块与假输入后端：真实的 `realtime-core`、主进程一侧的 `CoreLink` 与测试设备联调。

拓扑：测试设备（`FakeDevice`，测试一方：测试许可源加假输入设备，在自己的线程上）← 实时核 → 主进程
（`CoreLink`）。帧由测试一方写进假显示面（`FakeDisplay`，在自己的线程上按 50 Hz 显示），经实时核的
假采集源进入帧环。许可只来自测试设备，主进程只能让输入停下。

失效与释放（`docs/architecture.md` 的“失效与释放”）的每种情形都先让实时核按住一个键，再制造失效，
核对测试一方看到抬起与“释放全部”，以及 `input.released` 的原因（主进程已死时看测试一方）。打印的
时延只是假后端上的数值，不代表真实输入（G2 实测）。Windows 上走命名管道，Linux 容器里走 UDS。
"""

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from core_protocol import models
from core_protocol.methods import ErrorCode
from core_protocol.models import ReleaseReason
from game_runtime.channel import RpcError, session_credentials
from game_runtime.core_link import (
    CORE_ROLE,
    ENV_CHANNEL_KEY,
    ENV_CHANNEL_PATH,
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
from game_runtime.frame_ring import FrameRing, RingFrame
from game_runtime.frame_ring.fake import FakeDisplay
from game_runtime.kernel import (
    CONTROL_SUBMIT,
    ControlGrant,
    ControlRevokedError,
    Kernel,
    Manifest,
    Plugin,
    PluginContext,
    ResolvedEntry,
    State,
    Submission,
    Tier,
    Transition,
    catalog,
)
from synthetic_game import (
    Click,
    Effect,
    EffectKind,
    GameConfig,
    Input,
    Key,
    ManualClock,
    MouseButton,
    Press,
    SceneType,
    Scroll,
    SyntheticGame,
)

WAIT = 5.0
HEARTBEAT_INTERVAL = 0.05
HEARTBEAT_TIMEOUT = 0.3
MAX_FRAME_AGE = 0.2
PRESENT_INTERVAL = 0.02
W = 0x11
"""按住的键：W 的扫描码。"""
LIFELINE = {ReleaseReason.CORE_CONNECTION_LOST, ReleaseReason.STDIN_CLOSED}


def kill(pid: int) -> None:
    """强制终止进程：Windows 上 `os.kill` 即 TerminateProcess，其它平台发 SIGKILL。"""
    os.kill(pid, signal.SIGTERM if sys.platform == "win32" else signal.SIGKILL)


async def until[T](probe: Callable[[], T | None], timeout: float = WAIT) -> T:
    """等到 `probe()` 不是 None，返回它。"""
    deadline = time.monotonic() + timeout
    while (value := probe()) is None:
        assert time.monotonic() < deadline, "等待的条件没有成立"
        await asyncio.sleep(0.002)
    return value


def ms(nanoseconds: int) -> float:
    return nanoseconds / 1e6


# ---------------------------------------------------------------- 测试一方：显示面

type FrameSource = Callable[[], tuple[bytes, int, int]]


class Presenter:
    """在自己的线程上按固定间隔把画面写进假显示面，相当于游戏持续绘制。`freeze` 让采集冻结。"""

    def __init__(self, display: FakeDisplay, source: FrameSource) -> None:
        self._display = display
        self._source = source
        self._lock = threading.Lock()
        self._frozen = False
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name="presenter", daemon=True)
        self.last_capture_ns = 0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._thread.join(5)

    def present(self) -> None:
        """立即显示一帧（输入改变了画面时）。"""
        with self._lock:
            if self._frozen:
                return
            pixels, width, height = self._source()
            now = time.perf_counter_ns()
            self._display.present(pixels, width, height, capture_ns=now)
            self.last_capture_ns = now

    def freeze(self) -> int:
        """不再显示新帧；返回最后一帧的采集时刻。"""
        with self._lock:
            self._frozen = True
            return self.last_capture_ns

    def resume(self) -> None:
        with self._lock:
            self._frozen = False

    def _run(self) -> None:
        while not self._stopped.wait(PRESENT_INTERVAL):
            self.present()


def solid(width: int, height: int) -> FrameSource:
    pixels = bytes(width * height * 4)
    return lambda: (pixels, width, height)


# ---------------------------------------------------------------- 主进程一侧加测试一方


def held_key(scan_code: int) -> models.HeldInput:
    return models.HeldInput(root=models.HeldKey(scan_code=scan_code))


def click_at(action_id: int, x: int, y: int) -> models.Action:
    return models.Action(root=models.Click(id=action_id, x=x, y=y, button=models.MouseButton.LEFT))


def key_press(action_id: int, scan_code: int) -> models.Action:
    return models.Action(root=models.KeyPress(id=action_id, scan_code=scan_code))


def step(
    held: list[models.HeldInput] | None = None,
    actions: list[models.Action] | None = None,
    offset_ms: int = 0,
) -> models.ControlStep:
    return models.ControlStep(
        offset_ns=offset_ms * 1_000_000, held=held or [], actions=actions or []
    )


@dataclass
class Rig:
    """一套联调环境：测试设备、假显示面与显示线程、拉起的实时核与一份提交资格。"""

    link: CoreLink
    device: FakeDevice
    presenter: Presenter
    grant: ControlGrant
    released: list[tuple[models.InputReleasedParams, int]] = field(
        default_factory=list[tuple[models.InputReleasedParams, int]]
    )
    health: list[models.CoreHealthParams] = field(default_factory=list[models.CoreHealthParams])

    @property
    def ring(self) -> FrameRing:
        ring = self.link.frames
        assert ring is not None
        return ring

    def latest_frame(self) -> RingFrame:
        frame = self.ring.read_latest()
        assert frame is not None
        return frame

    async def submit(self, *steps: models.ControlStep, valid_for: float = 1.0) -> Submission:
        """以帧环里的最新帧为基准帧提交一块动作。"""
        frame = self.latest_frame()
        return await self.grant.submit(
            list(steps), base_frame=frame.frame_number, valid_for=valid_for
        )

    async def ready(self, lease: models.LeaseStatus, after: int = 0) -> None:
        """等到一条（第 `after` 条之后的）`core.health`：产出者是这份资格的，许可是 `lease`，
        本实例已写过帧。"""
        producer = models.Producer(id=self.grant.producer, generation=self.grant.generation)
        await until(
            lambda: next(
                (
                    True
                    for h in self.health[after:]
                    if h.producer == producer and h.lease is lease and h.latest_frame is not None
                ),
                None,
            )
        )

    async def hold(self, scan_code: int = W) -> None:
        """让实时核按住一个键，直到测试设备看到它按下。"""
        submission = await self.submit(step(held=[held_key(scan_code)]), valid_for=30)
        assert isinstance(submission.result, models.Accepted), submission.result
        await self.device.until(lambda d: True if ("key", scan_code) in d.held else None)

    async def core_released(
        self, reason: ReleaseReason, after: int = 0
    ) -> models.InputReleasedParams:
        """等到主进程收到原因为 `reason` 的 `input.released`（只看第 `after` 条之后的）。"""

        def probe() -> models.InputReleasedParams | None:
            return next((r for r, _ in self.released[after:] if r.reason is reason), None)

        return await until(probe)

    async def device_released(self, reason: ReleaseReason, after: int = 0) -> DeviceEvent:
        """等到测试设备收到原因为 `reason` 的“释放全部”（只看第 `after` 个事件之后的）。"""
        return await self.device.until(lambda d: released_event(d.events()[after:], reason))


def released_event(events: list[DeviceEvent], reason: ReleaseReason) -> DeviceEvent | None:
    return next(
        (
            e
            for e in events
            if isinstance(e.event, models.FakeReleaseAll) and e.event.reason is reason
        ),
        None,
    )


def assert_lifted_before(
    events: list[DeviceEvent], release: DeviceEvent, scan_code: int = W
) -> DeviceEvent:
    """`release` 之前测试设备收到了这个键的抬起，`release` 时它记下的按住的项为空。返回那次抬起。"""
    ups = [
        e
        for e in events
        if e.connection == release.connection
        and e.seq < release.seq
        and isinstance(e.event, models.FakeKeyUp)
        and e.event.scan_code == scan_code
    ]
    assert ups, [e.type for e in events]
    assert release.held_after == frozenset(), release.held_after
    return ups[-1]


def link_options(
    core_exe: Path, display: FakeDisplay, device: FakeDevice | None
) -> CoreLinkOptions:
    surface = display.target.surface
    return CoreLinkOptions(
        core_exe,
        heartbeat_interval=HEARTBEAT_INTERVAL,
        heartbeat_timeout=HEARTBEAT_TIMEOUT,
        health_interval=0.05,
        max_frame_age=MAX_FRAME_AGE,
        capture=CaptureConfig(
            target=display.target, max_width=surface.max_width, max_height=surface.max_height
        ),
        test_device=None if device is None else device.credentials,
    )


@contextlib.asynccontextmanager
async def rig(
    core_exe: Path,
    *,
    size: tuple[int, int] = (64, 48),
    source: FrameSource | None = None,
    on_event: Callable[[DeviceEvent], None] | None = None,
    fake_backend: bool = True,
) -> AsyncGenerator[Rig]:
    """拉起一套联调环境：许可已授予并按 250ms 续约，产出者 step 代次 1 的心跳已到达，
    帧在流入帧环。"""
    width, height = size
    with FakeDevice(on_event=on_event) as device, FakeDisplay.create(width, height) as display:
        presenter = Presenter(display, source or solid(width, height))
        presenter.start()
        link: CoreLink | None = None
        try:
            options = link_options(core_exe, display, device if fake_backend else None)
            holder: list[Rig] = []
            link = CoreLink(
                options,
                on_released=lambda r: holder[0].released.append((r, time.perf_counter_ns())),
                on_health=lambda h: holder[0].health.append(h),
            )
            grant = ControlGrant("step", "step#1", 1, lambda: True, link.submit)
            current = Rig(link, device, presenter, grant)
            holder.append(current)
            link.bind_control(lambda: current.grant)
            await link.start()
            if fake_backend:
                await device.until_connected()
                await device.renew()
                await current.ready(models.LeaseStatus.VALID)
            else:
                await current.ready(models.LeaseStatus.NOT_GRANTED)
            yield current
        finally:
            if link is not None:
                await link.stop()
            presenter.stop()


# ---------------------------------------------------------------- 闭环


def game_input(event: DeviceEvent) -> Input | None:
    """假输入事件转成合成游戏的输入；按住类的事件合成游戏不处理。"""
    keys = {0x1C: Key.ENTER, 0xE01C: Key.ENTER, 0x39: Key.SPACE, 0x01: Key.ESCAPE, 0xE048: Key.UP}
    keys[0xE050] = Key.DOWN
    match event.event:
        case models.FakeClick(x=x, y=y, button=button):
            return Click(x, y, MouseButton(button.value))
        case models.FakeKeyPress(scan_code=code) if code in keys:
            return Press(keys[code])
        case models.FakeScroll(x=x, y=y, notches=notches):
            return Scroll(x, y, notches)
        case _:
            return None


def test_a_click_from_the_main_process_reaches_the_game_and_changes_the_frame(
    core_exe: Path,
) -> None:
    """G0 出口：假输入后端的点击回到状态机并改变画面。主进程按帧环里的帧提交点击；按钮的位置取自
    `truth()`，那是测试的真值，不冒充感知。"""
    game = SyntheticGame(GameConfig(320, 180), ManualClock())
    lock = threading.Lock()
    effects: list[Effect] = []
    presenter: list[Presenter] = []

    def frame() -> tuple[bytes, int, int]:
        with lock:
            rendered = game.frame()
        return rendered.data, rendered.width, rendered.height

    def on_event(event: DeviceEvent) -> None:
        # 测试设备的线程上：把输入交给合成游戏，画面变了就立即显示。
        item = game_input(event)
        if item is None:
            return
        with lock:
            effects.append(game.handle(item))
        presenter[0].present()

    async def main() -> None:
        async with rig(core_exe, size=(320, 180), source=frame, on_event=on_event) as r:
            presenter.append(r.presenter)

            def showing(scene: SceneType) -> RingFrame | None:
                current = r.ring.read_latest()
                with lock:
                    expected = game.frame().data if game.truth().scene is scene else None
                return current if current is not None and current.pixels == expected else None

            menu = await until(lambda: showing(SceneType.MENU))
            with lock:
                shop = next(t for t in game.truth().targets if t.id == "menu.shop")
            x, y = shop.rect.center
            submission = await r.grant.submit(
                [step(actions=[click_at(1, x, y)])], base_frame=menu.frame_number, valid_for=0.5
            )
            assert isinstance(submission.result, models.Accepted)
            outcomes = await asyncio.wait_for(submission.outcomes(), WAIT)
            assert isinstance(outcomes[1], models.Executed)
            # 假后端把点击原样交给测试设备（回执与事件走两条连接，先后不定），测试设备交给合成游戏：
            # 命中商店按钮。
            clicks = await r.device.until(
                lambda d: (
                    [e.event for e in d.events() if isinstance(e.event, models.FakeClick)] or None
                )
            )
            assert clicks == [models.FakeClick(id=1, x=x, y=y, button=models.MouseButton.LEFT)]
            await until(lambda: effects or None)
            assert (effects[0].kind, effects[0].target) == (EffectKind.HIT, "menu.shop")
            assert shop.rect.contains(x, y)
            # 画面改变后新帧进入帧环，主进程读到。
            in_shop = await until(lambda: showing(SceneType.SHOP))
            assert in_shop.frame_number > menu.frame_number
            assert in_shop.pixels != menu.pixels

            # 短按 Esc 离开商店，回到菜单。
            submission = await r.grant.submit(
                [step(actions=[key_press(2, 0x01)])],
                base_frame=in_shop.frame_number,
                valid_for=0.5,
            )
            assert isinstance((await submission.outcomes())[2], models.Executed)
            await until(lambda: showing(SceneType.MENU))
            assert r.device.anomalies == []

    asyncio.run(main())


def test_a_click_based_on_a_frame_of_another_size_is_dropped(core_exe: Path) -> None:
    sizes = [(64, 48)]

    def frame() -> tuple[bytes, int, int]:
        width, height = sizes[-1]
        return bytes(width * height * 4), width, height

    async def main() -> None:
        async with rig(core_exe, size=(64, 48), source=frame) as r:
            base = r.latest_frame()
            sizes.append((48, 48))
            await until(lambda: True if r.latest_frame().width == 48 else None)
            submission = await r.grant.submit(
                [step(actions=[click_at(1, 5, 5), key_press(2, 0x1C)])],
                base_frame=base.frame_number,
                valid_for=0.5,
            )
            outcomes = await submission.outcomes()
            assert outcomes[1] == models.Dropped(id=1, reason=models.DropReason.GEOMETRY_CHANGED)
            assert isinstance(outcomes[2], models.Executed)
            assert not [e for e in r.device.events() if isinstance(e.event, models.FakeClick)]

    asyncio.run(main())


# ---------------------------------------------------------------- 失效与释放


def test_main_process_hang_releases_on_heartbeat_timeout(core_exe: Path) -> None:
    async def main() -> None:
        async with rig(core_exe) as r:
            await r.hold()
            seen = len(r.device.events())
            # 主进程卡死：事件循环被占住，心跳停发。测试设备与显示线程照常运行，许可照常续约。
            hung = time.perf_counter_ns()
            time.sleep(HEARTBEAT_TIMEOUT * 2)
            release = released_event(r.device.events()[seen:], ReleaseReason.HEARTBEAT_TIMEOUT)
            assert release is not None, [e.type for e in r.device.events()[seen:]]
            up = assert_lifted_before(r.device.events(), release)
            latency = ms(up.at_ns - hung)
            print(
                f"主进程卡死 → 抬起：{latency:.1f} ms（心跳超时 {HEARTBEAT_TIMEOUT * 1000:.0f} ms）"
            )
            assert latency > HEARTBEAT_TIMEOUT * 1000 - HEARTBEAT_INTERVAL * 1000 - 20
            assert latency < HEARTBEAT_TIMEOUT * 1000 + 100
            # 恢复后主进程收到释放的通知。
            await r.core_released(ReleaseReason.HEARTBEAT_TIMEOUT)

    asyncio.run(main())


def test_capture_freeze_releases_on_frame_stale_and_later_blocks_do_not_run(
    core_exe: Path,
) -> None:
    async def main() -> None:
        async with rig(core_exe) as r:
            await r.hold()
            seen = len(r.device.events())
            last = r.presenter.freeze()
            release = await r.device_released(ReleaseReason.FRAME_STALE, after=seen)
            up = assert_lifted_before(r.device.events(), release)
            latency = ms(up.at_ns - last)
            print(
                f"最后一帧 → 抬起：{latency:.1f} ms（动作允许的帧龄 {MAX_FRAME_AGE * 1000:.0f} ms）"
            )
            assert MAX_FRAME_AGE * 1000 <= latency < MAX_FRAME_AGE * 1000 + 100
            await r.core_released(ReleaseReason.FRAME_STALE)
            # 之后的动作块不执行：回执 released，原因仍是帧龄。
            count = len(r.released)
            submission = await r.submit(step(actions=[key_press(1, 0x1C)]))
            assert isinstance(submission.result, models.Accepted)
            outcomes = await submission.outcomes()
            assert outcomes[1] == models.Dropped(id=1, reason=models.DropReason.RELEASED)
            await r.core_released(ReleaseReason.FRAME_STALE, after=count)
            assert not [e for e in r.device.events() if isinstance(e.event, models.FakeKeyPress)]
            # 画面恢复后照常执行。
            r.presenter.resume()
            await until(lambda: True if r.latest_frame().capture_ns > last else None)
            submission = await r.submit(step(actions=[key_press(2, 0x1C)]))
            assert isinstance((await submission.outcomes())[2], models.Executed)

    asyncio.run(main())


def test_lease_expiry_releases_and_nothing_runs_without_a_lease(core_exe: Path) -> None:
    async def main() -> None:
        async with rig(core_exe) as r:
            await r.hold()
            seen = len(r.device.events())
            # 测试许可源停止续约。
            await r.device.stop_renewing()
            expires = r.device.lease_expires_ns
            assert expires is not None
            release = await r.device_released(ReleaseReason.LEASE_EXPIRED, after=seen)
            up = assert_lifted_before(r.device.events(), release)
            latency = ms(up.at_ns - expires)
            print(f"许可到期 → 抬起：{latency:.1f} ms")
            assert 0 <= latency < 100
            await r.core_released(ReleaseReason.LEASE_EXPIRED)
            submission = await r.submit(step(actions=[key_press(1, 0x1C)]))
            outcomes = await submission.outcomes()
            assert outcomes[1] == models.Dropped(id=1, reason=models.DropReason.RELEASED)
            assert r.health[-1].lease in {models.LeaseStatus.EXPIRED, models.LeaseStatus.VALID}
            await until(lambda: True if r.health[-1].lease is models.LeaseStatus.EXPIRED else None)

    asyncio.run(main())


def test_a_revoked_lease_or_a_stale_token_sends_nothing(core_exe: Path) -> None:
    async def main() -> None:
        async with rig(core_exe) as r:
            await r.hold()
            seen = len(r.device.events())
            # 吊销当前的令牌（rig 授予的第一个令牌是 1）。实时核释放完成后才回应；抬起经发送队列
            # 写给测试设备，可能晚于回应到达。
            revoked = time.perf_counter_ns()
            await r.device.revoke()
            release = await r.device_released(ReleaseReason.LEASE_REVOKED, after=seen)
            up = assert_lifted_before(r.device.events(), release)
            print(f"吊销 → 抬起：{ms(up.at_ns - revoked):.1f} ms")
            await r.core_released(ReleaseReason.LEASE_REVOKED)
            # 已吊销的令牌、比当前小的令牌都不被接受，许可不会因此恢复。
            for stale in (1, 0):
                with pytest.raises(RpcError) as refused:
                    await r.device.grant_raw(stale, time.perf_counter_ns() + 10**9)
                assert refused.value.code == ErrorCode.LEASE_REFUSED
            count = len(r.released)
            submission = await r.submit(step(actions=[key_press(1, 0x1C)]))
            assert (await submission.outcomes())[1].type == "dropped"
            await r.core_released(ReleaseReason.LEASE_REVOKED, after=count)
            assert not [e for e in r.device.events() if isinstance(e.event, models.FakeKeyPress)]
            # 换发更大的令牌之后照常执行。
            await r.device.renew()
            submission = await r.submit(step(actions=[key_press(2, 0x1C)]))
            assert isinstance((await submission.outcomes())[2], models.Executed)

    asyncio.run(main())


def test_losing_the_test_device_withdraws_the_lease(core_exe: Path) -> None:
    async def main() -> None:
        async with rig(core_exe) as r:
            await r.hold()
            # 测试设备断开后收不到抬起，只看主进程收到的 `input.released`。
            dropped = time.perf_counter_ns()
            await r.device.disconnect()
            released = await r.core_released(ReleaseReason.LEASE_SOURCE_LOST)
            print(f"测试设备断开 → 释放完成：{ms(released.released_at_ns - dropped):.1f} ms")
            await until(
                lambda: (
                    True
                    if not r.health[-1].inputs_held
                    and r.health[-1].lease is models.LeaseStatus.SOURCE_LOST
                    else None
                )
            )
            # 实时核照常运行，但没有许可，不发任何输入。
            count = len(r.released)
            submission = await r.submit(step(actions=[key_press(1, 0x1C)]))
            outcomes = await submission.outcomes()
            assert outcomes[1] == models.Dropped(id=1, reason=models.DropReason.RELEASED)
            await r.core_released(ReleaseReason.LEASE_SOURCE_LOST, after=count)
            assert r.link.running

    asyncio.run(main())


def test_without_the_fake_backend_there_is_no_lease_and_no_input(core_exe: Path) -> None:
    """G0 的生产配置：没有输入后端，也没有许可源。动作块照常校验，但从不发出输入。"""

    async def main() -> None:
        async with rig(core_exe, fake_backend=False) as r:
            submission = await r.submit(step(held=[held_key(W)], actions=[key_press(1, 0x1C)]))
            assert isinstance(submission.result, models.Accepted)
            outcomes = await submission.outcomes()
            assert outcomes[1] == models.Dropped(id=1, reason=models.DropReason.RELEASED)
            await r.core_released(ReleaseReason.NO_LEASE)
            assert not r.health[-1].inputs_held
            assert r.device.connections == 0

    asyncio.run(main())


def test_a_higher_generation_releases_first_and_old_generations_are_refused(
    core_exe: Path,
) -> None:
    async def main() -> None:
        async with rig(core_exe) as r:
            await r.hold()
            old = r.grant
            seen = len(r.device.events())
            # 更换控制产出者：更高代次的第一块之前先释放旧代次的全部输入，再切换。
            r.grant = ControlGrant("vla", "vla#2", 2, lambda: True, r.link.submit)
            switched = time.perf_counter_ns()
            submission = await r.submit(step(held=[held_key(0x1E)]), valid_for=30)
            assert isinstance(submission.result, models.Accepted)
            await r.device.until(lambda d: True if ("key", 0x1E) in d.held else None)
            events = r.device.events()[seen:]
            print(f"更高代次 → 抬起：{ms(events[0].at_ns - switched):.1f} ms")
            order = [
                (e.type, getattr(e.event, "scan_code", getattr(e.event, "reason", None)))
                for e in events
            ]
            assert order[:3] == [
                ("key_up", W),
                ("release_all", ReleaseReason.PRODUCER_CHANGED),
                ("key_down", 0x1E),
            ], order
            await r.core_released(ReleaseReason.PRODUCER_CHANGED)
            # 旧代次的动作块：拒绝，什么都不执行。
            seen = len(r.device.events())
            frame = r.latest_frame()
            stale = await old.submit(
                [step(held=[held_key(W)], actions=[key_press(9, 0x1C)])],
                base_frame=frame.frame_number,
                valid_for=1.0,
            )
            assert isinstance(stale.result, models.Rejected)
            assert stale.result.reason is models.RejectReason.STALE_GENERATION
            assert await stale.outcomes() == {}
            await asyncio.sleep(0.1)
            assert r.device.events()[seen:] == []
            assert r.device.held == {("key", 0x1E)}

    asyncio.run(main())


def test_switching_producers_through_the_kernel_releases_before_the_new_one_starts(
    core_exe: Path,
) -> None:
    """与插件内核联调 QUIESCING：旧产出者的提交资格失效 → 作废代次、实时核释放 → 新产出者才激活。"""
    log: list[tuple[str, int]] = []

    def producer_plugin(plugin_id: str) -> type[Plugin[Any]]:
        class Producer(Plugin[Any]):
            manifest = Manifest(
                id=plugin_id, version="1.0.0", tier=Tier.CRITICAL, permissions=(CONTROL_SUBMIT,)
            )

            async def activate(self, ctx: PluginContext[Any]) -> None:
                del ctx

        return Producer

    def entry(entry_id: str) -> ResolvedEntry:
        return ResolvedEntry(
            id=entry_id,
            plugin=entry_id,
            config={},
            disabled=False,
            grant=frozenset({CONTROL_SUBMIT}),
        )

    def transition(change: Transition) -> None:
        if change.before is not change.after:
            log.append((f"{change.entry_id} {change.after}", time.perf_counter_ns()))

    async def main() -> None:
        async with rig(core_exe) as r:
            kernel = Kernel(
                catalog=catalog(producer_plugin("step"), producer_plugin("vla")),
                core=r.link,
                on_transition=transition,
            )
            r.link.bind_control(lambda: kernel.active_control)
            kernel.add([entry("step"), entry("vla")])
            await kernel.set_control_producer("step")
            grant = kernel.active_control
            assert grant is not None
            r.grant = grant
            await r.hold()
            seen = len(r.device.events())

            switching = time.perf_counter_ns()
            await kernel.set_control_producer("vla")
            # 旧产出者卸载后回到 WAITING（不再是当前控制产出者）。
            assert kernel.status("step").state is State.WAITING
            assert kernel.status("vla").state is State.ACTIVE
            release = await r.device_released(ReleaseReason.GENERATION_REVOKED, after=seen)
            up = assert_lifted_before(r.device.events(), release)
            print(f"更换控制产出者（内核）→ 抬起：{ms(up.at_ns - switching):.1f} ms")
            # 比实时核交出抬起的时刻（`at_ns`）：内核等的是 `producer.revoke` 的回应，即实时核里的
            # 释放已完成；事件经发送队列送到测试设备，收到的时刻可能晚于回应。
            loading = next(at for name, at in log if name == "vla loading")
            assert up.at_ns <= release.at_ns < loading, "新产出者在输入释放之前激活了"
            unloading = next(at for name, at in log if name == "step unloading")
            assert release.at_ns < unloading
            # 旧资格已失效：什么也不发。
            with pytest.raises(ControlRevokedError):
                await grant.submit([step(actions=[key_press(1, 0x1C)])], base_frame=1, valid_for=1)
            # 新产出者以新代次提交，照常执行。
            new = kernel.active_control
            assert new is not None
            assert new.generation == grant.generation + 1
            r.grant = new
            submission = await r.submit(step(actions=[key_press(2, 0x1C)]))
            assert isinstance((await submission.outcomes())[2], models.Executed)
            await kernel.shutdown()

    asyncio.run(main())


def test_a_restarted_core_releases_first_and_the_killed_one_left_its_key_down(
    core_exe: Path,
) -> None:
    """实时核崩溃：被杀的实例发不出抬起，测试设备上那个键一直按着，这是残余风险（G2 用
    `GetAsyncKeyState` 核验）。新实例先释放全部输入（`core_start`），再接受动作块。"""

    async def main() -> None:
        async with rig(core_exe) as r:
            await r.hold()
            pid = r.link.pid
            assert pid is not None
            kill(pid)
            await until(lambda: True if not r.link.running else None)
            await asyncio.sleep(0.2)
            # 残余风险：没有任何进程发出抬起。
            assert ("key", W) in r.device.held
            assert not [e for e in r.device.events() if isinstance(e.event, models.FakeKeyUp)]

            healths = len(r.health)
            restarting = time.perf_counter_ns()
            await r.link.start()
            await r.device.until_connected(2)
            first = await r.device.until(
                lambda d: next((e for e in d.events() if e.connection == 2), None)
            )
            print(f"重启 → 新实例的 core_start：{ms(first.at_ns - restarting):.1f} ms")
            # 新实例写出第一帧、拿到许可（测试设备照常续约）之后才按。
            await r.ready(models.LeaseStatus.VALID, after=healths)
            await r.hold(0x1E)
            second = [e for e in r.device.events() if e.connection == 2]
            assert second[0].seq == 1
            assert second[0].event == models.FakeReleaseAll(reason=ReleaseReason.CORE_START)
            assert [e.type for e in second[1:]] == ["key_down"]
            assert r.device.held == {("key", W), ("key", 0x1E)}

    asyncio.run(main())


def read_line(stream: Any, timeout: float) -> str:
    import queue

    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: lines.put(stream.readline()), daemon=True).start()
    return lines.get(timeout=timeout)


def test_killing_the_main_process_releases_held_inputs_via_the_lifeline(
    core_exe: Path, main_process: Path, tmp_path: Path, gone: Callable[[int, float], bool]
) -> None:
    """主进程崩溃：真的杀掉跑主进程代码的子进程。主进程已死，只能看测试设备与日志。"""
    log_path = tmp_path / "main.log"
    with FakeDevice() as device, FakeDisplay.create(64, 48) as display:
        presenter = Presenter(display, solid(64, 48))
        presenter.start()
        env = {
            **os.environ,
            ENV_TEST_DEVICE_PATH: device.credentials.path,
            ENV_TEST_DEVICE_KEY: device.credentials.encoded_key,
        }
        target = display.target.model_dump_json()
        with log_path.open("wb") as log:
            main = subprocess.Popen(
                [sys.executable, str(main_process), str(core_exe), target],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log,
                encoding="utf-8",
                env=env,
            )
        core_pid: int | None = None
        try:
            assert main.stdin is not None
            assert main.stdout is not None
            asyncio.run(device.until_connected(timeout=60))
            asyncio.run(device.renew())
            line = read_line(main.stdout, 60)
            assert line.startswith("core "), log_path.read_text(errors="replace")
            core_pid = int(line.split()[1])
            main.stdin.write("hold\n")
            main.stdin.flush()
            assert read_line(main.stdout, 10).strip() == "held"
            asyncio.run(device.until(lambda d: True if ("key", W) in d.held else None))
            seen = len(device.events())

            killed = time.perf_counter_ns()
            main.kill()
            main.wait(10)

            async def lifeline() -> DeviceEvent:
                def probe(d: FakeDevice) -> DeviceEvent | None:
                    return next(
                        (
                            e
                            for e in d.events()[seen:]
                            if isinstance(e.event, models.FakeReleaseAll)
                            and e.event.reason in LIFELINE
                        ),
                        None,
                    )

                return await device.until(probe)

            release = asyncio.run(lifeline())
            up = assert_lifted_before(device.events(), release)
            print(f"杀掉主进程 → 测试设备收到抬起：{ms(up.received_ns - killed):.1f} ms")
            assert gone(core_pid, 5), "实时核没有退出"
            text = log_path.read_text(encoding="utf-8", errors="replace")
            assert "released all inputs, exiting" in text, text
        finally:
            if main.poll() is None:
                main.kill()
                main.wait(10)
            if core_pid is not None and not gone(core_pid, 0):
                with contextlib.suppress(OSError):
                    kill(core_pid)
            for stream in (main.stdin, main.stdout):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()
            presenter.stop()


# ---------------------------------------------------------------- 门禁


def test_the_test_lease_source_only_runs_with_the_fake_backend(
    core_exe: Path, tmp_path: Path
) -> None:
    """测试许可源不能与非假后端（G0 里即没有后端）同时启用；假后端也离不开测试设备；真实后端
    unsupported。三种都以参数不合法（2）退出，连主进程都不连。"""

    async def exit_code(argv: list[str], extra: dict[str, str]) -> tuple[int, str]:
        with session_credentials(f"g{os.getpid()}") as core, session_credentials("gd") as dev:
            env = {
                **base_environment(),
                ENV_CHANNEL_PATH: core.path,
                ENV_CHANNEL_KEY: core.encoded_key,
                ENV_ROLE: CORE_ROLE,
            }
            device = {ENV_TEST_DEVICE_PATH: dev.path, ENV_TEST_DEVICE_KEY: dev.encoded_key}
            log_path = tmp_path / "gate.log"
            with log_path.open("wb") as log:
                process = ManagedProcess.start(
                    "realtime-core",
                    [str(core_exe), *argv],
                    {**env, **(device if extra else {})},
                    stderr=log,
                )
            try:
                code = await asyncio.wait_for(asyncio.shield(process.exited), WAIT)
            finally:
                process.kill()
            return code, log_path.read_text(encoding="utf-8", errors="replace")

    async def main() -> None:
        code, log = await exit_code([], {"device": "yes"})
        assert code == 2
        assert "only available with --input-backend fake" in log
        code, log = await exit_code(["--input-backend", "fake"], {})
        assert code == 2
        assert ENV_TEST_DEVICE_PATH in log
        code, log = await exit_code(["--input-backend", "sendinput"], {"device": "yes"})
        assert code == 2
        assert "unsupported" in log

    asyncio.run(main())


def test_options_pass_the_frame_age_and_the_test_device() -> None:
    with session_credentials("opt") as credentials:
        options = CoreLinkOptions(Path("core"), max_frame_age=0.25, test_device=credentials)
        assert options.argv()[-4:] == ["--max-frame-age-ms", "250", "--input-backend", "fake"]
        assert options.child_env() == {
            ENV_TEST_DEVICE_PATH: credentials.path,
            ENV_TEST_DEVICE_KEY: credentials.encoded_key,
        }
    production = CoreLinkOptions(Path("core"))
    assert "--input-backend" not in production.argv()
    assert production.child_env() == {}
    with pytest.raises(ValueError, match="必须为正"):
        CoreLinkOptions(Path("core"), max_frame_age=0)
