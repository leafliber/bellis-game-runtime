"""GS-04 的口径：字符准确率（按编辑距离）与时延分布，定义见 `docs/modules/perception.md` 的
“GS-04 的评测工具”。"""

import math
import statistics
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass


def strict_text(text: str) -> str:
    """严格口径：去掉全部空白。"""
    return "".join(text.split())


def nfkc_text(text: str) -> str:
    """宽松口径：NFKC 归一（全角与半角视为相同）后去掉全部空白。"""
    return strict_text(unicodedata.normalize("NFKC", text))


def edit_distance(expected: str, actual: str) -> int:
    """按字的 Levenshtein 距离：插入、删除与替换各计 1。"""
    if len(expected) < len(actual):
        expected, actual = actual, expected
    previous = list(range(len(actual) + 1))
    for i, a in enumerate(expected, 1):
        current = [i]
        for j, b in enumerate(actual, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (a != b)))
        previous = current
    return previous[-1]


@dataclass(frozen=True)
class Accuracy:
    distance: int
    """编辑距离之和。"""
    characters: int
    """真值字数之和。"""

    @property
    def value(self) -> float | None:
        """1 减去编辑距离之和除以真值字数之和；可以为负（多认出很多字时）。没有字时为 None。"""
        if self.characters == 0:
            return None
        return 1 - self.distance / self.characters


def accuracy(pairs: Sequence[tuple[str, str]]) -> Accuracy:
    """（真值、预测）对的字符准确率，两边都已按同一口径归一。"""
    return Accuracy(
        sum(edit_distance(expected, actual) for expected, actual in pairs),
        sum(len(expected) for expected, _ in pairs),
    )


@dataclass(frozen=True)
class Latency:
    """时延分布，单位毫秒。"""

    count: int
    mean: float
    p50: float
    p90: float
    p95: float
    p99: float
    max: float


def percentile(sorted_values: Sequence[float], percent: float) -> float:
    """最近秩法：第 ⌈p/100 · n⌉ 小的值。"""
    rank = max(1, math.ceil(percent / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def latency(milliseconds: Sequence[float]) -> Latency | None:
    if not milliseconds:
        return None
    values = sorted(milliseconds)
    return Latency(
        count=len(values),
        mean=statistics.fmean(values),
        p50=percentile(values, 50),
        p90=percentile(values, 90),
        p95=percentile(values, 95),
        p99=percentile(values, 99),
        max=values[-1],
    )
