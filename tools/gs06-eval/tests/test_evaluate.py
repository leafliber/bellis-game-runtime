"""整组评测与命令行。

合成的“配音”（正弦段）与噪声背景配按音量判断的假模型，在任何环境里跑通整组评测与报告。标
`vad_model` 的一项用 SAPI 合成的原创台词与本地数据目录里的真实模型跑一遍 `gs06 synth` 与
`gs06 eval`，没有 SAPI 或模型时跳过。
"""

import json
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from game_runtime.artifacts import ArtifactError, verify_artifact
from game_runtime.local_data import local_data_dir
from game_runtime.voice_activity import SILERO_VAD, VoiceParams
from game_runtime.voice_activity.fake import EnergyModel
from gs06_eval import sapi
from gs06_eval.cli import main
from gs06_eval.evaluate import plan, run_evaluation
from gs06_eval.report import format_summary
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


def test_the_plan_covers_every_background_and_gain() -> None:
    names = [s.name for s in plan(["bgm", "sfx"], [-6.0, 0.0])]
    assert names == [
        "voice",
        "bgm only",
        "voice + bgm -6 dB",
        "voice + bgm +0 dB",
        "sfx only",
        "voice + sfx -6 dB",
        "voice + sfx +0 dB",
    ]


def test_a_full_evaluation_with_the_energy_model() -> None:
    voice = bursts((1.0, 0), (1.0, 0.3), (1.0, 0), (1.0, 0.3), (1.0, 0))
    results = run_evaluation(
        voice=voice,
        backgrounds={"quiet": noise(2.0, 0.001), "loud": noise(2.0, 0.05)},
        model=EnergyModel(threshold=0.01),
        params=VoiceParams(),
        gains=(0.0,),
        normal_mix=voice,
    )
    assert len(results["truth"]["segments"]) == 2
    by_name = {s["name"]: s["metrics"] for s in results["scenarios"]}
    clean = by_name["voice"]
    assert (clean["missed_segments"], clean["false_segments"], clean["early_idle"]) == (0, 0, 0)
    assert clean["onset_latency"]["n"] == 2
    assert 0.1 <= clean["onset_latency"]["max"] <= 0.2
    assert 0.5 <= clean["offset_latency"]["min"] <= clean["offset_latency"]["max"] <= 0.6
    assert by_name["quiet only"]["false_fraction"] == 0.0
    # 按音量判断的假模型把响的噪声全当作语音：这正是不按音量判断的理由。
    assert by_name["loud only"]["false_fraction"] == pytest.approx(1.0)
    assert by_name["voice + loud +0 dB"]["false_segments"] == 0
    assert by_name["voice + loud +0 dB"]["false_fraction"] > 0.9
    # 每段从判忙（晚约 0.13 秒）到判闲（晚约 0.54 秒）。
    assert results["normal_mix"]["busy_seconds"] == pytest.approx(2 * (1.0 - 0.13 + 0.54), abs=0.1)
    summary = format_summary(results)
    assert "voice + loud +0 dB" in summary
    json.dumps(results)


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
    assert main(["synth", str(samples)]) == 0
    out = tmp_path / "reports"
    argv = ["eval", "--voice", str(samples / "voice.wav"), "--out", str(out), "--gains=-6,0"]
    argv += ["--background", f"bgm={samples / 'bgm.wav'}", "--mix", str(samples / "mix.wav")]
    assert main(argv) == 0
    [report] = out.glob("gs06-*.json")
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["model"]["artifact"] == {"name": "silero-vad", "version": "v6.2"}
    assert data["model"]["sha256"] == SILERO_VAD.sha256
    clean = data["scenarios"][0]["metrics"]
    assert clean["missed_segments"] == 0
    assert clean["false_segments"] == 0
    assert clean["early_idle"] == 0
    assert clean["onset_latency"]["max"] < 0.3
    assert "voice + bgm -6 dB" in capsys.readouterr().out
