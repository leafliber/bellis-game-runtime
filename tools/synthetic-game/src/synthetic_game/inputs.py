"""合成游戏接受的输入：统一动作空间里会到实时核的点击、短按与滚动。

这些是本包自己的小类型，不依赖核协议；假输入后端与窗口外壳各自把动作转换成它们。
坐标一律是客户区的物理像素，原点在客户区左上角。
"""

from dataclasses import dataclass
from enum import StrEnum


class MouseButton(StrEnum):
    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"


class Key(StrEnum):
    """合成游戏用到的键；其它键在外壳里直接丢弃。"""

    ENTER = "enter"
    SPACE = "space"
    ESCAPE = "escape"
    UP = "up"
    DOWN = "down"


@dataclass(frozen=True)
class Click:
    """在客户区 (x, y) 按下鼠标键。"""

    x: int
    y: int
    button: MouseButton = MouseButton.LEFT


@dataclass(frozen=True)
class Press:
    """短按一个键。"""

    key: Key


@dataclass(frozen=True)
class Scroll:
    """在客户区 (x, y) 滚动滚轮。

    `notches` 为格数，正数向上（远离用户），与 Windows 滚轮消息的符号一致。
    """

    x: int
    y: int
    notches: int


type Input = Click | Press | Scroll
