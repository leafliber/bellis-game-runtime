"""统一检查入口（`docs/architecture.md` 的“仓库与目录”）。

`uv run check` 在本机依次运行 `STEPS` 的每一项，全部跑完后汇总，任一项失败即以非零退出。
`uv run check --linux` 在本机 Docker 的 Linux 容器里跑同一检查：仓库以 bind mount 挂进容器，
容器的虚拟环境、cargo target 与各工具的缓存都放在 Docker 卷里，不写回仓库。
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# 本文件位于 tools/check/src/game_runtime_check/，工作区成员以可编辑方式安装。
REPO_ROOT = Path(__file__).resolve().parents[4]
PYTHON = sys.executable


@dataclass(frozen=True)
class Step:
    """一项检查：在仓库根下的 `cwd` 里、附加环境变量 `env` 运行 `argv`，退出码为 0 即通过。"""

    name: str
    argv: tuple[str, ...]
    cwd: str = "."
    env: tuple[tuple[str, str], ...] = ()


# 检查项与顺序见 docs/architecture.md 的“仓库与目录”。
STEPS: tuple[Step, ...] = (
    Step("ruff format", (PYTHON, "-m", "ruff", "format", "--check", ".")),
    Step("ruff check", (PYTHON, "-m", "ruff", "check", ".")),
    # 不让 pyright 的 PyPI 包每次联网查询新版本；版本由 uv.lock 锁定。
    Step(
        "pyright",
        (PYTHON, "-m", "pyright", "--pythonpath", PYTHON),
        env=(("PYRIGHT_PYTHON_IGNORE_WARNINGS", "1"),),
    ),
    Step("cargo fmt", ("cargo", "fmt", "--all", "--check"), cwd="core"),
    Step(
        "cargo clippy",
        ("cargo", "clippy", "--workspace", "--all-targets", "--locked", "--", "-D", "warnings"),
        cwd="core",
    ),
    Step("cargo test", ("cargo", "test", "--workspace", "--locked"), cwd="core"),
    Step("pytest", (PYTHON, "-m", "pytest")),
    # 生成物核对：重新生成到临时目录，与入库的逐字节比较（game_runtime_check.generate）。
    Step("generated files", (PYTHON, "-m", "game_runtime_check.generate", "--check")),
)

# --linux 用的镜像与卷。下载缓存各检出目录共用；虚拟环境、cargo target 与工具缓存按检出目录
# 各用一个卷，以免多个工作树同时检查时互相改写。
LINUX_IMAGE = "bellis-game-runtime-check"
LINUX_DOCKERFILE_DIR = "tools/check"
LINUX_SHARED_VOLUMES = (
    ("bellis-game-runtime-check-uv-cache", "/cache/uv"),
    ("bellis-game-runtime-check-cargo-registry", "/usr/local/cargo/registry"),
)
LINUX_WORK_VOLUME_PREFIX = "bellis-game-runtime-check-work-"
LINUX_WORK_VOLUME_TARGET = "/cache/work"


@dataclass(frozen=True)
class Outcome:
    step: Step
    returncode: int
    seconds: float

    @property
    def passed(self) -> bool:
        return self.returncode == 0


def run_step(step: Step, root: Path) -> Outcome:
    """运行一项检查；找不到程序或无法启动也记为失败，不中断后续检查。"""
    print(f"\n==> {step.name}: {' '.join(step.argv)}", flush=True)
    start = time.monotonic()
    program = shutil.which(step.argv[0])
    if program is None:
        print(f"{step.argv[0]}: not found on PATH", file=sys.stderr, flush=True)
        returncode = 127
    else:
        argv = [program, *step.argv[1:]]
        env = {**os.environ, **dict(step.env)}
        try:
            with subprocess.Popen(argv, cwd=root / step.cwd, env=env) as process:
                returncode = process.wait()
        except OSError as error:
            print(f"{step.argv[0]}: {error}", file=sys.stderr, flush=True)
            returncode = 126
    return Outcome(step, returncode, time.monotonic() - start)


def run_all(steps: Sequence[Step], root: Path) -> list[Outcome]:
    return [run_step(step, root) for step in steps]


def summarize(outcomes: Sequence[Outcome]) -> int:
    """打印汇总；全部通过返回 0，否则返回 1。"""
    print("\n==> summary", flush=True)
    for outcome in outcomes:
        status = "PASS" if outcome.passed else f"FAIL (exit {outcome.returncode})"
        print(f"  {status:<16} {outcome.step.name} ({outcome.seconds:.1f}s)", flush=True)
    return 0 if all(outcome.passed for outcome in outcomes) else 1


def linux_work_volume(root: Path) -> str:
    digest = hashlib.sha256(os.path.normcase(root.resolve()).encode()).hexdigest()
    return LINUX_WORK_VOLUME_PREFIX + digest[:12]


def linux_steps(root: Path) -> tuple[Step, Step]:
    """构建检查镜像、在容器里以 `uv run --locked check` 运行同一检查的两步。

    `--locked` 让 uv 在 `uv.lock` 过期时报错而不是改写它；cargo 各项自带 `--locked`。
    """
    build = Step("docker build", ("docker", "build", "--tag", LINUX_IMAGE, LINUX_DOCKERFILE_DIR))
    mounts = [
        f"type=bind,source={root},target=/work",
        f"type=volume,source={linux_work_volume(root)},target={LINUX_WORK_VOLUME_TARGET}",
        *(f"type=volume,source={name},target={target}" for name, target in LINUX_SHARED_VOLUMES),
    ]
    run = Step(
        "docker run",
        (
            "docker",
            "run",
            "--rm",
            *(arg for mount in mounts for arg in ("--mount", mount)),
            LINUX_IMAGE,
            "uv",
            "run",
            "--locked",
            "check",
        ),
    )
    return build, run


def check_linux(root: Path) -> int:
    build, run = linux_steps(root)
    outcomes = [run_step(build, root)]
    if outcomes[0].passed:
        outcomes.append(run_step(run, root))
    return summarize(outcomes)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check", description="Run the full repository check.")
    parser.add_argument(
        "--linux",
        action="store_true",
        help="run the same check in a Linux container on the local Docker engine",
    )
    args = parser.parse_args(argv)
    if args.linux:
        return check_linux(REPO_ROOT)
    return summarize(run_all(STEPS, REPO_ROOT))
