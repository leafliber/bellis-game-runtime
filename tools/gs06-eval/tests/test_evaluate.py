"""整组评测与命令行。

合成的“配音”（正弦段）与噪声背景配按音量判断的假模型，在任何环境里跑通整组评测、门槛、占用、按
语音与语速的分组与报告。标 `vad_model` 的一项用 SAPI 合成的原创台词与本地数据目录里的真实模型跑一遍
`gs06 synth` 与 `gs06 eval --suite`，没有 SAPI 或模型时跳过。
"""

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from numpy.typing import NDArray

from game_runtime.artifacts import ArtifactError, verify_artifact
from game_runtime.local_data import local_data_dir
from game_runtime.voice_activity import SILERO_VAD, VoiceParams
from game_runtime.voice_activity.fake import EnergyModel
from gs06_eval import sapi
from gs06_eval.cli import main
from gs06_eval.evaluate import CPU_TARGET_MS_PER_SECOND, Criteria, judge, plan, run_evaluation
from gs06_eval.report import format_summary
from gs06_eval.suite import Suite, SuiteBackground, SuiteLine, read_suite, write_suite
from gs06_eval.wav import Audio, write_wav

RATE = 48_000


def bursts(*parts: tuple[float, float]) -> Audio:
    pieces: list[NDArray[np.float64]] = []
    for seconds, amplitude in parts:
        t = np.arange(round(seconds * RATE)) / RATE
        pieces.append(amplitude * np.sin(2 * np.pi * 220 * t))
    mono = np.concatenate(pieces)
    return Audio(np.stack([mono, mono], axis=1).astype(np.float32), RATE)


def noise(seconds: float, amplitude: float) -> Audio:
    values = amplitude * np.random.default_rng(3).standard_normal((round(seconds * RATE), 2))
    return Audio(values.astype(np.float32), RATE)


VOICE = ((1.0, 0), (1.0, 0.3), (1.0, 0), (1.0, 0.3), (1.0, 0))
LINES = (
    SuiteLine(voice="A", culture="zh-CN", rate=0, start=0.9, end=2.1, text="一"),
    SuiteLine(voice="B", culture="en-US", rate=4, start=2.9, end=4.1, text="two"),
)


def test_the_plan_covers_every_background_and_gain() -> None:
    scenarios = plan(["music", "vocal"], [-6.0, 0.0, 6.0], vocal={"vocal"})
    assert [(s.name, s.gated) for s in scenarios] == [
        ("voice", True),
        ("music only", False),
        ("voice + music -6 dB", True),
        ("voice + music +0 dB", True),
        ("voice + music +6 dB", False),
        ("vocal only", False),
        ("voice + vocal -6 dB", False),
        ("voice + vocal +0 dB", False),
        ("voice + vocal +6 dB", False),
    ]
    assert [s.with_voice for s in scenarios[:3]] == [True, False, True]


def metrics(
    *, segments: int = 20, missed: int = 0, false: float | None = 0.0, on: float | None = 0.2
) -> dict[str, Any]:
    return {
        "truth_segments": segments,
        "missed_segments": missed,
        "false_fraction": false,
        "onset_latency": {"p90": on},
        "offset_latency": {"p90": 0.7},
    }


def test_judging_against_the_criteria() -> None:
    criteria = Criteria()
    assert judge(metrics(), criteria)["pass"]
    # 20 段漏 1 段是 5%，恰好不超过。
    assert judge(metrics(missed=1), criteria)["pass"]
    failed = judge(metrics(missed=2, false=0.11), criteria)
    assert not failed["pass"]
    assert [k for k, c in failed["checks"].items() if not c["pass"]] == [
        "missed_segments",
        "false_fraction",
    ]
    assert failed["checks"]["missed_segments"]["value"] == pytest.approx(0.1)
    # 没法计算的项算不满足。
    assert not judge(metrics(on=None), criteria)["checks"]["onset_p90"]["pass"]
    assert not judge(metrics(segments=0), criteria)["pass"]
    assert not judge(metrics(false=None), criteria)["pass"]


def evaluation(**kwargs: Any) -> dict[str, Any]:
    voice = bursts(*VOICE)
    return run_evaluation(
        voice=voice,
        backgrounds={"noise": noise(2.0, 0.05), "vocal": noise(1.5, 0.01)},
        model=EnergyModel(threshold=0.01),
        params=VoiceParams(),
        gains=(-40.0, 0.0),
        vocal={"vocal"},
        **kwargs,
    )


def test_a_full_evaluation_with_the_energy_model() -> None:
    results = evaluation(normal_mix=bursts(*VOICE), lines=LINES)
    assert len(results["truth"]["segments"]) == 2
    by_name = {s["name"]: s for s in results["scenarios"]}
    clean = by_name["voice"]["metrics"]
    assert (clean["missed_segments"], clean["false_segments"], clean["early_idle"]) == (0, 0, 0)
    assert clean["onset_latency"]["n"] == 2
    assert 0.1 <= clean["onset_latency"]["max"] <= 0.2
    assert 0.5 <= clean["offset_latency"]["min"] <= clean["offset_latency"]["max"] <= 0.6
    assert by_name["voice"]["verdict"]["pass"]
    # 背景按配音的电平缩放：−40 dB 时噪声低于假模型的阈值，0 dB 时与配音一样响。
    voice_level = results["levels_db"]["voice"]
    assert voice_level == pytest.approx(20 * np.log10(0.3 / np.sqrt(2)), abs=0.1)
    quiet = by_name["voice + noise -40 dB"]
    assert quiet["applied_gain_db"] == pytest.approx(
        voice_level - 40 - results["levels_db"]["background noise"]
    )
    assert quiet["verdict"]["pass"]
    # 按音量判断的假模型把响的噪声全当作语音：这正是不按音量判断的理由。
    assert by_name["noise only"]["metrics"]["false_fraction"] == pytest.approx(1.0)
    assert by_name["noise only"]["verdict"] == {"gated": False, "pass": None}
    loud = by_name["voice + noise +0 dB"]
    assert loud["gated"]
    assert not loud["verdict"]["pass"]
    assert not loud["verdict"]["checks"]["false_fraction"]["pass"]
    # 带人声的背景只记录：算了各项，但不判达标。
    vocal = by_name["voice + vocal -40 dB"]
    assert (vocal["gated"], vocal["vocal"], vocal["verdict"]["pass"]) == (False, True, None)
    assert vocal["verdict"]["checks"]["missed_segments"]["pass"]
    assert results["verdict"] == {"gated": 3, "passed": 2, "failed": ["voice + noise +0 dB"]}
    # 占用：各情形合计。
    occupancy = results["occupancy"]
    assert occupancy["audio_seconds"] == pytest.approx(7 * 5.0)
    assert occupancy["ms_per_second"] > 0
    assert occupancy["meets_target"] == (occupancy["ms_per_second"] <= CPU_TARGET_MS_PER_SECOND)
    assert occupancy["slowest"] in by_name
    # 按语音与语速：设门槛的 3 个情形里各 1 段。
    groups = results["breakdown"]
    assert groups["voice"]["A"]["segments"] == groups["voice"]["B"]["segments"] == 3
    assert groups["voice"]["A"]["missed"] == 0
    assert set(groups["rate"]) == {"+0", "+4"}
    # 每段从判忙（晚约 0.13 秒）到判闲（晚约 0.54 秒）。
    assert results["normal_mix"]["busy_seconds"] == pytest.approx(2 * (1.0 - 0.13 + 0.54), abs=0.1)
    summary = format_summary(results)
    assert "voice + noise +0 dB" in summary
    assert "不达标：多判时长" in summary
    assert "设门槛的 3 个情形：2 个达标" in summary
    assert "按语音" in summary
    json.dumps(results)


def test_without_lines_there_is_no_breakdown() -> None:
    results = evaluation()
    assert results["breakdown"] is None
    assert "按语音" not in format_summary(results)
    with pytest.raises(ValueError, match="带人声的背景不在背景里"):
        run_evaluation(
            voice=bursts(*VOICE),
            backgrounds={},
            model=EnergyModel(),
            params=VoiceParams(),
            vocal={"vocal"},
        )


def write_suite_dir(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    write_wav(directory / "voice.wav", bursts(*VOICE))
    write_wav(directory / "noise.wav", noise(2.0, 0.05))
    suite = Suite(
        voice="voice.wav",
        backgrounds=(SuiteBackground(name="noise", file="noise.wav"),),
        lines=LINES,
    )
    write_suite(directory, suite, ["测试用的清单"])
    return directory


def test_a_suite_round_trips_into_an_evaluation(tmp_path: Path) -> None:
    directory = write_suite_dir(tmp_path / "suite")
    suite = read_suite(directory)
    assert suite.lines == LINES
    assert suite.backgrounds[0].vocal is False
    assert (directory / "suite.toml").read_text(encoding="utf-8").startswith("# 测试用的清单")


def test_eval_rejects_mixing_a_suite_with_single_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = write_suite_dir(tmp_path / "suite")
    argv = ["eval", "--suite", str(directory), "--background", f"x={directory / 'noise.wav'}"]
    assert main(argv) == 2
    assert "--suite 不与" in capsys.readouterr().err
    assert main(["eval", "--suite", str(tmp_path / "missing")]) == 2
    assert "suite.toml" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["eval", "--suite", str(directory), "--voice", str(directory / "voice.wav")])


def test_synth_rejects_bad_rates() -> None:
    with pytest.raises(SystemExit):
        main(["synth", "x", "--rates=11"])
    with pytest.raises(SystemExit):
        main(["synth", "x", "--rates=fast"])


def test_the_truth_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "voice.wav"
    write_wav(path, bursts((1.0, 0), (1.0, 0.3), (1.0, 0)))
    assert main(["truth", str(path)]) == 0
    out = capsys.readouterr().out
    assert "1 段" in out
    assert "1.000" in out


@pytest.mark.vad_model
def test_synth_and_eval_with_the_real_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    try:
        verify_artifact(SILERO_VAD, local_data_dir())
    except ArtifactError as error:
        pytest.skip(f"语音检测模型不可用：{error}")
    if not sapi.available():
        pytest.skip("没有可用的 SAPI 语音（只在 Windows 上）")
    samples = tmp_path / "samples"
    assert main(["synth", str(samples), "--max-voices", "1", "--rates=0"]) == 0
    suite = read_suite(samples)
    assert [b.name for b in suite.backgrounds] == ["music", "effects", "noise", "vocal-music"]
    assert len(suite.lines) == 4
    out = tmp_path / "reports"
    assert main(["eval", "--suite", str(samples), "--out", str(out), "--gains=-6,0"]) == 0
    [report] = out.glob("gs06-*.json")
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["model"]["artifact"] == {"name": "silero-vad", "version": "v6.2"}
    assert data["model"]["sha256"] == SILERO_VAD.sha256
    assert data["suite"] == str(samples.resolve())
    assert len(data["scenarios"]) == 1 + 4 * 3
    clean = data["scenarios"][0]
    assert clean["verdict"]["pass"]
    assert clean["metrics"]["missed_segments"] == 0
    assert clean["metrics"]["onset_latency"]["max"] < 0.3
    assert data["verdict"]["gated"] == 1 + 3 * 2
    assert data["occupancy"]["ms_per_second"] > 0
    assert list(data["breakdown"]["voice"]) == [suite.lines[0].voice]
    printed = capsys.readouterr().out
    assert "voice + music -6 dB" in printed
    assert "占用：每秒声音" in printed
