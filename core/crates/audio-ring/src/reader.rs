//! 音频环的读取方：按 seqlock 规则逐块读。
//!
//! 实时核本身不读音频环；读取方供只用于自动测试的假音频源从假音频输出取声音，以及测试核对写入方。

use std::sync::atomic::{Ordering, fence};

use audio_capture::AudioFormat;
use core_protocol::messages::AudioRingAttachParams;

use crate::error::{AudioRingError, ReadError};
use crate::ring::{BlockMeta, Ring};

/// 读一块的结果。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BlockRead {
    /// 读到了完整的一块，样本值已写入调用方的缓冲。
    Block(BlockMeta),
    /// 块槽里不是这一块：它已被更新的块覆盖，或还没有写入。
    Missing,
    /// 写方正在写这个块槽；稍后再读。
    Torn,
}

/// 音频环的读取方。
#[derive(Debug)]
pub struct AudioRingReader {
    ring: Ring,
}

impl AudioRingReader {
    /// 打开音频环并核对头部，规则与 [`crate::AudioRingWriter::attach`] 相同。
    ///
    /// # Errors
    ///
    /// 见 [`AudioRingError`]。
    pub fn open(params: &AudioRingAttachParams) -> Result<Self, AudioRingError> {
        Ring::open(params).map(|ring| Self { ring })
    }

    /// 音频环的格式。
    #[must_use]
    pub fn format(&self) -> AudioFormat {
        self.ring.format()
    }

    /// 块槽数：比最新一块早这么多块的块已被覆盖。
    #[must_use]
    pub fn block_count(&self) -> u32 {
        self.ring.block_count()
    }

    /// 最新一块的块号，0 表示还没有块。
    #[must_use]
    pub fn latest_block_number(&self) -> u64 {
        self.ring.write_seq().load(Ordering::Acquire)
    }

    /// 读块号为 `number` 的块：元数据作为结果返回，样本值（`sample_count × channels × 4` 字节）写进
    /// `samples`，复用它的容量。返回 [`BlockRead::Block`] 之外的结果时，`samples` 的内容不确定。
    ///
    /// # Errors
    ///
    /// 块头部在 seqlock 下稳定却不合布局时返回 [`ReadError::Corrupt`]。
    ///
    /// # Panics
    ///
    /// `number` 为 0 时 panic。
    pub fn read_block(&self, number: u64, samples: &mut Vec<u8>) -> Result<BlockRead, ReadError> {
        assert!(number > 0, "block numbers start at 1");
        let ring = &self.ring;
        let slot = ring.slot_of(number);
        let seq = ring.block_seq(slot);
        let before = seq.load(Ordering::Acquire);
        if before & 1 == 1 {
            return Ok(BlockRead::Torn);
        }
        let meta = ring.load_meta(slot);
        if meta.block_number != number {
            return Ok(BlockRead::Missing);
        }
        if let Err(reason) = ring.check_meta(&meta) {
            fence(Ordering::Acquire);
            if seq.load(Ordering::Relaxed) != before {
                return Ok(BlockRead::Torn);
            }
            return Err(ReadError::Corrupt(reason));
        }
        samples.resize(ring.sample_bytes(meta.sample_count), 0);
        ring.load_samples(slot, samples);
        // Acquire 栅栏让上面的读排在下面重读 `seq` 之前。
        fence(Ordering::Acquire);
        if seq.load(Ordering::Relaxed) == before {
            Ok(BlockRead::Block(meta))
        } else {
            Ok(BlockRead::Torn)
        }
    }
}
