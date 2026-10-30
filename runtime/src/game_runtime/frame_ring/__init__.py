"""帧环的主进程一侧：创建共享内存并写好头部，把 `frame_ring.attach` 的参数交给实时核，
按 seqlock 规则读最新一帧，算帧龄，最后销毁。实时核一侧在 `core/crates/frame-ring`。

设计见 `docs/modules/core-protocol.md` 的“帧环与音频环”。只用于自动测试的假显示面在
`game_runtime.frame_ring.fake`。
"""

from .ring import (
    DEFAULT_READ_ATTEMPTS,
    DEFAULT_SLOT_COUNT,
    FrameRing,
    FrameRingCorruptError,
    FrameRingError,
    FrameRingTornError,
    RingFrame,
    frame_age_ns,
    slot_stride_for,
)

__all__ = [
    "DEFAULT_READ_ATTEMPTS",
    "DEFAULT_SLOT_COUNT",
    "FrameRing",
    "FrameRingCorruptError",
    "FrameRingError",
    "FrameRingTornError",
    "RingFrame",
    "frame_age_ns",
    "slot_stride_for",
]
