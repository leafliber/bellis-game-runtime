"""`uv run gs06`：GS-06 配音检测的离线评测（`docs/modules/perception.md` 的“GS-06 的评测”）。

- `gs06 truth <纯配音.wav>`：打印按能量得出的配音段。
- `gs06 eval --suite <目录>`：整组评测 `gs06 synth` 写出的目录（按其中的 `suite.toml`）。
- `gs06 eval --voice <纯配音.wav> [--background 名字=<背景.wav>]...
  [--vocal-background 名字=<背景.wav>]... [--mix <正常混音.wav>]`：评测给出的文件。
- `gs06 synth <目录>`：用 SAPI 与合成的背景生成整组样本。

报告写进 `reports/gs06/`。
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from game_runtime.artifacts import ArtifactError, verify_artifact
from game_runtime.local_data import local_data_dir
from game_runtime.voice_activity import SILERO_VAD, SileroVad, VoiceParams
from gs06_eval import sapi
from gs06_eval.evaluate import DEFAULT_GAINS, run_evaluation
from gs06_eval.metrics import Collars
from gs06_eval.report import environment, format_summary, reports_dir, sha256_file, write_report
from gs06_eval.suite import SuiteError, SuiteLine, read_suite
from gs06_eval.synth import RATES, write_samples
from gs06_eval.truth import TruthParams, voice_truth
from gs06_eval.wav import Audio, WavError, read_wav


def _truth_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("真值（按能量）")
    defaults = TruthParams()
    group.add_argument("--frame", type=float, default=defaults.frame, help="帧长（秒）")
    group.add_argument("--percentile", type=float, default=defaults.percentile)
    group.add_argument(
        "--relative-db", type=float, default=defaults.relative_db, help="阈值低于百分位的分贝数"
    )
    group.add_argument("--floor-db", type=float, default=defaults.floor_db, help="阈值的下限")
    group.add_argument(
        "--merge-gap", type=float, default=defaults.merge_gap, help="合并短于它的间隔（秒）"
    )
    group.add_argument(
        "--min-segment", type=float, default=defaults.min_segment, help="丢掉短于它的段（秒）"
    )


def _truth_params(args: argparse.Namespace) -> TruthParams:
    return TruthParams(
        frame=args.frame,
        percentile=args.percentile,
        relative_db=args.relative_db,
        floor_db=args.floor_db,
        merge_gap=args.merge_gap,
        min_segment=args.min_segment,
    )


def _numbers(text: str) -> tuple[float, ...]:
    try:
        return tuple(float(part) for part in text.split(",") if part.strip())
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"须为逗号分隔的数：{text}") from error


def _rates(text: str) -> tuple[int, ...]:
    try:
        rates = tuple(int(part) for part in text.split(",") if part.strip())
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"语速须为逗号分隔的整数：{text}") from error
    if not rates or any(rate not in sapi.RATES for rate in rates):
        raise argparse.ArgumentTypeError(f"语速须为 −10 到 10 之间的整数，至少一个：{text}")
    return rates


def _background(text: str) -> tuple[str, Path]:
    name, sep, path = text.partition("=")
    if not sep or not name or not path:
        raise argparse.ArgumentTypeError(f"背景须写成 名字=路径：{text}")
    return name, Path(path)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="gs06", description="GS-06 配音检测的离线评测")
    commands = root.add_subparsers(dest="command", required=True)

    truth = commands.add_parser("truth", help="打印纯配音录音按能量得出的配音段")
    truth.add_argument("voice", type=Path, help="纯配音的 WAV")
    _truth_options(truth)

    run = commands.add_parser("eval", help="评测，报告写进 reports/gs06/")
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--suite",
        type=Path,
        metavar="目录",
        help="整组评测：gs06 synth 写出的、含 suite.toml 的目录",
    )
    source.add_argument("--voice", type=Path, help="纯配音的 WAV（背景静音）")
    run.add_argument(
        "--background",
        type=_background,
        action="append",
        default=[],
        metavar="名字=WAV",
        help="与 --voice 一起：只有背景（配音静音）的 WAV，可给多段，如 music=… effects=…",
    )
    run.add_argument(
        "--vocal-background",
        type=_background,
        action="append",
        default=[],
        metavar="名字=WAV",
        help="与 --voice 一起：带人声的背景，只记录、不设门槛",
    )
    run.add_argument(
        "--gains",
        type=_numbers,
        default=DEFAULT_GAINS,
        help="背景相对配音电平的增益（dB，逗号分隔，写成 --gains=-12,-6,0,6），缺省 -12,-6,0,6",
    )
    run.add_argument("--mix", type=Path, help="与 --voice 一起：正常混音的 WAV，只给判定")
    run.add_argument(
        "--model",
        type=Path,
        help="另一个 Silero VAD 接口的模型文件（对照其它型号）；缺省为本地数据目录里核对过的"
        "候选制品，没有时先运行 uv run fetch-models",
    )
    run.add_argument("--out", type=Path, help="报告的目录，缺省为 reports/gs06/")
    defaults = VoiceParams()
    group = run.add_argument_group("判定（同插件的配置）")
    group.add_argument("--onset", type=float, default=defaults.onset)
    group.add_argument("--offset", type=float, default=defaults.offset)
    group.add_argument("--min-speech", type=float, default=defaults.min_speech, help="秒")
    group.add_argument("--min-silence", type=float, default=defaults.min_silence, help="秒")
    collars = Collars()
    group = run.add_argument_group("容差")
    group.add_argument("--onset-collar", type=float, default=collars.onset, help="秒")
    group.add_argument("--offset-collar", type=float, default=collars.offset, help="秒")
    _truth_options(run)

    synth = commands.add_parser("synth", help="用 SAPI 与合成的背景生成整组样本（只在 Windows）")
    synth.add_argument("directory", type=Path, help="写各段 WAV 与 suite.toml 的目录")
    synth.add_argument("--sample-rate", type=int, default=48_000)
    synth.add_argument(
        "--max-voices",
        type=int,
        help="至多用几把语音（中文的优先），缺省为中文 4 把加其它语言 2 把",
    )
    synth.add_argument(
        "--rates",
        type=_rates,
        default=RATES,
        help=f"SAPI 的语速（−10 到 10，逗号分隔，写成 --rates=-3,0,4），缺省 "
        f"{','.join(map(str, RATES))}",
    )
    return root


def _input(path: Path, role: str) -> tuple[Audio, dict[str, object]]:
    audio = read_wav(path)
    return audio, {
        "role": role,
        "file": path.name,
        "duration": audio.duration,
        "sample_rate": audio.sample_rate,
        "channels": audio.channels,
    }


def command_truth(args: argparse.Namespace) -> int:
    truth = voice_truth(read_wav(args.voice), _truth_params(args))
    print(f"阈值 {truth.threshold_db:.1f} dBFS，{len(truth.segments)} 段")
    for segment in truth.segments:
        print(f"{segment.start:9.3f} {segment.end:9.3f} {segment.duration:7.3f}")
    return 0


class _InputError(Exception):
    pass


def _sources(
    args: argparse.Namespace,
) -> tuple[Path, list[tuple[str, Path, bool]], Path | None, tuple[SuiteLine, ...]]:
    """纯配音、背景（名字、路径、是否带人声）、正常混音与每句的出处。"""
    if args.suite is None:
        backgrounds = [(name, path, False) for name, path in args.background]
        backgrounds += [(name, path, True) for name, path in args.vocal_background]
        return args.voice, backgrounds, args.mix, ()
    if args.background or args.vocal_background or args.mix is not None:
        raise _InputError("--suite 不与 --background、--vocal-background、--mix 一起用")
    directory: Path = args.suite
    try:
        suite = read_suite(directory)
    except SuiteError as error:
        raise _InputError(str(error)) from None
    backgrounds = [(b.name, directory / b.file, b.vocal) for b in suite.backgrounds]
    normal = None if suite.mix is None else directory / suite.mix
    return directory / suite.voice, backgrounds, normal, suite.lines


def command_eval(args: argparse.Namespace) -> int:
    try:
        voice_path, sources, mix_path, lines = _sources(args)
        voice, voice_info = _input(voice_path, "voice")
        inputs = [voice_info]
        backgrounds: dict[str, Audio] = {}
        vocal: set[str] = set()
        for name, path, is_vocal in sources:
            if name in backgrounds:
                raise _InputError(f"背景 {name} 重复")
            role = "vocal background" if is_vocal else "background"
            audio, info = _input(path, f"{role} {name}")
            backgrounds[name] = audio
            inputs.append(info)
            if is_vocal:
                vocal.add(name)
        normal_mix = None
        if mix_path is not None:
            normal_mix, info = _input(mix_path, "normal mix")
            inputs.append(info)
    except (_InputError, OSError, WavError) as error:
        print(error, file=sys.stderr)
        return 2
    artifact: dict[str, str] | None = None
    if args.model is None:
        try:
            model_path = verify_artifact(SILERO_VAD, local_data_dir())
        except ArtifactError as error:
            print(error, file=sys.stderr)
            return 1
        artifact = {"name": SILERO_VAD.name, "version": SILERO_VAD.version}
    else:
        model_path = args.model
    model = SileroVad(model_path)
    params = VoiceParams(
        onset=args.onset,
        offset=args.offset,
        min_speech=args.min_speech,
        min_silence=args.min_silence,
    )
    try:
        results = run_evaluation(
            voice=voice,
            backgrounds=backgrounds,
            model=model,
            params=params,
            gains=args.gains,
            vocal=vocal,
            normal_mix=normal_mix,
            lines=lines,
            truth_params=_truth_params(args),
            collars=Collars(onset=args.onset_collar, offset=args.offset_collar),
        )
    except ValueError as error:
        print(f"无法评测：{error}", file=sys.stderr)
        return 2
    report: dict[str, Any] = {
        "spike": "GS-06",
        "environment": environment(),
        "command": ["gs06", *args.argv],
        "suite": None if args.suite is None else str(args.suite.resolve()),
        "model": {
            "artifact": artifact,
            "file": model_path.name,
            "path": str(model_path),
            "sha256": sha256_file(model_path),
            "size": model_path.stat().st_size,
        },
        "inputs": inputs,
        **results,
    }
    print(format_summary(results))
    path = write_report(report, args.out or reports_dir())
    print(f"\n报告：{path}")
    return 0


def command_synth(args: argparse.Namespace) -> int:
    if not sapi.available():
        print("本机没有可用的 SAPI 语音（只在 Windows 上可用）", file=sys.stderr)
        return 1
    if args.max_voices is not None and args.max_voices < 1:
        print("--max-voices 至少为 1", file=sys.stderr)
        return 2
    try:
        written = write_samples(
            args.directory,
            sample_rate=args.sample_rate,
            max_voices=args.max_voices,
            rates=args.rates,
        )
    except sapi.SapiError as error:
        print(error, file=sys.stderr)
        return 1
    names = "，".join(f"{v.name}（{v.culture}）" for v in written.voices)
    print(f"语音：{names}；语速：{', '.join(map(str, args.rates))}")
    print(f"带人声的背景：{written.chatter_voice.name}")
    print(f"纯配音 {written.duration:.1f} 秒，{len(written.suite.lines)} 句，写进 {args.directory}")
    print(f'评测：uv run gs06 eval --suite "{args.directory}"')
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    args.argv = list(sys.argv[1:] if argv is None else argv)
    handlers = {"truth": command_truth, "eval": command_eval, "synth": command_synth}
    return handlers[args.command](args)
