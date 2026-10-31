"""配音检测 `voice-activity@1`（GR-08）：从音频环读游戏的声音，判断配音忙、闲或未知。

设计见 `docs/modules/perception.md` 的“配音检测”。核心 `VoiceActivityDetector` 是“块进、变化出”的
纯逻辑，实时运行、回放与 GS-06 的评测共用；插件 `VoiceActivityPlugin` 是读音频环的外壳。
只用于测试的假模型在 `game_runtime.voice_activity.fake`。
"""

from game_runtime.voice_activity.detector import (
    FrameObserver,
    SpeechModel,
    VoiceActivityDetector,
)
from game_runtime.voice_activity.frontend import Frame, Frontend, Resampler
from game_runtime.voice_activity.models import SILERO_VAD, all_artifacts
from game_runtime.voice_activity.plugin import (
    VOICE_ACTIVITY,
    VoiceActivityConfig,
    VoiceActivityPlugin,
    VoiceActivityService,
    VoiceHub,
    VoiceListener,
    VoiceRunner,
)
from game_runtime.voice_activity.silero import (
    ModelInterfaceError,
    ModelNotFoundError,
    SileroVad,
    runtime_version,
)
from game_runtime.voice_activity.state import (
    INITIAL_STATUS,
    Hysteresis,
    UnknownReason,
    VoiceChange,
    VoiceParams,
    VoiceState,
    VoiceStatus,
)

__all__ = [
    "INITIAL_STATUS",
    "SILERO_VAD",
    "VOICE_ACTIVITY",
    "Frame",
    "FrameObserver",
    "Frontend",
    "Hysteresis",
    "ModelInterfaceError",
    "ModelNotFoundError",
    "Resampler",
    "SileroVad",
    "SpeechModel",
    "UnknownReason",
    "VoiceActivityConfig",
    "VoiceActivityDetector",
    "VoiceActivityPlugin",
    "VoiceActivityService",
    "VoiceChange",
    "VoiceHub",
    "VoiceListener",
    "VoiceParams",
    "VoiceRunner",
    "VoiceState",
    "VoiceStatus",
    "all_artifacts",
    "runtime_version",
]
