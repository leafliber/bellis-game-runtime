"""可注入的时钟：状态机只从这里读时间，测试用手动时钟推进，不真睡。"""

import time
from typing import Protocol


class Clock(Protocol):
    """单调时钟，单位为秒；只要求单调不减，起点任意。"""

    def now(self) -> float: ...


class MonotonicClock:
    """真实时钟，窗口外壳使用。"""

    def now(self) -> float:
        return time.monotonic()


class ManualClock:
    """只在调用 `advance` 时前进的时钟，供测试与假后端使用。"""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError(f"时钟不能倒退：{seconds}")
        self._now += seconds
