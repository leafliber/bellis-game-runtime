"""主进程一侧的实时核管理：拉起非提权的实时核子进程、核连接、控制产出者心跳与动作块、R-06 停止顺序，
以及帧环与音频环的创建、挂接与采集目标、音频目标。

设计见 `docs/modules/realtime-core.md` 的“启动与生命线”“采集主循环”“音频主循环”与“动作块的执行”。
只用于测试的测试设备（测试许可源与假输入设备）在 `game_runtime.core_link.fake_device`，
这里不导入它。
"""

from game_runtime.core_link.audio import (
    DEFAULT_BLOCK_CAPACITY,
    DEFAULT_BLOCK_COUNT,
    DEFAULT_CHANNELS,
    DEFAULT_SAMPLE_RATE,
    AudioConfig,
)
from game_runtime.core_link.capture import (
    DEFAULT_MAX_HEIGHT,
    DEFAULT_MAX_WIDTH,
    CaptureConfig,
    WindowRule,
)
from game_runtime.core_link.link import (
    CORE_ROLE,
    ENV_CHANNEL_KEY,
    ENV_CHANNEL_PATH,
    ENV_OPERATOR_KEY,
    ENV_OPERATOR_PATH,
    ENV_ROLE,
    ENV_TEST_DEVICE_KEY,
    ENV_TEST_DEVICE_PATH,
    BellisCoreOptions,
    CoreLink,
    CoreLinkOptions,
    CoreStartError,
    RestartPolicy,
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
    "DEFAULT_BLOCK_CAPACITY",
    "DEFAULT_BLOCK_COUNT",
    "DEFAULT_CHANNELS",
    "DEFAULT_MAX_HEIGHT",
    "DEFAULT_MAX_WIDTH",
    "DEFAULT_SAMPLE_RATE",
    "ENV_CHANNEL_KEY",
    "ENV_CHANNEL_PATH",
    "ENV_OPERATOR_KEY",
    "ENV_OPERATOR_PATH",
    "ENV_ROLE",
    "ENV_TEST_DEVICE_KEY",
    "ENV_TEST_DEVICE_PATH",
    "AudioConfig",
    "BellisCoreOptions",
    "CaptureConfig",
    "CoreLink",
    "CoreLinkOptions",
    "CoreStartError",
    "ManagedProcess",
    "RestartPolicy",
    "StopResult",
    "StopStage",
    "StopTimeouts",
    "WindowRule",
    "base_environment",
    "shutdown_runtime",
]
