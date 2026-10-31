"""关键平面插件 `game-adapter`：把操作员在 profile 里选定的适配包以 `game-adapter@1` 交给其它插件。

设计见 `docs/modules/game-adapter.md` 的“插件与服务”。
"""

import asyncio
import logging
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from game_runtime.adapter.load import GAMES_DIR, load_adapter
from game_runtime.adapter.schema import Adapter, Ident
from game_runtime.kernel import Manifest, Plugin, PluginContext, ServiceToken, Tier

logger = logging.getLogger(__name__)

GAME_ADAPTER = ServiceToken[Adapter]("game-adapter", 1)
"""当前游戏适配包：校验过的声明，只读。"""


class GameAdapterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    game: Ident
    """适配包的 `id`，即 `games/` 下的目录名。"""
    games_dir: Path | None = None
    """适配包目录所在的目录，绝对路径；缺省为本仓库的 `games/`。"""

    @model_validator(mode="after")
    def _absolute(self) -> Self:
        if self.games_dir is not None and not self.games_dir.is_absolute():
            raise ValueError("games_dir 须为绝对路径")
        return self


class GameAdapterPlugin(Plugin[GameAdapterConfig]):
    """激活时读取并校验选定的适配包；不合法即激活失败，按内核的退避重试。"""

    manifest = Manifest(
        id="game-adapter",
        version="1",
        tier=Tier.CRITICAL,
        provides=(GAME_ADAPTER.ref,),
        config=GameAdapterConfig,
    )

    async def activate(self, ctx: PluginContext[GameAdapterConfig]) -> None:
        games_dir = ctx.config.games_dir or GAMES_DIR
        adapter = await asyncio.to_thread(load_adapter, games_dir / ctx.config.game)
        for item in adapter.uncalibrated():
            logger.warning("适配包 %s 的%s尚未标定", adapter.id, item)
        ctx.provide(GAME_ADAPTER, adapter)
