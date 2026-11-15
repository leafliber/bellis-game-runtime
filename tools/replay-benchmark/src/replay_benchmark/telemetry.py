"""在输入边界记录提交坐标、实时核回执与假设备的旁证。"""

import logging
import time
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock

from core_protocol import models
from game_runtime.core_link import CoreLink, CoreLinkOptions
from game_runtime.core_link.fake_device import DeviceEvent, FakeEvent
from game_runtime.kernel import ControlGrant, Submission
from game_runtime.models import CallMetrics
from game_runtime.step_control import StepResult
from replay_benchmark.score import DeviceEvidence, ObservedAction, ObservedStep, Trace


class _Submission:
    def __init__(self, submitted: Submission, actions: dict[int, ObservedAction]) -> None:
        self._submitted = submitted
        self._actions = actions

    @property
    def block(self) -> models.ActionBlock:
        return self._submitted.block

    @property
    def result(self) -> models.Accepted | models.Rejected:
        return self._submitted.result

    async def outcomes(self) -> Mapping[int, models.Executed | models.Dropped | models.Uncertain]:
        results = await self._submitted.outcomes()
        for identity, outcome in results.items():
            action = self._actions.get(identity)
            if action is None:
                continue
            if isinstance(outcome, models.Executed):
                action.result = "executed"
                action.executed_ns = outcome.at_ns
            elif isinstance(outcome, models.Dropped):
                action.result = "dropped"
            else:
                action.result = "uncertain"
        return results


def core_for(trace: Trace) -> type[CoreLink]:
    class RecordingCore(CoreLink):
        def __init__(
            self,
            options: CoreLinkOptions,
            *,
            on_health: Callable[[models.CoreHealthParams], None] | None = None,
            on_released: Callable[[models.InputReleasedParams], None] | None = None,
            on_restart: Callable[[int], None] | None = None,
        ) -> None:
            check_fake_options(options)
            super().__init__(
                options, on_health=on_health, on_released=on_released, on_restart=on_restart
            )

        async def submit(
            self,
            grant: ControlGrant,
            steps: Sequence[models.ControlStep],
            *,
            base_frame: int,
            valid_for: float,
            start_delay: float = 0.0,
        ) -> Submission:
            now = time.perf_counter_ns()
            actions: dict[int, ObservedAction] = {}
            for step in steps:
                for item in step.actions:
                    action = item.root
                    point = (
                        (action.x, action.y)
                        if isinstance(action, models.Click | models.Scroll)
                        else None
                    )
                    control: int | str | None = None
                    if isinstance(action, models.Click):
                        control = action.button.value
                    elif isinstance(action, models.KeyPress):
                        control = action.scan_code
                    actions[action.id] = ObservedAction(
                        base_frame, action.type, now, point, action.id, control
                    )
            trace.actions.extend(actions.values())
            sent = await super().submit(
                grant, steps, base_frame=base_frame, valid_for=valid_for, start_delay=start_delay
            )
            if isinstance(sent.result, models.Rejected):
                for action in actions.values():
                    action.result = "rejected"
            return _Submission(sent, actions)

    return RecordingCore


def check_fake_options(options: CoreLinkOptions) -> None:
    capture = options.capture
    if (
        options.real_input
        or options.test_device is None
        or options.operator is None
        or options.input_policy is None
        or capture is None
        or not isinstance(capture.target, models.FakeTarget)
        or options.audio is not None
    ):
        raise ValueError("离线基准必须使用假采集、假设备、操作员许可与输入策略，并关闭音频采集")


def _matches_press(action: ObservedAction, event: FakeEvent) -> bool:
    if isinstance(event, models.FakeMouseDown):
        return action.kind == "click" and action.control == event.button.value
    if isinstance(event, models.FakeKeyDown):
        return action.kind == "key_press" and action.control == event.scan_code
    return False


def _reports_action_id(action: ObservedAction, event: FakeEvent) -> bool:
    return (
        isinstance(
            event, models.FakeClick | models.FakeKeyPress | models.FakeScroll | models.FakeMouseMove
        )
        and action.kind == event.type
        and action.action_id == event.id
    )


def record_device(trace: Trace, events: Sequence[DeviceEvent]) -> None:
    trace.device_events = len(events)
    ordered = sorted(events, key=lambda event: event.at_ns)
    times = [event.at_ns for event in ordered]
    completed: list[tuple[int, ObservedAction]] = []
    for action in trace.actions:
        action.device_evidence = None
        if action.result == "executed" and action.executed_ns is not None:
            completed.append((action.executed_ns, action))
    completed.sort(key=lambda item: item[0])
    receipt_counts = Counter(instant for instant, _ in completed)
    assigned: set[tuple[int, int]] = set()
    last_execution_ns = -1
    for executed_ns, action in completed:
        previous_ns, last_execution_ns = last_execution_ns, executed_ns
        start = bisect_left(times, action.submitted_ns)
        end = bisect_right(times, executed_ns)
        matching = [item for item in ordered[start:end] if _reports_action_id(action, item.event)]
        if matching:
            match = "action-id"
        elif receipt_counts[executed_ns] == 1:
            # 核在每个事件发出后记录 Executed。同一提交内也可按相邻回执划窗。
            start = bisect_right(times, previous_ns, lo=start, hi=end)
            matching = [item for item in ordered[start:end] if _matches_press(action, item.event)]
            match = "timed-press"
        else:
            continue
        if len(matching) != 1:
            continue
        (item,) = matching
        key = item.connection, item.seq
        event = item.event
        point = (
            (event.x, event.y) if isinstance(event, models.FakeClick | models.FakeScroll) else None
        )
        if (
            key in assigned
            or (point is not None and point != action.submitted_point)
            or (isinstance(event, models.FakeClick) and event.button.value != action.control)
            or (isinstance(event, models.FakeKeyPress) and event.scan_code != action.control)
        ):
            continue
        action.device_evidence = DeviceEvidence(
            match, item.type, item.connection, item.seq, item.at_ns, point
        )
        assigned.add(key)


_trace: ContextVar[Trace | None] = ContextVar("benchmark_trace", default=None)
_log_lock = Lock()
_prior_levels: dict[logging.Logger, int] = {}


class _Metrics(logging.Handler):
    def __init__(self, trace: Trace) -> None:
        super().__init__()
        self.trace = trace

    def emit(self, record: logging.LogRecord) -> None:
        if _trace.get() is not self.trace:
            return
        metrics = getattr(record, "model_call", None)
        step = getattr(record, "step_result", None)
        if isinstance(metrics, CallMetrics):
            self.trace.calls.append(metrics)
        if isinstance(step, StepResult):
            self.trace.steps.append(ObservedStep(time.perf_counter_ns(), step))


@contextmanager
def collect(trace: Trace) -> Generator[None]:
    loggers = (
        logging.getLogger("game_runtime.models.client"),
        logging.getLogger("game_runtime.step_control.controller"),
    )
    handler = _Metrics(trace)
    token = _trace.set(trace)
    try:
        with _log_lock:
            for logger in loggers:
                if logger not in _prior_levels:
                    _prior_levels[logger] = logger.level
                    logger.setLevel(logging.INFO)
                logger.addHandler(handler)
        yield
    finally:
        _trace.reset(token)
        with _log_lock:
            for logger in loggers:
                logger.removeHandler(handler)
                if not any(isinstance(item, _Metrics) for item in logger.handlers):
                    logger.setLevel(_prior_levels.pop(logger))
