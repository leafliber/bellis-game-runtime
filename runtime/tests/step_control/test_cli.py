"""模型游玩只从操作员指定的 profile 组装。"""

import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from game_runtime.adapter import GAMES_DIR
from game_runtime.core_link import CoreLinkOptions
from game_runtime.game_bridge.session import BellisConfig
from game_runtime.game_bridge.task_config import read_task_config
from game_runtime.kernel import ResolvedEntry, read_profile, resolve_profile
from game_runtime.secret_store import MemorySecretStore, SecretStore
from game_runtime.step_control import cli, supervise

EXAMPLE = GAMES_DIR.parent / "profiles" / "supervised.example.json"


@pytest.mark.parametrize("enable_input", [False, True])
@pytest.mark.parametrize("enable_record", [False, True])
def test_supervise_cli_resolves_patches_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enable_input: bool, enable_record: bool
) -> None:
    enable_voice = tmp_path / "enable.json"
    disable_voice = tmp_path / "disable.json"
    enable_voice.write_text(
        '{"name":"enable","ops":[{"op":"enable","id":"voice"},{"op":"disable","id":"narrative"}]}',
        encoding="utf-8",
    )
    disable_voice.write_text(
        '{"name":"disable","ops":[{"op":"disable","id":"voice"}]}', encoding="utf-8"
    )
    executable = tmp_path / "core"
    store = MemorySecretStore()

    async def launched(
        game: str,
        entries: Sequence[ResolvedEntry],
        core: Path,
        data_dir: Path,
        selected_store: SecretStore,
        *,
        real_input: bool,
        record: bool,
        name: str | None,
    ) -> int:
        assert game == "synthetic-game"
        assert core == executable
        assert data_dir == tmp_path
        assert selected_store is store
        assert real_input is enable_input
        assert record is enable_record
        assert name == ("local-run" if enable_record else None)
        voice = next(entry for entry in entries if entry.id == "voice")
        assert voice.disabled
        assert voice.grant == frozenset({"audio.read"})
        assert next(entry for entry in entries if entry.id == "narrative").disabled
        return 0

    monkeypatch.setattr(cli, "CredentialManagerStore", lambda: store)
    monkeypatch.setattr(cli, "_run", launched)
    args = [
        "synthetic-game",
        "--profile",
        str(EXAMPLE),
        "--patch",
        str(enable_voice),
        "--patch",
        str(disable_voice),
        "--core",
        str(executable),
        "--data-dir",
        str(tmp_path),
    ]
    if enable_input:
        args.append("--real-input")
    if enable_record:
        args.extend(("--record", "--name", "local-run"))
    assert cli.main(args) == 0


def test_profile_cannot_change_the_selected_game(tmp_path: Path) -> None:
    entries = resolve_profile(read_profile(EXAMPLE))
    entries[0] = replace(entries[0], config={"game": "witch-trial"})

    async def scenario() -> None:
        with pytest.raises(ValueError, match="不同的游戏适配包"):
            async with supervise(
                "synthetic-game",
                None,
                CoreLinkOptions(Path(sys.executable)),
                data_dir=tmp_path,
                store=MemorySecretStore(),
                profile=entries,
            ):
                pytest.fail("不一致的适配包通过了启动校验")

    asyncio.run(scenario())


@pytest.mark.parametrize("configured", [False, True])
def test_supervise_cli_explicit_telemetry_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: bool
) -> None:
    profile = tmp_path / "profile.json"
    entries: list[dict[str, object]] = []
    if configured:
        entries.append(
            {
                "id": "local-traces",
                "plugin": "telemetry",
                "disabled": True,
                "config": {"directory": str(tmp_path / "old"), "max_queue": 12},
            }
        )
    profile.write_text(
        json.dumps({"name": "test", "packs": [{"name": "test", "entries": entries}]}),
        encoding="utf-8",
    )
    directory = tmp_path / "traces"

    async def launched(
        game: str,
        entries: Sequence[ResolvedEntry],
        core: Path,
        data_dir: Path,
        store: SecretStore,
        *,
        real_input: bool,
        record: bool,
        name: str | None,
    ) -> int:
        (entry,) = entries
        assert entry.id == ("local-traces" if configured else "telemetry")
        assert entry.plugin == "telemetry"
        assert not entry.disabled
        assert not entry.grant
        assert entry.config["directory"] == str(directory)
        assert entry.config.get("max_queue") == (12 if configured else None)
        assert not record
        assert name is None
        return 0

    monkeypatch.setattr(cli, "CredentialManagerStore", MemorySecretStore)
    monkeypatch.setattr(cli, "_run", launched)
    assert (
        cli.main(
            [
                "synthetic-game",
                "--profile",
                str(profile),
                "--core",
                str(tmp_path / "core"),
                "--data-dir",
                str(tmp_path),
                "--telemetry-dir",
                str(directory),
            ]
        )
        == 0
    )


def test_supervise_cli_recording_name_requires_record(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match="2"):
        cli.main(["synthetic-game", "--profile", str(EXAMPLE), "--name", "local-run"])
    assert "--name requires --record" in capsys.readouterr().err


def write_task_config(tmp_path: Path, *, adapter_id: str = "synthetic-game") -> Path:
    path = tmp_path / "task.json"
    path.write_text(
        json.dumps(
            {
                "control_target_id": "local-fixture",
                "adapter_id": adapter_id,
                "ledger_path": str(tmp_path / "ledger.sqlite3"),
                "limits": {"calls": 3, "inputs": 5, "no_progress_ms": 3100},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_task_config_requires_bellis(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match="2"):
        cli.main(
            [
                "synthetic-game",
                "--profile",
                str(EXAMPLE),
                "--task-config",
                str(tmp_path / "absent.json"),
            ]
        )
    assert "--task-config requires --bellis" in capsys.readouterr().err


def test_bellis_cli_passes_selected_configuration_without_touching_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = write_task_config(tmp_path)
    expected = read_task_config(path)
    store = MemorySecretStore()

    async def inspect(
        game: str,
        entries: Sequence[ResolvedEntry],
        core: Path | None,
        data_dir: Path,
        selected_store: SecretStore,
        *,
        real_input: bool,
        bellis: BellisConfig,
        record: bool,
        name: str | None,
    ) -> int:
        assert game == expected.adapter_id
        assert real_input
        assert selected_store is store
        assert bellis.session_store is store
        assert bellis.confirmed == frozenset({"safe"})
        assert bellis.task_authority == expected
        assert bellis.test_session_file is None
        assert core is None
        return 0

    def credential_store(_service: str = "") -> SecretStore:
        return store

    monkeypatch.setattr(cli, "CredentialManagerStore", credential_store)
    monkeypatch.setattr(cli, "_run", inspect)
    assert (
        cli.main(
            [
                "synthetic-game",
                "--profile",
                str(EXAMPLE),
                "--data-dir",
                str(tmp_path),
                "--bellis",
                "--real-input",
                "--confirm-precondition",
                "safe",
                "--task-config",
                str(path),
            ]
        )
        == 0
    )
    assert not expected.ledger_path.exists()


@pytest.mark.parametrize(
    ("adapter_id", "message"),
    [
        ("synthetic-game", "GR-38 v2 真实输入 unsupported"),
        ("witch-trial", "适配包与启动所选游戏不匹配"),
    ],
)
def test_task_config_cli_fails_closed_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    adapter_id: str,
    message: str,
) -> None:
    path = write_task_config(tmp_path, adapter_id=adapter_id)
    store = MemorySecretStore()

    def forbidden() -> None:
        pytest.fail("配置未获启动许可时不得编译或启动实时核")

    def credential_store(_service: str = "") -> SecretStore:
        return store

    monkeypatch.setattr(cli, "CredentialManagerStore", credential_store)
    monkeypatch.setattr(cli, "build_realtime_core", forbidden)
    monkeypatch.setattr(cli, "supervise", forbidden)
    assert (
        cli.main(
            [
                "synthetic-game",
                "--profile",
                str(EXAMPLE),
                "--data-dir",
                str(tmp_path),
                "--bellis",
                "--real-input",
                "--task-config",
                str(path),
            ]
        )
        == 2
    )
    assert message in capsys.readouterr().err
    assert not (tmp_path / "ledger.sqlite3").exists()


def test_direct_supervise_retains_task_real_input_gate(tmp_path: Path) -> None:
    async def run() -> None:
        store = MemorySecretStore()
        authority = read_task_config(write_task_config(tmp_path))
        with pytest.raises(ValueError, match="GR-38 v2 真实输入 unsupported"):
            async with supervise(
                "synthetic-game",
                None,
                CoreLinkOptions(Path(sys.executable)),
                data_dir=tmp_path,
                store=store,
                real_input=True,
                bellis=BellisConfig(store, task_authority=authority),
            ):
                pytest.fail("真实 v2 入口不得越过门禁")
        assert not authority.ledger_path.exists()

    asyncio.run(run())
