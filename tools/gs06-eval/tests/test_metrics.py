"""指标：用写好的变化序列与真值，延迟、漏判、多判与句中判闲都是已知的。"""

import pytest

from game_runtime.voice_activity import UnknownReason, VoiceChange, VoiceState
from gs06_eval.metrics import Collars, evaluate, intervals, summary
from gs06_eval.truth import Segment

S = 1_000_000_000


def change(state: VoiceState, at: float) -> VoiceChange:
    reason = UnknownReason.GAP if state is VoiceState.UNKNOWN else None
    return VoiceChange(state, round(at * S), round(at * S), reason)


BUSY, IDLE, UNKNOWN = VoiceState.BUSY, VoiceState.IDLE, VoiceState.UNKNOWN


def test_latencies_and_errors() -> None:
    truth = [Segment(1.0, 2.0), Segment(4.0, 5.0), Segment(5.3, 6.0), Segment(8.0, 9.0)]
    changes = [
        change(IDLE, 0.5),
        change(BUSY, 1.1),  # 开始晚 100 ms
        change(IDLE, 1.5),  # 句中判闲
        change(BUSY, 1.6),
        change(IDLE, 2.6),  # 结束晚 600 ms
        change(BUSY, 4.2),  # 开始晚 200 ms，一直忙到下一个真值段
        change(IDLE, 6.5),  # 第三段的结束晚 500 ms
        change(BUSY, 11.0),  # 多判
        change(IDLE, 11.5),
    ]
    m = evaluate(truth, changes, 12.0, Collars(onset=0.25, offset=1.0))
    assert m["truth_segments"] == 4
    assert m["detected_segments"] == 4
    assert m["onset_latency"]["n"] == 2
    assert m["onset_latency"]["min"] == pytest.approx(0.1)
    assert m["onset_latency"]["max"] == pytest.approx(0.2)
    # 第二段的忙延续到第三段，结束不计；第三段的开始接着上一段，不计。
    assert (m["offset_merged"], m["onset_carried"]) == (1, 1)
    assert sorted(m["offset_latency"][k] for k in ("min", "max")) == pytest.approx([0.5, 0.6])
    assert m["missed_segments"] == 1  # 第四段
    assert m["false_segments"] == 1
    assert m["early_idle"] == 1
    # 漏判：第一段 [1.25, 2) 里的 [1.5, 1.6)，第四段 [8.25, 9)；真值忙去掉开头容差共 2.7 秒。
    assert m["missed_seconds"] == pytest.approx(0.1 + 0.75, abs=0.002)
    assert m["missed_fraction"] == pytest.approx(0.85 / 2.7, abs=0.001)
    # 多判：[5, 5.3) 与 [6, 6.5) 都在结束后的容差里，只剩 [11, 11.5)。
    assert m["false_seconds"] == pytest.approx(0.5, abs=0.002)
    # 开头 0.5 秒是未知（启动），不计入比例。
    assert m["unknown_seconds"] == pytest.approx(0.5, abs=0.002)


def test_unknown_time_is_excluded() -> None:
    truth = [Segment(1.0, 3.0)]
    changes = [change(BUSY, 1.0), change(UNKNOWN, 2.0), change(BUSY, 2.5), change(IDLE, 3.5)]
    m = evaluate(truth, changes, 5.0)
    assert m["missed_seconds"] == pytest.approx(0.0, abs=0.002)
    assert m["unknown_seconds"] == pytest.approx(1.0 + 0.5, abs=0.002)
    assert intervals(changes, 5.0, UNKNOWN) == [Segment(2.0, 2.5)]


def test_no_detection_at_all() -> None:
    m = evaluate([Segment(1.0, 2.0)], [], 3.0)
    assert m["missed_segments"] == 1
    assert m["missed_fraction"] is None  # 全程未知
    assert summary([]) == {
        "n": 0,
        "mean": None,
        "min": None,
        "p50": None,
        "p90": None,
        "p95": None,
        "max": None,
    }
