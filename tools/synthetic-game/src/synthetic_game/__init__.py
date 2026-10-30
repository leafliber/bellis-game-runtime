"""合成游戏夹具（GR-30）。设计见 `docs/modules/offline-tools.md` 的“合成游戏”。

无窗口的状态机加 Pillow 渲染：`SyntheticGame.handle` 接受客户区物理像素下的点击、短按与滚动，
`SyntheticGame.frame` 给出 BGRA8 帧，`SyntheticGame.truth` 给测试核对画面类型与点击是否命中。
窗口外壳在 `synthetic_game.shell`。
"""

from synthetic_game.clock import Clock, ManualClock, MonotonicClock
from synthetic_game.game import (
    Effect,
    EffectKind,
    GameConfig,
    Overlay,
    SceneType,
    SyntheticGame,
    Target,
    Truth,
)
from synthetic_game.inputs import Click, Input, Key, MouseButton, Press, Scroll
from synthetic_game.layout import Rect
from synthetic_game.render import PIXEL_FORMAT, Frame

__all__ = [
    "PIXEL_FORMAT",
    "Click",
    "Clock",
    "Effect",
    "EffectKind",
    "Frame",
    "GameConfig",
    "Input",
    "Key",
    "ManualClock",
    "MonotonicClock",
    "MouseButton",
    "Overlay",
    "Press",
    "Rect",
    "SceneType",
    "Scroll",
    "SyntheticGame",
    "Target",
    "Truth",
]
