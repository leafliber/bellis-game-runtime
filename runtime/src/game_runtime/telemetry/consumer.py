"""可丢失的遥测调用和一次步骤尝试的本地关联。"""

from collections.abc import Callable
from dataclasses import dataclass, replace

from game_runtime.telemetry.service import (
    RuntimeTelemetry,
    SpanContext,
    TelemetrySpan,
    parse_traceparent,
)


def try_telemetry[T](operation: Callable[[], T]) -> T | None:
    try:
        return operation()
    except Exception:
        return None


def valid_traceparent(value: str | None) -> str | None:
    return value if parse_traceparent(value) is not None else None


def recorded_traceparent(
    span: TelemetrySpan,
    provider: RuntimeTelemetry,
    parent: str | None,
    lookup: Callable[[], RuntimeTelemetry | None],
) -> str | None:
    value = try_telemetry(lambda: span.traceparent)
    ids = parse_traceparent(value)
    old = parse_traceparent(parent)
    if ids is None or (old is not None and (ids[0] != old[0] or ids[1] == old[1])):
        return None
    if try_telemetry(lookup) is not provider or not try_telemetry(
        lambda: provider.records(span, ids)
    ):
        return None
    return value


@dataclass(slots=True)
class StepTrace:
    context: SpanContext
    provider: RuntimeTelemetry
    span: TelemetrySpan | None
    submitted_at: int | None = None

    def event(self, stage: str, *, at_monotonic_ns: int | None = None) -> None:
        span = self.span
        if span is not None:
            if at_monotonic_ns is None:
                try_telemetry(lambda: span.event(stage))
            else:
                try_telemetry(lambda: span.event(stage, at_monotonic_ns=at_monotonic_ns))

    def flush_submitted(self) -> None:
        at, self.submitted_at = self.submitted_at, None
        if at is not None:
            self.event("submitted", at_monotonic_ns=at)

    def end(self, outcome: str) -> None:
        self.flush_submitted()
        span, self.span = self.span, None
        if span is not None:
            try_telemetry(lambda: span.end(outcome))

    def reparent(self, current: SpanContext | None) -> None:
        span = self.span
        if (
            span is not None
            and current is not None
            and replace(self.context, traceparent=current.traceparent) == current
            and try_telemetry(lambda: self.provider.reparent_step(span, current.traceparent))
        ):
            self.context = current
        else:
            self.end("invalidated")

    def for_context(
        self, context: SpanContext | None, lookup: Callable[[], RuntimeTelemetry | None]
    ) -> str | None:
        span = self.span
        if span is None or context != self.context:
            return None
        return recorded_traceparent(span, self.provider, self.context.traceparent, lookup)
