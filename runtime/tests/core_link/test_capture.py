"""主进程一侧的帧环与采集：`CoreLink` 创建帧环、挂接给真实的 `realtime-core` 并设定采集目标。

自动测试用假采集源：合成游戏的帧写进假显示面（`FakeDisplay`），实时核的 `FakeCapture` 取帧写进帧环，
主进程经 `CoreLink.frames` 读出。真机上经 WGC 的测试见 `test_capture_window.py`（手动）。
Windows 上跑命名文件映射，Linux 容器里跑 POSIX 共享内存。
"""

import asyncio
import contextlib
import os
import signal
import sys
import time
from collections.abc import AsyncGenerator, Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from core_protocol import models
from core_protocol.models import CaptureState, CoreHealthParams
from game_runtime.core_link import (
    CaptureConfig,
    CoreLink,
    CoreLinkOptions,
    CoreStartError,
    StopResult,
    StopStage,
    WindowRule,
)
from game_runtime.frame_ring import FrameRing, RingFrame, frame_age_ns
from game_runtime.frame_ring.fake import FakeDisplay
from synthetic_game import Click, EffectKind, GameConfig, ManualClock, MouseButton, SyntheticGame
from synthetic_game.shell import WINDOW_TITLE

WAIT = 5.0


def link_options(exe: Path, capture: CaptureConfig) -> CoreLinkOptions:
    return CoreLinkOptions(
        exe,
        heartbeat_interval=0.05,
        heartbeat_timeout=0.3,
        health_interval=0.05,
        capture=capture,
    )


def kill(pid: int) -> None:
    """强制终止进程：Windows 上 `os.kill` 即 TerminateProcess，其它平台发 SIGKILL。"""
    os.kill(pid, signal.SIGTERM if sys.platform == "win32" else signal.SIGKILL)


async def until[T](probe: Callable[[], T | None], timeout: float = WAIT) -> T:
    """等到 `probe()` 不是 None，返回它。"""
    deadline = time.monotonic() + timeout
    while (value := probe()) is None:
        assert time.monotonic() < deadline, "等待的条件没有成立"
        await asyncio.sleep(0.005)
    return value


class Healths:
    """收下实时核发来的 `core.health`。"""

    def __init__(self) -> None:
        self.items: list[tuple[CoreHealthParams, int]] = []

    def __call__(self, health: CoreHealthParams) -> None:
        self.items.append((health, time.perf_counter_ns()))

    async def after(
        self, at_ns: int, condition: Callable[[CoreHealthParams], bool] = lambda _: True
    ) -> CoreHealthParams:
        """第一条 `now_ns` 不早于 `at_ns` 且满足条件的。实时核先读时刻再读状态，所以它的状态不早于
        那一刻。"""

        def probe() -> CoreHealthParams | None:
            return next((h for h, _ in self.items if h.now_ns >= at_ns and condition(h)), None)

        return await until(probe)


@contextlib.asynccontextmanager
async def running(link: CoreLink) -> AsyncGenerator[FrameRing]:
    """启动实时核，交出帧环；结束时（测试失败也一样）停止实时核，帧环随之销毁。"""
    await link.start()
    try:
        ring = link.frames
        assert ring is not None
        yield ring
    finally:
        await link.stop()
    assert link.frames is None
    assert ring.closed


def present(display: FakeDisplay, pixels: bytes, width: int, height: int, stride: int) -> int:
    """把一帧写进假显示面，返回采集时刻；用它在帧环里认出这一帧。"""
    capture_ns = time.perf_counter_ns()
    display.present(pixels, width, height, row_stride=stride, capture_ns=capture_ns)
    return capture_ns


async def in_ring(ring: FrameRing, capture_ns: int) -> RingFrame:
    """等到帧环的最新帧是采集时刻为 `capture_ns` 的那一帧。"""

    def probe() -> RingFrame | None:
        frame = ring.read_latest()
        return frame if frame is not None and frame.capture_ns == capture_ns else None

    return await until(probe)


def test_synthetic_game_frames_reach_the_main_process_through_the_core(core_exe: Path) -> None:
    game = SyntheticGame(GameConfig(320, 180), ManualClock())

    async def main() -> None:
        healths = Healths()
        with FakeDisplay.create(320, 180, slot_count=2) as display:
            capture = CaptureConfig(target=display.target, max_width=320, max_height=180)
            link = CoreLink(link_options(core_exe, capture), on_health=healths)
            async with running(link) as ring:

                async def show() -> RingFrame:
                    frame = game.frame()
                    at = present(display, frame.data, frame.width, frame.height, frame.stride)
                    got = await in_ring(ring, at)
                    assert (got.width, got.height, got.row_stride) == (320, 180, frame.stride)
                    assert got.pixels == frame.data
                    return got

                menu = await show()
                # 这里直接点合成游戏，只看帧的链路：画面变了，帧环里的帧随之变化。
                # 经假输入后端的点击见 test_actions.py。
                shop = next(t for t in game.truth().targets if t.id == "menu.shop")
                effect = game.handle(Click(*shop.rect.center, MouseButton.LEFT))
                assert (effect.kind, effect.target) == (EffectKind.HIT, "menu.shop")
                after = await show()
                assert after.frame_number > menu.frame_number
                assert after.pixels != menu.pixels
                # 实时核记下的最近一帧就是帧环里的这一帧。
                written = models.LatestFrame(
                    frame_number=after.frame_number, capture_ns=after.capture_ns
                )
                health = await healths.after(
                    time.perf_counter_ns(), lambda h: h.latest_frame == written
                )
                assert health.capture is CaptureState.CAPTURING

    asyncio.run(main())


def test_frozen_capture_lets_the_frame_age_grow(core_exe: Path) -> None:
    async def main() -> None:
        healths = Healths()
        with FakeDisplay.create(16, 12, slot_count=2) as display:
            capture = CaptureConfig(target=display.target, max_width=16, max_height=12)
            link = CoreLink(link_options(core_exe, capture), on_health=healths)
            async with running(link) as ring:
                pixels = bytes(16 * 12 * 4)
                last_at = present(display, pixels, 16, 12, 16 * 4)
                for _ in range(9):
                    await asyncio.sleep(0.02)
                    last_at = present(display, pixels, 16, 12, 16 * 4)
                last = await in_ring(ring, last_at)
                written = models.LatestFrame(frame_number=last.frame_number, capture_ns=last_at)
                # 假显示面不再显示新帧：采集冻结。
                first_age = frame_age_ns(last.capture_ns)
                before = await healths.after(
                    time.perf_counter_ns(), lambda h: h.latest_frame == written
                )
                await asyncio.sleep(0.4)
                later = ring.read_latest()
                assert later is not None
                assert later.frame_number == last.frame_number
                assert frame_age_ns(later.capture_ns) >= first_age + 300_000_000
                # 实时核记下的最近一帧不再前进；采集源还在，只是没有新帧。
                # 据帧龄释放输入见 test_actions.py。
                after = await healths.after(time.perf_counter_ns())
                assert after.now_ns - before.now_ns >= 300_000_000
                assert after.latest_frame == written
                assert after.capture is CaptureState.CAPTURING

    asyncio.run(main())


def test_a_restarted_core_reattaches_and_frame_numbers_continue(core_exe: Path) -> None:
    async def main() -> None:
        with FakeDisplay.create(16, 12, slot_count=2) as display:
            capture = CaptureConfig(target=display.target, max_width=16, max_height=12)
            link = CoreLink(link_options(core_exe, capture))
            await link.start()
            try:
                ring = link.frames
                assert ring is not None
                numbers: list[int] = []
                for value in range(3):
                    at = present(display, bytes([value]) * (16 * 12 * 4), 16, 12, 16 * 4)
                    numbers.append((await in_ring(ring, at)).frame_number)
                # 实时核崩溃：帧环留着，不再有写方。
                pid = link.pid
                assert pid is not None
                kill(pid)
                await until(lambda: True if not link.running else None)
                assert link.frames is ring
                assert not ring.closed
                present(display, bytes(16 * 12 * 4), 16, 12, 16 * 4)
                await asyncio.sleep(0.2)
                assert ring.latest_frame_number() == numbers[-1]

                # 新实例在旧进程退出之后才挂接同一个帧环，帧号接着写，不回退。
                await link.start()
                assert link.frames is ring
                for value in range(3, 6):
                    at = present(display, bytes([value]) * (16 * 12 * 4), 16, 12, 16 * 4)
                    numbers.append((await in_ring(ring, at)).frame_number)
                assert numbers == sorted(set(numbers)), numbers
            finally:
                await link.stop()
            assert link.frames is None
            assert ring.closed

    asyncio.run(main())


def test_stop_destroys_the_ring_even_after_a_crash(core_exe: Path) -> None:
    async def main() -> None:
        with FakeDisplay.create(16, 12) as display:
            capture = CaptureConfig(target=display.target, max_width=16, max_height=12)
            link = CoreLink(link_options(core_exe, capture))
            await link.start()
            ring = link.frames
            assert ring is not None
            pid = link.pid
            assert pid is not None
            kill(pid)
            await until(lambda: True if not link.running else None)
            assert not ring.closed
            result = await link.stop()
            assert result.stage is StopStage.ALREADY_EXITED
            assert result.returncode != 0
            assert ring.closed
            assert link.frames is None

    asyncio.run(main())


def test_start_fails_when_the_core_refuses_the_target(core_exe: Path) -> None:
    async def main() -> None:
        with FakeDisplay.create(16, 12) as display:
            # 参数里的槽数与假显示面不符：实时核以 -32002 拒绝。
            surface = display.target.surface.model_copy(update={"slot_count": 2})
            capture = CaptureConfig(target=models.FakeTarget(surface=surface))
            link = CoreLink(link_options(core_exe, capture))
            with pytest.raises(CoreStartError, match="没有接受帧环或采集目标") as failed:
                await link.start()
            assert "header slot_count is 3" in str(failed.value)
            assert not link.running
            # 帧环保留到 stop，供重试时接着用。
            assert link.frames is not None
            assert await link.stop() == StopResult(StopStage.ALREADY_EXITED, 0)
            assert link.frames is None

    asyncio.run(main())


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 上支持窗口目标，见手动测试")
def test_window_targets_are_unsupported_off_windows(core_exe: Path) -> None:
    async def main() -> None:
        rule = WindowRule(process_name="python.exe", window_class="TkTopLevel", title=WINDOW_TITLE)
        link = CoreLink(link_options(core_exe, CaptureConfig(target=rule)))
        with pytest.raises(CoreStartError, match="only available on Windows"):
            await link.start()
        await link.stop()

    asyncio.run(main())


# 配置


def test_window_rules_need_a_process_and_a_class_or_title() -> None:
    rule = WindowRule(process_name="python.exe", window_class="TkTopLevel", title=WINDOW_TITLE)
    assert rule.target() == models.WindowTarget(
        process_name="python.exe", window_class="TkTopLevel", title=WINDOW_TITLE
    )
    WindowRule(process_name="game.exe", title="Game")
    with pytest.raises(ValidationError, match="窗口类与标题至少给出一个"):
        WindowRule(process_name="game.exe")
    for empty in ({"process_name": ""}, {"window_class": ""}, {"title": ""}):
        with pytest.raises(ValidationError):
            WindowRule.model_validate({"process_name": "game.exe", "title": "Game", **empty})


def test_capture_config_reads_a_window_rule() -> None:
    config = CaptureConfig.model_validate(
        {
            "target": {
                "type": "window",
                "process_name": "python.exe",
                "window_class": "TkTopLevel",
                "title": WINDOW_TITLE,
            },
            "max_width": 1280,
            "max_height": 720,
        }
    )
    assert (config.max_width, config.max_height, config.slot_count) == (1280, 720, 3)
    assert config.set_target_params().model_dump(mode="json") == {
        "target": {
            "type": "window",
            "process_name": "python.exe",
            "window_class": "TkTopLevel",
            "title": WINDOW_TITLE,
        }
    }
    with pytest.raises(ValidationError):
        CaptureConfig.model_validate({"target": {"process_name": "python.exe", "title": "x"}})
    with pytest.raises(ValidationError):
        CaptureConfig.model_validate({"target": config.target, "slot_count": 1})
