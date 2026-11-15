"""规划用途的模型回复。权限、实例号与结果由本机代码给出。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from game_runtime.vision_agent.effects import EffectReply

type ShortText = Annotated[str, Field(min_length=1, max_length=240)]


class _Reply(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NoteReply(_Reply):
    section: Annotated[str, Field(min_length=1, max_length=48)]
    text: Annotated[str, Field(max_length=1000)]


class HighlightReply(_Reply):
    kind: Literal["evidence", "testimony", "contradiction", "reversal"]
    text: ShortText
    importance: Literal["high", "medium"]


class SummaryReply(_Reply):
    summary: Annotated[str, Field(max_length=2000)]
    notes: Annotated[tuple[NoteReply, ...], Field(max_length=32)] = ()
    highlights: Annotated[tuple[HighlightReply, ...], Field(max_length=8)] = ()


class ChoiceReply(_Reply):
    index: Annotated[int, Field(ge=0)]
    reason: ShortText
    quotes: Annotated[tuple[int, ...], Field(max_length=4)] = ()


class DecisionReply(_Reply):
    ranked: Annotated[tuple[ChoiceReply, ...], Field(min_length=1, max_length=32)]


class SubgoalReply(_Reply):
    text: ShortText
    completion: EffectReply | None = None


class GoalReply(_Reply):
    subgoals: Annotated[tuple[SubgoalReply, ...], Field(min_length=1, max_length=8)]
    overall_completion: EffectReply | None = None


class EndingReply(_Reply):
    summary: ShortText
