"""读画面的插件取帧的统一接口，设计见 `docs/modules/perception.md` 的“取帧”。

插件经 `ctx.frames` 取得宿主交给内核的帧源（清单须申请 `frames.read`）。
"""

from game_runtime.frames.source import (
    DEFAULT_POLL_INTERVAL,
    FrameSource,
    MemoryFrameSource,
    RingFrameSource,
    check_frame,
)

__all__ = [
    "DEFAULT_POLL_INTERVAL",
    "FrameSource",
    "MemoryFrameSource",
    "RingFrameSource",
    "check_frame",
]
