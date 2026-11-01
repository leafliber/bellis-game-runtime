"""剧情记录的数据：一句的记录、剧情范围、中断，以及比较文字用的归一与同一句的判断。

字段与规则见 `docs/modules/narrative-planner.md` 的“剧情记录”。
"""

import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

REVISION_TOLERANCE: Final = 4
"""同一句的文字容差：当前句的比较文本有 n 个字时，容许的编辑距离为 ⌊n / 4⌋。"""


class Completeness(StrEnum):
    """一句的完整度。"""

    PARTIAL = "partial"
    """还在显示，尚未判为完整。"""
    COMPLETE = "complete"
    """文字连续若干帧不变，或继续标记可见。"""
    GAP = "gap"
    """在部分时被结束（未读完整即被推进、换了画面或积压超限）；文字只是这一句的一部分。"""


@dataclass(frozen=True, slots=True)
class LowConfidenceLine:
    """识别置信度低的一行。"""

    roi: str
    text: str
    confidence: float


@dataclass(frozen=True, slots=True)
class NarrativeRecord:
    """一句的记录（某一时刻的状态）。"""

    session: int
    """运行号：插件每激活一次新开一个。"""
    occurrence: int
    """出现编号：本次运行内按出现的顺序从 1 起。"""
    scope: int
    """剧情范围号。"""
    screen: str | None
    """对白框所在的画面；对白框没写画面（通用缺省）时为 None。"""
    speaker: str | None
    """说话人：这一句第一次读到的非空名字；没有名字栏或一直读不到时为 None。"""
    text: str
    """最近一次修订读到的原文（各行依次连接）。"""
    revision: int
    """修订号：从 1 起，比较文本每变一次加一。"""
    completeness: Completeness
    first_frame: int
    first_ns: int
    last_frame: int
    last_ns: int
    revision_frame: int
    """读出当前文字的帧号。"""
    complete_frame: int | None
    """判为完整的帧号；现在不是完整时为 None。"""
    closed_frame: int | None
    """结束这一句的帧号；还没结束时为 None。"""
    low_confidence: tuple[LowConfidenceLine, ...]
    """当前修订里识别置信度低的行。"""

    @property
    def closed(self) -> bool:
        return self.closed_frame is not None


@dataclass(frozen=True, slots=True)
class Scope:
    """一个剧情范围。"""

    session: int
    scope: int
    after_screen: str | None
    """触发它的画面（坏结局、存读档之类）；运行的第一个范围为 None。"""
    first_frame: int
    """范围里第一句的首帧。"""


@dataclass(frozen=True, slots=True)
class Discontinuity:
    """积压超限时丢掉的一段更新。"""

    session: int
    first_frame: int
    last_frame: int
    count: int


def compare_key(text: str) -> str:
    """比较文本：NFKC 归一（全角与半角标点视为相同），去掉全部空白。"""
    return "".join(unicodedata.normalize("NFKC", text).split())


def continues(current: str, observed: str) -> bool:
    """`observed` 是否还是比较文本为 `current` 的那一句：`observed` 的前 n 个字与 `current`
    （n 个字）的编辑距离不超过 ⌊n / 4⌋。两者都不为空。"""
    head = observed[: len(current)]
    if head == current:
        return True
    return _within_distance(current, head, len(current) // REVISION_TOLERANCE)


def _within_distance(a: str, b: str, limit: int) -> bool:
    """`a` 与 `b` 的编辑距离是否不超过 `limit`：只算对角线两侧 `limit` 宽的带。"""
    if abs(len(a) - len(b)) > limit:
        return False
    if limit == 0:
        return a == b
    width = len(b)
    over = limit + 1
    previous = [j if j <= limit else over for j in range(width + 1)]
    for i in range(1, len(a) + 1):
        low, high = max(1, i - limit), min(width, i + limit)
        current = [over] * (width + 1)
        if i <= limit:
            current[0] = i
        for j in range(low, high + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost, over)
        if min(current[max(0, low - 1) : high + 1]) > limit:
            return False
        previous = current
    return previous[width] <= limit
