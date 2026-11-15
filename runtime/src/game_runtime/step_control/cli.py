"""从操作员选定的 profile 启动受监督的模型游玩。"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from game_runtime.adapter import AdapterError
from game_runtime.adapter.load import describe
from game_runtime.core_link import CoreLinkOptions
from game_runtime.kernel import ResolvedEntry, State, read_patch, read_profile, resolve_profile
from game_runtime.local_data import LocalDataError
from game_runtime.operator import OperatorError
from game_runtime.recording.cli import build_realtime_core, data_dir_option, setup_output
from game_runtime.secret_store import CredentialManagerStore, SecretStore, SecretStoreError
from game_runtime.step_control.host import supervise


async def _run(
    game: str,
    entries: Sequence[ResolvedEntry],
    core: Path,
    data_dir: Path,
    store: SecretStore,
    *,
    real_input: bool,
) -> int:
    async with supervise(
        game,
        None,
        CoreLinkOptions(core),
        data_dir=data_dir,
        store=store,
        real_input=real_input,
        profile=entries,
    ) as run:
        for entry in run.kernel.snapshot():
            if entry.enabled and entry.state is not State.ACTIVE:
                details = "，".join(
                    f"{reason.code}（{reason.subject}）" for reason in entry.reasons
                )
                print(f"{entry.id} {entry.state} {details}", file=sys.stderr, flush=True)
        if run.controller is None:
            print("逐步控制未激活", file=sys.stderr, flush=True)
            return 2
        print("已连接操作员控制台，输入许可由控制台管理。", flush=True)
        await run.wait_ended()
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="supervise",
        description="Run a model play session with a separate operator console.",
    )
    parser.add_argument("game", help="adapter id (a directory under games/)")
    parser.add_argument("--profile", type=Path, required=True, help="profile JSON file")
    parser.add_argument(
        "--patch", type=Path, action="append", default=[], help="apply a profile patch"
    )
    parser.add_argument("--core", type=Path, help="an already built realtime-core executable")
    parser.add_argument("--data-dir", type=Path, help="local data directory (absolute)")
    parser.add_argument("--real-input", action="store_true", help="enable supervised Windows input")
    parser.add_argument("--verbose", action="store_true", help="log at INFO level")
    args = parser.parse_args(argv)
    setup_output(args.verbose)
    try:
        profile = resolve_profile(
            read_profile(args.profile), [read_patch(path) for path in args.patch]
        )
        data_dir = data_dir_option(args.data_dir)
        store = CredentialManagerStore()
        core = args.core or build_realtime_core()
        return asyncio.run(
            _run(args.game, profile, core, data_dir, store, real_input=args.real_input)
        )
    except KeyboardInterrupt:
        return 130
    except ValidationError as error:
        for problem in describe(error):
            print(problem, file=sys.stderr)
    except (
        AdapterError,
        LocalDataError,
        LookupError,
        OperatorError,
        OSError,
        RuntimeError,
        SecretStoreError,
        ValueError,
    ) as error:
        print(f"不能启动受监督游玩。{error}", file=sys.stderr)
    return 2
