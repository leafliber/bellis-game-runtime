"""音频环的主进程一侧：创建、读最近一段与销毁。布局与读写规则见 `docs/modules/core-protocol.md` 的
“音频环”，布局的源是 `core_protocol.audio_ring`。

**平台假设**与帧环相同（`game_runtime.frame_ring.ring`）：Python 没有原子操作，
头部的 `write_seq` 与块的 `seq` 经 ctypes 的 `c_uint64` 字段读写，依赖 x86-64 上对齐的
8 字节访存是原子的、且不与更早的读或写重排；其它架构上创建音频环即报错。

`AudioRing` 不是线程安全的：`close` 不能与读并发。
"""

import platform
from dataclasses import dataclass
from types import TracebackType
from typing import ClassVar, Final, Protocol, Self

from core_protocol import models
from core_protocol.audio_ring import (
    BLOCK_ALIGN,
    BLOCK_SAMPLES_OFFSET,
    BLOCKS_OFFSET,
    BYTES_PER_VALUE,
    FLAG_DISCONTINUITY,
    KNOWN_FLAGS,
    LAYOUT_VERSION,
    MAGIC,
    MAX_CHANNELS,
    MAX_SAMPLE_RATE,
    MIN_SAMPLE_RATE,
    SAMPLE_FORMAT_F32,
    AudioBlockHeader,
    AudioRingHeader,
)
from game_runtime._shm import SharedMemory, create_shared_memory, shared_memory_name

DEFAULT_READ_ATTEMPTS: Final = 8
"""读最近一段时缺省最多尝试的次数。"""
MAX_U32: Final = 0xFFFFFFFF
"""块容量与块槽数的上限（头部里是 u32）。"""

_SUPPORTED_MACHINES = frozenset({"amd64", "x86_64"})


class AudioRingError(Exception):
    """音频环的错误：平台不支持、已关闭等。"""


class AudioRingTornError(AudioRingError):
    """每次尝试最新的一块都撕裂或已被覆盖（写方在读的期间绕了整整一圈）；稍后再读即可。"""


class AudioRingCorruptError(AudioRingError):
    """块头部在 seqlock 下稳定，内容却不合布局：写方有缺陷。"""


@dataclass(frozen=True)
class AudioBlock:
    """从音频环读出的一块：拷贝出的样本值与元数据。"""

    block_number: int
    """块号，从 1 起在本音频环内每块加一。"""
    capture_ns: int
    """第一个采样点的采集时刻，与 `time.perf_counter_ns()` 同一时钟；第 i 个采样点的采集时刻为
    `capture_ns + i × 10⁹ / sample_rate`。"""
    sample_count: int
    """采样点数；一个采样点是同一时刻各声道的样本值。"""
    discontinuity: bool
    """这一块与上一块之间不连续：丢了声音或时间轴断开。"""
    samples: bytes
    """样本值：F32、本机字节序、各声道交错，`sample_count × channels × 4` 字节。"""


@dataclass(frozen=True)
class AudioRead:
    """读最近一段的结果：格式与按块号从小到大排列的块。"""

    sample_rate: int
    """采样率（Hz）。"""
    channels: int
    """声道数。"""
    blocks: tuple[AudioBlock, ...]
    """读到的块，按块号从小到大；没有比 `after` 更新的块时为空。"""

    def duration_ns(self, sample_count: int) -> int:
        """`sample_count` 个采样点的时长（纳秒），向下取整。"""
        return sample_count * 1_000_000_000 // self.sample_rate


class AudioReader(Protocol):
    """读音频环的接口，供插件（`ctx.audio`）与宿主代码使用。"""

    def read_recent(self, duration_ns: int, *, after: int = 0) -> AudioRead | None:
        """读最近一段：从最新的块往前取，取到总时长不少于 `duration_ns`、
        遇到块号不大于 `after` 的块，或更早的块已被覆盖为止。没有音频环时为 `None`。

        持续读取的一方把上次取到的最大块号作为 `after`；返回的第一块的块号大于 `after + 1`
        即其间的块已被覆盖。每次尝试最新的一块都撕裂时报 `AudioRingTornError`，
        稍后再读即可。
        """
        ...


def block_stride_for(block_capacity: int, channels: int) -> int:
    """放得下 `block_capacity` 个采样点的最小块槽跨度：块头部加样本值，向上取整到 `BLOCK_ALIGN`。"""
    needed = BLOCK_SAMPLES_OFFSET + block_capacity * channels * BYTES_PER_VALUE
    return -(-needed // BLOCK_ALIGN) * BLOCK_ALIGN


@dataclass
class _Views:
    """映射上的 ctypes 视图；持有它们时映射不能关闭。"""

    header: AudioRingHeader
    blocks: list[AudioBlockHeader]


class AudioRing:
    """主进程创建的音频环。用 `create` 创建，`attach_params` 交给实时核，`read_recent` 读最近一段，
    `close` 销毁。"""

    NAME_KIND: ClassVar[str] = "audio"
    """共享内存名中的用途。"""

    def __init__(
        self,
        memory: SharedMemory,
        *,
        sample_rate: int,
        channels: int,
        block_capacity: int,
        block_count: int,
        block_stride: int,
    ) -> None:
        """在已创建的共享内存上写好头部（`write_seq` 为 0）。一般用 `create`。"""
        self._memory = memory
        self._sample_rate = sample_rate
        self._channels = channels
        self._block_capacity = block_capacity
        self._block_count = block_count
        self._block_stride = block_stride
        buffer = memory.buffer
        header = AudioRingHeader.from_buffer(buffer, 0)
        header.layout_version = LAYOUT_VERSION
        header.block_count = block_count
        header.block_stride = block_stride
        header.block_capacity = block_capacity
        header.sample_rate = sample_rate
        header.channels = channels
        header.sample_format = SAMPLE_FORMAT_F32
        header.write_seq = 0
        header.magic = MAGIC
        blocks = [
            AudioBlockHeader.from_buffer(buffer, self._slot_start(slot))
            for slot in range(block_count)
        ]
        self._views: _Views | None = _Views(header, blocks)

    @classmethod
    def create(
        cls, sample_rate: int, channels: int, *, block_capacity: int, block_count: int
    ) -> Self:
        """创建 `sample_rate` Hz、`channels` 声道的音频环：`block_count` 个块槽，每块至多
        `block_capacity` 个采样点。"""
        if platform.machine().lower() not in _SUPPORTED_MACHINES:
            raise AudioRingError(
                f"音频环的 Python 一方依赖 x86-64 的内存模型，不支持 {platform.machine()}"
            )
        if not MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE:
            raise ValueError(
                f"采样率须在 {MIN_SAMPLE_RATE} 到 {MAX_SAMPLE_RATE} Hz 之间：{sample_rate}"
            )
        if not 1 <= channels <= MAX_CHANNELS:
            raise ValueError(f"声道数须在 1 到 {MAX_CHANNELS} 之间：{channels}")
        if not 1 <= block_capacity <= MAX_U32:
            raise ValueError(f"块容量须在 1 到 {MAX_U32} 之间：{block_capacity}")
        if not 2 <= block_count <= MAX_U32:
            raise ValueError(f"块槽数须在 2 到 {MAX_U32} 之间：{block_count}")
        block_stride = block_stride_for(block_capacity, channels)
        size = BLOCKS_OFFSET + block_count * block_stride
        memory = create_shared_memory(shared_memory_name(cls.NAME_KIND), size)
        try:
            return cls(
                memory,
                sample_rate=sample_rate,
                channels=channels,
                block_capacity=block_capacity,
                block_count=block_count,
                block_stride=block_stride,
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
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def channels(self) -> int:
        return self._channels

    @property
    def block_capacity(self) -> int:
        return self._block_capacity

    @property
    def block_count(self) -> int:
        return self._block_count

    @property
    def block_stride(self) -> int:
        return self._block_stride

    @property
    def closed(self) -> bool:
        return self._views is None

    def attach_params(self) -> models.AudioRingAttachParams:
        """`audio_ring.attach` 的 params。"""
        return models.AudioRingAttachParams(
            name=self.name,
            size_bytes=self.size_bytes,
            layout_version=LAYOUT_VERSION,
            block_count=self._block_count,
            block_stride=self._block_stride,
            block_capacity=self._block_capacity,
            sample_rate=self._sample_rate,
            channels=self._channels,
            sample_format=SAMPLE_FORMAT_F32,
        )

    def latest_block_number(self) -> int:
        """最新一块的块号（头部的 `write_seq`），0 表示还没有块。"""
        return self._open_views().header.write_seq

    def read_recent(
        self, duration_ns: int, *, after: int = 0, max_attempts: int = DEFAULT_READ_ATTEMPTS
    ) -> AudioRead:
        """按 seqlock 规则读最近一段，见 `AudioReader.read_recent`。

        从最新的块 W 往前逐块读：块槽的 `seq` 为奇数、拷贝期间 `seq` 变化或块槽里不是所求的块，
        都说明这一块撕裂或已被覆盖。W 本身如此时从读 `write_seq` 起重试，`max_attempts`
        次都如此即报 `AudioRingTornError`；更早的块如此只说明写方已追上它，返回比它新的那些块。
        块头部稳定却不合布局时报 `AudioRingCorruptError`。
        """
        if duration_ns < 0:
            raise ValueError(f"duration_ns 不能为负：{duration_ns}")
        if after < 0:
            raise ValueError(f"after 不能为负：{after}")
        if max_attempts < 1:
            raise ValueError(f"max_attempts 须为正数：{max_attempts}")
        views = self._open_views()
        for _ in range(max_attempts):
            latest = views.header.write_seq
            if latest <= after:
                return self._result(())
            oldest = max(after + 1, latest - self._block_count + 1)
            blocks: list[AudioBlock] = []
            total = 0
            for number in range(latest, oldest - 1, -1):
                block = self._read_block(views, number)
                if block is None:
                    break
                blocks.append(block)
                total += block.sample_count * 1_000_000_000 // self._sample_rate
                if total >= duration_ns:
                    break
            if blocks and blocks[0].block_number == latest:
                blocks.reverse()
                return self._result(tuple(blocks))
        raise AudioRingTornError(f"连续 {max_attempts} 次读到撕裂或已被覆盖的最新一块")

    def close(self) -> None:
        """销毁音频环：解除映射，Windows 上关闭句柄（实时核也关闭后对象消失），POSIX 上删除名字。
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

    # 以下供本类与假音频输出使用。

    def _open_views(self) -> _Views:
        if self._views is None:
            raise AudioRingError(f"音频环已关闭：{self.name}")
        return self._views

    def _result(self, blocks: tuple[AudioBlock, ...]) -> AudioRead:
        return AudioRead(self._sample_rate, self._channels, blocks)

    def _slot_start(self, slot: int) -> int:
        return BLOCKS_OFFSET + slot * self._block_stride

    def _samples_start(self, slot: int) -> int:
        return self._slot_start(slot) + BLOCK_SAMPLES_OFFSET

    @property
    def _bytes_per_sample(self) -> int:
        return self._channels * BYTES_PER_VALUE

    def _check_block(self, header: AudioBlockHeader) -> str | None:
        """块头部合布局时为 `None`，否则为说明。"""
        if not 1 <= header.sample_count <= self._block_capacity:
            return f"采样点数 {header.sample_count} 不在 1 到块容量 {self._block_capacity} 之间"
        if header.flags & ~KNOWN_FLAGS:
            return f"块标志 {header.flags:#x} 有未定义的位"
        return None

    def _copy_samples(self, slot: int, length: int) -> bytes:
        """拷贝块槽样本区开头的 `length` 字节。"""
        start = self._samples_start(slot)
        return self._memory.buffer[start : start + length]

    def _read_block(self, views: _Views, number: int) -> AudioBlock | None:
        """读块号为 `number` 的块；撕裂或已被覆盖时为 `None`。"""
        slot = (number - 1) % self._block_count
        live = views.blocks[slot]
        before = live.seq
        if before & 1:
            return None
        start = self._slot_start(slot)
        header = AudioBlockHeader.from_buffer_copy(self._memory.buffer, start)
        if header.block_number != number:
            return None
        problem = self._check_block(header)
        if problem is not None:
            if live.seq != before:
                return None
            raise AudioRingCorruptError(f"块槽 {slot} 的块 {number}：{problem}")
        samples = self._copy_samples(slot, header.sample_count * self._bytes_per_sample)
        if live.seq != before:
            return None
        return AudioBlock(
            block_number=number,
            capture_ns=header.capture_ns,
            sample_count=header.sample_count,
            discontinuity=bool(header.flags & FLAG_DISCONTINUITY),
            samples=samples,
        )
