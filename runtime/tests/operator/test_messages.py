"""操作员连接上的消息：拒绝未知字段，决策点的选项、候选与关闭方式彼此一致。"""

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from game_runtime.operator import messages


def option(index: int, text: str = "选项") -> dict[str, Any]:
    return {"index": index, "text": text}


def opened(**changes: Any) -> dict[str, Any]:
    return {
        "id": "d1",
        "prompt": "去哪里？",
        "options": [option(0), option(1)],
        "candidates": [1, 0],
        "deadline_ns": 5_000_000_000,
        **changes,
    }


SAMPLES: list[tuple[type[BaseModel], dict[str, Any]]] = [
    (
        messages.Session,
        {
            "adapter": "synthetic-game",
            "state": "running",
            "confirmed": ["window-visible"],
            "goal": None,
            "ends_at_ns": 1,
            "goal_revision": 4,
        },
    ),
    (messages.DecisionOpened, opened()),
    (messages.DecisionUpdated, {"id": "d1", "candidates": [1, 0], "deadline_ns": 4_000_000_000}),
    (messages.DecisionResolved, {"id": "d1", "index": 1}),
    (messages.DecisionClosed, {"id": "d1", "index": None, "by": "unknown", "executed_at_ns": 1}),
    (messages.Action, {"category": "advance", "summary": "回车"}),
]


@pytest.mark.parametrize(
    ("model", "value"), SAMPLES, ids=lambda item: getattr(item, "__name__", "")
)
def test_messages_round_trip_and_forbid_unknown_fields(
    model: type[BaseModel], value: dict[str, Any]
) -> None:
    parsed = model.model_validate(value)
    assert parsed.model_dump(mode="json") == value
    with pytest.raises(ValidationError, match="extra_forbidden"):
        model.model_validate({**value, "unexpected": True})
    if model is messages.DecisionOpened:
        nested = {**value, "options": [{**option(0), "x": 1}, option(1)]}
        with pytest.raises(ValidationError, match="extra_forbidden"):
            model.model_validate(nested)


@pytest.mark.parametrize(
    "changes",
    [
        {"options": []},
        {"options": [option(0), option(0)]},
        {"candidates": [2]},
        {"candidates": [0, 0]},
        {"prompt": ""},
        {"deadline_ns": -1},
    ],
)
def test_a_decision_must_be_consistent(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        messages.DecisionOpened.model_validate(opened(**changes))


def test_closing_by_withdrawal_or_unknown_has_no_index() -> None:
    for by in ("operator", "default"):
        messages.DecisionClosed(id="d", index=0, by=by)
        with pytest.raises(ValidationError):
            messages.DecisionClosed(id="d", index=None, by=by)
    for by in ("withdrawn", "unknown"):
        messages.DecisionClosed(id="d", index=None, by=by)
        with pytest.raises(ValidationError):
            messages.DecisionClosed(id="d", index=0, by=by)
    with pytest.raises(ValidationError, match="没有已执行的动作"):
        messages.DecisionClosed(id="d", index=None, by="withdrawn", executed_at_ns=1)


def test_unknown_states_and_categories_are_refused() -> None:
    with pytest.raises(ValidationError):
        messages.Session.model_validate({**SAMPLES[0][1], "state": "live"})
    with pytest.raises(ValidationError):
        messages.Action.model_validate({"category": "attack", "summary": "x"})
