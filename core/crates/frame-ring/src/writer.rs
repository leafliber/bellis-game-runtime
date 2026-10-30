//! 帧环的写入方：实时核按 seqlock 规则写槽并发布 `write_seq`。写方只有实时核一个。

use std::sync::atomic::{Ordering, fence};

use capture::{BYTES_PER_PIXEL, Frame, FrameMeta};
use core_protocol::messages::FrameRingAttachParams;

use crate::error::{FrameRingError, WriteError};
use crate::ring::{Ring, SlotMeta};

/// 帧环的写入方。
///
/// 帧号 = 续写基数 + 采集源的帧号。挂接时续写基数取头部的 `write_seq`，所以实时核重启后帧号续写、
/// 不回退；采集源的帧号不再增大（新建了采集源，帧号从 1 重新数起）时，续写基数改为已写出的最大帧号。
/// 同一采集源的帧号之差因此保留下来，相邻两帧的帧号之差减一仍是其间丢弃的帧数。
#[derive(Debug)]
pub struct FrameRingWriter {
    ring: Ring,
    base: u64,
    last_number: u64,
    last_source_number: u64,
}

impl FrameRingWriter {
    /// 打开 `frame_ring.attach` 指明的帧环并核对头部；帧号从头部的 `write_seq` 续写。
    ///
    /// # Errors
    ///
    /// 参数不合布局规则、打不开共享内存，或头部与参数不符，见 [`FrameRingError`]。
    pub fn attach(params: &FrameRingAttachParams) -> Result<Self, FrameRingError> {
        let ring = Ring::open(params)?;
        let resumed = ring.write_seq().load(Ordering::Acquire);
        Ok(Self {
            ring,
            base: resumed,
            last_number: resumed,
            last_source_number: 0,
        })
    }

    /// 最近写出的帧号；还没写过时是挂接时的 `write_seq`。
    #[must_use]
    pub fn last_frame_number(&self) -> u64 {
        self.last_number
    }

    /// 写入一帧，返回它在帧环中的帧号。
    ///
    /// # Errors
    ///
    /// 见 [`Self::write_parts`]。
    pub fn write(&mut self, frame: &Frame) -> Result<u64, WriteError> {
        self.write_parts(&frame.meta, &frame.pixels)
    }

    /// 写入元数据为 `meta`、像素为 `pixels` 的一帧，返回它在帧环中的帧号。
    ///
    /// `pixels` 每行 `meta.stride` 字节、前 `meta.width * 4` 字节有效；写进槽时去掉行尾的填充，
    /// 槽的行跨度为 `width * 4`。`meta` 中的客户区原点与 DPI 不进帧环。
    ///
    /// # Errors
    ///
    /// 帧比帧环大或帧本身不合规则时返回 [`WriteError`]，帧环不受影响。
    pub fn write_parts(&mut self, meta: &FrameMeta, pixels: &[u8]) -> Result<u64, WriteError> {
        let (row_bytes, row_stride) = self.check(meta, pixels)?;
        let (base, number) = self.next_number(meta.number)?;
        let ring = &self.ring;
        let slot = ring.slot_of(number);
        let seq = ring.slot_seq(slot);

        // 置为下一个奇数：偶数加一，崩溃后遗留的奇数加二。Release 栅栏让后面写槽的内容排在它之后。
        let previous = seq.load(Ordering::Relaxed);
        let odd = previous.wrapping_add(1 + (previous & 1));
        seq.store(odd, Ordering::Relaxed);
        fence(Ordering::Release);

        ring.store_meta(
            slot,
            &SlotMeta {
                frame_number: number,
                capture_ns: meta.captured_at_ns,
                transform_version: meta.transform_version,
                width: meta.width,
                height: meta.height,
                row_stride,
            },
        );
        let stride = meta.stride as usize;
        if stride == row_bytes {
            ring.store_pixels(slot, 0, &pixels[..row_bytes * meta.height as usize]);
        } else {
            for (y, row) in pixels.chunks(stride).take(meta.height as usize).enumerate() {
                ring.store_pixels(slot, y * row_bytes, &row[..row_bytes]);
            }
        }

        seq.store(odd.wrapping_add(1), Ordering::Release);
        ring.write_seq().store(number, Ordering::Release);
        self.base = base;
        self.last_number = number;
        self.last_source_number = meta.number;
        Ok(number)
    }

    /// 核对帧，返回一行有效像素的字节数，即槽的行跨度。
    fn check(&self, meta: &FrameMeta, pixels: &[u8]) -> Result<(usize, u32), WriteError> {
        if meta.number == 0 {
            return Err(WriteError::InvalidFrame("frame number is 0"));
        }
        if meta.width == 0 || meta.height == 0 {
            return Err(WriteError::InvalidFrame("the frame is empty"));
        }
        if meta.width > self.ring.max_width() || meta.height > self.ring.max_height() {
            return Err(WriteError::TooLarge {
                width: meta.width,
                height: meta.height,
                max_width: self.ring.max_width(),
                max_height: self.ring.max_height(),
            });
        }
        let row_stride = meta
            .width
            .checked_mul(BYTES_PER_PIXEL)
            .ok_or(WriteError::InvalidFrame("width * 4 overflows u32"))?;
        if meta.stride < row_stride {
            return Err(WriteError::InvalidFrame("stride is less than width * 4"));
        }
        let needed = u64::from(meta.stride) * u64::from(meta.height);
        if (pixels.len() as u64) < needed {
            return Err(WriteError::InvalidFrame(
                "fewer pixels than stride * height",
            ));
        }
        Ok((row_stride as usize, row_stride))
    }

    /// 这一帧的续写基数与帧号：采集源的帧号不再增大时，续写基数改为已写出的最大帧号。
    fn next_number(&self, source_number: u64) -> Result<(u64, u64), WriteError> {
        let base = if source_number <= self.last_source_number {
            self.last_number
        } else {
            self.base
        };
        let number = base
            .checked_add(source_number)
            .ok_or(WriteError::InvalidFrame("frame number overflows u64"))?;
        Ok((base, number))
    }
}
