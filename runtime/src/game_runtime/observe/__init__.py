"""只读运行入口：按适配包拉起实时核与感知插件，在终端里实时显示结果，可同时录制；不发任何输入，不连
Bellis。

设计见 `docs/modules/offline-tools.md` 的“只读运行”。命令行入口在 `game_runtime.observe.cli`
（`uv run observe`），这里不导入它；运行循环 `run_live` 与录制工具共用。
"""

from game_runtime.observe.display import Formatter, banner, clock, status_line, summary
from game_runtime.observe.hub import (
    Event,
    LanguageEvent,
    LanguageResult,
    LanguageWatch,
    LineEvent,
    LineKind,
    NarrativeFinal,
    ObserveHub,
    PluginEvent,
    SceneEvent,
    Share,
    VoiceEvent,
    scene_key,
)
from game_runtime.observe.plugin import (
    OBSERVER_ENTRY,
    ObserverPlugin,
    observe_catalog,
    observer_plugin,
)
from game_runtime.observe.run import Monitor, run_live

__all__ = [
    "OBSERVER_ENTRY",
    "Event",
    "Formatter",
    "LanguageEvent",
    "LanguageResult",
    "LanguageWatch",
    "LineEvent",
    "LineKind",
    "Monitor",
    "NarrativeFinal",
    "ObserveHub",
    "ObserverPlugin",
    "PluginEvent",
    "SceneEvent",
    "Share",
    "VoiceEvent",
    "banner",
    "clock",
    "observe_catalog",
    "observer_plugin",
    "run_live",
    "scene_key",
    "status_line",
    "summary",
]
