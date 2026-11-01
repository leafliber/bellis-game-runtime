"""操作员控制台与操作员连接（GR-36），设计见 `docs/modules/game-bridge.md` 的“独立运行”。

- `console`：控制台的核心，测试会话即测试许可，是实时核的许可源与主进程的对端；
- `terminal`、`cli`：终端界面与入口 `uv run operate <适配包 id>`；
- `client`：主进程一侧的 `OperatorClient`；
- `messages`：操作员连接上的消息。

入口 `cli` 不在这里导入。
"""

from game_runtime.operator.client import (
    OperatorAdapterMismatchError,
    OperatorClient,
    OperatorError,
    OperatorUnavailableError,
)
from game_runtime.operator.console import (
    DEFAULT_MAX_MINUTES,
    LEASE_TTL_NS,
    MAIN_ROLE,
    PAUSE_ON,
    RENEW_INTERVAL_NS,
    SUPERVISED,
    Clock,
    Confirmation,
    ConsoleError,
    ConsoleEvent,
    ConsoleOptions,
    EventKind,
    MonotonicClock,
    OperatorConsole,
    SessionLog,
    sessions_dir,
)
from game_runtime.operator.messages import SessionState

__all__ = [
    "DEFAULT_MAX_MINUTES",
    "LEASE_TTL_NS",
    "MAIN_ROLE",
    "PAUSE_ON",
    "RENEW_INTERVAL_NS",
    "SUPERVISED",
    "Clock",
    "Confirmation",
    "ConsoleError",
    "ConsoleEvent",
    "ConsoleOptions",
    "EventKind",
    "MonotonicClock",
    "OperatorAdapterMismatchError",
    "OperatorClient",
    "OperatorConsole",
    "OperatorError",
    "OperatorUnavailableError",
    "SessionLog",
    "SessionState",
    "sessions_dir",
]
