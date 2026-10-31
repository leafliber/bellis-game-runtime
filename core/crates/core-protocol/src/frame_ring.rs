//! 帧环的二进制布局，见 `docs/modules/core-protocol.md` 的“帧环与音频环”。
//!
//! 帧环是主进程创建的一块共享内存：起点是 [`FrameRingHeader`]，从 [`SLOTS_OFFSET`] 起是
//! `slot_count` 个跨度为 `slot_stride` 的槽；每个槽起点是 [`FrameSlotHeader`]，从
//! [`SLOT_PIXELS_OFFSET`] 起是像素。所有整数按本机字节序（Windows 与 Linux 的 x86-64 都是小端）。
//! 本模块只定布局、常量与偏移表；读写由实时核与主进程各自实现。

use std::mem::size_of;
use std::sync::atomic::AtomicU64;

use crate::layout::{Constant, Layout, struct_layout};

/// 帧环头部的 magic：小端读出的 `b"BGRTRING"`。
pub const MAGIC: u64 = u64::from_le_bytes(*b"BGRTRING");
/// 布局版本：头部、槽头部或下列常量变化时加一。
pub const LAYOUT_VERSION: u32 = 1;
/// 槽 0 相对共享内存起点的偏移。
pub const SLOTS_OFFSET: u64 = 64;
/// 像素相对槽起点的偏移。
pub const SLOT_PIXELS_OFFSET: u64 = 64;
/// 槽跨度的对齐字节数。
pub const SLOT_ALIGN: u64 = 64;
/// 像素格式 BGRA8：每像素 4 字节，依次为 B、G、R、A。
pub const PIXEL_FORMAT_BGRA8: u32 = 1;

/// 帧环头部。主进程创建时写入除 `write_seq` 外的全部字段，此后不变；`write_seq` 只由实时核写。
#[repr(C)]
#[derive(Debug)]
pub struct FrameRingHeader {
    /// 固定为 [`MAGIC`]。
    pub magic: u64,
    /// 固定为 [`LAYOUT_VERSION`]。
    pub layout_version: u32,
    /// 槽数，至少 2。
    pub slot_count: u32,
    /// 相邻两槽起点之间的字节数，是 [`SLOT_ALIGN`] 的倍数。
    pub slot_stride: u64,
    /// 帧的最大宽度（物理像素）。
    pub max_width: u32,
    /// 帧的最大高度（物理像素）。
    pub max_height: u32,
    /// 最新一帧的帧号；0 表示还没有帧。
    pub write_seq: AtomicU64,
}

/// 槽头部。只由实时核写，以 `seq` 作单写者多读者的 seqlock。
#[repr(C)]
#[derive(Debug)]
pub struct FrameSlotHeader {
    /// seqlock 序号：奇数表示正在写，偶数表示稳定。
    pub seq: AtomicU64,
    /// 帧号，从 1 起在本帧环内严格递增；槽号为 `(frame_number - 1) % slot_count`。
    pub frame_number: u64,
    /// 采集时刻，与核协议中的 `_ns` 时刻同一时钟。
    pub capture_ns: u64,
    /// 窗口与 DPI 变换版本：窗口移动、缩放或 DPI 变化时加一。
    pub transform_version: u64,
    /// 帧宽（物理像素），不超过 `max_width`。
    pub width: u32,
    /// 帧高（物理像素），不超过 `max_height`。
    pub height: u32,
    /// 相邻两行起点之间的字节数，不小于 `width * 4`。
    pub row_stride: u32,
    /// 像素格式，目前只有 [`PIXEL_FORMAT_BGRA8`]。
    pub pixel_format: u32,
}

const _: () = assert!(size_of::<FrameRingHeader>() as u64 <= SLOTS_OFFSET);
const _: () = assert!(size_of::<FrameSlotHeader>() as u64 <= SLOT_PIXELS_OFFSET);
const _: () = assert!(SLOTS_OFFSET.is_multiple_of(SLOT_ALIGN));
const _: () = assert!(SLOT_PIXELS_OFFSET.is_multiple_of(SLOT_ALIGN));

/// 帧环的偏移表，导出到 `protocol/frame-ring-layout.json`。
#[must_use]
pub fn layout() -> Layout {
    Layout {
        constants: vec![
            Constant {
                name: "MAGIC",
                value: MAGIC,
            },
            Constant {
                name: "LAYOUT_VERSION",
                value: LAYOUT_VERSION.into(),
            },
            Constant {
                name: "SLOTS_OFFSET",
                value: SLOTS_OFFSET,
            },
            Constant {
                name: "SLOT_PIXELS_OFFSET",
                value: SLOT_PIXELS_OFFSET,
            },
            Constant {
                name: "SLOT_ALIGN",
                value: SLOT_ALIGN,
            },
            Constant {
                name: "PIXEL_FORMAT_BGRA8",
                value: PIXEL_FORMAT_BGRA8.into(),
            },
        ],
        structs: vec![
            struct_layout!(FrameRingHeader {
                magic: u64,
                layout_version: u32,
                slot_count: u32,
                slot_stride: u64,
                max_width: u32,
                max_height: u32,
                write_seq: AtomicU64,
            }),
            struct_layout!(FrameSlotHeader {
                seq: AtomicU64,
                frame_number: u64,
                capture_ns: u64,
                transform_version: u64,
                width: u32,
                height: u32,
                row_stride: u32,
                pixel_format: u32,
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
        assert_eq!(&MAGIC.to_le_bytes(), b"BGRTRING");
    }

    #[test]
    fn layout_lists_every_field_without_padding() {
        assert_no_padding(&layout());
    }
}
