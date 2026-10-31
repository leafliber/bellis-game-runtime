//! 音频环的二进制布局，见 `docs/modules/core-protocol.md` 的“音频环”。
//!
//! 音频环是主进程创建的一块共享内存：起点是 [`AudioRingHeader`]，从 [`BLOCKS_OFFSET`] 起是
//! `block_count` 个跨度为 `block_stride` 的块槽；每个块槽起点是 [`AudioBlockHeader`]，从
//! [`BLOCK_SAMPLES_OFFSET`] 起是样本值。一块是一段连续的采样点，一个采样点是同一时刻各声道的样本值。
//! 所有整数与样本值按本机字节序（Windows 与 Linux 的 x86-64 都是小端）。本模块只定布局、常量与
//! 偏移表；读写由实时核与主进程各自实现。

use std::mem::size_of;
use std::sync::atomic::AtomicU64;

use crate::layout::{Constant, Layout, struct_layout};

/// 音频环头部的 magic：小端读出的 `b"BGRAUDIO"`。
pub const MAGIC: u64 = u64::from_le_bytes(*b"BGRAUDIO");
/// 布局版本：头部、块头部或下列常量变化时加一。
pub const LAYOUT_VERSION: u32 = 1;
/// 块槽 0 相对共享内存起点的偏移。
pub const BLOCKS_OFFSET: u64 = 64;
/// 样本值相对块槽起点的偏移。
pub const BLOCK_SAMPLES_OFFSET: u64 = 64;
/// 块槽跨度的对齐字节数。
pub const BLOCK_ALIGN: u64 = 64;
/// 样本格式 F32：每个样本值是 32 位 IEEE 浮点，各声道交错排列。
pub const SAMPLE_FORMAT_F32: u32 = 1;
/// 一个 F32 样本值的字节数。
pub const BYTES_PER_VALUE: u32 = 4;
/// 声道数的上限。
pub const MAX_CHANNELS: u32 = 2;
/// 采样率的下限（Hz）。
pub const MIN_SAMPLE_RATE: u32 = 8_000;
/// 采样率的上限（Hz）。
pub const MAX_SAMPLE_RATE: u32 = 192_000;
/// 块标志：这一块与上一块之间不连续（丢了声音或时间轴断开）。
pub const FLAG_DISCONTINUITY: u32 = 1;
/// 已定义的全部块标志；其余位须为 0。
pub const KNOWN_FLAGS: u32 = FLAG_DISCONTINUITY;

/// 音频环头部。主进程创建时写入除 `write_seq` 外的全部字段，此后不变；`write_seq` 只由实时核写。
#[repr(C)]
#[derive(Debug)]
pub struct AudioRingHeader {
    /// 固定为 [`MAGIC`]。
    pub magic: u64,
    /// 固定为 [`LAYOUT_VERSION`]。
    pub layout_version: u32,
    /// 块槽数，至少 2。
    pub block_count: u32,
    /// 相邻两块槽起点之间的字节数，是 [`BLOCK_ALIGN`] 的倍数。
    pub block_stride: u64,
    /// 一块最多的采样点数，至少 1。
    pub block_capacity: u32,
    /// 采样率（Hz），在 [`MIN_SAMPLE_RATE`] 到 [`MAX_SAMPLE_RATE`] 之间。
    pub sample_rate: u32,
    /// 声道数，1 到 [`MAX_CHANNELS`]。
    pub channels: u32,
    /// 样本格式，目前只有 [`SAMPLE_FORMAT_F32`]。
    pub sample_format: u32,
    /// 最新一块的块号；0 表示还没有块。
    pub write_seq: AtomicU64,
}

/// 块头部。只由实时核写，以 `seq` 作单写者多读者的 seqlock。
#[repr(C)]
#[derive(Debug)]
pub struct AudioBlockHeader {
    /// seqlock 序号：奇数表示正在写，偶数表示稳定。
    pub seq: AtomicU64,
    /// 块号，从 1 起在本音频环内每块加一；块槽号为 `(block_number - 1) % block_count`。
    pub block_number: u64,
    /// 第一个采样点的采集时刻，与核协议中的 `_ns` 时刻同一时钟。
    pub capture_ns: u64,
    /// 采样点数，1 到 `block_capacity`。
    pub sample_count: u32,
    /// 块标志，见 [`FLAG_DISCONTINUITY`]。
    pub flags: u32,
}

const _: () = assert!(size_of::<AudioRingHeader>() as u64 <= BLOCKS_OFFSET);
const _: () = assert!(size_of::<AudioBlockHeader>() as u64 <= BLOCK_SAMPLES_OFFSET);
const _: () = assert!(BLOCKS_OFFSET.is_multiple_of(BLOCK_ALIGN));
const _: () = assert!(BLOCK_SAMPLES_OFFSET.is_multiple_of(BLOCK_ALIGN));

/// 音频环的偏移表，导出到 `protocol/audio-ring-layout.json`。
#[must_use]
pub fn layout() -> Layout {
    let constant = |name, value: u32| Constant {
        name,
        value: value.into(),
    };
    Layout {
        constants: vec![
            Constant {
                name: "MAGIC",
                value: MAGIC,
            },
            constant("LAYOUT_VERSION", LAYOUT_VERSION),
            Constant {
                name: "BLOCKS_OFFSET",
                value: BLOCKS_OFFSET,
            },
            Constant {
                name: "BLOCK_SAMPLES_OFFSET",
                value: BLOCK_SAMPLES_OFFSET,
            },
            Constant {
                name: "BLOCK_ALIGN",
                value: BLOCK_ALIGN,
            },
            constant("SAMPLE_FORMAT_F32", SAMPLE_FORMAT_F32),
            constant("BYTES_PER_VALUE", BYTES_PER_VALUE),
            constant("MAX_CHANNELS", MAX_CHANNELS),
            constant("MIN_SAMPLE_RATE", MIN_SAMPLE_RATE),
            constant("MAX_SAMPLE_RATE", MAX_SAMPLE_RATE),
            constant("FLAG_DISCONTINUITY", FLAG_DISCONTINUITY),
            constant("KNOWN_FLAGS", KNOWN_FLAGS),
        ],
        structs: vec![
            struct_layout!(AudioRingHeader {
                magic: u64,
                layout_version: u32,
                block_count: u32,
                block_stride: u64,
                block_capacity: u32,
                sample_rate: u32,
                channels: u32,
                sample_format: u32,
                write_seq: AtomicU64,
            }),
            struct_layout!(AudioBlockHeader {
                seq: AtomicU64,
                block_number: u64,
                capture_ns: u64,
                sample_count: u32,
                flags: u32,
            }),
        ],
    }
}

#[cfg(test)]
mod tests {
    use super::{MAGIC, layout};
    use crate::layout::assert_no_padding;

    #[test]
    fn magic_reads_as_its_ascii_name() {
        assert_eq!(&MAGIC.to_le_bytes(), b"BGRAUDIO");
    }

    #[test]
    fn layout_lists_every_field_without_padding() {
        assert_no_padding(&layout());
    }
}
