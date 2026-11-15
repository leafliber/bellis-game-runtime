from decimal import Decimal

import pytest

from core_protocol import models
from game_runtime.core_link.fake_device import DeviceEvent, FakeEvent
from game_runtime.models import CallMetrics, Purpose, Usage
from game_runtime.step_control import Category, StepResult, StepStatus
from replay_benchmark.labels import Box, ClickTarget, FrameSpan, Labels, ModelPrice, Prices
from replay_benchmark.score import ObservedAction, ObservedFrame, ObservedStep, Trace, score
from replay_benchmark.telemetry import record_device


def _event(seq: int, at_ns: int, event: FakeEvent) -> DeviceEvent:
    return DeviceEvent(1, seq, at_ns, at_ns, event, frozenset())


def test_score_requires_both_core_execution_and_matching_device_event() -> None:
    labels = Labels(
        targets=(ClickTarget(first=10, last=10, box=Box(left=4, top=4, right=10, bottom=10)),),
        completion=FrameSpan(first=20, last=20),
    )
    trace = Trace(
        frames={1: ObservedFrame(10, 100, 105, "stable"), 2: ObservedFrame(20, 200, 205, "stable")}
    )
    trace.actions.extend(
        (
            ObservedAction(
                1,
                "click",
                110,
                (10, 6),
                1,
                models.MouseButton.LEFT.value,
                result="executed",
                executed_ns=120,
            ),
            ObservedAction(
                1,
                "click",
                130,
                (6, 6),
                2,
                models.MouseButton.LEFT.value,
                result="executed",
                executed_ns=140,
            ),
            ObservedAction(
                1, "click", 150, (6, 6), 3, models.MouseButton.LEFT.value, result="dropped"
            ),
            ObservedAction(
                1,
                "click",
                170,
                (6, 6),
                4,
                models.MouseButton.LEFT.value,
                result="executed",
                executed_ns=180,
            ),
        )
    )
    trace.steps.append(
        ObservedStep(
            210, StepResult(StepStatus.VERIFIED, "screen-verified", 1, Category.NAVIGATE, 40)
        )
    )
    trace.steps.append(
        ObservedStep(220, StepResult(StepStatus.REJECTED, "scene-changed", 2, None, None))
    )
    record_device(
        trace,
        [
            _event(i, timestamp, models.FakeMouseDown(button=models.MouseButton.LEFT))
            for i, timestamp in enumerate((115, 145, 175), start=1)
        ],
    )
    result = score(labels, trace, backend="policy", signatures=0)
    assert result.submitted_coordinate_hits == 1
    assert result.submitted_coordinate_wrong == 1
    assert [item.verdict for item in result.submitted_coordinates] == [
        "wrong",
        "missing-device-evidence",
        "not-executed",
        "hit",
    ]
    assert result.rejection_reasons == {"scene-changed": 1}
    assert result.replay_goal_steps == 4
    assert result.closed_loop_goal == "PENDING"
    assert result.model.status == "PENDING"


@pytest.mark.parametrize("seen", [(True, True), (True, False), (False, True)])
def test_actions_in_one_submission_use_distinct_device_events(seen: tuple[bool, bool]) -> None:
    # 一个动作块里的两次同键点击带同一提交时刻，但各有独立的按下和执行回执。
    left = models.MouseButton.LEFT
    clicks = [
        ObservedAction(1, "click", 100, point, i, left.value, result="executed", executed_ns=at_ns)
        for i, (point, at_ns) in enumerate([((5, 6), 120), ((7, 8), 170)], start=1)
    ]
    scroll = ObservedAction(1, "scroll", 100, (7, 8), 3, result="executed", executed_ns=220)
    trace = Trace(actions=[*clicks, scroll])
    events = [
        _event(seq, time, models.FakeMouseDown(button=left))
        for visible, seq, time in zip(seen, (1, 3), (115, 165), strict=True)
        if visible
    ]
    events.append(_event(5, 215, models.FakeScroll(id=3, x=7, y=8, notches=1)))
    record_device(trace, events)

    for action, visible, seq in zip(clicks, seen, (1, 3), strict=True):
        evidence = action.device_evidence
        if visible:
            assert evidence is not None
            assert evidence.match == "timed-press"
            assert evidence.seq == seq
            assert evidence.reported_point is None
        else:
            assert evidence is None
    identified = scroll.device_evidence
    assert identified is not None
    assert identified.match == "action-id"
    assert identified.seq == 5
    assert identified.reported_point == (7, 8)


def test_price_is_only_estimated_when_model_and_token_prices_are_known() -> None:
    labels = Labels(targets=())
    trace = Trace()
    trace.calls.append(
        CallMetrics(
            Purpose.AGENT, "vendor", "route", 0.2, None, Usage(100, 200, 100, 300), None, None
        )
    )
    prices = Prices(
        currency="CNY",
        models=(
            ModelPrice(
                vendor="vendor",
                model="route",
                input_per_million=Decimal(2),
                output_per_million=Decimal(4),
            ),
        ),
    )
    no_price = score(labels, trace, backend="model", signatures=0)
    assert no_price.model.calls_logged == 1
    assert no_price.model.status == "measured"
    assert no_price.model.cost_status == "PENDING"
    assert no_price.model.estimated_cost is None
    with_price = score(labels, trace, backend="model", signatures=0, prices=prices)
    assert with_price.model.estimated_cost == "0.001"
    assert with_price.model.currency == "CNY"
    trace.calls.append(
        CallMetrics(Purpose.REASONING, "vendor", "route", 0.2, None, None, None, None)
    )
    assert (
        score(labels, trace, backend="model", signatures=0, prices=prices).model.estimated_cost
        is None
    )
