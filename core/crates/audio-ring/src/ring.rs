//! 打开音频环并核对：参数本身合布局规则，共享内存不小于 `size_bytes`，头部与参数一致。
//! 块头部的读写也在这里，供写入方与读取方共用。

use std::mem::offset_of;
use std::sync::atomic::{AtomicU64, Ordering};

use audio_capture::AudioFormat;
use core_protocol::audio_ring::{
    AudioBlockHeader, AudioRingHeader, BLOCK_ALIGN, BLOCK_SAMPLES_OFFSET, BLOCKS_OFFSET,
    BYTES_PER_VALUE, KNOWN_FLAGS, LAYOUT_VERSION, MAGIC, MAX_CHANNELS, MAX_SAMPLE_RATE,
    MIN_SAMPLE_RATE, SAMPLE_FORMAT_F32,
};
use core_protocol::messages::AudioRingAttachParams;
use shared_memory::SharedMemory;

use crate::error::AudioRingError;

/// 块头部中 `seq` 之外的字段。
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct BlockMeta {
    /// 块号，从 1 起在本音频环内每块加一。
    pub block_number: u64,
    /// 第一个采样点的采集时刻，与核协议的 `_ns` 时刻同一时钟。
    pub capture_ns: u64,
    /// 采样点数。
    pub sample_count: u32,
    /// 块标志。
    pub flags: u32,
}

/// 已打开并核对过的音频环。
#[derive(Debug)]
pub(crate) struct Ring {
    memory: SharedMemory,
    block_count: u32,
    block_stride: usize,
    block_capacity: u32,
    format: AudioFormat,
}

/// 参数中的 `name` 之外的布局，核对过且换算成了 usize。
struct Geometry {
    size: usize,
    block_stride: usize,
}

fn invalid(reason: impl Into<String>) -> AudioRingError {
    AudioRingError::InvalidParams(reason.into())
}

/// 一块最多占的样本字节数：`block_capacity × channels × 4`。
fn block_bytes(params: &AudioRingAttachParams) -> u64 {
    u64::from(params.block_capacity) * u64::from(params.channels) * u64::from(BYTES_PER_VALUE)
}

/// 参数本身须合布局规则（`docs/modules/core-protocol.md` 的“音频环”）。
fn check_params(params: &AudioRingAttachParams) -> Result<Geometry, AudioRingError> {
    if params.name.is_empty() || params.name.contains('\0') {
        return Err(invalid("the name is empty or contains NUL"));
    }
    if params.layout_version != LAYOUT_VERSION {
        return Err(invalid(format!(
            "layout_version is {}, this implementation is {LAYOUT_VERSION}",
            params.layout_version
        )));
    }
    if params.block_count < 2 {
        return Err(invalid("block_count is less than 2"));
    }
    if params.sample_format != SAMPLE_FORMAT_F32 {
        return Err(invalid(format!(
            "sample_format {} is not F32",
            params.sample_format
        )));
    }
    if !(1..=MAX_CHANNELS).contains(&params.channels) {
        return Err(invalid(format!(
            "{} channels; 1 to {MAX_CHANNELS} are supported",
            params.channels
        )));
    }
    if !(MIN_SAMPLE_RATE..=MAX_SAMPLE_RATE).contains(&params.sample_rate) {
        return Err(invalid(format!(
            "a sample rate of {} Hz is outside {MIN_SAMPLE_RATE} to {MAX_SAMPLE_RATE}",
            params.sample_rate
        )));
    }
    if params.block_capacity == 0 {
        return Err(invalid("block_capacity is 0"));
    }
    if !params.block_stride.is_multiple_of(BLOCK_ALIGN) {
        return Err(invalid(format!(
            "block_stride {} is not a multiple of {BLOCK_ALIGN}",
            params.block_stride
        )));
    }
    if params.block_stride < BLOCK_SAMPLES_OFFSET + block_bytes(params) {
        return Err(invalid(format!(
            "block_stride {} cannot hold {} samples of {} channel(s)",
            params.block_stride, params.block_capacity, params.channels
        )));
    }
    let needed = u64::from(params.block_count)
        .checked_mul(params.block_stride)
        .and_then(|blocks| blocks.checked_add(BLOCKS_OFFSET))
        .ok_or_else(|| invalid("the blocks overflow u64"))?;
    if params.size_bytes < needed {
        return Err(invalid(format!(
            "size_bytes {} is less than the {needed} bytes the blocks need",
            params.size_bytes
        )));
    }
    let size = usize::try_from(params.size_bytes)
        .map_err(|_| invalid("size_bytes does not fit in memory"))?;
    let block_stride = usize::try_from(params.block_stride)
        .map_err(|_| invalid("block_stride does not fit in memory"))?;
    Ok(Geometry { size, block_stride })
}

/// 头部字段在共享内存中的偏移，取自 `core_protocol` 的 `#[repr(C)]` 结构。
pub(crate) mod header {
    use super::{AudioRingHeader, offset_of};

    pub(crate) const MAGIC: usize = offset_of!(AudioRingHeader, magic);
    pub(crate) const LAYOUT_VERSION: usize = offset_of!(AudioRingHeader, layout_version);
    pub(crate) const BLOCK_COUNT: usize = offset_of!(AudioRingHeader, block_count);
    pub(crate) const BLOCK_STRIDE: usize = offset_of!(AudioRingHeader, block_stride);
    pub(crate) const BLOCK_CAPACITY: usize = offset_of!(AudioRingHeader, block_capacity);
    pub(crate) const SAMPLE_RATE: usize = offset_of!(AudioRingHeader, sample_rate);
    pub(crate) const CHANNELS: usize = offset_of!(AudioRingHeader, channels);
    pub(crate) const SAMPLE_FORMAT: usize = offset_of!(AudioRingHeader, sample_format);
    pub(crate) const WRITE_SEQ: usize = offset_of!(AudioRingHeader, write_seq);
}

/// 块头部字段相对块槽起点的偏移。
pub(crate) mod block {
    use super::{AudioBlockHeader, offset_of};

    pub(crate) const SEQ: usize = offset_of!(AudioBlockHeader, seq);
    pub(crate) const BLOCK_NUMBER: usize = offset_of!(AudioBlockHeader, block_number);
    pub(crate) const CAPTURE_NS: usize = offset_of!(AudioBlockHeader, capture_ns);
    pub(crate) const SAMPLE_COUNT: usize = offset_of!(AudioBlockHeader, sample_count);
    pub(crate) const FLAGS: usize = offset_of!(AudioBlockHeader, flags);
}

#[allow(
    clippy::cast_possible_truncation,
    reason = "布局常量都很小，在任何平台上都放得进 usize"
)]
const BLOCKS_START: usize = BLOCKS_OFFSET as usize;
#[allow(
    clippy::cast_possible_truncation,
    reason = "布局常量都很小，在任何平台上都放得进 usize"
)]
const SAMPLES_START: usize = BLOCK_SAMPLES_OFFSET as usize;

impl Ring {
    /// 打开 `params` 指明的共享内存，核对参数与头部。
    pub(crate) fn open(params: &AudioRingAttachParams) -> Result<Self, AudioRingError> {
        let geometry = check_params(params)?;
        let memory = SharedMemory::open(&params.name, geometry.size).map_err(|source| {
            AudioRingError::Open {
                name: params.name.clone(),
                source,
            }
        })?;
        let ring = Self {
            memory,
            block_count: params.block_count,
            block_stride: geometry.block_stride,
            block_capacity: params.block_capacity,
            format: AudioFormat {
                sample_rate: params.sample_rate,
                channels: params.channels,
            },
        };
        ring.check_header(params)?;
        Ok(ring)
    }

    fn check_header(&self, params: &AudioRingAttachParams) -> Result<(), AudioRingError> {
        let m = &self.memory;
        let u32_field = |offset| u64::from(m.u32_at(offset).load(Ordering::Relaxed));
        let fields = [
            (
                "magic",
                m.u64_at(header::MAGIC).load(Ordering::Acquire),
                MAGIC,
            ),
            (
                "layout_version",
                u32_field(header::LAYOUT_VERSION),
                u64::from(params.layout_version),
            ),
            (
                "block_count",
                u32_field(header::BLOCK_COUNT),
                u64::from(params.block_count),
            ),
            (
                "block_stride",
                m.u64_at(header::BLOCK_STRIDE).load(Ordering::Relaxed),
                params.block_stride,
            ),
            (
                "block_capacity",
                u32_field(header::BLOCK_CAPACITY),
                u64::from(params.block_capacity),
            ),
            (
                "sample_rate",
                u32_field(header::SAMPLE_RATE),
                u64::from(params.sample_rate),
            ),
            (
                "channels",
                u32_field(header::CHANNELS),
                u64::from(params.channels),
            ),
            (
                "sample_format",
                u32_field(header::SAMPLE_FORMAT),
                u64::from(params.sample_format),
            ),
        ];
        for (field, header, expected) in fields {
            if header != expected {
                return Err(AudioRingError::Mismatch {
                    field,
                    header,
                    expected,
                });
            }
        }
        Ok(())
    }

    pub(crate) fn format(&self) -> AudioFormat {
        self.format
    }

    pub(crate) fn block_count(&self) -> u32 {
        self.block_count
    }

    pub(crate) fn block_capacity(&self) -> u32 {
        self.block_capacity
    }

    /// 音频环头部的 `write_seq`：最新一块的块号，0 表示还没有块。
    pub(crate) fn write_seq(&self) -> &AtomicU64 {
        self.memory.u64_at(header::WRITE_SEQ)
    }

    /// 块号所在的块槽：`(block_number - 1) mod block_count`。块号须大于 0。
    pub(crate) fn slot_of(&self, block_number: u64) -> u32 {
        u32::try_from((block_number - 1) % u64::from(self.block_count))
            .expect("the remainder is less than block_count")
    }

    fn slot_start(&self, slot: u32) -> usize {
        assert!(slot < self.block_count, "slot {slot} out of range");
        BLOCKS_START + slot as usize * self.block_stride
    }

    /// 块槽的 seqlock 序号。
    pub(crate) fn block_seq(&self, slot: u32) -> &AtomicU64 {
        self.memory.u64_at(self.slot_start(slot) + block::SEQ)
    }

    /// 以 Relaxed 写块头部中 `seq` 之外的字段。
    pub(crate) fn store_meta(&self, slot: u32, meta: &BlockMeta) {
        let start = self.slot_start(slot);
        let m = &self.memory;
        let relaxed = Ordering::Relaxed;
        m.u64_at(start + block::BLOCK_NUMBER)
            .store(meta.block_number, relaxed);
        m.u64_at(start + block::CAPTURE_NS)
            .store(meta.capture_ns, relaxed);
        m.u32_at(start + block::SAMPLE_COUNT)
            .store(meta.sample_count, relaxed);
        m.u32_at(start + block::FLAGS).store(meta.flags, relaxed);
    }

    /// 以 Relaxed 读块头部中 `seq` 之外的字段。
    pub(crate) fn load_meta(&self, slot: u32) -> BlockMeta {
        let start = self.slot_start(slot);
        let m = &self.memory;
        let relaxed = Ordering::Relaxed;
        BlockMeta {
            block_number: m.u64_at(start + block::BLOCK_NUMBER).load(relaxed),
            capture_ns: m.u64_at(start + block::CAPTURE_NS).load(relaxed),
            sample_count: m.u32_at(start + block::SAMPLE_COUNT).load(relaxed),
            flags: m.u32_at(start + block::FLAGS).load(relaxed),
        }
    }

    /// 块头部是否合布局：采样点数在 1 到块容量之间，标志没有未定义的位。
    pub(crate) fn check_meta(&self, meta: &BlockMeta) -> Result<(), String> {
        if meta.sample_count == 0 || meta.sample_count > self.block_capacity {
            return Err(format!(
                "{} samples, the capacity is {}",
                meta.sample_count, self.block_capacity
            ));
        }
        if meta.flags & !KNOWN_FLAGS != 0 {
            return Err(format!("unknown flags 0x{:X}", meta.flags));
        }
        Ok(())
    }

    /// 样本值的字节数：`sample_count × channels × 4`。
    pub(crate) fn sample_bytes(&self, sample_count: u32) -> usize {
        sample_count as usize * self.format.bytes_per_sample()
    }

    /// 以 Relaxed 写块槽的样本值。
    pub(crate) fn store_samples(&self, slot: u32, samples: &[u8]) {
        assert!(samples.len() <= self.block_stride - SAMPLES_START);
        self.memory
            .store(self.slot_start(slot) + SAMPLES_START, samples);
    }

    /// 以 Relaxed 读块槽样本区开头的 `samples.len()` 字节。
    pub(crate) fn load_samples(&self, slot: u32, samples: &mut [u8]) {
        assert!(samples.len() <= self.block_stride - SAMPLES_START);
        self.memory
            .load(self.slot_start(slot) + SAMPLES_START, samples);
    }
}

/// 测试用：按参数把音频环头部写进一块新建的共享内存，`write_seq` 为 0。生产中由主进程写。
#[cfg(test)]
pub(crate) fn write_header(memory: &SharedMemory, params: &AudioRingAttachParams) {
    let relaxed = Ordering::Relaxed;
    memory
        .u32_at(header::LAYOUT_VERSION)
        .store(params.layout_version, relaxed);
    memory
        .u32_at(header::BLOCK_COUNT)
        .store(params.block_count, relaxed);
    memory
        .u64_at(header::BLOCK_STRIDE)
        .store(params.block_stride, relaxed);
    memory
        .u32_at(header::BLOCK_CAPACITY)
        .store(params.block_capacity, relaxed);
    memory
        .u32_at(header::SAMPLE_RATE)
        .store(params.sample_rate, relaxed);
    memory
        .u32_at(header::CHANNELS)
        .store(params.channels, relaxed);
    memory
        .u32_at(header::SAMPLE_FORMAT)
        .store(params.sample_format, relaxed);
    memory.u64_at(header::WRITE_SEQ).store(0, relaxed);
    memory.u64_at(header::MAGIC).store(MAGIC, Ordering::Release);
}
