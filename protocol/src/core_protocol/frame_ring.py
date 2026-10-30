"""帧环的二进制布局，见 `docs/modules/core-protocol.md` 的“帧环与音频环”。

布局的源是 Rust 的 `core_protocol::frame_ring`；这里是手写的镜像，由 `tests/test_frame_ring.py`
与 Rust 导出的 `protocol/frame-ring-layout.json` 逐字段核对。整数按本机字节序。
"""

import ctypes
from typing import Final

MAGIC: Final = int.from_bytes(b"BGRTRING", "little")
"""帧环头部的 magic：小端读出的 `b"BGRTRING"`。"""
LAYOUT_VERSION: Final = 1
"""布局版本：头部、槽头部或下列常量变化时加一。"""
SLOTS_OFFSET: Final = 64
"""槽 0 相对共享内存起点的偏移。"""
SLOT_PIXELS_OFFSET: Final = 64
"""像素相对槽起点的偏移。"""
SLOT_ALIGN: Final = 64
"""槽跨度的对齐字节数。"""
PIXEL_FORMAT_BGRA8: Final = 1
"""像素格式 BGRA8：每像素 4 字节，依次为 B、G、R、A。"""


class FrameRingHeader(ctypes.Structure):
    """帧环头部。

    主进程创建时写入除 `write_seq` 外的全部字段，此后不变；`write_seq` 只由实时核写。
    """

    _fields_ = (
        ("magic", ctypes.c_uint64),
        ("layout_version", ctypes.c_uint32),
        ("slot_count", ctypes.c_uint32),
        ("slot_stride", ctypes.c_uint64),
        ("max_width", ctypes.c_uint32),
        ("max_height", ctypes.c_uint32),
        ("write_seq", ctypes.c_uint64),  # Rust 侧为 AtomicU64
    )
    magic: int
    layout_version: int
    slot_count: int
    slot_stride: int
    max_width: int
    max_height: int
    write_seq: int


class FrameSlotHeader(ctypes.Structure):
    """槽头部。只由实时核写，以 `seq` 作单写者多读者的 seqlock。"""

    _fields_ = (
        ("seq", ctypes.c_uint64),  # Rust 侧为 AtomicU64
        ("frame_number", ctypes.c_uint64),
        ("capture_ns", ctypes.c_uint64),
        ("transform_version", ctypes.c_uint64),
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("row_stride", ctypes.c_uint32),
        ("pixel_format", ctypes.c_uint32),
    )
    seq: int
    frame_number: int
    capture_ns: int
    transform_version: int
    width: int
    height: int
    row_stride: int
    pixel_format: int


STRUCTS: Final = (FrameRingHeader, FrameSlotHeader)
"""帧环的全部二进制结构。"""
