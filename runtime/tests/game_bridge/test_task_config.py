"""维护者指定的任务配置及显式建账。"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from pydantic import ValidationError

from game_runtime.game_bridge import ledger_cli
from game_runtime.game_bridge.task_config import read_task_config
from game_runtime.game_bridge.task_ledger import LedgerUnavailableError, TaskLedger, TaskLimits


def config_data(ledger_path: Path) -> dict[str, object]:
    return {
        "control_target_id": "configured-slot",
        "adapter_id": "synthetic-game",
        "ledger_path": str(ledger_path),
        "limits": {"calls": 4, "inputs": 6, "no_progress_ms": 2100},
    }


@pytest.mark.parametrize("field", ["control_target_id", "adapter_id", "ledger_path", "limits"])
def test_config_requires_every_top_level_field(tmp_path: Path, field: str) -> None:
    config = config_data(tmp_path / "ledger.sqlite3")
    config.pop(field)
    path = tmp_path / "task.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValidationError, match="Field required"):
        read_task_config(path)


@pytest.mark.parametrize(
    "override",
    [
        {"control_target_id": "bad id"},
        {"adapter_id": "../synthetic-game"},
        {"ledger_path": "relative.sqlite3"},
        {"limits": {"calls": 0, "no_progress_ms": 2500}},
        {"limits": {"calls": True, "inputs": 1, "no_progress_ms": 2500}},
        {"limits": {"calls": 1, "inputs": -1, "no_progress_ms": 2500}},
        {"limits": {"calls": 1, "inputs": 1, "no_progress_ms": 0}},
        {"limits": {"calls": 1, "inputs": 1, "no_progress_ms": 2500, "reset": True}},
    ],
)
def test_config_rejects_invalid_identity_path_or_limits(
    tmp_path: Path, override: dict[str, object]
) -> None:
    ledger_path = tmp_path / "ledger.sqlite3"
    path = tmp_path / "task.json"
    path.write_text(json.dumps(config_data(ledger_path) | override), encoding="utf-8")
    with pytest.raises(ValueError, match=r"被控对象|绝对路径|预算|validation error"):
        read_task_config(path)
    assert not ledger_path.exists()


def test_only_explicit_provision_creates_ledger_and_repeat_keeps_original(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger_path = tmp_path / "ledger.sqlite3"
    alias_dir = tmp_path / "alias"
    alias_dir.mkdir()
    path = tmp_path / "task.json"
    config = config_data(alias_dir / ".." / ledger_path.name)
    path.write_text(json.dumps(config), encoding="utf-8")
    authority = read_task_config(path)
    assert authority.ledger_path == ledger_path.resolve()
    assert authority.limits == TaskLimits(calls=4, inputs=6, no_progress_ns=2_100_000_000)
    with pytest.raises(LedgerUnavailableError, match="路径不可用"):
        TaskLedger.start(authority.ledger_path, authority.control_target_id)
    assert not ledger_path.exists()

    assert ledger_cli.main(["provision", str(path)]) == 0
    original = ledger_path.read_bytes()
    config["control_target_id"] = "other-slot"
    path.write_text(json.dumps(config), encoding="utf-8")
    assert ledger_cli.main(["provision", str(path)]) == 2
    assert "路径已存在" in capsys.readouterr().err
    assert ledger_path.read_bytes() == original
    wrong = read_task_config(path)
    with pytest.raises(LedgerUnavailableError, match="被控对象不符"):
        TaskLedger.start(wrong.ledger_path, wrong.control_target_id)
    current = TaskLedger.start(authority.ledger_path, authority.control_target_id)
    assert current.status().active_task is None


def test_provision_checks_adapter_before_creating_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger_path = tmp_path / "ledger.sqlite3"
    path = tmp_path / "task.json"
    config = config_data(ledger_path) | {"adapter_id": "fixture-missing-adapter"}
    path.write_text(json.dumps(config), encoding="utf-8")
    assert ledger_cli.main(["provision", str(path)]) == 2
    assert "fixture-missing-adapter" in capsys.readouterr().err
    assert not ledger_path.exists()


def test_concurrent_provision_creates_one_usable_ledger(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.sqlite3"
    path = tmp_path / "task.json"
    path.write_text(json.dumps(config_data(ledger_path)), encoding="utf-8")
    ready = Barrier(2, timeout=5)

    def provision() -> int:
        ready.wait()
        return ledger_cli.main(["provision", str(path)])

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(provision), pool.submit(provision)
        assert sorted((first.result(), second.result())) == [0, 2]
    authority = read_task_config(path)
    current = TaskLedger.start(authority.ledger_path, authority.control_target_id)
    assert current.status().outstanding is None
