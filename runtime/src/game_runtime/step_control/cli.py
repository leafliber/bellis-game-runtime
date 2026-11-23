"""从操作员选定的 profile 启动受监督的模型游玩。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from game_runtime.adapter import AdapterError
from game_runtime.adapter.load import describe
from game_runtime.core_link import CoreLinkOptions
from game_runtime.kernel import ResolvedEntry, State, read_patch, read_profile, resolve_profile
from game_runtime.local_data import LocalDataError
from game_runtime.operator import OperatorError
from game_runtime.recording.cli import data_dir_option, setup_output
from game_runtime.recording.layout import check_name
from game_runtime.recording.retention import QuotaRefusedError
from game_runtime.secret_store import CredentialManagerStore, SecretStoreError
from game_runtime.step_control.host import supervise
from game_runtime.telemetry import TelemetryConfig, TelemetryPlugin

if TYPE_CHECKING:
    from game_runtime.game_bridge.session import BellisConfig


def _set_telemetry_directory(entries: list[ResolvedEntry], directory: Path) -> None:
    plugin = TelemetryPlugin.manifest.id
    configured = [index for index, entry in enumerate(entries) if entry.plugin == plugin]
    candidates = [index for index in configured if not entries[index].disabled] or configured
    if len(candidates) > 1:
        raise ValueError("--telemetry-dir 须对应一个 telemetry 条目")
    path = str(TelemetryConfig(directory=directory).directory)
    if candidates:
        (index,) = candidates
        entry = entries[index]
        entries[index] = replace(entry, config={**entry.config, "directory": path}, disabled=False)
    else:
        entries.append(ResolvedEntry(plugin, plugin, {"directory": path}, False, frozenset()))


async def _run(
    game: str,
    entries: Sequence[ResolvedEntry],
    core: Path | None,
    data_dir: Path,
    *,
    real_input: bool,
    bellis: BellisConfig | None = None,
    record: bool = False,
    name: str | None = None,
) -> int:
    if bellis is not None:
        bellis.require_task_backend(real_input=real_input, has_test_device=False)
    async with supervise(
        game,
        None,
        CoreLinkOptions(core) if core is not None else None,
        data_dir=data_dir,
        real_input=real_input,
        profile=entries,
        bellis=bellis,
        record=record,
        name=name,
    ) as run:
        for entry in run.kernel.snapshot():
            if entry.enabled and entry.state is not State.ACTIVE:
                details = "，".join(
                    f"{reason.code}（{reason.subject}）" for reason in entry.reasons
                )
                print(f"{entry.id} {entry.state} {details}", file=sys.stderr, flush=True)
        if run.controller is None and bellis is None:
            print("逐步控制未激活", file=sys.stderr, flush=True)
            return 2
        if bellis is None:
            print("已连接操作员控制台，输入许可由控制台管理。", flush=True)
        else:
            print("已启动 Bellis 语义桥，输入许可由实时核直连 Bellis 取得。", flush=True)
        if run.recording_path is not None:
            print(f"本地录制目录 {run.recording_path}", flush=True)
        await run.wait_ended()
    if (meta := run.recording_meta) is not None:
        reason = meta.stop_reason or "未封存"
        print(f"录制 {meta.state}（{reason}） {run.recording_path}", flush=True)
        if meta.state != "complete" or reason in {"error", "quota"}:
            return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="supervise",
        description="Run supervised model play with the operator console or Bellis.",
    )
    parser.add_argument("game", help="adapter id (a directory under games/)")
    parser.add_argument("--profile", type=Path, required=True, help="profile JSON file")
    parser.add_argument(
        "--patch", type=Path, action="append", default=[], help="apply a profile patch"
    )
    parser.add_argument("--core", type=Path, help="an already built realtime-core executable")
    parser.add_argument("--data-dir", type=Path, help="local data directory (absolute)")
    parser.add_argument("--telemetry-dir", type=Path, help="local telemetry directory (absolute)")
    parser.add_argument(
        "--record",
        action="store_true",
        help="record admitted frames and configured game audio locally",
    )
    parser.add_argument("--name", type=check_name, help="recording name (with --record)")
    parser.add_argument("--real-input", action="store_true", help="enable supervised Windows input")
    parser.add_argument("--bellis", action="store_true", help="connect to the active Bellis host")
    parser.add_argument("--task-config", type=Path, help="local JSON task configuration for Bellis")
    parser.add_argument(
        "--confirm-precondition",
        action="append",
        default=[],
        metavar="ID",
        help="confirm a verified adapter prerequisite for this Bellis session (repeatable)",
    )
    parser.add_argument("--verbose", action="store_true", help="log at INFO level")
    args = parser.parse_args(argv)
    if args.bellis and not args.real_input:
        parser.error("--bellis requires an explicit --real-input backend")
    if args.task_config is not None and not args.bellis:
        parser.error("--task-config requires --bellis")
    if args.confirm_precondition and not args.bellis:
        parser.error("--confirm-precondition requires --bellis")
    if args.name is not None and not args.record:
        parser.error("--name requires --record")
    setup_output(args.verbose)
    try:
        profile = resolve_profile(
            read_profile(args.profile), [read_patch(path) for path in args.patch]
        )
        if args.telemetry_dir is not None:
            _set_telemetry_directory(profile, args.telemetry_dir)
        data_dir = data_dir_option(args.data_dir)
        core = args.core
        if args.bellis:
            from game_runtime.game_bridge.session import BellisConfig
            from game_runtime.game_bridge.task_config import read_task_config

            authority = read_task_config(args.task_config) if args.task_config is not None else None
            if authority is not None and authority.adapter_id != args.game:
                raise ValueError("任务配置的适配包与启动所选游戏不匹配")
            return asyncio.run(
                _run(
                    args.game,
                    profile,
                    core,
                    data_dir,
                    real_input=args.real_input,
                    bellis=BellisConfig(
                        CredentialManagerStore("bellis"),
                        frozenset(args.confirm_precondition),
                        task_authority=authority,
                    ),
                    record=args.record,
                    name=args.name,
                )
            )
        return asyncio.run(
            _run(
                args.game,
                profile,
                core,
                data_dir,
                real_input=args.real_input,
                record=args.record,
                name=args.name,
            )
        )
    except KeyboardInterrupt:
        return 130
    except ValidationError as error:
        for problem in describe(error):
            print(problem, file=sys.stderr)
    except AdapterError as error:
        print(f"适配包 NOT_READY。{error}", file=sys.stderr)
    except (
        LocalDataError,
        LookupError,
        OperatorError,
        OSError,
        QuotaRefusedError,
        RuntimeError,
        SecretStoreError,
        ValueError,
    ) as error:
        print(f"不能启动受监督游玩。{error}", file=sys.stderr)
    return 2
