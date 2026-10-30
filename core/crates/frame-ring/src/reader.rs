//! 帧环的读取方：按 seqlock 规则取最新一帧，读到撕裂就重试。
//!
//! 实时核本身不读帧环；读取方供只用于自动测试的假采集源从假显示面取帧，以及测试核对写入方。

use std::hint::spin_loop;
use std::sync::atomic::{Ordering, fence};

use core_protocol::messages::FrameRingAttachParams;

use crate::error::{FrameRingError, ReadError};
use crate::ring::{Ring, SlotMeta};

/// 读最新一帧的结果。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReadOutcome {
    /// 还没有帧（`write_seq` 为 0）。
    Empty,
    /// 读到了完整的一帧，像素已写入调用方的缓冲。
    Frame(SlotMeta),
    /// 每次尝试都读到撕裂的帧（写方正在写这个槽或已经超过它），放弃这次读取；稍后再读即可。
    Torn,
}

/// 帧环的读取方。
#[derive(Debug)]
pub struct FrameRingReader {
    ring: Ring,
}

impl FrameRingReader {
    /// 打开帧环并核对头部，规则与 [`crate::FrameRingWriter::attach`] 相同。
    ///
    /// # Errors
    ///
    /// 见 [`FrameRingError`]。
    pub fn open(params: &FrameRingAttachParams) -> Result<Self, FrameRingError> {
        Ring::open(params).map(|ring| Self { ring })
    }

    /// 最新一帧的帧号，0 表示还没有帧。
    #[must_use]
    pub fn latest_frame_number(&self) -> u64 {
        self.ring.write_seq().load(Ordering::Acquire)
    }

    /// 读最新一帧：元数据作为结果返回，像素（`row_stride * height` 字节）写进 `pixels`，
    /// 复用它的容量。最多尝试 `attempts` 次，每次都撕裂时返回 [`ReadOutcome::Torn`]。
    /// 返回 [`ReadOutcome::Frame`] 之外的结果时，`pixels` 的内容不确定。
    ///
    /// # Errors
    ///
    /// 槽头部在 seqlock 下稳定却不合布局时返回 [`ReadError::Corrupt`]。
    pub fn read_latest(
        &self,
        pixels: &mut Vec<u8>,
        attempts: u32,
    ) -> Result<ReadOutcome, ReadError> {
        let ring = &self.ring;
        for _ in 0..attempts {
            let number = ring.write_seq().load(Ordering::Acquire);
            if number == 0 {
                return Ok(ReadOutcome::Empty);
            }
            let slot = ring.slot_of(number);
            let seq = ring.slot_seq(slot);
            let before = seq.load(Ordering::Acquire);
            if before & 1 == 1 {
                spin_loop();
                continue;
            }
            let (meta, pixel_format) = ring.load_meta(slot);
            if meta.frame_number != number {
                // 写方已经超过这一槽，或还没写完它；重读最新的帧。
                continue;
            }
            if let Err(reason) = ring.check_meta(&meta, pixel_format) {
                fence(Ordering::Acquire);
                if seq.load(Ordering::Relaxed) != before {
                    continue;
                }
                return Err(ReadError::Corrupt(reason));
            }
            // 放得进槽，所以放得进 usize。
            #[allow(clippy::cast_possible_truncation, reason = "已核对不超过槽的大小")]
            pixels.resize(meta.pixel_bytes() as usize, 0);
            ring.load_pixels(slot, pixels);
            // Acquire 栅栏让上面的读排在下面重读 `seq` 之前。
            fence(Ordering::Acquire);
            if seq.load(Ordering::Relaxed) == before {
                return Ok(ReadOutcome::Frame(meta));
            }
        }
        Ok(ReadOutcome::Torn)
    }
}
