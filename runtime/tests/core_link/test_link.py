"""主进程一侧的 `CoreLink` 与真实的 `realtime-core`：启动与停止、插件内核联调、重启、主进程被杀。"""

import asyncio
import contextlib
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

import pytest

from core_protocol.models import CoreHealthParams, InputReleasedParams, Producer
from game_runtime.core_link import (
    CoreLink,
    CoreLinkOptions,
    StopResult,
    StopStage,
    shutdown_runtime,
)
from game_runtime.kernel import (
    CONTROL_SUBMIT,
    Kernel,
    KernelOptions,
    Manifest,
    Plugin,
    PluginContext,
    ReasonCode,
    ResolvedEntry,
    State,
    Tier,
    Transition,
    catalog,
)

WAIT = 5.0


def link_options(exe: Path) -> CoreLinkOptions:
    return CoreLinkOptions(
        exe, heartbeat_interval=0.05, heartbeat_timeout=0.3, health_interval=0.05
    )


def kill(pid: int) -> None:
    """强制终止进程：Windows 上 `os.kill` 即 TerminateProcess，其它平台发 SIGKILL。"""
    if sys.platform == "win32":
        os.kill(pid, signal.SIGTERM)
    else:
        os.kill(pid, signal.SIGKILL)


async def until(condition: Callable[[], bool], timeout: float = WAIT) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "等待的条件没有成立"
        await asyncio.sleep(0.01)


class Recorder:
    """按到达顺序记下内核的状态变化、实时核的释放与插件的注册撤销。"""

    def __init__(self) -> None:
        self.log: list[str] = []
        self.health: CoreHealthParams | None = None

    def released(self, released: InputReleasedParams) -> None:
        self.log.append(f"released {released.reason}")

    def transition(self, transition: Transition) -> None:
        if transition.before is not transition.after:
            self.log.append(f"{transition.entry_id} {transition.after}")

    def on_health(self, health: CoreHealthParams) -> None:
        self.health = health

    def producer_is(self, producer: Producer | None) -> Callable[[], bool]:
        return lambda: self.health is not None and self.health.producer == producer

    def order(self, *events: str) -> None:
        indices = [self.log.index(event) for event in events]
        assert indices == sorted(indices), self.log


def step_plugin(recorder: Recorder) -> type[Plugin[Any]]:
    class Step(Plugin[Any]):
        manifest = Manifest(
            id="step", version="1.0.0", tier=Tier.CRITICAL, permissions=(CONTROL_SUBMIT,)
        )

        async def activate(self, ctx: PluginContext[Any]) -> None:
            ctx.register(lambda: recorder.log.append(f"revoke {ctx.instance_id}"))

    return Step


STEP = ResolvedEntry(
    id="step", plugin="step", config={}, disabled=False, grant=frozenset({CONTROL_SUBMIT})
)


def make_runtime(
    exe: Path, kernel_options: KernelOptions | None = None
) -> tuple[Recorder, CoreLink, Kernel]:
    recorder = Recorder()
    link = CoreLink(link_options(exe), on_released=recorder.released, on_health=recorder.on_health)
    kernel = Kernel(
        catalog=catalog(step_plugin(recorder)),
        core=link,
        options=kernel_options,
        on_transition=recorder.transition,
    )
    link.bind_control(lambda: kernel.active_control)
    return recorder, link, kernel


@contextlib.asynccontextmanager
async def stopped_at_end(link: CoreLink) -> AsyncGenerator[CoreLink]:
    """测试失败时也停止实时核，不留下进程。"""
    try:
        yield link
    finally:
        if link.running:
            await link.stop()


def test_link_starts_and_stops_the_core(core_exe: Path) -> None:
    async def main() -> None:
        recorder = Recorder()
        link = CoreLink(
            link_options(core_exe), on_released=recorder.released, on_health=recorder.on_health
        )
        await link.start()
        async with stopped_at_end(link):
            assert link.running
            assert link.pid is not None
            await until(lambda: recorder.health is not None)
            assert link.healthy
            # 第一条 core.health 是实时核收到握手回应之后读的时钟。
            first = recorder.health
            assert first is not None
            assert link.hello_at_ns is not None
            assert link.hello_at_ns <= first.now_ns <= time.perf_counter_ns()
            result = await link.stop()
            assert result == StopResult(StopStage.SHUTDOWN, 0)
            assert recorder.log == ["released core_start", "released stop"]
            assert not link.running
            assert not link.healthy
            assert await link.stop() == StopResult(StopStage.ALREADY_EXITED, 0)

    asyncio.run(main())


def test_real_core_control_completes_quiescing_in_order(core_exe: Path) -> None:
    async def main() -> None:
        recorder, link, kernel = make_runtime(core_exe)
        await link.start()
        async with stopped_at_end(link):
            kernel.add([STEP])
            await kernel.set_control_producer("step")
            assert kernel.status("step").state is State.ACTIVE
            # 心跳的身份与代次取自提交资格，到达实时核。
            await until(recorder.producer_is(Producer(id="step", generation=1)))

            await kernel.disable("step")
            assert kernel.status("step").state is State.DISPOSED
            recorder.order(
                "step quiescing",
                "released generation_revoked",
                "step unloading",
                "revoke step#1",
                "step disposed",
            )
            await until(recorder.producer_is(None))
            # 提交资格收回即停发心跳：实时核那边已没有产出者，也不会超时释放。
            await asyncio.sleep(0.5)
            assert recorder.log.count("released heartbeat_timeout") == 0

            await kernel.enable("step")
            await until(recorder.producer_is(Producer(id="step", generation=2)))
            recorder.log.clear()
            result = await shutdown_runtime(kernel, link)
            assert result == StopResult(StopStage.SHUTDOWN, 0)
            # 先停内核（控制产出者走完 QUIESCING），再停实时核。
            assert recorder.log == [
                "step quiescing",
                "released generation_revoked",
                "step unloading",
                "revoke step#2",
                "step disposed",
                "released stop",
            ]

    asyncio.run(main())


def test_restarted_core_releases_first_and_completes_the_wait(core_exe: Path) -> None:
    async def main() -> None:
        recorder, link, kernel = make_runtime(core_exe, KernelOptions(release_timeout=0.3))
        await link.start()
        async with stopped_at_end(link):
            kernel.add([STEP])
            await kernel.set_control_producer("step")
            await until(recorder.producer_is(Producer(id="step", generation=1)))

            # 实时核崩溃：没有进程发出按键抬起，等待不能就此完成。
            pid = link.pid
            assert pid is not None
            kill(pid)
            await until(lambda: not link.running)
            await kernel.disable("step")
            status = kernel.status("step")
            assert status.state is State.QUIESCING
            assert [r.code for r in status.reasons] == [ReasonCode.RELEASE_TIMEOUT]
            recorder.log.clear()

            # 新实例先释放全部输入，等待因此完成，控制产出者走完 QUIESCING。
            await link.start()
            await until(lambda: kernel.status("step").state is State.DISPOSED)
            assert recorder.log[0] == "released core_start"
            recorder.order("released core_start", "step unloading", "revoke step#1")

    asyncio.run(main())


def test_wait_completes_after_a_clean_exit(core_exe: Path) -> None:
    async def main() -> None:
        _, link, kernel = make_runtime(core_exe)
        await link.start()
        kernel.add([STEP])
        await kernel.set_control_producer("step")
        grant = kernel.active_control
        assert grant is not None
        # 实时核按生命线释放后以 0 退出：此后没有实例在运行，等待立即完成。
        assert (await link.stop()).returncode == 0
        await asyncio.wait_for(link.wait_released(grant), WAIT)
        await kernel.shutdown()
        assert kernel.status("step").state is State.DISPOSED

    asyncio.run(main())


# 真的杀掉主进程


class CoreWatch:
    """在主进程被杀之前打开实时核的句柄（Windows），之后取它的退出码。"""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._handle: Any = None
        self._kernel32: Any = None
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._kernel32.OpenProcess.restype = wintypes.HANDLE
            self._kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            synchronize, query_limited = 0x00100000, 0x1000
            self._handle = self._kernel32.OpenProcess(synchronize | query_limited, False, pid)
            assert self._handle, "打不开实时核的进程"

    def exit_code(self) -> int | None:
        """Windows 上的退出码；其它平台不是本进程的子进程，取不到，为 None。"""
        if self._handle is None:
            return None
        import ctypes
        from ctypes import wintypes

        code = wintypes.DWORD()
        assert self._kernel32.GetExitCodeProcess(self._handle, ctypes.byref(code))
        return int(code.value)

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def read_line(stream: Any, timeout: float) -> str:
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: lines.put(stream.readline()), daemon=True).start()
    return lines.get(timeout=timeout)


def test_killing_the_main_process_lets_the_core_release_and_exit(
    core_exe: Path, main_process: Path, tmp_path: Path, gone: Callable[[int, float], bool]
) -> None:
    log_path = tmp_path / "main.log"
    with log_path.open("wb") as log:
        main = subprocess.Popen(
            [sys.executable, str(main_process), str(core_exe)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=log,
            encoding="utf-8",
        )
    core_pid: int | None = None
    watch: CoreWatch | None = None
    try:
        assert main.stdin is not None
        assert main.stdout is not None
        line = read_line(main.stdout, 60)
        assert line.startswith("core "), log_path.read_text(errors="replace")
        core_pid = int(line.split()[1])
        watch = CoreWatch(core_pid)

        if sys.platform == "win32":
            # 主进程崩溃时系统关闭它的全部句柄。这里先单独关闭它持有的 job 句柄：job 设了
            # KILL_ON_JOB_CLOSE，但实时核持有的那份让 job 不关闭，实时核照常运行、发 core.health。
            # 没有那份句柄时实时核在这一步就被系统终止（实测）。
            main.stdin.write("drop-job\n")
            main.stdin.flush()
            assert read_line(main.stdout, 10).strip() == "core alive"

        main.kill()
        main.wait(10)
        # 实时核没有随主进程被杀，而是按生命线释放全部输入后自己退出。
        assert gone(core_pid, 5), "实时核没有退出"
        text = log_path.read_text(encoding="utf-8", errors="replace")
        assert "released all inputs, exiting" in text, text
        assert "core connection" in text or "stdin closed" in text, text
        if sys.platform == "win32":
            assert watch.exit_code() == 0
    finally:
        if main.poll() is None:
            main.kill()
            main.wait(10)
        if core_pid is not None and not gone(core_pid, 0):
            with contextlib.suppress(OSError):
                kill(core_pid)
        if watch is not None:
            watch.close()
        for stream in (main.stdin, main.stdout):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()


def test_options_are_checked() -> None:
    with pytest.raises(ValueError, match="必须为正"):
        CoreLinkOptions(Path("core"), heartbeat_interval=0.0)
    with pytest.raises(ValueError, match="心跳超时须大于心跳周期"):
        CoreLinkOptions(Path("core"), heartbeat_timeout=0.05)
    with pytest.raises(ValueError, match="健康超时须大于健康周期"):
        CoreLinkOptions(Path("core"), health_timeout=1.0)


def test_options_build_the_command_line() -> None:
    options = CoreLinkOptions(Path("core"), heartbeat_timeout=0.25, health_interval=0.0505)
    assert options.argv() == [
        "core",
        "--heartbeat-timeout-ms",
        "250",
        "--health-interval-ms",
        "51",
    ]
