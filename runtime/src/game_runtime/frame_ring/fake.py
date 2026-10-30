"""假显示面，只用于自动测试：测试一侧把合成游戏的帧逐帧写进去，实时核的假采集源从中取帧
（`docs/modules/realtime-core.md` 的“假采集源”）。

假显示面就是一个帧环：同一布局、同样由本进程创建与销毁，写法与实时核写帧环相同（seqlock，见
`docs/modules/core-protocol.md` 的“帧环”）。写入依赖与读取相同的 x86-64 平台假设（`ring` 模块）。
生产代码不使用它。
"""

import time
from collections.abc import Buffer
from typing import ClassVar

from core_protocol import models
from core_protocol.frame_ring import PIXEL_FORMAT_BGRA8

from .ring import BYTES_PER_PIXEL, FrameRing


class FakeDisplay(FrameRing):
    """假显示面：`present` 写入一帧，`target` 交给实时核作采集目标。只用于自动测试。"""

    NAME_KIND: ClassVar[str] = "fake-display"

    @property
    def target(self) -> models.FakeTarget:
        """`capture.set_target` 的 `fake` 分支。"""
        return models.FakeTarget(surface=self.attach_params())

    def present(
        self,
        pixels: Buffer,
        width: int,
        height: int,
        *,
        row_stride: int | None = None,
        capture_ns: int | None = None,
        transform_version: int = 1,
    ) -> int:
        """写入一帧，返回它的帧号（从 1 起，每次加一）。

        `pixels` 为 BGRA8，自上而下逐行，每行 `row_stride`（缺省 `width * 4`）字节，按原样写进槽。
        采集时刻缺省取 `time.perf_counter_ns()`，即“显示”这一帧的时刻。
        """
        stride = width * BYTES_PER_PIXEL if row_stride is None else row_stride
        if not 1 <= width <= self.max_width or not 1 <= height <= self.max_height:
            raise ValueError(f"{width}×{height} 超出假显示面的 {self.max_width}×{self.max_height}")
        if stride < width * BYTES_PER_PIXEL:
            raise ValueError(f"行跨度 {stride} 小于宽 {width} × 4")
        length = stride * height
        if length > self._slot_capacity:
            raise ValueError(f"{height} 行 × {stride} 字节放不进槽")
        data = memoryview(pixels).cast("B")
        if len(data) < length:
            raise ValueError(f"像素只有 {len(data)} 字节，少于 {length}")
        when = time.perf_counter_ns() if capture_ns is None else capture_ns

        views = self._open_views()
        number = views.header.write_seq + 1
        slot = (number - 1) % self.slot_count
        live = views.slots[slot]
        # 置为下一个奇数（崩溃后遗留的奇数也跳过），写槽，再置为偶数，最后发布帧号。
        odd = live.seq + 1 + (live.seq & 1)
        live.seq = odd
        live.frame_number = number
        live.capture_ns = when
        live.transform_version = transform_version
        live.width = width
        live.height = height
        live.row_stride = stride
        live.pixel_format = PIXEL_FORMAT_BGRA8
        start = self._pixels_start(slot)
        self._memory.buffer[start : start + length] = data[:length]
        live.seq = odd + 1
        views.header.write_seq = number
        return number
