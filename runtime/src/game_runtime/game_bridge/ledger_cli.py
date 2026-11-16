"""操作员显式建账入口。"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from game_runtime.adapter import GAMES_DIR, AdapterError, load_adapter
from game_runtime.adapter.load import describe
from game_runtime.game_bridge.task_config import read_task_config
from game_runtime.game_bridge.task_ledger import LedgerError, TaskLedger


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="task-ledger", description="Provision the ledger for a configured game control target."
    )
    commands = parser.add_subparsers(required=True)
    provision = commands.add_parser("provision", help="create a new ledger")
    provision.add_argument("task_config", type=Path, help="local JSON task configuration")
    args = parser.parse_args(argv)
    try:
        authority = read_task_config(args.task_config)
        load_adapter(GAMES_DIR / authority.adapter_id)
        TaskLedger.provision(authority.ledger_path, authority.control_target_id)
    except ValidationError as error:
        for problem in describe(error):
            print(problem, file=sys.stderr)
    except FileExistsError:
        print("账库路径已存在，建账被拒绝。", file=sys.stderr)
    except (AdapterError, LedgerError, OSError, ValueError) as error:
        print(f"任务账库建账失败。{error}", file=sys.stderr)
    else:
        print(f"已为 {authority.control_target_id} 建立账库 {authority.ledger_path}")
        return 0
    return 2
