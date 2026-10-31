"""主进程一侧的音频环：创建与命名、读最近一段的 seqlock 规则、假音频输出与销毁。

写块用只在测试中使用的假音频输出（它就是一个音频环，写法与实时核相同）；与 Rust 写入方的互通见
`test_interop.py`。
"""

import array
import sys

import pytest

from core_protocol.audio_ring import (
    BLOCK_ALIGN,
    BLOCK_SAMPLES_OFFSET,
    BLOCKS_OFFSET,
    LAYOUT_VERSION,
    SAMPLE_FORMAT_F32,
)
from game_runtime.audio_ring import (
    AudioBlock,
    AudioRing,
    AudioRingCorruptError,
    AudioRingError,
    AudioRingTornError,
    block_stride_for,
)
from game_runtime.audio_ring.fake import FakeAudioOutput


def tone(value: float, sample_count: int, channels: int = 2) -> bytes:
    """每个样本值都是 `value` 的一段声音。"""
    return array.array("f", [value] * (sample_count * channels)).tobytes()


class Probe(FakeAudioOutput):
    """测试用：直接改块槽的 seqlock 序号与头部，模拟写方的各种中间状态。"""

    copies: int = 0
    on_copy: tuple[int, ...] = ()
    """第几次拷贝之后让 seq 变化，像是写方在拷贝期间写了这一块槽。"""

    def bump_seq(self, slot: int, by: int) -> None:
        self._open_views().blocks[slot].seq += by

    def set_sample_count(self, slot: int, count: int) -> None:
        self._open_views().blocks[slot].sample_count = count

    def _copy_samples(self, slot: int, length: int) -> bytes:
        samples = super()._copy_samples(slot, length)
        self.copies += 1
        if self.copies in self.on_copy:
            self.bump_seq(slot, 2)
        return samples


def test_create_writes_the_header_and_names_the_shared_memory() -> None:
    with AudioRing.create(48_000, 2, block_capacity=960, block_count=4) as ring:
        params = ring.attach_params()
        stride = block_stride_for(960, 2)
        assert stride % BLOCK_ALIGN == 0
        assert stride >= BLOCK_SAMPLES_OFFSET + 960 * 2 * 4
        assert params.model_dump() == {
            "name": ring.name,
            "size_bytes": BLOCKS_OFFSET + 4 * stride,
            "layout_version": LAYOUT_VERSION,
            "block_count": 4,
            "block_stride": stride,
            "block_capacity": 960,
            "sample_rate": 48_000,
            "channels": 2,
            "sample_format": SAMPLE_FORMAT_F32,
        }
        prefix = "Local\\bellis-audio-" if sys.platform == "win32" else "/bellis-audio-"
        assert ring.name.startswith(prefix)
        assert ring.latest_block_number() == 0
        empty = ring.read_recent(1_000_000_000)
        assert (empty.sample_rate, empty.channels, empty.blocks) == (48_000, 2, ())


@pytest.mark.parametrize(
    ("sample_rate", "channels", "capacity", "count"),
    [
        (7_999, 2, 960, 4),
        (192_001, 2, 960, 4),
        (48_000, 0, 960, 4),
        (48_000, 3, 960, 4),
        (48_000, 2, 0, 4),
        (48_000, 2, 960, 1),
    ],
)
def test_create_refuses_layouts_the_core_would_refuse(
    sample_rate: int, channels: int, capacity: int, count: int
) -> None:
    with pytest.raises(ValueError, match="须在"):
        AudioRing.create(sample_rate, channels, block_capacity=capacity, block_count=count)


def test_played_blocks_read_back_in_order_with_their_times() -> None:
    with FakeAudioOutput.create(16_000, 1, block_capacity=160, block_count=8) as output:
        for i in range(3):
            output.play(
                tone(i / 10, 160, channels=1),
                capture_ns=1_000_000 + 10_000_000 * i,
                discontinuity=i == 0,
            )
        read = output.read_recent(1_000_000_000)
        assert (read.sample_rate, read.channels) == (16_000, 1)
        assert read.blocks == tuple(
            AudioBlock(
                block_number=i + 1,
                capture_ns=1_000_000 + 10_000_000 * i,
                sample_count=160,
                discontinuity=i == 0,
                samples=tone(i / 10, 160, channels=1),
            )
            for i in range(3)
        )
        assert read.duration_ns(160) == 10_000_000


def test_read_recent_stops_once_the_duration_is_covered() -> None:
    with FakeAudioOutput.create(48_000, 2, block_capacity=480, block_count=8) as output:
        for _ in range(5):
            output.play(tone(0.0, 480))
        # 每块 10 ms：15 ms 要两块，20 ms 也是两块，0 只要最新的一块。
        assert [b.block_number for b in output.read_recent(15_000_000).blocks] == [4, 5]
        assert [b.block_number for b in output.read_recent(20_000_000).blocks] == [4, 5]
        assert [b.block_number for b in output.read_recent(0).blocks] == [5]


def test_after_skips_blocks_already_read() -> None:
    with FakeAudioOutput.create(48_000, 2, block_capacity=480, block_count=8) as output:
        for _ in range(5):
            output.play(tone(0.0, 480))
        assert [b.block_number for b in output.read_recent(10**9, after=3).blocks] == [4, 5]
        assert output.read_recent(10**9, after=5).blocks == ()
        assert output.read_recent(10**9, after=9).blocks == ()


def test_overwritten_blocks_are_not_returned() -> None:
    with FakeAudioOutput.create(48_000, 2, block_capacity=480, block_count=3) as output:
        for _ in range(10):
            output.play(tone(0.0, 480))
        read = output.read_recent(10**9)
        assert [b.block_number for b in read.blocks] == [8, 9, 10]
        # 持续读取的一方看到块号跳了，即知道其间的块已被覆盖。
        assert read.blocks[0].block_number > 2 + 1


def test_an_older_block_overwritten_during_the_read_ends_the_read() -> None:
    with Probe.create(48_000, 2, block_capacity=480, block_count=4) as output:
        for _ in range(4):
            output.play(tone(0.0, 480))
        # 拷贝第二块（块 3）期间写方动了它的块槽：只返回比它新的块 4。
        output.on_copy = (2,)
        assert [b.block_number for b in output.read_recent(10**9).blocks] == [4]


def test_a_torn_newest_block_is_retried_then_given_up() -> None:
    with Probe.create(48_000, 2, block_capacity=480, block_count=4) as output:
        output.play(tone(0.0, 480))
        output.play(tone(0.1, 480))
        # 第一次拷贝最新的块时撕裂，第二次成功。
        output.on_copy = (1,)
        assert [b.block_number for b in output.read_recent(10_000_000).blocks] == [2]
        assert output.copies == 2
        # 正在写（seq 为奇数）：每次都撕裂。
        output.bump_seq(1, 1)
        with pytest.raises(AudioRingTornError):
            output.read_recent(10_000_000, max_attempts=3)


def test_a_stable_block_that_breaks_the_layout_is_corrupt() -> None:
    with Probe.create(48_000, 2, block_capacity=480, block_count=4) as output:
        output.play(tone(0.0, 480))
        output.set_sample_count(0, 481)
        with pytest.raises(AudioRingCorruptError):
            output.read_recent(10_000_000)


def test_play_refuses_blocks_that_do_not_fit() -> None:
    with FakeAudioOutput.create(48_000, 2, block_capacity=480, block_count=4) as output:
        with pytest.raises(ValueError, match="不是整数个"):
            output.play(b"")
        with pytest.raises(ValueError, match="不是整数个"):
            output.play(b"\0" * 12)
        with pytest.raises(ValueError, match="超过块容量"):
            output.play(tone(0.0, 481))
        assert output.latest_block_number() == 0
        assert output.target.output == output.attach_params()


def test_close_destroys_the_ring_and_can_be_repeated() -> None:
    ring = AudioRing.create(48_000, 2, block_capacity=480, block_count=2)
    name = ring.name
    ring.close()
    ring.close()
    assert ring.closed
    with pytest.raises(AudioRingError):
        ring.read_recent(0)
    with pytest.raises(AudioRingError):
        ring.latest_block_number()
    # 名字已释放：可以再建同名的（POSIX 上已 shm_unlink，Windows 上对象已消失）。
    from game_runtime._shm import create_shared_memory

    create_shared_memory(name, 4096).close()


def test_read_recent_rejects_negative_arguments() -> None:
    with AudioRing.create(48_000, 2, block_capacity=480, block_count=2) as ring:
        with pytest.raises(ValueError, match="duration_ns"):
            ring.read_recent(-1)
        with pytest.raises(ValueError, match="after"):
            ring.read_recent(0, after=-1)
        with pytest.raises(ValueError, match="max_attempts"):
            ring.read_recent(0, max_attempts=0)
