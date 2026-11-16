"""决策点的身份、计时与选项。选项坐标由控制器在执行前重新读取。"""

import re
from dataclasses import dataclass, replace
from typing import Literal

from game_runtime.adapter.schema import DecisionTimer
from game_runtime.ocr import OcrLine
from game_runtime.operator import messages
from game_runtime.step_control.contracts import Click, DecisionView, StepProposal, TextTarget
from game_runtime.step_control.phrase_location import PhraseIdentity
from game_runtime.step_control.risk import text_key

_TIME = re.compile(
    r"(?:(?P<minutes>[0-9]{1,3}):)?(?P<seconds>[0-9]{1,4})"
    r"(?:(?P<separator>[.:])(?P<fraction>[0-9]{1,3}))?"
)

type DecisionKey = (
    tuple[str, str, tuple[str, ...]] | tuple[str, str, tuple[str, ...], PhraseIdentity]
)


def timer_deadline(timer: DecisionTimer, text: str, capture_ns: int) -> int | None:
    """把屏幕剩余时间换成同一单调时钟上的保守截止时刻。"""
    text = text_key(text)
    prefix, suffix = text_key(timer.prefix), text_key(timer.suffix)
    if not text.startswith(prefix) or not text.endswith(suffix):
        return None
    body = text[len(prefix) :]
    if suffix:
        body = body[: -len(suffix)]
    match = _TIME.fullmatch(body)
    if match is None:
        return None
    seconds = int(match["seconds"])
    minutes = match["minutes"]
    if minutes is None and match["separator"] == ":":
        return None
    if minutes is not None:
        if seconds >= 60:
            return None
        seconds += 60 * int(minutes)
    fraction = match["fraction"] or ""
    unit = 10 ** (9 - len(fraction))
    remaining = seconds * 10**9 + int(fraction or "0") * unit
    # 显示可能向上舍入，至多扣除一个显示单位。
    return capture_ns + max(0, remaining - unit)


@dataclass(frozen=True, slots=True)
class DecisionEvidence:
    screen: str
    prompt: str
    lines: tuple[OcrLine, ...]
    deadline_ns: int | None
    proposed: StepProposal | None = None
    phrase_identity: PhraseIdentity | None = None

    @property
    def identity(self) -> DecisionKey:
        base = self.screen, text_key(self.prompt), tuple(text_key(line.text) for line in self.lines)
        return base if self.phrase_identity is None else (*base, self.phrase_identity)

    @property
    def view(self) -> DecisionView:
        match self.proposed:
            case StepProposal(action=Click(target=TextTarget(phrase=str(phrase)))):
                labels = (phrase,)
            case _:
                labels = tuple(line.text for line in self.lines)
        return DecisionView(
            self.prompt,
            tuple(messages.DecisionOption(index=i, text=text) for i, text in enumerate(labels)),
            self.deadline_ns,
        )


@dataclass(slots=True)
class PendingDecision:
    id: str
    evidence: DecisionEvidence
    candidates: tuple[int, ...]
    deadline_ns: int | None
    opened_frame: int
    selected: int | None = None
    selected_at_ns: int = 0
    selected_source: Literal["director", "vote"] | None = None
    selected_traceparent: str | None = None
    last_timing_frame: int = 0
    executed_at_ns: int | None = None
    opened_transform: int | None = None

    @property
    def view(self) -> DecisionView:
        return replace(self.evidence.view, deadline_ns=self.deadline_ns)

    def refresh_deadline(self, deadline: int | None) -> None:
        if deadline is not None and self.deadline_ns is not None:
            self.deadline_ns = min(deadline, self.deadline_ns)

    def accept(
        self,
        resolved: messages.DecisionResolved,
        now_ns: int,
        margin_ns: int,
        *,
        source: Literal["director", "vote"] | None = None,
        traceparent: str | None = None,
    ) -> bool:
        if (
            resolved.id == self.id
            and self.selected is None
            and 0 <= resolved.index < len(self.evidence.lines)
            and (self.deadline_ns is None or now_ns < self.deadline_ns - margin_ns)
        ):
            self.selected = resolved.index
            self.selected_at_ns = now_ns
            self.selected_source = source
            self.selected_traceparent = traceparent
            return True
        return False

    def choose(
        self, now_ns: int, margin_ns: int
    ) -> tuple[int, Literal["operator", "default"]] | None:
        if self.deadline_ns is not None:
            if now_ns >= self.deadline_ns:
                return None
            cutoff = self.deadline_ns - margin_ns
            if now_ns >= cutoff and (self.selected is None or self.selected_at_ns >= cutoff):
                return (self.candidates[0], "default") if self.candidates else None
        return None if self.selected is None else (self.selected, "operator")
