"""主进程管理的子进程：启动与 R-06 停止顺序，对应 Bellis `packages/process` 的 `ManagedProcess`。

设计见 `docs/modules/realtime-core.md` 的“启动与生命线”：

- 一律以 `subprocess.Popen` 和参数列表直接启动，不经 shell，不脱离父进程；
  环境变量只有系统必需的与显式给出的。
- stdin 是生命线：管道，不传数据；子进程读到 EOF 即释放后退出。
- Windows 上子进程挂起创建，放进 Job Object 后才恢复运行（`_win32`）。
- 停止顺序：停止请求（RPC）→ 关 stdin → 超时强制终止，不依赖信号；停在哪一步记日志。
"""

import asyncio
import contextlib
import logging
import os
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import IO, Any, Protocol

if sys.platform == "win32":
    from game_runtime.core_link import _win32

logger = logging.getLogger(__name__)


class _Job(Protocol):
    """管住子进程与孙进程的 Job Object（`_win32.JobObject`），只在 Windows 上有。"""

    def active_processes(self) -> int: ...
    def terminate(self, exit_code: int) -> None: ...
    def close(self) -> None: ...


# 子进程需要的系统环境变量，与 Bellis `baseEnvironment` 相同。Windows 缺少 SYSTEMROOT 等变量时
# Winsock 初始化会失败。
_WINDOWS_KEYS = frozenset(
    {
        "APPDATA",
        "COMPUTERNAME",
        "COMSPEC",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERDOMAIN",
        "USERNAME",
        "USERPROFILE",
        "WINDIR",
    }
)
_POSIX_KEYS = frozenset(
    {
        "DBUS_SESSION_BUS_ADDRESS",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "PATH",
        "SHELL",
        "TMPDIR",
        "TZ",
        "USER",
        "XDG_RUNTIME_DIR",
    }
)


def base_environment(
    source: Mapping[str, str] | None = None, *, windows: bool = sys.platform == "win32"
) -> dict[str, str]:
    """从 `source`（缺省为当前环境）中挑出子进程必需的系统变量，其余一概不继承。

    Windows 的环境变量名不区分大小写，按大写比较、保留原名。
    """
    env = os.environ if source is None else source
    keys = _WINDOWS_KEYS if windows else _POSIX_KEYS
    return {k: v for k, v in env.items() if (k.upper() if windows else k) in keys}


class StopStage(StrEnum):
    """子进程在停止顺序的哪一步退出。"""

    ALREADY_EXITED = "already-exited"
    """停止之前就已退出。"""
    SHUTDOWN = "shutdown"
    """收到停止请求后自行退出。"""
    STDIN_EOF = "stdin-eof"
    """关闭 stdin 后自行退出。"""
    KILLED = "killed"
    """被强制终止。"""


@dataclass(frozen=True, slots=True)
class StopTimeouts:
    """停止各步的时限，单位为秒。缺省值同 Bellis（每步 2 秒），依据见模块文档。"""

    shutdown: float = 2.0
    """第 1 步：从发出停止请求到子进程退出。"""
    stdin: float = 2.0
    """第 2 步：从关闭 stdin 到子进程退出。"""
    kill: float = 5.0
    """第 3 步：从强制终止到确认退出；超过只记录错误。"""

    def __post_init__(self) -> None:
        if min(self.shutdown, self.stdin, self.kill) <= 0:
            raise ValueError("停止的时限必须为正")


@dataclass(frozen=True, slots=True)
class StopResult:
    stage: StopStage
    returncode: int | None
    """退出码；强制终止后仍未确认退出时为 None。"""


type Redirect = int | IO[Any] | None
"""子进程的 stdout 或 stderr：缺省继承主进程，也可以是 `subprocess.DEVNULL` 或打开的文件。"""


class ManagedProcess:
    """主进程管理的子进程。以 `start` 创建，须在事件循环中使用。"""

    def __init__(
        self,
        name: str,
        popen: "subprocess.Popen[bytes]",
        job: _Job | None,
    ) -> None:
        self.name = name
        self._popen = popen
        self._job = job
        self._loop = asyncio.get_running_loop()
        self.exited: asyncio.Future[int] = self._loop.create_future()
        """子进程退出时完成，结果是退出码；不会失败。"""
        threading.Thread(target=self._wait, name=f"{name}-exit", daemon=True).start()

    @classmethod
    def start(
        cls,
        name: str,
        argv: Sequence[str],
        env: Mapping[str, str],
        *,
        stdout: Redirect = None,
        stderr: Redirect = None,
    ) -> "ManagedProcess":
        """启动子进程；可执行文件不存在等情况下抛 OSError。"""
        creationflags = 0
        if sys.platform == "win32":
            creationflags = _win32.CREATE_SUSPENDED
        popen = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            env=dict(env),
            creationflags=creationflags,
        )
        job: _Job | None = None
        try:
            if sys.platform == "win32":
                job = windows_job = _win32.JobObject()
                windows_job.adopt(popen.pid)
                _win32.resume_suspended(popen.pid)
        except BaseException:
            popen.kill()
            popen.wait()
            _close(popen)
            if job is not None:
                job.close()
            raise
        return cls(name, popen, job)

    @property
    def pid(self) -> int:
        return self._popen.pid

    @property
    def returncode(self) -> int | None:
        return self.exited.result() if self.exited.done() else None

    def active_processes(self) -> int | None:
        """Windows 上 job 里还在运行的进程数（子进程与它的孙进程）。

        其它平台没有 job，子进程退出后 job 已关闭，都为 None。
        """
        return None if self._job is None else self._job.active_processes()

    async def stop(
        self,
        shutdown: Callable[[], Awaitable[object]] | None = None,
        timeouts: StopTimeouts | None = None,
    ) -> StopResult:
        """按 R-06 的顺序停止：`shutdown`（停止请求）→ 关 stdin → 强制终止，返回在哪一步退出。

        `shutdown` 失败（连接已断、超时、对端报错）不影响后续步骤。
        """
        timeouts = timeouts or StopTimeouts()
        if self.exited.done():
            return StopResult(StopStage.ALREADY_EXITED, self.exited.result())
        if shutdown is not None:
            deadline = self._loop.time() + timeouts.shutdown
            try:
                async with asyncio.timeout_at(deadline):
                    await shutdown()
            except Exception as error:
                logger.info(
                    "%s 的停止请求未完成：%s", self.name, str(error) or type(error).__name__
                )
            if await self._wait_exit(deadline - self._loop.time()):
                return self._stopped(StopStage.SHUTDOWN)
        _close(self._popen)
        if await self._wait_exit(timeouts.stdin):
            return self._stopped(StopStage.STDIN_EOF)
        logger.warning("%s 关闭 stdin 后 %g 秒内仍未退出，强制终止", self.name, timeouts.stdin)
        self.kill()
        if not await self._wait_exit(timeouts.kill):
            logger.error("%s 强制终止后 %g 秒内仍未退出", self.name, timeouts.kill)
            return StopResult(StopStage.KILLED, None)
        return self._stopped(StopStage.KILLED)

    def kill(self) -> None:
        """立即强制终止，不经收尾。Windows 上终止整个 job，连同孙进程。"""
        if self.exited.done():
            return
        if self._job is not None:
            with contextlib.suppress(OSError):
                self._job.terminate(1)
                return
        with contextlib.suppress(OSError):
            self._popen.kill()

    def _stopped(self, stage: StopStage) -> StopResult:
        result = StopResult(stage, self.exited.result())
        logger.info("%s 在“%s”一步退出，退出码 %s", self.name, stage, result.returncode)
        return result

    async def _wait_exit(self, seconds: float) -> bool:
        if not self.exited.done() and seconds > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(self.exited), seconds)
        return self.exited.done()

    def _wait(self) -> None:
        returncode = self._popen.wait()
        with contextlib.suppress(RuntimeError):  # 事件循环已关闭
            self._loop.call_soon_threadsafe(self._on_exit, returncode)

    def _on_exit(self, returncode: int) -> None:
        _close(self._popen)
        if self._job is not None:
            # 子进程已退出；job 里剩下的只能是它的孙进程，一并终止，再关闭本进程的句柄。
            try:
                if self._job.active_processes() > 0:
                    logger.warning("%s 退出后 job 里仍有进程，终止它们", self.name)
                    self._job.terminate(1)
            except OSError:
                logger.exception("%s 的 job 清理失败", self.name)
            self._job.close()
            self._job = None
        if not self.exited.done():
            self.exited.set_result(returncode)


def _close(popen: "subprocess.Popen[bytes]") -> None:
    """关闭 stdin 管道：子进程读到 EOF。子进程已退出时写端可能报错，忽略。"""
    if popen.stdin is not None and not popen.stdin.closed:
        with contextlib.suppress(OSError):
            popen.stdin.close()
