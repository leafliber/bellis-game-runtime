"""检查入口的规则：每一项都要跑，最后汇总，任一项失败即以非零退出；Linux 镜像与本机锁定同一版本。"""

import re
import sys
import tomllib
from pathlib import Path

from game_runtime_check import (
    LINUX_DOCKERFILE_DIR,
    REPO_ROOT,
    Step,
    linux_work_volume,
    run_all,
    run_step,
    summarize,
)


def python_step(name: str, code: str, cwd: str = ".") -> Step:
    return Step(name, (sys.executable, "-c", code), cwd)


def test_steps_after_a_failure_still_run_and_the_check_fails(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    steps = [
        python_step("fails", "raise SystemExit(3)"),
        python_step("runs anyway", f"import pathlib; pathlib.Path({str(marker)!r}).touch()"),
    ]

    outcomes = run_all(steps, tmp_path)

    assert [outcome.returncode for outcome in outcomes] == [3, 0]
    assert marker.exists()
    assert summarize(outcomes) == 1


def test_check_passes_only_when_every_step_passes(tmp_path: Path) -> None:
    outcomes = run_all([python_step("first", "pass"), python_step("second", "pass")], tmp_path)

    assert summarize(outcomes) == 0


def test_missing_program_fails_the_step_without_stopping_the_check(tmp_path: Path) -> None:
    steps = [Step("missing", ("bellis-no-such-program",)), python_step("next", "pass")]

    outcomes = run_all(steps, tmp_path)

    assert [outcome.returncode for outcome in outcomes] == [127, 0]
    assert summarize(outcomes) == 1


def test_step_runs_in_its_directory_under_the_root(tmp_path: Path) -> None:
    (tmp_path / "core").mkdir()
    code = "import pathlib, sys; sys.exit(pathlib.Path.cwd().name != 'core')"

    assert run_step(python_step("cwd", code, cwd="core"), tmp_path).passed


def test_step_environment_is_added_to_the_inherited_one(tmp_path: Path) -> None:
    code = (
        "import os, sys; sys.exit(os.environ['CHECK_PROBE'] != 'set' or 'PATH' not in os.environ)"
    )
    step = Step("env", (sys.executable, "-c", code), env=(("CHECK_PROBE", "set"),))

    assert run_step(step, tmp_path).passed


def test_each_checkout_gets_its_own_linux_work_volume(tmp_path: Path) -> None:
    first = linux_work_volume(tmp_path / "first")
    second = linux_work_volume(tmp_path / "second")

    assert first != second
    for name in (first, second):
        assert re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+", name), name


def test_linux_image_pins_the_same_rust_and_python_as_the_repository() -> None:
    toolchain = tomllib.loads((REPO_ROOT / "rust-toolchain.toml").read_text(encoding="utf-8"))
    rust = toolchain["toolchain"]["channel"]
    python = (REPO_ROOT / ".python-version").read_text(encoding="utf-8").strip()
    dockerfile = (REPO_ROOT / LINUX_DOCKERFILE_DIR / "Dockerfile").read_text(encoding="utf-8")

    assert re.search(rf"^FROM rust:{re.escape(rust)}-slim-\w+$", dockerfile, re.MULTILINE)
    assert re.search(rf"^RUN uv python install {re.escape(python)}$", dockerfile, re.MULTILINE)
