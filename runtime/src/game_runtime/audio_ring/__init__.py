"""音频环的主进程一侧：创建共享内存并写好头部，把 `audio_ring.attach` 的参数交给实时核，
按 seqlock 规则读最近一段，最后销毁。实时核一侧在 `core/crates/audio-ring`。

设计见 `docs/modules/core-protocol.md` 的“音频环”。插件经 `AudioReader`（`ctx.audio`）读；
只用于自动测试的假音频输出在 `game_runtime.audio_ring.fake`。
"""

from .ring import (
    DEFAULT_READ_ATTEMPTS,
    AudioBlock,
    AudioRead,
    AudioReader,
    AudioRing,
    AudioRingCorruptError,
    AudioRingError,
    AudioRingTornError,
    block_stride_for,
)

__all__ = [
    "DEFAULT_READ_ATTEMPTS",
    "AudioBlock",
    "AudioRead",
    "AudioReader",
    "AudioRing",
    "AudioRingCorruptError",
    "AudioRingError",
    "AudioRingTornError",
    "block_stride_for",
]
