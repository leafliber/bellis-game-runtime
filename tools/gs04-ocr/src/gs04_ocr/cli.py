"""`uv run gs04`：`synth` 渲染合成游戏与泛化的样本，`eval` 评测一个样本目录并写报告。"""

import argparse
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path

from game_runtime.adapter import GAMES_DIR, AdapterError, NotCalibratedError
from game_runtime.ocr import DEFAULT_MODEL_SET, EngineOptions
from gs04_ocr.engines import ENGINE_NAMES, create_engine
from gs04_ocr.errors import EngineUnavailableError
from gs04_ocr.evaluate import (
    REPORTS_DIR,
    TAG_TITLES,
    TOTAL,
    Summary,
    build_report,
    run_engine,
    summarize_tags,
    write_report,
)
from gs04_ocr.general import available_fonts
from gs04_ocr.samples import SampleError, load_samples
from gs04_ocr.synth import write_synthetic_samples


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gs04", description="GS-04 OCR evaluation.")
    commands = parser.add_subparsers(dest="command", required=True)

    synth = commands.add_parser(
        "synth", help="render synthetic-game and generalization samples with ground truth"
    )
    synth.add_argument("directory", type=Path)
    synth.add_argument("--width", type=int, default=1280)
    synth.add_argument("--height", type=int, default=720)
    synth.add_argument(
        "--no-system-fonts",
        action="store_true",
        help="render the generalization samples with the synthetic game's font subset only",
    )

    evaluate = commands.add_parser("eval", help="evaluate one engine on a sample directory")
    evaluate.add_argument("directory", type=Path)
    evaluate.add_argument("--engine", choices=ENGINE_NAMES, default=DEFAULT_MODEL_SET)
    evaluate.add_argument(
        "--threads",
        type=int,
        default=EngineOptions.threads,
        help="ONNX Runtime intra-op threads; 0 lets ONNX Runtime decide",
    )
    evaluate.add_argument(
        "--det-max-side",
        type=int,
        default=EngineOptions.det_max_side,
        help="upper bound of the detector input's long side",
    )
    evaluate.add_argument("--no-merge", action="store_true", help="do not merge same-row boxes")
    evaluate.add_argument("--no-pad", action="store_true", help="do not widen line crops")
    evaluate.add_argument("--min-confidence", type=float, default=EngineOptions.min_confidence)
    evaluate.add_argument("--games-dir", type=Path, default=GAMES_DIR)
    evaluate.add_argument("--report-dir", type=Path, default=REPORTS_DIR)
    return parser


def _line(name: str, summary: Summary) -> str:
    strict, nfkc, lat = summary.strict.value, summary.nfkc.value, summary.latency
    return (
        f"{name}：{summary.samples} 个，字符准确率 "
        f"{'—' if strict is None else f'{strict:.4f}'}（NFKC 后 "
        f"{'—' if nfkc is None else f'{nfkc:.4f}'}），"
        + ("没有时延样本" if lat is None else f"p50 {lat.p50:.1f} ms，p95 {lat.p95:.1f} ms")
    )


def run(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "synth":
        fonts = available_fonts(system=not args.no_system_fonts)
        path = write_synthetic_samples(args.directory, args.width, args.height, fonts=fonts)
        print("泛化样本的字体：" + "，".join(f"{f.name}（{f.tag}）" for f in fonts))
        print(f"已写出 {path}")
        return 0

    options = EngineOptions(
        threads=args.threads or None,
        det_max_side=args.det_max_side,
        merge_gap_ratio=None if args.no_merge else EngineOptions.merge_gap_ratio,
        rec_pad_ratio=0.0 if args.no_pad else EngineOptions.rec_pad_ratio,
        min_confidence=args.min_confidence,
    )
    try:
        samples = load_samples(args.directory, args.games_dir)
        engine = create_engine(args.engine, options)
    except (SampleError, AdapterError, NotCalibratedError, LookupError) as error:
        print(f"样本不可用：{error}", file=sys.stderr)
        return 2
    except EngineUnavailableError as error:
        print(f"引擎 {args.engine} 不可用：{error}", file=sys.stderr)
        return 2
    results = run_engine(engine, samples)
    command = shlex.join(["gs04", *(sys.argv[1:] if argv is None else argv)])
    report, summaries = build_report(engine, args.directory.resolve(), results, command)
    by_tag = summarize_tags(results)
    json_path, md_path = write_report(report, summaries, args.report_dir, by_tag)
    for name, summary in summaries.items():
        print(_line(name, summary))
    for key, values in by_tag.items():
        print(f"按{TAG_TITLES.get(key, key)}（{key}）：")
        for value, summary in values.items():
            print("  " + _line(value, summary))
    print(f"报告：{md_path}\n      {json_path}")
    total = summaries[TOTAL].strict.value
    return 0 if total is not None else 1


def main() -> int:
    return run()
