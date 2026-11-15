"""普通插件 `vlm`，从 `game-adapter@1` 与内核授权构造 `vlm@1`。"""

from game_runtime.adapter import GAME_ADAPTER
from game_runtime.kernel import Manifest, Plugin, PluginContext, ServiceToken, Tier
from game_runtime.models.client import ModelClient
from game_runtime.models.config import VlmConfig
from game_runtime.models.errors import ModelCallError, ModelFailure

VLM = ServiceToken[ModelClient]("vlm", 1)


class VlmPlugin(Plugin[VlmConfig]):
    manifest = Manifest(
        id="vlm",
        version="1",
        tier=Tier.ORDINARY,
        provides=(VLM.ref,),
        inject=(GAME_ADAPTER.ref,),
        permissions=VlmConfig().permissions,
        config=VlmConfig,
    )

    def __init__(self) -> None:
        self._client: ModelClient | None = None

    async def activate(self, ctx: PluginContext[VlmConfig]) -> None:
        if not ctx.permissions.issuperset(ctx.config.permissions):
            raise ModelCallError(ModelFailure.PERMISSION_DENIED)
        client = ModelClient(
            ctx.config, adapter=ctx.service(GAME_ADAPTER), permissions=ctx.permissions
        )
        self._client = client
        ctx.register(client.close)
        ctx.provide(VLM, client)

    async def quiesce(self, ctx: PluginContext[VlmConfig]) -> None:
        if self._client is not None:
            await self._client.close()


def vlm_plugin_for(config: VlmConfig) -> type[VlmPlugin]:
    """宿主在构造目录时按已选 profile 声明权限。内核仍逐项要求 profile 的 grant。"""
    required = config.permissions

    class ConfiguredVlmPlugin(VlmPlugin):
        manifest = VlmPlugin.manifest.model_copy(update={"permissions": required})

    return ConfiguredVlmPlugin
