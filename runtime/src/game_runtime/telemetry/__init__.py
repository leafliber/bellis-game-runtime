"""Game Runtime 的本地 `telemetry@1` 服务。"""

from game_runtime.telemetry.exporter import TelemetryConfig
from game_runtime.telemetry.plugin import TELEMETRY, TelemetryPlugin
from game_runtime.telemetry.service import RuntimeTelemetry, SpanContext, TelemetrySpan

__all__ = [
    "TELEMETRY",
    "RuntimeTelemetry",
    "SpanContext",
    "TelemetryConfig",
    "TelemetryPlugin",
    "TelemetrySpan",
]
