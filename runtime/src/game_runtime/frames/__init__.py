"""读画面的插件取帧的统一接口，设计见 `docs/modules/perception.md` 的“取帧”。

插件经 `ctx.frames` 取得宿主交给内核的帧源（清单须申请 `frames.read`）：实时运行是定序帧源
`SequencedFrameSource`，测试是 `MemoryFrameSource`，回放是 `game_runtime.recording` 的回放帧源。
"""

from game_runtime.frames.sequenced import (
    DEFAULT_BACKLOG,
    DEFAULT_IDLE_INTERVAL,
    DEFAULT_MAX_RATE,
    DEFAULT_POLL_INTERVAL,
    AdmissionPolicy,
    FrameBacklogError,
    FrameSink,
    SequencedFrameSource,
    SequencerStats,
)
from game_runtime.frames.source import FrameSource, MemoryFrameSource, check_frame

__all__ = [
    "DEFAULT_BACKLOG",
    "DEFAULT_IDLE_INTERVAL",
    "DEFAULT_MAX_RATE",
    "DEFAULT_POLL_INTERVAL",
    "AdmissionPolicy",
    "FrameBacklogError",
    "FrameSink",
    "FrameSource",
    "MemoryFrameSource",
    "SequencedFrameSource",
    "SequencerStats",
    "check_frame",
]
