"""一组录音的评测：真值、各种情形的混合与判定、指标与是否达标、占用，以及正常混音的判定。

口径见 `docs/modules/perception.md` 的“GS-06 的评测”。
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from game_runtime.voice_activity import SpeechModel, VoiceParams, VoiceState
from gs06_eval.metrics import Collars, evaluate, intervals, summary
from gs06_eval.mix import (
    Detection,
    background_level_db,
    detect,
    fitted,
    mix,
    relative_gain_db,
    rms_db,
    voice_level_db,
)
from gs06_eval.suite import SuiteLine
from gs06_eval.truth import Segment, TruthParams, voice_truth
from gs06_eval.wav import Audio

DEFAULT_GAINS = (-12.0, -6.0, 0.0, 6.0)
"""背景相对配音电平的增益（dB）。"""

CPU_TARGET_MS_PER_SECOND = 30.0
"""占用的目标：每处理 1 秒的声音耗时不超过 30 ms（见 `docs/modules/perception.md` 的
“语音检测模型”）。"""

WARMUP_SECONDS = 2.0
"""评测开始前先喂给模型预热的纯配音的长度（秒），不计入占用。"""


@dataclass(frozen=True)
class Criteria:
    """设门槛的情形须同时满足的各项。"""

    missed_segments: float = 0.05
    """漏判的段占真值段数的比例的上限。"""
    false_fraction: float = 0.10
    """多判的时长占真值为闲的比例的上限。"""
    onset_p90: float = 0.5
    """开始延迟 p90 的上限（秒）。"""
    offset_p90: float = 1.0
    """结束延迟 p90 的上限（秒）。"""
    max_gated_gain_db: float = 0.0
    """增益不高于它、背景不带人声的情形设门槛。"""


@dataclass(frozen=True)
class Scenario:
    name: str
    background: str | None
    """背景的名字；纯配音时为 None。"""
    gain_db: float | None
    """相对配音电平的增益。"""
    with_voice: bool
    """有配音：按真值评测；没有配音（只有背景）时真值为空，判定的忙都是多判。"""
    gated: bool
    """设门槛；否则只记录。"""


def plan(
    backgrounds: Sequence[str],
    gains: Sequence[float],
    vocal: Collection[str] = (),
    criteria: Criteria | None = None,
) -> list[Scenario]:
    """纯配音、每段背景单独（0 dB），以及纯配音加每段背景乘以每个增益。

    纯配音与不带人声的背景在增益不高于 `max_gated_gain_db` 时设门槛，其余只记录。
    """
    criteria = criteria or Criteria()
    result = [Scenario("voice", None, None, with_voice=True, gated=True)]
    for name in backgrounds:
        result.append(Scenario(f"{name} only", name, 0.0, with_voice=False, gated=False))
        for gain in gains:
            gated = name not in vocal and gain <= criteria.max_gated_gain_db
            result.append(
                Scenario(f"voice + {name} {gain:+g} dB", name, gain, with_voice=True, gated=gated)
            )
    return result


def _check(value: float | None, limit: float) -> dict[str, Any]:
    return {"value": value, "limit": limit, "pass": value is not None and value <= limit}


def judge(metrics: Mapping[str, Any], criteria: Criteria) -> dict[str, Any]:
    """一种有配音的情形的各项是否满足口径；没法算的项（如没有可计的延迟）算不满足。"""
    segments = metrics["truth_segments"]
    missed = metrics["missed_segments"] / segments if segments else None
    checks = {
        "missed_segments": _check(missed, criteria.missed_segments),
        "false_fraction": _check(metrics["false_fraction"], criteria.false_fraction),
        "onset_p90": _check(metrics["onset_latency"]["p90"], criteria.onset_p90),
        "offset_p90": _check(metrics["offset_latency"]["p90"], criteria.offset_p90),
    }
    return {"checks": checks, "pass": all(check["pass"] for check in checks.values())}


def _probabilities(detection: Detection) -> dict[str, float | None]:
    if not detection.probabilities:
        return {"p50": None, "p90": None, "p99": None, "max": None}
    values = np.asarray(detection.probabilities)
    p50, p90, p99 = (float(v) for v in np.percentile(values, [50, 90, 99]))
    return {"p50": p50, "p90": p90, "p99": p99, "max": float(values.max())}


def _timing(detection: Detection) -> dict[str, float | int]:
    frames = detection.frames
    duration = detection.duration
    return {
        "frames": frames,
        "seconds": detection.seconds,
        "ms_per_frame": 1000 * detection.seconds / frames if frames else 0.0,
        "ms_per_second": 1000 * detection.seconds / duration if duration else 0.0,
        "real_time_factor": detection.seconds / duration if duration else 0.0,
    }


def occupancy(scenarios: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """各情形合计的处理耗时除以声音的总时长，与目标比较；另给出最慢的情形。"""
    seconds = sum(s["timing"]["seconds"] for s in scenarios)
    audio = sum(s["duration"] for s in scenarios)
    frames = sum(s["timing"]["frames"] for s in scenarios)
    ms_per_second = 1000 * seconds / audio if audio else None
    slowest = max(scenarios, key=lambda s: s["timing"]["ms_per_second"], default=None)
    return {
        "audio_seconds": audio,
        "processing_seconds": seconds,
        "ms_per_second": ms_per_second,
        "ms_per_frame": 1000 * seconds / frames if frames else None,
        "slowest": None if slowest is None else slowest["name"],
        "slowest_ms_per_second": None if slowest is None else slowest["timing"]["ms_per_second"],
        "target_ms_per_second": CPU_TARGET_MS_PER_SECOND,
        "meets_target": ms_per_second is not None and ms_per_second <= CPU_TARGET_MS_PER_SECOND,
    }


def _owner(segment: Segment, lines: Sequence[SuiteLine]) -> SuiteLine | None:
    """与真值段重叠最多的一句。"""
    best: SuiteLine | None = None
    overlap = 0.0
    for line in lines:
        shared = min(segment.end, line.end) - max(segment.start, line.start)
        if shared > overlap:
            best, overlap = line, shared
    return best


def breakdown(
    truth: Sequence[Segment], lines: Sequence[SuiteLine], scenarios: Sequence[Mapping[str, Any]]
) -> dict[str, dict[str, dict[str, Any]]]:
    """设门槛的各情形合计，按语音与按语速分组：真值段数、漏判段数、开始与结束延迟的 p90。"""
    owners = [_owner(segment, lines) for segment in truth]
    groups: dict[str, dict[str, dict[str, list[Any]]]] = {"voice": {}, "rate": {}}
    for scenario in scenarios:
        if not scenario["gated"]:
            continue
        for detail, owner in zip(scenario["metrics"]["segments"], owners, strict=True):
            if owner is None:
                continue
            for key, value in (("voice", owner.voice), ("rate", f"{owner.rate:+d}")):
                bucket = groups[key].setdefault(value, {"missed": [], "onset": [], "offset": []})
                bucket["missed"].append(bool(detail.get("missed")))
                if detail.get("onset") is not None:
                    bucket["onset"].append(detail["onset"])
                if detail.get("offset") is not None:
                    bucket["offset"].append(detail["offset"])
    return {
        key: {
            value: {
                "segments": len(bucket["missed"]),
                "missed": sum(bucket["missed"]),
                "onset_p90": summary(bucket["onset"])["p90"],
                "offset_p90": summary(bucket["offset"])["p90"],
            }
            for value, bucket in values.items()
        }
        for key, values in groups.items()
    }


def run_evaluation(
    *,
    voice: Audio,
    backgrounds: Mapping[str, Audio],
    model: SpeechModel,
    params: VoiceParams,
    gains: Sequence[float] = DEFAULT_GAINS,
    vocal: Collection[str] = (),
    normal_mix: Audio | None = None,
    lines: Sequence[SuiteLine] = (),
    truth_params: TruthParams | None = None,
    collars: Collars | None = None,
    criteria: Criteria | None = None,
) -> dict[str, Any]:
    """评测一组录音，返回可写成 JSON 的结果。每种情形用新的核心，模型从零状态开始。

    `vocal` 是带人声的背景的名字；`lines` 是纯配音里每句的出处，有了即按语音与语速分组。
    """
    collars = collars or Collars()
    criteria = criteria or Criteria()
    unknown = set(vocal) - set(backgrounds)
    if unknown:
        raise ValueError(f"带人声的背景不在背景里：{sorted(unknown)}")
    truth = voice_truth(voice, truth_params)
    voice_level = voice_level_db(voice, truth.segments)
    background_levels = {
        name: background_level_db(audio, voice.length) for name, audio in backgrounds.items()
    }
    levels = {"voice": voice_level, "voice (whole)": rms_db(voice)}
    levels.update({f"background {name}": level for name, level in background_levels.items()})
    # 预热：ONNX Runtime 的第一次推理慢，不计入占用。
    warmup = round(WARMUP_SECONDS * voice.sample_rate)
    detect(Audio(voice.samples[:warmup], voice.sample_rate), model, params)
    scenarios: list[dict[str, Any]] = []
    for scenario in plan(list(backgrounds), gains, vocal, criteria):
        applied: float | None = None
        if scenario.background is None:
            audio = voice
        else:
            background = backgrounds[scenario.background]
            applied = relative_gain_db(
                voice_level, background_levels[scenario.background], scenario.gain_db or 0.0
            )
            if scenario.with_voice:
                audio = mix(voice, background, applied)
            else:
                audio = fitted(background, voice.length, applied)
        detection = detect(audio, model, params)
        segments: tuple[Segment, ...] = truth.segments if scenario.with_voice else ()
        metrics = evaluate(segments, detection.changes, audio.duration, collars)
        verdict: dict[str, Any] = {"gated": scenario.gated, "pass": None}
        if scenario.with_voice:
            verdict.update(judge(metrics, criteria))
            if not scenario.gated:
                verdict["pass"] = None
        scenarios.append(
            {
                "name": scenario.name,
                "background": scenario.background,
                "vocal": scenario.background in vocal,
                "gain_db": scenario.gain_db,
                "applied_gain_db": applied,
                "with_voice": scenario.with_voice,
                "gated": scenario.gated,
                "duration": audio.duration,
                "metrics": metrics,
                "verdict": verdict,
                "probability": _probabilities(detection),
                "timing": _timing(detection),
            }
        )
    gated = [s for s in scenarios if s["gated"]]
    failed = [s["name"] for s in gated if not s["verdict"]["pass"]]
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
        "criteria": asdict(criteria),
        "truth": {
            "params": asdict(truth.params),
            "threshold_db": truth.threshold_db,
            "segments": [[s.start, s.end] for s in truth.segments],
            "busy_seconds": sum(s.duration for s in truth.segments),
            "voice_duration": voice.duration,
        },
        "levels_db": levels,
        "scenarios": scenarios,
        "verdict": {"gated": len(gated), "passed": len(gated) - len(failed), "failed": failed},
        "occupancy": occupancy(scenarios),
        "breakdown": breakdown(truth.segments, lines, scenarios) if lines else None,
        "normal_mix": mixed,
    }
