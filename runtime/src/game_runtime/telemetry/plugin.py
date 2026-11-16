"""普通遥测插件。"""

from collections.abc import Callable

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

    def __init__(self, activated: Callable[[RuntimeTelemetry], None] | None = None) -> None:
        self._activated = activated

    async def activate(self, ctx: PluginContext[TelemetryConfig]) -> None:
        service = RuntimeTelemetry(ctx.config)
        ctx.register(service.close)
        ctx.provide(TELEMETRY, service)
        if self._activated is not None:
            self._activated(service)


def telemetry_plugin_for(activated: Callable[[RuntimeTelemetry], None]) -> type[TelemetryPlugin]:
    class SupervisedTelemetryPlugin(TelemetryPlugin):
        def __init__(self) -> None:
            super().__init__(activated)

    return SupervisedTelemetryPlugin
