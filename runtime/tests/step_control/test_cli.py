"""模型游玩只从操作员指定的 profile 组装。"""

import asyncio
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from game_runtime.adapter import GAMES_DIR
from game_runtime.core_link import CoreLinkOptions
from game_runtime.kernel import ResolvedEntry, read_profile, resolve_profile
from game_runtime.secret_store import MemorySecretStore, SecretStore
from game_runtime.step_control import cli, supervise

EXAMPLE = GAMES_DIR.parent / "profiles" / "supervised.example.json"


@pytest.mark.parametrize("enable_input", [False, True])
def test_supervise_cli_resolves_patches_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enable_input: bool
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
    ) -> int:
        assert game == "synthetic-game"
        assert core == executable
        assert data_dir == tmp_path
        assert selected_store is store
        assert real_input is enable_input
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
