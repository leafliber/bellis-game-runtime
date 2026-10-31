"""`uv run gs06`：GS-06 配音检测的离线评测（`docs/modules/perception.md` 的“GS-06 的评测”）。

- `gs06 truth <纯配音.wav>`：打印按能量得出的配音段。
- `gs06 eval --voice <纯配音.wav> [--background 名字=<背景.wav>]... [--mix <正常混音.wav>]`：
  评测，报告写进 `reports/gs06/`。
- `gs06 synth <目录>`：用 SAPI 与合成的背景生成自测样本。
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from game_runtime.artifacts import ArtifactError, verify_artifact
from game_runtime.local_data import local_data_dir
from game_runtime.voice_activity import SILERO_VAD, SileroVad, VoiceParams
from gs06_eval import sapi
from gs06_eval.evaluate import DEFAULT_GAINS, run_evaluation
from gs06_eval.metrics import Collars
from gs06_eval.mix import mix
from gs06_eval.report import environment, format_summary, reports_dir, sha256_file, write_report
from gs06_eval.synth import effects, music, voice_track
from gs06_eval.truth import TruthParams, voice_truth
from gs06_eval.wav import Audio, read_wav, write_wav


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


def _gains(text: str) -> tuple[float, ...]:
    try:
        return tuple(float(part) for part in text.split(",") if part.strip())
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"增益须为逗号分隔的数：{text}") from error


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
    run.add_argument("--voice", type=Path, required=True, help="纯配音的 WAV（背景静音）")
    run.add_argument(
        "--background",
        type=_background,
        action="append",
        default=[],
        metavar="名字=WAV",
        help="只有背景（配音静音）的 WAV，可给多段，如 bgm=… sfx=…",
    )
    run.add_argument(
        "--gains",
        type=_gains,
        default=DEFAULT_GAINS,
        help="背景的增益（dB，逗号分隔，写成 --gains=-12,-6,0,6），缺省 -12,-6,0,6",
    )
    run.add_argument("--mix", type=Path, help="正常混音的 WAV：只给判定，不算准确率")
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

    synth = commands.add_parser("synth", help="用 SAPI 与合成的背景生成自测样本（只在 Windows）")
    synth.add_argument(
        "directory", type=Path, help="写 voice.wav、bgm.wav、sfx.wav、mix.wav 的目录"
    )
    synth.add_argument("--sample-rate", type=int, default=48_000)
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


def command_eval(args: argparse.Namespace) -> int:
    voice, voice_info = _input(args.voice, "voice")
    inputs = [voice_info]
    backgrounds: dict[str, Audio] = {}
    for name, path in args.background:
        if name in backgrounds:
            print(f"背景 {name} 重复", file=sys.stderr)
            return 2
        audio, info = _input(path, f"background {name}")
        backgrounds[name] = audio
        inputs.append(info)
    normal_mix = None
    if args.mix is not None:
        normal_mix, info = _input(args.mix, "normal mix")
        inputs.append(info)
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
    results = run_evaluation(
        voice=voice,
        backgrounds=backgrounds,
        model=model,
        params=params,
        gains=args.gains,
        normal_mix=normal_mix,
        truth_params=_truth_params(args),
        collars=Collars(onset=args.onset_collar, offset=args.offset_collar),
    )
    report = {
        "spike": "GS-06",
        "environment": environment(),
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
    directory: Path = args.directory
    directory.mkdir(parents=True, exist_ok=True)
    track = voice_track(args.sample_rate)
    voice = Audio(track.audio.samples.repeat(2, axis=1), track.audio.sample_rate)
    bgm = music(voice.duration, args.sample_rate)
    sfx = effects(voice.duration, args.sample_rate)
    normal = mix(mix(voice, bgm, 0.0), sfx, 0.0)
    for name, audio in (("voice", voice), ("bgm", bgm), ("sfx", sfx), ("mix", normal)):
        write_wav(directory / f"{name}.wav", audio)
    print(f"语音：{track.voice}，{voice.duration:.1f} 秒，写进 {directory}")
    print(
        "评测：uv run gs06 eval "
        f'--voice "{directory / "voice.wav"}" '
        f'--background bgm="{directory / "bgm.wav"}" '
        f'--background sfx="{directory / "sfx.wav"}" '
        f'--mix "{directory / "mix.wav"}"'
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    handlers = {"truth": command_truth, "eval": command_eval, "synth": command_synth}
    return handlers[args.command](args)
