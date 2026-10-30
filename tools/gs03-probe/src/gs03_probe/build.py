"""编译提权一侧的 Rust 探针 `gs03-core-probe`，返回可执行文件路径。尊重 `CARGO_TARGET_DIR`。"""

import json
import shutil
import subprocess
from pathlib import Path

PACKAGE = "gs03-probe"
BINARY = "gs03-core-probe"


def build_probe(repo_root: Path, *, release: bool = False) -> Path:
    """`cargo build -p gs03-probe --bin gs03-core-probe`，从 cargo 的 JSON 输出取可执行文件路径。"""
    cargo = shutil.which("cargo")
    if cargo is None:
        raise RuntimeError("PATH 上没有 cargo，无法编译提权一侧探针")
    argv = [cargo, "build", "--locked", "-p", PACKAGE, "--bin", BINARY, "--message-format=json"]
    if release:
        argv.insert(2, "--release")
    with subprocess.Popen(
        argv,
        cwd=repo_root / "core",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    ) as process:
        stdout, stderr = process.communicate(timeout=600)
    if process.returncode != 0:
        raise RuntimeError(f"编译 {BINARY} 失败：\n{stderr}")
    for line in stdout.splitlines():
        message = json.loads(line)
        if (
            message.get("reason") == "compiler-artifact"
            and message.get("target", {}).get("name") == BINARY
            and message.get("executable")
        ):
            return Path(message["executable"])
    raise RuntimeError(f"cargo 没有给出 {BINARY} 的路径")
