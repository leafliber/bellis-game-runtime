"""音频环的二进制布局，见 `docs/modules/core-protocol.md` 的“音频环”。

布局的源是 Rust 的 `core_protocol::audio_ring`；这里是手写的镜像，由 `tests/test_ring_layouts.py`
与 Rust 导出的 `protocol/audio-ring-layout.json` 逐字段核对。整数与样本值按本机字节序。
"""

import ctypes
from typing import Final

MAGIC: Final = int.from_bytes(b"BGRAUDIO", "little")
"""音频环头部的 magic：小端读出的 `b"BGRAUDIO"`。"""
LAYOUT_VERSION: Final = 1
"""布局版本：头部、块头部或下列常量变化时加一。"""
BLOCKS_OFFSET: Final = 64
"""块槽 0 相对共享内存起点的偏移。"""
BLOCK_SAMPLES_OFFSET: Final = 64
"""样本值相对块槽起点的偏移。"""
BLOCK_ALIGN: Final = 64
"""块槽跨度的对齐字节数。"""
SAMPLE_FORMAT_F32: Final = 1
"""样本格式 F32：每个样本值是 32 位 IEEE 浮点，各声道交错排列。"""
BYTES_PER_VALUE: Final = 4
"""一个 F32 样本值的字节数。"""
MAX_CHANNELS: Final = 2
"""声道数的上限。"""
MIN_SAMPLE_RATE: Final = 8_000
"""采样率的下限（Hz）。"""
MAX_SAMPLE_RATE: Final = 192_000
"""采样率的上限（Hz）。"""
FLAG_DISCONTINUITY: Final = 1
"""块标志：这一块与上一块之间不连续（丢了声音或时间轴断开）。"""
KNOWN_FLAGS: Final = FLAG_DISCONTINUITY
"""已定义的全部块标志；其余位须为 0。"""


class AudioRingHeader(ctypes.Structure):
    """音频环头部。

    主进程创建时写入除 `write_seq` 外的全部字段，此后不变；`write_seq` 只由实时核写。
    """

    _fields_ = (
        ("magic", ctypes.c_uint64),
        ("layout_version", ctypes.c_uint32),
        ("block_count", ctypes.c_uint32),
        ("block_stride", ctypes.c_uint64),
        ("block_capacity", ctypes.c_uint32),
        ("sample_rate", ctypes.c_uint32),
        ("channels", ctypes.c_uint32),
        ("sample_format", ctypes.c_uint32),
        ("write_seq", ctypes.c_uint64),  # Rust 侧为 AtomicU64
    )
    magic: int
    layout_version: int
    block_count: int
    block_stride: int
    block_capacity: int
    sample_rate: int
    channels: int
    sample_format: int
    write_seq: int


class AudioBlockHeader(ctypes.Structure):
    """块头部。只由实时核写，以 `seq` 作单写者多读者的 seqlock。"""

    _fields_ = (
        ("seq", ctypes.c_uint64),  # Rust 侧为 AtomicU64
        ("block_number", ctypes.c_uint64),
        ("capture_ns", ctypes.c_uint64),
        ("sample_count", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
    )
    seq: int
    block_number: int
    capture_ns: int
    sample_count: int
    flags: int


STRUCTS: Final = (AudioRingHeader, AudioBlockHeader)
"""音频环的全部二进制结构。"""
