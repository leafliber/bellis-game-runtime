"""uv run replay-benchmark。控制台使用本次运行内存中的会话连接信息。"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from game_runtime.adapter import GAMES_DIR, AdapterError, load_adapter
from game_runtime.kernel import read_patch, read_profile, resolve_profile
from game_runtime.local_data import LocalDataError
from game_runtime.operator import ConsoleError, OperatorError
from game_runtime.operator.terminal import stdin_lines
from game_runtime.recording.cli import build_realtime_core, data_dir_option, setup_output
from game_runtime.recording.reader import Recording, RecordingError
from replay_benchmark.labels import read_labels, read_prices
from replay_benchmark.runner import replay_case
from replay_benchmark.score import score


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="replay-benchmark",
        description="Evaluate a policy on a local recording with supervised fake input.",
    )
    parser.add_argument("recording", type=Path, help="local recording directory")
    parser.add_argument("--labels", type=Path, required=True, help="separate scoring JSON file")
    parser.add_argument("--goal", required=True, help="task instruction independent of the labels")
    parser.add_argument("--profile", type=Path, required=True, help="approved model profile")
    parser.add_argument(
        "--patch", type=Path, action="append", default=[], help="apply a profile patch"
    )
    parser.add_argument("--prices", type=Path, help="optional model prices JSON file")
    parser.add_argument("--core", type=Path, help="an already built realtime-core executable")
    parser.add_argument("--data-dir", type=Path, help="local model assets and adapter templates")
    parser.add_argument("--out", type=Path, required=True, help="local JSON report path")
    parser.add_argument("--verbose", action="store_true", help="log at INFO level")
    args = parser.parse_args(argv)
    setup_output(args.verbose)
    try:
        recording = Recording.open(args.recording)
        labels = read_labels(args.labels)
        labels.check(recording)
        prices = None if args.prices is None else read_prices(args.prices)
        profile = resolve_profile(
            read_profile(args.profile), [read_patch(path) for path in args.patch]
        )
        trace = asyncio.run(
            replay_case(
                recording,
                goal=args.goal,
                core=args.core or build_realtime_core(),
                data_dir=data_dir_option(args.data_dir),
                profile=profile,
                commands=stdin_lines(),
                write=lambda line: print(line, flush=True),
            )
        )
        adapter = load_adapter(GAMES_DIR / recording.meta.game.id)
        result = score(
            labels,
            trace,
            backend="model",
            signatures=len(adapter.signatures),
            fast_paths=sum(box.advance == "click-text" for box in adapter.dialogue_boxes),
            prices=prices,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("x", encoding="utf-8", newline="\n") as report:
            report.write(json.dumps(asdict(result), ensure_ascii=False, indent=2) + "\n")
        print(f"报告已写入 {args.out}")
        return 0 if result.finished else 2
    except KeyboardInterrupt:
        return 130
    except (
        AdapterError,
        ConsoleError,
        LocalDataError,
        LookupError,
        OperatorError,
        OSError,
        RecordingError,
        RuntimeError,
        ValueError,
    ) as error:
        print(f"离线基准未完成。{error}", file=sys.stderr)
        return 2
