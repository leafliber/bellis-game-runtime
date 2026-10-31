//! 打开帧环并核对：参数本身合布局规则，共享内存不小于 `size_bytes`，头部与参数一致。
//! 槽头部的读写也在这里，供写入方与读取方共用。

use std::mem::offset_of;
use std::sync::atomic::{AtomicU64, Ordering};

use core_protocol::frame_ring::{
    FrameRingHeader, FrameSlotHeader, LAYOUT_VERSION, MAGIC, PIXEL_FORMAT_BGRA8, SLOT_ALIGN,
    SLOT_PIXELS_OFFSET, SLOTS_OFFSET,
};
use core_protocol::messages::FrameRingAttachParams;

use crate::error::FrameRingError;
use shared_memory::SharedMemory;

/// BGRA8 每个像素的字节数。
const BYTES_PER_PIXEL: u64 = 4;

/// 槽头部中 `seq` 之外的字段，即一帧的元数据。
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct SlotMeta {
    /// 帧号，从 1 起在本帧环内严格递增。
    pub frame_number: u64,
    /// 采集时刻，与核协议的 `_ns` 时刻同一时钟。
    pub capture_ns: u64,
    /// 窗口与 DPI 变换版本。
    pub transform_version: u64,
    /// 帧宽（物理像素）。
    pub width: u32,
    /// 帧高（物理像素）。
    pub height: u32,
    /// 相邻两行起点之间的字节数。
    pub row_stride: u32,
}

impl SlotMeta {
    /// 像素的字节数：`row_stride * height`。
    #[must_use]
    pub fn pixel_bytes(&self) -> u64 {
        u64::from(self.row_stride) * u64::from(self.height)
    }
}

/// 已打开并核对过的帧环。
#[derive(Debug)]
pub(crate) struct Ring {
    memory: SharedMemory,
    slot_count: u32,
    slot_stride: usize,
    max_width: u32,
    max_height: u32,
}

/// 参数中的 `name` 与 `size_bytes` 之外的布局，核对过且换算成了 usize。
struct Geometry {
    size: usize,
    slot_stride: usize,
}

fn invalid(reason: impl Into<String>) -> FrameRingError {
    FrameRingError::InvalidParams(reason.into())
}

/// 参数本身须合布局规则（`docs/modules/core-protocol.md` 的“帧环”）。
fn check_params(params: &FrameRingAttachParams) -> Result<Geometry, FrameRingError> {
    if params.name.is_empty() || params.name.contains('\0') {
        return Err(invalid("the name is empty or contains NUL"));
    }
    if params.layout_version != LAYOUT_VERSION {
        return Err(invalid(format!(
            "layout_version is {}, this implementation is {LAYOUT_VERSION}",
            params.layout_version
        )));
    }
    if params.slot_count < 2 {
        return Err(invalid("slot_count is less than 2"));
    }
    if params.max_width == 0 || params.max_height == 0 {
        return Err(invalid("max_width and max_height must be positive"));
    }
    if !params.slot_stride.is_multiple_of(SLOT_ALIGN) {
        return Err(invalid(format!(
            "slot_stride {} is not a multiple of {SLOT_ALIGN}",
            params.slot_stride
        )));
    }
    let largest_frame =
        u64::from(params.max_width) * u64::from(params.max_height) * BYTES_PER_PIXEL;
    if params.slot_stride < SLOT_PIXELS_OFFSET + largest_frame {
        return Err(invalid(format!(
            "slot_stride {} cannot hold a {}x{} frame",
            params.slot_stride, params.max_width, params.max_height
        )));
    }
    let needed = u64::from(params.slot_count)
        .checked_mul(params.slot_stride)
        .and_then(|slots| slots.checked_add(SLOTS_OFFSET))
        .ok_or_else(|| invalid("the slots overflow u64"))?;
    if params.size_bytes < needed {
        return Err(invalid(format!(
            "size_bytes {} is less than the {needed} bytes the slots need",
            params.size_bytes
        )));
    }
    let size = usize::try_from(params.size_bytes)
        .map_err(|_| invalid("size_bytes does not fit in memory"))?;
    let slot_stride = usize::try_from(params.slot_stride)
        .map_err(|_| invalid("slot_stride does not fit in memory"))?;
    Ok(Geometry { size, slot_stride })
}

/// 头部字段在共享内存中的偏移，取自 `core_protocol` 的 `#[repr(C)]` 结构。
pub(crate) mod header {
    use super::{FrameRingHeader, offset_of};

    pub(crate) const MAGIC: usize = offset_of!(FrameRingHeader, magic);
    pub(crate) const LAYOUT_VERSION: usize = offset_of!(FrameRingHeader, layout_version);
    pub(crate) const SLOT_COUNT: usize = offset_of!(FrameRingHeader, slot_count);
    pub(crate) const SLOT_STRIDE: usize = offset_of!(FrameRingHeader, slot_stride);
    pub(crate) const MAX_WIDTH: usize = offset_of!(FrameRingHeader, max_width);
    pub(crate) const MAX_HEIGHT: usize = offset_of!(FrameRingHeader, max_height);
    pub(crate) const WRITE_SEQ: usize = offset_of!(FrameRingHeader, write_seq);
}

/// 槽头部字段相对槽起点的偏移。
mod slot {
    use super::{FrameSlotHeader, offset_of};

    pub(super) const SEQ: usize = offset_of!(FrameSlotHeader, seq);
    pub(super) const FRAME_NUMBER: usize = offset_of!(FrameSlotHeader, frame_number);
    pub(super) const CAPTURE_NS: usize = offset_of!(FrameSlotHeader, capture_ns);
    pub(super) const TRANSFORM_VERSION: usize = offset_of!(FrameSlotHeader, transform_version);
    pub(super) const WIDTH: usize = offset_of!(FrameSlotHeader, width);
    pub(super) const HEIGHT: usize = offset_of!(FrameSlotHeader, height);
    pub(super) const ROW_STRIDE: usize = offset_of!(FrameSlotHeader, row_stride);
    pub(super) const PIXEL_FORMAT: usize = offset_of!(FrameSlotHeader, pixel_format);
}

#[allow(
    clippy::cast_possible_truncation,
    reason = "布局常量都很小，在任何平台上都放得进 usize"
)]
const SLOTS_START: usize = SLOTS_OFFSET as usize;
#[allow(
    clippy::cast_possible_truncation,
    reason = "布局常量都很小，在任何平台上都放得进 usize"
)]
const PIXELS_START: usize = SLOT_PIXELS_OFFSET as usize;

impl Ring {
    /// 打开 `params` 指明的共享内存，核对参数与头部。
    pub(crate) fn open(params: &FrameRingAttachParams) -> Result<Self, FrameRingError> {
        let geometry = check_params(params)?;
        let memory = SharedMemory::open(&params.name, geometry.size).map_err(|source| {
            FrameRingError::Open {
                name: params.name.clone(),
                source,
            }
        })?;
        let ring = Self {
            memory,
            slot_count: params.slot_count,
            slot_stride: geometry.slot_stride,
            max_width: params.max_width,
            max_height: params.max_height,
        };
        ring.check_header(params)?;
        Ok(ring)
    }

    fn check_header(&self, params: &FrameRingAttachParams) -> Result<(), FrameRingError> {
        let m = &self.memory;
        let fields = [
            (
                "magic",
                m.u64_at(header::MAGIC).load(Ordering::Acquire),
                MAGIC,
            ),
            (
                "layout_version",
                u64::from(m.u32_at(header::LAYOUT_VERSION).load(Ordering::Relaxed)),
                u64::from(params.layout_version),
            ),
            (
                "slot_count",
                u64::from(m.u32_at(header::SLOT_COUNT).load(Ordering::Relaxed)),
                u64::from(params.slot_count),
            ),
            (
                "slot_stride",
                m.u64_at(header::SLOT_STRIDE).load(Ordering::Relaxed),
                params.slot_stride,
            ),
            (
                "max_width",
                u64::from(m.u32_at(header::MAX_WIDTH).load(Ordering::Relaxed)),
                u64::from(params.max_width),
            ),
            (
                "max_height",
                u64::from(m.u32_at(header::MAX_HEIGHT).load(Ordering::Relaxed)),
                u64::from(params.max_height),
            ),
        ];
        for (field, header, expected) in fields {
            if header != expected {
                return Err(FrameRingError::Mismatch {
                    field,
                    header,
                    expected,
                });
            }
        }
        Ok(())
    }

    pub(crate) fn max_width(&self) -> u32 {
        self.max_width
    }

    pub(crate) fn max_height(&self) -> u32 {
        self.max_height
    }

    /// 帧环头部的 `write_seq`：最新一帧的帧号，0 表示还没有帧。
    pub(crate) fn write_seq(&self) -> &AtomicU64 {
        self.memory.u64_at(header::WRITE_SEQ)
    }

    /// 帧号所在的槽：`(frame_number - 1) mod slot_count`。帧号须大于 0。
    pub(crate) fn slot_of(&self, frame_number: u64) -> u32 {
        u32::try_from((frame_number - 1) % u64::from(self.slot_count))
            .expect("the remainder is less than slot_count")
    }

    fn slot_start(&self, slot: u32) -> usize {
        assert!(slot < self.slot_count, "slot {slot} out of range");
        SLOTS_START + slot as usize * self.slot_stride
    }

    /// 槽的 seqlock 序号。
    pub(crate) fn slot_seq(&self, slot: u32) -> &AtomicU64 {
        self.memory.u64_at(self.slot_start(slot) + slot::SEQ)
    }

    /// 以 Relaxed 写槽头部中 `seq` 之外的字段；像素格式总是 BGRA8。
    pub(crate) fn store_meta(&self, slot: u32, meta: &SlotMeta) {
        let start = self.slot_start(slot);
        let m = &self.memory;
        let relaxed = Ordering::Relaxed;
        m.u64_at(start + slot::FRAME_NUMBER)
            .store(meta.frame_number, relaxed);
        m.u64_at(start + slot::CAPTURE_NS)
            .store(meta.capture_ns, relaxed);
        m.u64_at(start + slot::TRANSFORM_VERSION)
            .store(meta.transform_version, relaxed);
        m.u32_at(start + slot::WIDTH).store(meta.width, relaxed);
        m.u32_at(start + slot::HEIGHT).store(meta.height, relaxed);
        m.u32_at(start + slot::ROW_STRIDE)
            .store(meta.row_stride, relaxed);
        m.u32_at(start + slot::PIXEL_FORMAT)
            .store(PIXEL_FORMAT_BGRA8, relaxed);
    }

    /// 以 Relaxed 读槽头部中 `seq` 之外的字段与像素格式。
    pub(crate) fn load_meta(&self, slot: u32) -> (SlotMeta, u32) {
        let start = self.slot_start(slot);
        let m = &self.memory;
        let relaxed = Ordering::Relaxed;
        let meta = SlotMeta {
            frame_number: m.u64_at(start + slot::FRAME_NUMBER).load(relaxed),
            capture_ns: m.u64_at(start + slot::CAPTURE_NS).load(relaxed),
            transform_version: m.u64_at(start + slot::TRANSFORM_VERSION).load(relaxed),
            width: m.u32_at(start + slot::WIDTH).load(relaxed),
            height: m.u32_at(start + slot::HEIGHT).load(relaxed),
            row_stride: m.u32_at(start + slot::ROW_STRIDE).load(relaxed),
        };
        (meta, m.u32_at(start + slot::PIXEL_FORMAT).load(relaxed))
    }

    /// 槽头部是否合布局：尺寸不超过最大值，行跨度不小于 `width * 4`，像素放得进槽，格式为 BGRA8。
    pub(crate) fn check_meta(&self, meta: &SlotMeta, pixel_format: u32) -> Result<(), String> {
        if pixel_format != PIXEL_FORMAT_BGRA8 {
            return Err(format!("pixel_format is {pixel_format}"));
        }
        if meta.width > self.max_width || meta.height > self.max_height {
            return Err(format!(
                "a {}x{} frame exceeds {}x{}",
                meta.width, meta.height, self.max_width, self.max_height
            ));
        }
        if u64::from(meta.row_stride) < u64::from(meta.width) * BYTES_PER_PIXEL {
            return Err(format!(
                "row_stride {} is less than width {} * 4",
                meta.row_stride, meta.width
            ));
        }
        let capacity = (self.slot_stride - PIXELS_START) as u64;
        if meta.pixel_bytes() > capacity {
            return Err(format!(
                "{} rows of {} bytes exceed the {capacity}-byte slot",
                meta.height, meta.row_stride
            ));
        }
        Ok(())
    }

    /// 以 Relaxed 写槽的像素，从像素区的 `offset` 字节处起。
    pub(crate) fn store_pixels(&self, slot: u32, offset: usize, pixels: &[u8]) {
        assert!(offset + pixels.len() <= self.slot_stride - PIXELS_START);
        self.memory
            .store(self.slot_start(slot) + PIXELS_START + offset, pixels);
    }

    /// 以 Relaxed 读槽像素区开头的 `pixels.len()` 字节。
    pub(crate) fn load_pixels(&self, slot: u32, pixels: &mut [u8]) {
        assert!(pixels.len() <= self.slot_stride - PIXELS_START);
        self.memory
            .load(self.slot_start(slot) + PIXELS_START, pixels);
    }
}

/// 测试用：按参数把帧环头部写进一块新建的共享内存，`write_seq` 为 0。生产中由主进程写。
#[cfg(test)]
pub(crate) fn write_header(memory: &SharedMemory, params: &FrameRingAttachParams) {
    let relaxed = Ordering::Relaxed;
    memory
        .u32_at(header::LAYOUT_VERSION)
        .store(params.layout_version, relaxed);
    memory
        .u32_at(header::SLOT_COUNT)
        .store(params.slot_count, relaxed);
    memory
        .u64_at(header::SLOT_STRIDE)
        .store(params.slot_stride, relaxed);
    memory
        .u32_at(header::MAX_WIDTH)
        .store(params.max_width, relaxed);
    memory
        .u32_at(header::MAX_HEIGHT)
        .store(params.max_height, relaxed);
    memory.u64_at(header::WRITE_SEQ).store(0, relaxed);
    memory.u64_at(header::MAGIC).store(MAGIC, Ordering::Release);
}
