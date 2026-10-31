"""一组录音的评测：真值、各种情形的混合与判定、指标，以及正常混音的判定。"""

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from game_runtime.voice_activity import SpeechModel, VoiceParams, VoiceState
from gs06_eval.metrics import Collars, evaluate, intervals
from gs06_eval.mix import Detection, detect, mix, rms_db
from gs06_eval.truth import Segment, TruthParams, voice_truth
from gs06_eval.wav import Audio

DEFAULT_GAINS = (-12.0, -6.0, 0.0, 6.0)
"""背景相对录下的电平的增益（dB）。"""


@dataclass(frozen=True)
class Scenario:
    name: str
    background: str | None
    """背景的名字；纯配音时为 None。"""
    gain_db: float | None
    with_voice: bool
    """有配音：按真值评测；没有配音（只有背景）时真值为空，判定的忙都是多判。"""


def plan(backgrounds: Sequence[str], gains: Sequence[float]) -> list[Scenario]:
    """纯配音、每段背景单独，以及纯配音加每段背景乘以每个增益。"""
    result = [Scenario("voice", None, None, with_voice=True)]
    for name in backgrounds:
        result.append(Scenario(f"{name} only", name, 0.0, with_voice=False))
        for gain in gains:
            result.append(Scenario(f"voice + {name} {gain:+g} dB", name, gain, with_voice=True))
    return result


def _probabilities(detection: Detection) -> dict[str, float | None]:
    if not detection.probabilities:
        return {"p50": None, "p90": None, "p99": None, "max": None}
    values = np.asarray(detection.probabilities)
    p50, p90, p99 = (float(v) for v in np.percentile(values, [50, 90, 99]))
    return {"p50": p50, "p90": p90, "p99": p99, "max": float(values.max())}


def _timing(detection: Detection) -> dict[str, float | int]:
    frames = detection.frames
    return {
        "frames": frames,
        "seconds": detection.seconds,
        "ms_per_frame": 1000 * detection.seconds / frames if frames else 0.0,
        "real_time_factor": detection.seconds / detection.duration if detection.duration else 0.0,
    }


def run_evaluation(
    *,
    voice: Audio,
    backgrounds: Mapping[str, Audio],
    model: SpeechModel,
    params: VoiceParams,
    gains: Sequence[float] = DEFAULT_GAINS,
    normal_mix: Audio | None = None,
    truth_params: TruthParams | None = None,
    collars: Collars | None = None,
) -> dict[str, Any]:
    """评测一组录音，返回可写成 JSON 的结果。每种情形用新的核心，模型从零状态开始。"""
    collars = collars or Collars()
    truth = voice_truth(voice, truth_params)
    levels = {"voice": rms_db(voice)}
    levels.update({f"background {name}": rms_db(audio) for name, audio in backgrounds.items()})
    scenarios: list[dict[str, Any]] = []
    for scenario in plan(list(backgrounds), gains):
        if scenario.background is None:
            audio = voice
        elif scenario.with_voice:
            audio = mix(voice, backgrounds[scenario.background], scenario.gain_db or 0.0)
        else:
            audio = backgrounds[scenario.background]
        detection = detect(audio, model, params)
        segments: tuple[Segment, ...] = truth.segments if scenario.with_voice else ()
        scenarios.append(
            {
                "name": scenario.name,
                "background": scenario.background,
                "gain_db": scenario.gain_db,
                "with_voice": scenario.with_voice,
                "duration": audio.duration,
                "metrics": evaluate(segments, detection.changes, audio.duration, collars),
                "probability": _probabilities(detection),
                "timing": _timing(detection),
            }
        )
    mixed: dict[str, Any] | None = None
    if normal_mix is not None:
        levels["normal mix"] = rms_db(normal_mix)
        detection = detect(normal_mix, model, params)
        busy = intervals(detection.changes, normal_mix.duration, VoiceState.BUSY)
        busy_seconds = sum(s.duration for s in busy)
        mixed = {
            "duration": normal_mix.duration,
            "busy": [[s.start, s.end] for s in busy],
            "busy_seconds": busy_seconds,
            "busy_fraction": busy_seconds / normal_mix.duration if normal_mix.duration else None,
            "probability": _probabilities(detection),
            "timing": _timing(detection),
        }
    return {
        "params": asdict(params),
        "collars": asdict(collars),
        "truth": {
            "params": asdict(truth.params),
            "threshold_db": truth.threshold_db,
            "segments": [[s.start, s.end] for s in truth.segments],
            "busy_seconds": sum(s.duration for s in truth.segments),
            "voice_duration": voice.duration,
        },
        "levels_db": levels,
        "scenarios": scenarios,
        "normal_mix": mixed,
    }
