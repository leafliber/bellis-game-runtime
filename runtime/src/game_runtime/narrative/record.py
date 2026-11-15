"""剧情记录的数据：一句的记录、剧情范围、播片段、中断，以及比较文字用的归一与同一句的判断。

字段与规则见 `docs/modules/narrative-planner.md` 的“剧情记录”。
"""

import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from game_runtime.adapter import ScreenType

REVISION_TOLERANCE: Final = 4
"""同一句的容差：当前句的比较文本有 n 个字时，文字容许的编辑距离为 ⌊n / 4⌋；名字较长一方有 m
个字时为 max(1, ⌊m / 4⌋)。"""


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
    cutscene: int | None
    """播片中读到的句子（字幕）所在的播片段编号；其余为 None。"""
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
class ScreenRecord:
    """已接受的画面观察，按原帧归入本机剧情时间线。"""

    session: int
    scope: int
    frame_number: int
    capture_ns: int
    transform_version: int
    screen_type: ScreenType
    confidence: float
    text: str


@dataclass(frozen=True, slots=True)
class Scope:
    """一个剧情范围。"""

    session: int
    scope: int
    after_screen: str | None
    """触发它的画面（坏结局、存读档之类）；运行的第一个范围为 None。"""
    first_frame: int
    """范围里第一句或第一个播片段的首帧。"""


@dataclass(frozen=True, slots=True)
class Cutscene:
    """一个播片段（某一时刻的状态）：`scene@1` 判出的一段播片，与其中读到的字幕。"""

    session: int
    index: int
    """播片段编号：本次运行内从 1 起；不占句子的出现编号。"""
    scope: int
    """剧情范围号。"""
    first_frame: int
    """判出播片的帧号。"""
    first_ns: int
    """`scene@1` 给出的开始时刻。"""
    last_frame: int
    """最后一个在播片中的帧的帧号。"""
    last_ns: int
    end_ns: int | None
    """`scene@1` 给出的结束时刻；还没结束或结束时刻随中断丢了时为 None。"""
    closed_frame: int | None
    """不再在播片中的那一帧；还没结束时为 None。"""
    sentences: tuple[int, ...]
    """其中各句的出现编号。"""

    @property
    def closed(self) -> bool:
        return self.closed_frame is not None

    @property
    def duration_ns(self) -> int:
        """时长：开始到结束时刻；结束时刻为空时到最后一个在播片中的帧。"""
        end = self.last_ns if self.end_ns is None else self.end_ns
        return max(0, end - self.first_ns)


@dataclass(frozen=True, slots=True)
class Discontinuity:
    """积压超限时丢掉的一段更新。"""

    session: int
    first_frame: int
    last_frame: int
    count: int


def compare_key(text: str) -> str:
    """比较文本：NFKC 归一后只保留字母与数字（Unicode 一般类别 L 与 N），去掉空白、标点与符号。"""
    return "".join(
        char
        for char in unicodedata.normalize("NFKC", text)
        if unicodedata.category(char)[0] in "LN"
    )


def continues(current: str, observed: str, *, partial: bool = False) -> bool:
    """`observed` 是否还是比较文本为 `current` 的那一句：`observed` 有一个前缀与 `current`
    （n 个字）的编辑距离不超过 ⌊n / 4⌋，前缀的长度不限。`observed` 比 `current` 短超过 ⌊n / 4⌋
    个字时没有这样的前缀（重新开始的逐字显示）。当前句还是部分（`partial`）而 n 不到 4 时容差取 1：
    那只是逐字显示开头的读数。"""
    if observed.startswith(current):
        return True
    limit = len(current) // REVISION_TOLERANCE
    if partial and limit == 0:
        limit = 1
    if len(observed) < len(current) - limit:
        return False
    row = _banded_row(current, observed[: len(current) + limit], limit)
    return row is not None and min(row[max(0, len(current) - limit) :]) <= limit


def same_name(current: str, observed: str) -> bool:
    """两边都读到了名字时，比较文本为 `current` 与 `observed` 的是不是同一个名字：编辑距离不超过
    max(1, ⌊m / 4⌋)，m 为较长一方的字数。"""
    if current == observed:
        return True
    limit = max(1, max(len(current), len(observed)) // REVISION_TOLERANCE)
    if abs(len(current) - len(observed)) > limit:
        return False
    row = _banded_row(current, observed, limit)
    return row is not None and row[len(observed)] <= limit


def _banded_row(a: str, b: str, limit: int) -> list[int] | None:
    """`a` 与 `b` 的各个前缀的编辑距离表的最后一行：第 j 项是 `a` 与 `b[:j]` 的距离，超过 `limit`
    的记为 `limit + 1`。只算对角线两侧 `limit` 宽的带；某一行全都超过 `limit` 时提前返回 None。
    `len(b)` 不小于 `len(a) - limit`。"""
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
            return None
        previous = current
    return previous
