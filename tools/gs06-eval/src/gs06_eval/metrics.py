"""判定与真值的比较：开始与结束的判定延迟、漏判与多判（段数与时长占比）、句中判闲。

口径见 `docs/modules/perception.md` 的“GS-06 的评测”。时刻以秒计，判定的忙段从转为忙的判定时刻
（`at_ns`）到下一条变化的判定时刻。
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from game_runtime.voice_activity import VoiceChange, VoiceState
from gs06_eval.truth import Segment

GRID = 0.001
"""算时长占比的网格（秒）。"""


@dataclass(frozen=True)
class Collars:
    onset: float = 0.25
    """每个真值段开头这么长不计入漏判的时长占比；开始得比真值早这么多以上的忙段算接着上一段。"""
    offset: float = 1.0
    """每个真值段结束后这么长不计入多判的时长占比，判多判的段数时真值段向后延这么长。"""


def intervals(changes: Sequence[VoiceChange], duration: float, state: VoiceState) -> list[Segment]:
    """处在 `state` 的时间段：从转入的判定时刻到下一条变化的判定时刻，最后一段到 `duration`。"""
    result: list[Segment] = []
    start: float | None = None
    for change in changes:
        at = change.at_ns / 1e9
        if change.state is state and start is None:
            start = at
        elif change.state is not state and start is not None:
            result.append(Segment(start, at))
            start = None
    if start is not None:
        result.append(Segment(start, duration))
    return result


def summary(values: Sequence[float]) -> dict[str, float | int | None]:
    """个数、平均、最小、中位数、p90、p95 与最大（秒）。"""
    if not values:
        return {
            "n": 0,
            "mean": None,
            "min": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "max": None,
        }
    array = np.asarray(values, dtype=np.float64)
    p50, p90, p95 = (float(v) for v in np.percentile(array, [50, 90, 95]))
    return {
        "n": len(values),
        "mean": float(array.mean()),
        "min": float(array.min()),
        "p50": p50,
        "p90": p90,
        "p95": p95,
        "max": float(array.max()),
    }


def _mask(
    segments: Sequence[Segment], cells: int, *, pad_after: float = 0.0
) -> np.ndarray[Any, Any]:
    mask = np.zeros(cells, dtype=bool)
    for segment in segments:
        first = max(0, math.floor(segment.start / GRID + 1e-9))
        last = min(cells, math.ceil((segment.end + pad_after) / GRID - 1e-9))
        mask[first:last] = True
    return mask


def _overlaps(a: Segment, b: Segment) -> bool:
    return a.start < b.end and a.end > b.start


def evaluate(
    truth: Sequence[Segment],
    changes: Sequence[VoiceChange],
    duration: float,
    collars: Collars | None = None,
) -> dict[str, Any]:
    """一种情形的指标。"""
    collars = collars or Collars()
    busy = intervals(changes, duration, VoiceState.BUSY)
    unknown = intervals(changes, duration, VoiceState.UNKNOWN)
    # 启动时的未知：第一条变化之前也是未知。
    if changes:
        unknown.insert(0, Segment(0.0, changes[0].at_ns / 1e9))
    else:
        unknown = [Segment(0.0, duration)]

    onsets: list[float] = []
    offsets: list[float] = []
    carried = merged = missed = 0
    details: list[dict[str, Any]] = []
    for index, segment in enumerate(truth):
        hits = [d for d in busy if _overlaps(d, segment)]
        detail: dict[str, Any] = {"start": segment.start, "end": segment.end}
        if not hits:
            missed += 1
            detail["missed"] = True
            details.append(detail)
            continue
        first = min(d.start for d in hits)
        last = max(d.end for d in hits)
        if first < segment.start - collars.onset:
            carried += 1
            detail["onset"] = None
        else:
            onsets.append(first - segment.start)
            detail["onset"] = first - segment.start
        following = truth[index + 1].start if index + 1 < len(truth) else None
        if (following is not None and last > following) or last >= duration:
            merged += 1
            detail["offset"] = None
        else:
            offsets.append(last - segment.end)
            detail["offset"] = last - segment.end
        details.append(detail)

    extended = [Segment(s.start, s.end + collars.offset) for s in truth]
    false_segments = [d for d in busy if not any(_overlaps(d, t) for t in extended)]

    early_idle = 0
    previous: VoiceState | None = None
    for change in changes:
        at = change.at_ns / 1e9
        if (
            change.state is VoiceState.IDLE
            and previous is VoiceState.BUSY
            and any(t.start < at < t.end for t in truth)
        ):
            early_idle += 1
        previous = change.state

    cells = max(1, math.ceil(duration / GRID))
    truth_busy = _mask(truth, cells)
    detected = _mask(busy, cells)
    known = ~_mask(unknown, cells)
    onset_collar = _mask([Segment(s.start, s.start + collars.onset) for s in truth], cells)
    offset_collar = _mask([Segment(s.end, s.end + collars.offset) for s in truth], cells)
    miss_base = truth_busy & known & ~onset_collar
    false_base = ~truth_busy & known & ~offset_collar
    miss_cells = int(np.count_nonzero(miss_base & ~detected))
    false_cells = int(np.count_nonzero(false_base & detected))
    miss_total = int(np.count_nonzero(miss_base))
    false_total = int(np.count_nonzero(false_base))

    return {
        "truth_segments": len(truth),
        "detected_segments": len(busy),
        "onset_latency": summary(onsets),
        "offset_latency": summary(offsets),
        "onset_carried": carried,
        "offset_merged": merged,
        "missed_segments": missed,
        "false_segments": len(false_segments),
        "early_idle": early_idle,
        "missed_fraction": miss_cells / miss_total if miss_total else None,
        "false_fraction": false_cells / false_total if false_total else None,
        "missed_seconds": miss_cells * GRID,
        "false_seconds": false_cells * GRID,
        "truth_busy_seconds": float(np.count_nonzero(truth_busy)) * GRID,
        "unknown_seconds": float(np.count_nonzero(~known)) * GRID,
        "busy": [[d.start, d.end] for d in busy],
        "false": [[d.start, d.end] for d in false_segments],
        "segments": details,
    }
