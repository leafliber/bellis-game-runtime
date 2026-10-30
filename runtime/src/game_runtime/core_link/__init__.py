"""主进程一侧的实时核管理：拉起非提权的实时核子进程、核连接、控制产出者心跳与 R-06 停止顺序。

设计见 `docs/modules/realtime-core.md` 的“启动与生命线”。
"""

from game_runtime.core_link.link import (
    CORE_ROLE,
    ENV_CHANNEL_KEY,
    ENV_CHANNEL_PATH,
    ENV_ROLE,
    CoreLink,
    CoreLinkOptions,
    CoreStartError,
    shutdown_runtime,
)
from game_runtime.core_link.process import (
    ManagedProcess,
    StopResult,
    StopStage,
    StopTimeouts,
    base_environment,
)

__all__ = [
    "CORE_ROLE",
    "ENV_CHANNEL_KEY",
    "ENV_CHANNEL_PATH",
    "ENV_ROLE",
    "CoreLink",
    "CoreLinkOptions",
    "CoreStartError",
    "ManagedProcess",
    "StopResult",
    "StopStage",
    "StopTimeouts",
    "base_environment",
    "shutdown_runtime",
]
