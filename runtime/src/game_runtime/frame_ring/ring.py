"""帧环的主进程一侧：创建、读最新一帧与销毁。布局与读写规则见 `docs/modules/core-protocol.md` 的
“帧环与音频环”，布局的源是 `core_protocol.frame_ring`。

**平台假设。** Python 没有原子操作。头部的 `write_seq` 与槽的 `seq` 经 ctypes 的 `c_uint64`
字段读写，CPython 以 8 字节的 memcpy 实现，假设它在 x86-64 上编译为单条访存；这两个字段
8 字节对齐，x86-64 上对齐的 8 字节访存是原子的，且读不与更早的读重排、写不与更早的写重排
（x86-TSO），seqlock 因此不需要显式的栅栏。生产平台只有 Windows x64，检查用的容器是 x86-64；
其它架构上创建帧环即报错。

`FrameRing` 不是线程安全的：`close` 不能与读并发。
"""

import platform
import time
from dataclasses import dataclass
from types import TracebackType
from typing import ClassVar, Final, Self

from core_protocol import models
from core_protocol.frame_ring import (
    LAYOUT_VERSION,
    MAGIC,
    PIXEL_FORMAT_BGRA8,
    SLOT_ALIGN,
    SLOT_PIXELS_OFFSET,
    SLOTS_OFFSET,
    FrameRingHeader,
    FrameSlotHeader,
)

from ._shm import SharedMemory, create_shared_memory, shared_memory_name

DEFAULT_SLOT_COUNT: Final = 3
"""缺省的槽数。"""
DEFAULT_READ_ATTEMPTS: Final = 8
"""读最新一帧时缺省最多尝试的次数。"""
BYTES_PER_PIXEL: Final = 4
"""BGRA8 每个像素的字节数。"""
MAX_DIMENSION: Final = 0xFFFFFFFF
"""宽、高与槽数的上限（头部里是 u32）。"""

_SUPPORTED_MACHINES = frozenset({"amd64", "x86_64"})


class FrameRingError(Exception):
    """帧环的错误：平台不支持、已关闭等。"""


class FrameRingTornError(FrameRingError):
    """每次尝试都读到撕裂的帧（写方正在写这一槽或已经超过它）；稍后再读即可。"""


class FrameRingCorruptError(FrameRingError):
    """槽头部在 seqlock 下稳定，内容却不合布局：写方有缺陷。"""


@dataclass(frozen=True)
class RingFrame:
    """从帧环读出的一帧：拷贝出的像素与元数据。"""

    frame_number: int
    """帧号，从 1 起在本帧环内严格递增。"""
    capture_ns: int
    """采集时刻，与 `time.perf_counter_ns()` 同一时钟。"""
    transform_version: int
    """窗口与 DPI 变换版本。"""
    width: int
    height: int
    row_stride: int
    """相邻两行起点之间的字节数。"""
    pixels: bytes
    """BGRA8，自上而下逐行，`row_stride * height` 字节。"""


def frame_age_ns(capture_ns: int, now_ns: int | None = None) -> int:
    """帧龄：现在减采集时刻，负值按 0。

    现在缺省取 `time.perf_counter_ns()`，与采集时刻同一时钟（Windows 上是 QPC）。
    WGC 给的采集时刻可能比帧交出的时刻晚几毫秒（`docs/modules/realtime-core.md` 的“时钟”），
    所以差可能为负。
    """
    now = time.perf_counter_ns() if now_ns is None else now_ns
    return max(0, now - capture_ns)


def slot_stride_for(max_width: int, max_height: int) -> int:
    """放得下 `max_width × max_height` 帧的最小槽跨度：槽头部加像素，向上取整到 `SLOT_ALIGN`。"""
    needed = SLOT_PIXELS_OFFSET + max_width * max_height * BYTES_PER_PIXEL
    return -(-needed // SLOT_ALIGN) * SLOT_ALIGN


@dataclass
class _Views:
    """映射上的 ctypes 视图；持有它们时映射不能关闭。"""

    header: FrameRingHeader
    slots: list[FrameSlotHeader]


class FrameRing:
    """主进程创建的帧环。用 `create` 创建，`attach_params` 交给实时核，`read_latest` 读最新一帧，
    `close` 销毁。"""

    NAME_KIND: ClassVar[str] = "frames"
    """共享内存名中的用途。"""

    def __init__(
        self,
        memory: SharedMemory,
        *,
        slot_count: int,
        slot_stride: int,
        max_width: int,
        max_height: int,
    ) -> None:
        """在已创建的共享内存上写好头部（`write_seq` 为 0）。一般用 `create`。"""
        self._memory = memory
        self._slot_count = slot_count
        self._slot_stride = slot_stride
        self._max_width = max_width
        self._max_height = max_height
        buffer = memory.buffer
        header = FrameRingHeader.from_buffer(buffer, 0)
        header.layout_version = LAYOUT_VERSION
        header.slot_count = slot_count
        header.slot_stride = slot_stride
        header.max_width = max_width
        header.max_height = max_height
        header.write_seq = 0
        header.magic = MAGIC
        slots = [
            FrameSlotHeader.from_buffer(buffer, self._slot_start(slot))
            for slot in range(slot_count)
        ]
        self._views: _Views | None = _Views(header, slots)

    @classmethod
    def create(
        cls, max_width: int, max_height: int, *, slot_count: int = DEFAULT_SLOT_COUNT
    ) -> Self:
        """创建能放下 `max_width × max_height` 帧、有 `slot_count` 个槽的帧环。"""
        if platform.machine().lower() not in _SUPPORTED_MACHINES:
            raise FrameRingError(
                f"帧环的 Python 一方依赖 x86-64 的内存模型，不支持 {platform.machine()}"
            )
        if not 1 <= max_width <= MAX_DIMENSION or not 1 <= max_height <= MAX_DIMENSION:
            raise ValueError(
                f"帧的最大尺寸须在 1 到 {MAX_DIMENSION} 之间：{max_width}×{max_height}"
            )
        if not 2 <= slot_count <= MAX_DIMENSION:
            raise ValueError(f"槽数须在 2 到 {MAX_DIMENSION} 之间：{slot_count}")
        slot_stride = slot_stride_for(max_width, max_height)
        size = SLOTS_OFFSET + slot_count * slot_stride
        memory = create_shared_memory(shared_memory_name(cls.NAME_KIND), size)
        try:
            return cls(
                memory,
                slot_count=slot_count,
                slot_stride=slot_stride,
                max_width=max_width,
                max_height=max_height,
            )
        except BaseException:
            memory.close()
            raise

    @property
    def name(self) -> str:
        return self._memory.name

    @property
    def size_bytes(self) -> int:
        return self._memory.size

    @property
    def slot_count(self) -> int:
        return self._slot_count

    @property
    def slot_stride(self) -> int:
        return self._slot_stride

    @property
    def max_width(self) -> int:
        return self._max_width

    @property
    def max_height(self) -> int:
        return self._max_height

    @property
    def closed(self) -> bool:
        return self._views is None

    def attach_params(self) -> models.FrameRingAttachParams:
        """`frame_ring.attach` 的 params。"""
        return models.FrameRingAttachParams(
            name=self.name,
            size_bytes=self.size_bytes,
            layout_version=LAYOUT_VERSION,
            slot_count=self._slot_count,
            slot_stride=self._slot_stride,
            max_width=self._max_width,
            max_height=self._max_height,
        )

    def latest_frame_number(self) -> int:
        """最新一帧的帧号（头部的 `write_seq`），0 表示还没有帧。"""
        return self._open_views().header.write_seq

    def read_latest(self, *, max_attempts: int = DEFAULT_READ_ATTEMPTS) -> RingFrame | None:
        """按 seqlock 规则读最新一帧；还没有帧时为 `None`。

        每次尝试：读 `write_seq` 得到帧号与槽；槽的 `seq` 为奇数（正在写）则重试；
        拷贝槽头部与像素；再读 `seq`，与先前不等或槽里的帧号不是所求的即重试。
        `max_attempts` 次都撕裂时报 `FrameRingTornError`；
        槽头部稳定却不合布局时报 `FrameRingCorruptError`。
        """
        if max_attempts < 1:
            raise ValueError(f"max_attempts 须为正数：{max_attempts}")
        views = self._open_views()
        for _ in range(max_attempts):
            number = views.header.write_seq
            if number == 0:
                return None
            slot = (number - 1) % self._slot_count
            live = views.slots[slot]
            before = live.seq
            if before & 1:
                continue
            start = self._slot_start(slot)
            header = FrameSlotHeader.from_buffer_copy(self._memory.buffer, start)
            if header.frame_number != number:
                continue
            problem = self._check_slot(header)
            if problem is not None:
                if live.seq != before:
                    continue
                raise FrameRingCorruptError(f"槽 {slot} 的帧 {number}：{problem}")
            pixels = self._copy_pixels(slot, header.row_stride * header.height)
            if live.seq != before:
                continue
            return RingFrame(
                frame_number=number,
                capture_ns=header.capture_ns,
                transform_version=header.transform_version,
                width=header.width,
                height=header.height,
                row_stride=header.row_stride,
                pixels=pixels,
            )
        raise FrameRingTornError(f"连续 {max_attempts} 次读到撕裂的帧")

    def close(self) -> None:
        """销毁帧环：解除映射，Windows 上关闭句柄（实时核也关闭后对象消失），POSIX 上删除名字。
        可以重复调用。"""
        if self._views is None:
            return
        # 先放掉 ctypes 视图：它们导出了映射的缓冲，映射在此之前不能关闭。
        self._views = None
        self._memory.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # 以下供本类与假显示面使用。

    def _open_views(self) -> _Views:
        if self._views is None:
            raise FrameRingError(f"帧环已关闭：{self.name}")
        return self._views

    def _slot_start(self, slot: int) -> int:
        return SLOTS_OFFSET + slot * self._slot_stride

    def _pixels_start(self, slot: int) -> int:
        return self._slot_start(slot) + SLOT_PIXELS_OFFSET

    @property
    def _slot_capacity(self) -> int:
        """槽里能放的像素字节数。"""
        return self._slot_stride - SLOT_PIXELS_OFFSET

    def _check_slot(self, header: FrameSlotHeader) -> str | None:
        """槽头部合布局时为 `None`，否则为说明。"""
        if header.pixel_format != PIXEL_FORMAT_BGRA8:
            return f"像素格式 {header.pixel_format} 不是 BGRA8"
        if header.width > self._max_width or header.height > self._max_height:
            return f"{header.width}×{header.height} 超过 {self._max_width}×{self._max_height}"
        if header.row_stride < header.width * BYTES_PER_PIXEL:
            return f"行跨度 {header.row_stride} 小于宽 {header.width} × 4"
        if header.row_stride * header.height > self._slot_capacity:
            return f"{header.height} 行 × {header.row_stride} 字节放不进槽"
        return None

    def _copy_pixels(self, slot: int, length: int) -> bytes:
        """拷贝槽像素区开头的 `length` 字节。"""
        start = self._pixels_start(slot)
        return self._memory.buffer[start : start + length]
