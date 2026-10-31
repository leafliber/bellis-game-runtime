"""假音频输出，只用于自动测试：测试一侧把声音逐块写进去，实时核的假音频源从中取声音
（`docs/modules/realtime-core.md` 的“假音频源”）。

假音频输出就是一个音频环：同一布局、同样由本进程创建与销毁，写法与实时核写音频环相同（seqlock，见
`docs/modules/core-protocol.md` 的“音频环”）。写入依赖与读取相同的 x86-64 平台假设（`ring` 模块）。
生产代码不使用它。
"""

import time
from collections.abc import Buffer
from typing import ClassVar

from core_protocol import models
from core_protocol.audio_ring import BYTES_PER_VALUE, FLAG_DISCONTINUITY

from .ring import AudioRing


class FakeAudioOutput(AudioRing):
    """假音频输出：`play` 写入一块，`target` 交给实时核作音频目标。只用于自动测试。"""

    NAME_KIND: ClassVar[str] = "fake-audio"

    @property
    def target(self) -> models.FakeAudioTarget:
        """`audio.set_target` 的 `fake` 分支。"""
        return models.FakeAudioTarget(output=self.attach_params())

    def play(
        self, samples: Buffer, *, capture_ns: int | None = None, discontinuity: bool = False
    ) -> int:
        """写入一块，返回它的块号（从 1 起，每次加一）。

        `samples` 为 F32、本机字节序、各声道交错，至少一个、至多 `block_capacity` 个采样点。
        采集时刻缺省取 `time.perf_counter_ns()`，即“发出”这一块声音的时刻。
        """
        data = memoryview(samples).cast("B")
        bytes_per_sample = self.channels * BYTES_PER_VALUE
        if len(data) == 0 or len(data) % bytes_per_sample:
            raise ValueError(f"{len(data)} 字节不是整数个 {bytes_per_sample} 字节的采样点")
        sample_count = len(data) // bytes_per_sample
        if sample_count > self.block_capacity:
            raise ValueError(f"{sample_count} 个采样点超过块容量 {self.block_capacity}")
        when = time.perf_counter_ns() if capture_ns is None else capture_ns

        views = self._open_views()
        number = views.header.write_seq + 1
        slot = (number - 1) % self.block_count
        live = views.blocks[slot]
        # 置为下一个奇数（崩溃后遗留的奇数也跳过），写块，再置为偶数，最后发布块号。
        odd = live.seq + 1 + (live.seq & 1)
        live.seq = odd
        live.block_number = number
        live.capture_ns = when
        live.sample_count = sample_count
        live.flags = FLAG_DISCONTINUITY if discontinuity else 0
        start = self._samples_start(slot)
        self._memory.buffer[start : start + len(data)] = data
        live.seq = odd + 1
        views.header.write_seq = number
        return number
