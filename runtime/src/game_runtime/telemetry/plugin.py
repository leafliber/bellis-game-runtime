"""普通遥测插件。"""

from game_runtime.kernel import Manifest, Plugin, PluginContext, ServiceToken, Tier
from game_runtime.telemetry.exporter import TelemetryConfig
from game_runtime.telemetry.service import RuntimeTelemetry

TELEMETRY = ServiceToken[RuntimeTelemetry]("telemetry", 1)


class TelemetryPlugin(Plugin[TelemetryConfig]):
    manifest = Manifest(
        id="telemetry",
        version="1",
        tier=Tier.ORDINARY,
        provides=(TELEMETRY.ref,),
        config=TelemetryConfig,
    )

    async def activate(self, ctx: PluginContext[TelemetryConfig]) -> None:
        service = RuntimeTelemetry(ctx.config)
        ctx.register(service.close)
        ctx.provide(TELEMETRY, service)
