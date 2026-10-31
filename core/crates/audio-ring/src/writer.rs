//! 音频环的写入方：实时核按 seqlock 规则写块并发布 `write_seq`。写方只有实时核一个。

use std::sync::atomic::{Ordering, fence};

use audio_capture::{AudioFormat, AudioPacket};
use core_protocol::audio_ring::FLAG_DISCONTINUITY;
use core_protocol::messages::AudioRingAttachParams;

use crate::error::{AudioRingError, WriteError};
use crate::ring::{BlockMeta, Ring};

/// 音频环的写入方。
///
/// 块号从挂接时头部的 `write_seq` 续写，每块加一，所以实时核重启后块号不回退。一个包超过块容量时拆成
/// 连续的几块，后面各块的采集时刻按采样率推算，不连续标志只在第一块上。
#[derive(Debug)]
pub struct AudioRingWriter {
    ring: Ring,
    last_number: u64,
}

impl AudioRingWriter {
    /// 打开 `audio_ring.attach` 指明的音频环并核对头部；块号从头部的 `write_seq` 续写。
    ///
    /// # Errors
    ///
    /// 参数不合布局规则、打不开共享内存，或头部与参数不符，见 [`AudioRingError`]。
    pub fn attach(params: &AudioRingAttachParams) -> Result<Self, AudioRingError> {
        let ring = Ring::open(params)?;
        let resumed = ring.write_seq().load(Ordering::Acquire);
        Ok(Self {
            ring,
            last_number: resumed,
        })
    }

    /// 音频环的格式：音频源须按它交出声音。
    #[must_use]
    pub fn format(&self) -> AudioFormat {
        self.ring.format()
    }

    /// 最近写出的块号；还没写过时是挂接时的 `write_seq`。
    #[must_use]
    pub fn last_block_number(&self) -> u64 {
        self.last_number
    }

    /// 写入一个包，返回写出的最后一块的块头部。
    ///
    /// # Errors
    ///
    /// 包没有采样点、字节数与音频环的格式不符，或块号超出 u64 时返回 [`WriteError`]，音频环不受影响。
    pub fn write(&mut self, packet: &AudioPacket) -> Result<BlockMeta, WriteError> {
        let format = self.ring.format();
        if packet.sample_count == 0 {
            return Err(WriteError::InvalidPacket(
                "the packet has no samples".into(),
            ));
        }
        let expected = self.ring.sample_bytes(packet.sample_count);
        if packet.data.len() != expected {
            return Err(WriteError::InvalidPacket(format!(
                "{} bytes for {} samples of {format}, expected {expected}",
                packet.data.len(),
                packet.sample_count
            )));
        }
        let blocks = packet.sample_count.div_ceil(self.ring.block_capacity());
        self.last_number
            .checked_add(u64::from(blocks))
            .ok_or_else(|| WriteError::InvalidPacket("block numbers overflow u64".into()))?;

        let capacity = self.ring.block_capacity();
        let mut offset = 0u32;
        let mut last = BlockMeta::default();
        while offset < packet.sample_count {
            let count = capacity.min(packet.sample_count - offset);
            let flags = if offset == 0 && packet.discontinuity {
                FLAG_DISCONTINUITY
            } else {
                0
            };
            let meta = BlockMeta {
                block_number: self.last_number + 1,
                capture_ns: packet
                    .capture_ns
                    .saturating_add(format.duration_ns(u64::from(offset))),
                sample_count: count,
                flags,
            };
            let start = self.ring.sample_bytes(offset);
            let end = start + self.ring.sample_bytes(count);
            self.write_block(&meta, &packet.data[start..end]);
            self.last_number = meta.block_number;
            last = meta;
            offset += count;
        }
        Ok(last)
    }

    fn write_block(&self, meta: &BlockMeta, samples: &[u8]) {
        let ring = &self.ring;
        let slot = ring.slot_of(meta.block_number);
        let seq = ring.block_seq(slot);
        // 置为下一个奇数：偶数加一，崩溃后遗留的奇数加二。Release 栅栏让后面写块的内容排在它之后。
        let previous = seq.load(Ordering::Relaxed);
        let odd = previous.wrapping_add(1 + (previous & 1));
        seq.store(odd, Ordering::Relaxed);
        fence(Ordering::Release);
        ring.store_meta(slot, meta);
        ring.store_samples(slot, samples);
        seq.store(odd.wrapping_add(1), Ordering::Release);
        ring.write_seq().store(meta.block_number, Ordering::Release);
    }
}
