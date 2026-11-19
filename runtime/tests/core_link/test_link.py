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
from unittest.mock import Mock

import pytest

from core_protocol import models
from core_protocol.models import (
    CoreHealthParams,
    InputReleasedParams,
    InputVerificationParams,
    Producer,
    ReleaseReason,
    VerificationResult,
)
from game_runtime.channel import ChannelError
from game_runtime.core_link import (
    CoreLink,
    CoreLinkOptions,
    RestartPolicy,
    StopResult,
    StopStage,
    shutdown_runtime,
)
from game_runtime.core_link.link import (
    _Instance,  # pyright: ignore[reportPrivateUsage]
    _Submission,  # pyright: ignore[reportPrivateUsage]
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


@pytest.mark.parametrize("conflicting", [False, True])
def test_receipt_matches_the_original_producer_and_action(
    conflicting: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        producer = Producer(id="controller", generation=1)
        block = models.ActionBlock(
            producer=producer,
            seq=1,
            base_frame=1,
            start_ns=10,
            deadline_ns=100,
            steps=[
                models.ControlStep(
                    offset_ns=0,
                    held=[],
                    actions=[models.Action(root=models.KeyPress(id=1, scan_code=1))],
                )
            ],
        )
        link = CoreLink(link_options(Path("realtime-core")))
        instance = Mock(spec=_Instance, hello_at_ns=10)
        monkeypatch.setattr(link, "_instance", instance)
        submission = _Submission(block, asyncio.get_running_loop().create_future())
        link._submissions[(producer.generation, block.seq)] = submission  # pyright: ignore[reportPrivateUsage]
        submission.resolve(models.Accepted())
        executed = models.Executed(id=1, at_ns=10)
        wrong_producer = models.ActionReceiptParams(
            producer=Producer(id="other", generation=producer.generation),
            block_seq=block.seq,
            outcomes=[models.ActionOutcome(root=executed)],
        )
        link._on_receipt_message(instance, wrong_producer.model_dump())  # pyright: ignore[reportPrivateUsage]
        assert not submission.settled

        outcomes = [models.ActionOutcome(root=executed)]
        if conflicting:
            outcomes.append(
                models.ActionOutcome(root=models.Dropped(id=1, reason=models.DropReason.RELEASED))
            )
        receipt = models.ActionReceiptParams(
            producer=producer, block_seq=block.seq, outcomes=outcomes
        )
        link._on_receipt_message(instance, receipt.model_dump())  # pyright: ignore[reportPrivateUsage]
        if conflicting:
            with pytest.raises(ChannelError, match="互相矛盾"):
                await submission.outcomes()
        else:
            assert await submission.outcomes() == {1: executed}

    asyncio.run(exercise())


def link_options(exe: Path) -> CoreLinkOptions:
    # 这些测试自己杀掉、自己重启实时核：关掉自动重启（它在下文的 GR-35 测试里单测）。
    return CoreLinkOptions(
        exe, heartbeat_interval=0.05, heartbeat_timeout=0.3, health_interval=0.05, restart=None
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
        self.releases: list[InputReleasedParams] = []

    def released(self, released: InputReleasedParams) -> None:
        self.releases.append(released)
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


def test_quiescing_waits_for_the_matching_physical_release(core_exe: Path) -> None:
    async def main() -> None:
        recorder, link, kernel = make_runtime(core_exe, KernelOptions(release_timeout=0.15))
        await link.start()
        async with stopped_at_end(link):
            # 子进程已按默认参数启动。只让主进程把替身回执当成真实输入模式的回执处理。
            object.__setattr__(link._options, "real_input", True)  # pyright: ignore[reportPrivateUsage]
            kernel.add([STEP])
            await kernel.set_control_producer("step")
            await until(recorder.producer_is(Producer(id="step", generation=1)))
            await kernel.disable("step")
            assert kernel.status("step").state is State.QUIESCING
            assert "revoke step#1" not in recorder.log

            release = next(
                r for r in recorder.releases if r.reason is ReleaseReason.GENERATION_REVOKED
            )
            instance = link._instance  # pyright: ignore[reportPrivateUsage]
            assert instance is not None

            def observed(release_id: int) -> None:
                params = InputVerificationParams(
                    release_id=release_id,
                    result=VerificationResult.VERIFIED,
                    checked_at_ns=time.perf_counter_ns(),
                ).model_dump(mode="json")
                link._on_verification_message(instance, params)  # pyright: ignore[reportPrivateUsage]

            observed(release.release_id - 1)
            await asyncio.sleep(0)
            assert kernel.status("step").state is State.QUIESCING
            observed(release.release_id)
            await until(lambda: kernel.status("step").state is State.DISPOSED)
            recorder.order("step quiescing", "released generation_revoked", "step unloading")

    asyncio.run(main())


@pytest.mark.parametrize("physical_check", [False, True])
def test_restarted_core_releases_first_and_completes_the_wait(
    core_exe: Path, physical_check: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def main() -> None:
        recorder, link, kernel = make_runtime(core_exe, KernelOptions(release_timeout=0.3))
        if physical_check:
            options = link._options  # pyright: ignore[reportPrivateUsage]
            default_argv = options.argv()

            def fake_argv(_: CoreLinkOptions) -> list[str]:
                return default_argv

            monkeypatch.setattr(CoreLinkOptions, "argv", fake_argv)
            object.__setattr__(options, "real_input", True)
        await link.start()
        old_instance = link._instance  # pyright: ignore[reportPrivateUsage]
        assert old_instance is not None
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

            # 真实输入需等启动编号的物理核验，假后端用启动回执完成等待。
            await link.start()
            if physical_check:
                assert kernel.status("step").state is State.QUIESCING
                instance = link._instance  # pyright: ignore[reportPrivateUsage]
                assert instance is not None
                params = InputVerificationParams(
                    release_id=recorder.releases[-1].release_id,
                    result=VerificationResult.VERIFIED,
                    checked_at_ns=time.perf_counter_ns(),
                ).model_dump(mode="json")
                link._on_verification_message(old_instance, params)  # pyright: ignore[reportPrivateUsage]
                await asyncio.sleep(0)
                assert kernel.status("step").state is State.QUIESCING
                link._on_verification_message(instance, params)  # pyright: ignore[reportPrivateUsage]
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
        assert "input release requested, exiting" in text, text
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


# GR-35 自动重启


def restart_options(exe: Path) -> CoreLinkOptions:
    return CoreLinkOptions(
        exe,
        heartbeat_interval=0.05,
        heartbeat_timeout=0.3,
        health_interval=0.05,
        restart=RestartPolicy(backoff=(0.05, 0.1, 0.2), reset_after=60.0),
    )


def test_auto_restart_after_a_crash(core_exe: Path) -> None:
    async def main() -> None:
        recorder = Recorder()
        restarts: list[int] = []
        link = CoreLink(
            restart_options(core_exe),
            on_released=recorder.released,
            on_health=recorder.on_health,
            on_restart=restarts.append,
        )
        await link.start()
        async with stopped_at_end(link):
            await until(lambda: link.healthy)
            first = link.pid
            assert first is not None
            # 崩溃（被强杀，非 0 退出）：自动重启，on_restart 报一次，新实例就绪。
            kill(first)
            await until(lambda: restarts == [1])
            await until(lambda: link.running and link.pid not in (None, first))
            await until(lambda: link.healthy)
            # 重启后照常释放（core_start）再接受动作。
            assert recorder.log.count("released core_start") >= 2

    asyncio.run(main())


def test_auto_restart_gives_up_after_the_backoff(core_exe: Path) -> None:
    async def main() -> None:
        recorder = Recorder()
        restarts: list[int] = []
        link = CoreLink(
            restart_options(core_exe),  # 退避 3 次
            on_released=recorder.released,
            on_restart=restarts.append,
        )
        await link.start()
        async with stopped_at_end(link):
            # 连杀：每次都在 60 秒内，次数累积；用尽 3 次后不再重启。
            for attempt in range(1, 4):
                await until(lambda: link.healthy)
                pid = link.pid
                assert pid is not None
                kill(pid)
                await until(lambda a=attempt: restarts[-1:] == [a] if restarts else False)
                await until(lambda: link.running)
            await until(lambda: link.healthy)
            pid = link.pid
            assert pid is not None
            kill(pid)
            await until(lambda: not link.running)
            # 次数用尽：不再重启。
            await asyncio.sleep(0.6)
            assert restarts == [1, 2, 3]
            assert not link.running

    asyncio.run(main())


def test_a_clean_stop_does_not_auto_restart(core_exe: Path) -> None:
    async def main() -> None:
        restarts: list[int] = []
        link = CoreLink(restart_options(core_exe), on_restart=restarts.append)
        await link.start()
        await until(lambda: link.healthy)
        # 主进程要求的停止（0 退出）：不自动重启。
        assert (await link.stop()).returncode == 0
        await asyncio.sleep(0.4)
        assert restarts == []
        assert not link.running

    asyncio.run(main())


def test_auto_restart_is_on_by_default() -> None:
    """GR-35 的自动重启缺省开着：退避 1、2、4 秒，就绪满 60 秒清零。"""
    assert CoreLinkOptions(Path("core")).restart == RestartPolicy(
        backoff=(1.0, 2.0, 4.0), reset_after=60.0
    )


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
        "--max-frame-age-ms",
        "500",
    ]


def test_real_input_options_are_gated_and_build_the_command_line() -> None:
    from game_runtime.channel import session_credentials

    with session_credentials("opt") as operator:
        # 真实后端须与操作员许可连接一起、不给测试设备。
        with pytest.raises(ValueError, match="操作员许可连接"):
            CoreLinkOptions(Path("core"), real_input=True)
        with pytest.raises(ValueError, match="不能同时给测试设备"):
            CoreLinkOptions(Path("core"), real_input=True, operator=operator, test_device=operator)
        # 急停组合键只与真实后端一起给出。
        with pytest.raises(ValueError, match="急停组合键"):
            CoreLinkOptions(Path("core"), emergency_hotkey="ctrl+shift+f12")
        options = CoreLinkOptions(
            Path("core"),
            real_input=True,
            operator=operator,
            emergency_hotkey="ctrl+alt+end",
        )
        assert options.argv()[-4:] == [
            "--input-backend",
            "sendinput",
            "--emergency-hotkey",
            "ctrl+alt+end",
        ]
        # 没给急停组合键时只加后端，交给实时核取缺省。
        plain = CoreLinkOptions(Path("core"), real_input=True, operator=operator)
        assert plain.argv()[-2:] == ["--input-backend", "sendinput"]
