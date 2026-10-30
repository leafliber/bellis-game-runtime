//! 探针写进帧环的帧图案。与 Python 一侧 `gs03_probe.pattern` 以及 `frame-ring` 示例 `ring_interop`
//! 的定义相同：尺寸、采集时刻与变换版本都由帧号算出，每个像素是同一个由帧号算出的 4 字节值，
//! 非提权一侧只凭元数据即可核对整帧、发现撕裂。

use capture::{Frame, FrameMeta, Point};

/// 第 `number` 帧（`number >= 1`）。要求帧环最大尺寸至少 7×5。
#[must_use]
pub fn pattern_frame(number: u64, max_width: u32, max_height: u32) -> Frame {
    let width = max_width - u32::try_from(number % 7).expect("number % 7 fits in u32");
    let height = max_height - u32::try_from(number % 5).expect("number % 5 fits in u32");
    #[allow(clippy::cast_possible_truncation, reason = "只取低 32 位作图案")]
    let word = (number as u32).wrapping_mul(0x9E37_79B1).to_le_bytes();
    let meta = FrameMeta {
        number,
        captured_at_ns: number * 1000 + 7,
        transform_version: number / 5 + 1,
        width,
        height,
        stride: width * 4,
        client_origin: Point { x: 0, y: 0 },
        dpi: 96,
    };
    let pixels = word.repeat((width * height) as usize);
    Frame { meta, pixels }
}
