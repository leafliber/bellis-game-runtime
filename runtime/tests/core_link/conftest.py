"""实时核测试的共用夹具：编译 `realtime-core`，以及按进程号确认进程已退出。"""

import json
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[3] / "core"
HERE = Path(__file__).resolve().parent


def cargo_binary(package: str, binary: str) -> Path:
    """编译 `package` 的可执行文件 `binary` 并返回路径。

    尊重 CARGO_TARGET_DIR：由 cargo 自己读取，路径取自 cargo 的输出。
    """
    cargo = shutil.which("cargo")
    assert cargo is not None, "PATH 上没有 cargo"
    argv = [cargo, "build", "--locked", "-p", package, "--bin", binary, "--message-format=json"]
    with subprocess.Popen(
        argv,
        cwd=CORE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    ) as process:
        stdout, stderr = process.communicate(timeout=600)
    assert process.returncode == 0, stderr
    for line in stdout.splitlines():
        message = json.loads(line)
        if (
            message.get("reason") == "compiler-artifact"
            and message["target"]["name"] == binary
            and "bin" in message["target"]["kind"]
            and message.get("executable")
        ):
            return Path(message["executable"])
    raise AssertionError(f"cargo 没有给出 {binary} 的路径")


@pytest.fixture(scope="session")
def core_exe() -> Path:
    return cargo_binary("realtime-core", "realtime-core")


@pytest.fixture(scope="session")
def standin() -> Path:
    """替身子进程的脚本，见 `standin.py`。"""
    return HERE / "standin.py"


@pytest.fixture(scope="session")
def main_process() -> Path:
    """跑主进程一侧代码的子进程脚本，见 `main_process.py`。"""
    return HERE / "main_process.py"


def _gone(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.restype = wintypes.HANDLE
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        wait = kernel32.WaitForSingleObject
        wait.restype = wintypes.DWORD
        wait.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        synchronize = 0x00100000
        handle = open_process(synchronize, False, pid)
        if not handle:
            return True  # 进程号已不存在
        try:
            return wait(handle, 0) == 0  # WAIT_OBJECT_0：已退出
        finally:
            kernel32.CloseHandle(handle)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return True
    # 父进程已不在时孤儿由容器的 1 号进程收养，它不一定回收，已退出的进程会停在僵尸状态。
    return stat.rsplit(")", 1)[1].split()[0] == "Z"


def wait_gone(pid: int, timeout: float) -> bool:
    """等到进程退出，至多 `timeout` 秒；返回是否已退出。"""
    deadline = time.monotonic() + timeout
    while not _gone(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


@pytest.fixture
def gone() -> Callable[[int, float], bool]:
    """`gone(pid, timeout)`：等到进程退出，返回是否已退出。"""
    return wait_gone
