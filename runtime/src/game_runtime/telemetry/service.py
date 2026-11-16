"""Runtime 的命令、步骤和决策 span。服务及句柄由宿主事件循环调用。"""

from dataclasses import dataclass, field
from enum import StrEnum
from re import fullmatch
from secrets import randbits
from time import perf_counter_ns, time_ns

from game_runtime.telemetry.exporter import EventRecord, LocalExporter, SpanRecord, TelemetryConfig

_COMMANDS = ("goal.submit", "decision.resolve")
_CATEGORIES = ("advance", "navigate", "select")
_TIMINGS = ("timed", "untimed")
_STEP_EVENTS = ("located", "submitted", "executed", "effect_checked")
_FAILURES = ("rejected", "no-effect", "unknown")


class _Kind(StrEnum):
    COMMAND = "game.command"
    STEP = "game.step"
    DECISION = "game.decision"


_ATTRIBUTE = {
    _Kind.COMMAND: "bellis.game.command",
    _Kind.STEP: "bellis.game.category",
    _Kind.DECISION: "bellis.game.timing",
}
_OUTCOMES = {
    _Kind.COMMAND: ("accepted", "rejected"),
    _Kind.STEP: ("verified", "rejected", "no-effect"),
    _Kind.DECISION: ("opened", "rejected"),
}
_COMMON_OUTCOMES = ("unknown", "cancelled", "invalidated")


@dataclass(frozen=True, slots=True)
class SpanContext:
    """调用方在关联命令、目标或决策时捕获的连接代次、标识和父 traceparent。"""

    connection_generation: int
    traceparent: str | None = None
    goal_id: str | None = None
    decision_id: str | None = None


@dataclass(slots=True)
class _OpenSpan:
    generation: int
    goal_id: str | None
    decision_id: str | None
    kind: _Kind
    value: str
    trace_id: str
    span_id: str
    parent_id: str | None
    started_ns: int
    last_event: int = -1
    events: list[EventRecord] = field(default_factory=list[EventRecord])


def _allowed(value: object, choices: tuple[str, ...]) -> str | None:
    if isinstance(value, str):
        return next((choice for choice in choices if choice == value), None)
    return None


def _valid_id(value: object) -> bool:
    return value is None or (
        isinstance(value, str) and fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", value) is not None
    )


def parse_traceparent(value: object) -> tuple[str, str] | None:
    if isinstance(value, str) and (
        match := fullmatch(r"00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}", value)
    ):
        trace_id, span_id = match.group(1, 2)
        if trace_id != "0" * 32 and span_id != "0" * 16:
            return trace_id, span_id
    return None


def _new_id(digits: int) -> str:
    return f"{randbits(digits * 4) or 1:0{digits}x}"


class TelemetrySpan:
    """一个 span 的句柄。相同句柄只会产生一条关闭记录。"""

    def __init__(self, owner: "RuntimeTelemetry", traceparent: str) -> None:
        self._owner = owner
        self._traceparent = traceparent

    @property
    def traceparent(self) -> str:
        return self._traceparent

    def event(self, stage: str, *, at_monotonic_ns: int | None = None) -> None:
        """记录固定阶段。延后交付时使用阶段发生时采样的 `perf_counter_ns()`。"""
        self._owner._event(self, stage, at_monotonic_ns)  # pyright: ignore[reportPrivateUsage]

    def end(self, outcome: str) -> None:
        self._owner._end(self, outcome)  # pyright: ignore[reportPrivateUsage]


class RuntimeTelemetry:
    def __init__(self, config: TelemetryConfig) -> None:
        self._epoch_ns = time_ns()
        self._origin_ns = perf_counter_ns()
        self._max_active = config.max_active_spans
        self._exporter = LocalExporter(config)
        self._active: dict[TelemetrySpan, _OpenSpan] = {}
        self._closed = False

    def command_received(self, context: SpanContext, command: str) -> TelemetrySpan | None:
        value = _allowed(command, _COMMANDS)
        if (value == "goal.submit" and context.goal_id is not None) or (
            value == "decision.resolve" and context.decision_id is not None
        ):
            return self._start(context, _Kind.COMMAND, value)
        return None

    def step(self, context: SpanContext, category: str) -> TelemetrySpan | None:
        if (value := _allowed(category, _CATEGORIES)) is not None:
            return self._start(context, _Kind.STEP, value)
        return None

    def decision_opened(self, context: SpanContext, timing: str) -> TelemetrySpan | None:
        if context.decision_id is not None and (value := _allowed(timing, _TIMINGS)) is not None:
            return self._start(context, _Kind.DECISION, value)
        return None

    def records(self, handle: TelemetrySpan, ids: tuple[str, str]) -> bool:
        """核对活动句柄对应的实际导出标识。"""
        span = self._active.get(handle)
        return not self._closed and span is not None and ids == (span.trace_id, span.span_id)

    def reparent_step(self, handle: TelemetrySpan, parent: str | None) -> bool:
        """定位复核选定另一来源时，保留同一次尝试的起点和 span ID。"""
        span = self._active.get(handle)
        if self._closed or span is None or span.kind is not _Kind.STEP or span.events:
            return False
        parsed = parse_traceparent(parent)
        try:
            trace_id, parent_id = parsed if parsed is not None else (_new_id(32), None)
        except OSError:
            return False
        span.trace_id, span.parent_id = trace_id, parent_id
        handle._traceparent = f"00-{trace_id}-{span.span_id}-01"  # pyright: ignore[reportPrivateUsage]
        return True

    def invalidate(
        self,
        connection_generation: int,
        *,
        goal_id: str | None = None,
        decision_id: str | None = None,
    ) -> None:
        """关闭指定旧代次的活动 span；提供标识时进一步限定到相应目标或决策。"""
        for handle, span in tuple(self._active.items()):
            if (
                span.generation == connection_generation
                and (goal_id is None or span.goal_id == goal_id)
                and (decision_id is None or span.decision_id == decision_id)
            ):
                self._end(handle, "invalidated")

    def close(self) -> None:
        """停止收集，取消活动 span，并通知写线程在截止时间内排空。"""
        if self._closed:
            return
        self._closed = True
        for handle in tuple(self._active):
            self._end(handle, "cancelled")
        self._exporter.close()

    async def aclose(self) -> None:
        """独立使用服务时，在同一个停用时限内等写线程排空。"""
        self.close()
        await self._exporter.aclose()

    def _now_ns(self) -> int:
        return self._epoch_ns + perf_counter_ns() - self._origin_ns

    def _start(self, context: SpanContext, kind: _Kind, value: str) -> TelemetrySpan | None:
        if (
            self._closed
            or len(self._active) >= self._max_active
            or not self._exporter.accepting
            or type(context.connection_generation) is not int
            or context.connection_generation < 0
            or (context.goal_id is None and context.decision_id is None)
            or not _valid_id(context.goal_id)
            or not _valid_id(context.decision_id)
        ):
            return None
        parent = parse_traceparent(context.traceparent)
        try:
            trace_id, parent_id = parent if parent is not None else (_new_id(32), None)
            span_id = _new_id(16)
        except OSError:
            return None
        handle = TelemetrySpan(self, f"00-{trace_id}-{span_id}-01")
        self._active[handle] = _OpenSpan(
            context.connection_generation,
            context.goal_id,
            context.decision_id,
            kind,
            value,
            trace_id,
            span_id,
            parent_id,
            self._now_ns(),
        )
        return handle

    def _event(self, handle: TelemetrySpan, stage: str, at_monotonic_ns: int | None) -> None:
        span = self._active.get(handle)
        if span is None or span.kind is not _Kind.STEP:
            return
        if (name := _allowed(stage, _STEP_EVENTS)) is None:
            return
        index = _STEP_EVENTS.index(name)
        if index <= span.last_event:
            return
        now = self._now_ns()
        timestamp = (
            now if at_monotonic_ns is None else self._epoch_ns + at_monotonic_ns - self._origin_ns
        )
        previous = int(span.events[-1]["timeUnixNano"]) if span.events else span.started_ns
        if previous <= timestamp <= now:
            span.last_event = index
            span.events.append({"name": f"game.step.{name}", "timeUnixNano": str(timestamp)})

    def _end(self, handle: TelemetrySpan, outcome: str) -> None:
        span = self._active.pop(handle, None)
        if span is None:
            return
        result = _allowed(outcome, _OUTCOMES[span.kind] + _COMMON_OUTCOMES) or "unknown"
        record: SpanRecord = {
            "traceId": span.trace_id,
            "spanId": span.span_id,
            "name": span.kind.value,
            "kind": 2 if span.kind is _Kind.COMMAND else 1,
            "startTimeUnixNano": str(span.started_ns),
            "endTimeUnixNano": str(self._now_ns()),
            "attributes": [
                {"key": _ATTRIBUTE[span.kind], "value": {"stringValue": span.value}},
                {"key": "bellis.game.outcome", "value": {"stringValue": result}},
            ],
            "status": {"code": 2 if result in _FAILURES else 0},
        }
        if span.parent_id is not None:
            record["parentSpanId"] = span.parent_id
        if span.events:
            record["events"] = span.events
        self._exporter.put(record)
