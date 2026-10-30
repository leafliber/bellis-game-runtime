//! 画面采集：采集源接口与 Windows Graphics Capture（WGC）实现。设计见
//! `docs/modules/realtime-core.md` 的“画面采集”。
//!
//! 接口、几何与时钟换算、窗口匹配在所有平台编译与测试；WGC 实现只在 Windows 上编译（[`win`]）。

mod clock;
mod geometry;
mod selector;
#[cfg(windows)]
pub mod win;

pub use clock::hundred_ns_to_ns;
pub use geometry::{
    CropBox, CropError, Point, Rect, Size, Transform, TransformTracker, WindowGeometry, client_crop,
};
pub use selector::{WindowInfo, WindowSelector};

use std::fmt;
use std::time::Duration;

/// BGRA8 每个像素的字节数。
pub const BYTES_PER_PIXEL: u32 = 4;

/// 采集源：每次调用交出最新的一帧。
///
/// 两次调用之间到达的旧帧被丢弃，帧号照样占用（见 [`FrameMeta::number`]）。
pub trait CaptureSource {
    /// 等待下一帧并写入 `frame`，复用 `frame` 的像素缓冲。
    ///
    /// 返回 `Ok(true)` 表示 `frame` 已换成新的一帧；`timeout` 内没有新帧时返回 `Ok(false)`，`frame` 不变。
    ///
    /// # Errors
    ///
    /// 见 [`CaptureError`]。[`CaptureError::is_recoverable`] 为真的错误之后可以继续调用，其余错误之后
    /// 这个采集源不再产出帧。返回错误时 `frame` 的内容不确定，不能当作一帧使用。
    fn next_frame(&mut self, timeout: Duration, frame: &mut Frame) -> Result<bool, CaptureError>;
}

/// 一帧的元数据。坐标与尺寸一律是物理像素。
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct FrameMeta {
    /// 帧号，从 1 开始。采集源取到的每一帧占一个，为交出最新帧或因窗口改变尺寸而丢弃的帧也占，
    /// 所以相邻两次交出的帧号之差减一就是这期间丢弃的帧数。
    pub number: u64,
    /// 采集时刻：QPC 时钟的纳秒数，与主进程 `time.perf_counter_ns()` 同一时钟。
    pub captured_at_ns: u64,
    /// 窗口与 DPI 变换版本，见 [`TransformTracker`]。
    pub transform_version: u64,
    /// 客户区宽度，即像素数据的宽度。
    pub width: u32,
    /// 客户区高度，即像素数据的高度。
    pub height: u32,
    /// 行跨度：相邻两行起点之间的字节数，不小于 `width * 4`。
    pub stride: u32,
    /// 客户区左上角在屏幕上的位置。
    pub client_origin: Point,
    /// 客户区所在显示器的有效 DPI，96 表示 100% 缩放。
    pub dpi: u32,
}

/// 一帧：客户区的 BGRA8 像素与元数据。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Frame {
    /// 元数据。
    pub meta: FrameMeta,
    /// 像素，自上而下逐行排列，每行 `meta.stride` 字节，前 `meta.width * 4` 字节有效；
    /// 长度为 `meta.stride * meta.height`。
    pub pixels: Vec<u8>,
}

impl Frame {
    /// 第 `y` 行的有效像素（`width * 4` 字节）；越界时为 `None`。
    #[must_use]
    pub fn row(&self, y: u32) -> Option<&[u8]> {
        if y >= self.meta.height {
            return None;
        }
        let start = usize::try_from(u64::from(y) * u64::from(self.meta.stride)).ok()?;
        let len = usize::try_from(u64::from(self.meta.width) * u64::from(BYTES_PER_PIXEL)).ok()?;
        self.pixels.get(start..start.checked_add(len)?)
    }

    /// `(x, y)` 处的像素，按 B、G、R、A 的顺序；越界时为 `None`。
    #[must_use]
    pub fn pixel(&self, x: u32, y: u32) -> Option<[u8; 4]> {
        if x >= self.meta.width {
            return None;
        }
        let row = self.row(y)?;
        let start = usize::try_from(x).ok()? * BYTES_PER_PIXEL as usize;
        row.get(start..start + BYTES_PER_PIXEL as usize)?
            .try_into()
            .ok()
    }
}

/// 采集失败的原因。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CaptureError {
    /// 没有符合识别条件的窗口，因而没有帧。
    WindowNotFound {
        /// 识别条件的描述。
        selector: String,
    },
    /// 符合识别条件的窗口不止一个；不猜测是哪一个。
    AmbiguousWindow {
        /// 识别条件的描述。
        selector: String,
        /// 符合条件的窗口数。
        matches: usize,
    },
    /// 窗口已关闭。须重新识别窗口并新建采集源。
    WindowClosed,
    /// 窗口最小化，没有帧。还原后可以继续。
    WindowMinimized,
    /// 窗口处于独占全屏，不支持。切回窗口化或无边框全屏后可以继续。
    ExclusiveFullscreen,
    /// 当前线程不是按显示器感知 DPI v2，坐标不是物理像素。
    NotPerMonitorDpiAwareV2,
    /// 系统不支持所需的能力。
    Unsupported(&'static str),
    /// 采集源取到的帧不合格式。只有假采集源会报：假显示面里的槽头部不合帧环布局。
    InvalidFrame(String),
    /// 系统调用失败。
    Os {
        /// 失败的调用。
        context: &'static str,
        /// HRESULT 或 Win32 错误码。
        code: i32,
        /// 系统给出的说明。
        message: String,
    },
}

impl CaptureError {
    /// 这个错误之后采集源是否还能继续产出帧。
    #[must_use]
    pub fn is_recoverable(&self) -> bool {
        matches!(self, Self::WindowMinimized | Self::ExclusiveFullscreen)
    }
}

impl fmt::Display for CaptureError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::WindowNotFound { selector } => write!(f, "no window matches {selector}"),
            Self::AmbiguousWindow { selector, matches } => {
                write!(f, "{matches} windows match {selector}; refusing to guess")
            }
            Self::WindowClosed => f.write_str("the captured window was closed"),
            Self::WindowMinimized => f.write_str("the captured window is minimized"),
            Self::ExclusiveFullscreen => {
                f.write_str("the captured window is in exclusive fullscreen, which is unsupported")
            }
            Self::NotPerMonitorDpiAwareV2 => {
                f.write_str("the calling thread is not per-monitor DPI aware v2")
            }
            Self::Unsupported(what) => write!(f, "unsupported: {what}"),
            Self::InvalidFrame(reason) => {
                write!(f, "the capture source got an invalid frame: {reason}")
            }
            Self::Os {
                context,
                code,
                message,
            } => write!(f, "{context} failed (0x{code:08X}): {message}"),
        }
    }
}

impl std::error::Error for CaptureError {}

#[cfg(test)]
mod tests {
    use super::{CaptureError, CaptureSource, Frame, FrameMeta, Point};
    use std::collections::VecDeque;
    use std::time::Duration;

    /// 只为测试接口本身的内存采集源：按顺序交出预先给定的帧。不是假采集源。
    struct ScriptedSource {
        frames: VecDeque<Result<Frame, CaptureError>>,
    }

    impl CaptureSource for ScriptedSource {
        fn next_frame(&mut self, _: Duration, frame: &mut Frame) -> Result<bool, CaptureError> {
            match self.frames.pop_front() {
                None => Ok(false),
                Some(Err(error)) => Err(error),
                Some(Ok(next)) => {
                    frame.meta = next.meta;
                    frame.pixels.clear();
                    frame.pixels.extend_from_slice(&next.pixels);
                    Ok(true)
                }
            }
        }
    }

    /// 2×2 的帧，行跨度带 4 字节填充；像素 (x, y) 的 B 通道为 10·y + x。
    fn padded_frame(number: u64) -> Frame {
        let meta = FrameMeta {
            number,
            captured_at_ns: 1_000 * number,
            transform_version: 1,
            width: 2,
            height: 2,
            stride: 12,
            client_origin: Point { x: -5, y: 7 },
            dpi: 144,
        };
        let mut pixels = vec![0xEE; 24];
        for y in 0..2u8 {
            for x in 0..2u8 {
                let at = usize::from(y) * 12 + usize::from(x) * 4;
                pixels[at..at + 4].copy_from_slice(&[10 * y + x, 1, 2, 255]);
            }
        }
        Frame { meta, pixels }
    }

    #[test]
    fn row_and_pixel_honor_stride_and_bounds() {
        let frame = padded_frame(1);
        assert_eq!(frame.row(0), Some(&[0, 1, 2, 255, 1, 1, 2, 255][..]));
        assert_eq!(frame.pixel(1, 1), Some([11, 1, 2, 255]));
        assert_eq!(frame.pixel(0, 1), Some([10, 1, 2, 255]));
        assert_eq!(frame.pixel(2, 0), None);
        assert_eq!(frame.pixel(0, 2), None);
        assert_eq!(frame.row(2), None);
    }

    #[test]
    fn row_is_none_when_pixels_are_shorter_than_meta() {
        let mut frame = padded_frame(1);
        frame.pixels.truncate(16);
        assert!(frame.row(0).is_some());
        assert_eq!(frame.row(1), None);
    }

    #[test]
    fn source_reuses_buffer_and_keeps_frame_on_timeout() {
        let mut source: Box<dyn CaptureSource> = Box::new(ScriptedSource {
            frames: VecDeque::from([
                Ok(padded_frame(1)),
                Ok(padded_frame(3)),
                Err(CaptureError::WindowMinimized),
                Err(CaptureError::WindowClosed),
            ]),
        });
        let mut frame = Frame::default();
        assert_eq!(source.next_frame(Duration::ZERO, &mut frame), Ok(true));
        assert_eq!(frame.meta.number, 1);
        let buffer = frame.pixels.as_ptr();
        assert_eq!(source.next_frame(Duration::ZERO, &mut frame), Ok(true));
        assert_eq!(frame.meta.number, 3);
        assert_eq!(
            frame.pixels.as_ptr(),
            buffer,
            "same-sized frames reuse the buffer"
        );

        let error = source.next_frame(Duration::ZERO, &mut frame).unwrap_err();
        assert!(error.is_recoverable());
        let error = source.next_frame(Duration::ZERO, &mut frame).unwrap_err();
        assert!(!error.is_recoverable());
        assert_eq!(source.next_frame(Duration::ZERO, &mut frame), Ok(false));
        assert_eq!(
            frame.meta.number, 3,
            "a timeout leaves the last frame in place"
        );
    }

    #[test]
    fn errors_describe_themselves() {
        let error = CaptureError::Os {
            context: "D3D11CreateDevice",
            code: -2_005_270_524,
            message: "device removed".into(),
        };
        assert_eq!(
            error.to_string(),
            "D3D11CreateDevice failed (0x887A0004): device removed"
        );
        let error = CaptureError::AmbiguousWindow {
            selector: "process \"a.exe\"".into(),
            matches: 2,
        };
        assert_eq!(
            error.to_string(),
            "2 windows match process \"a.exe\"; refusing to guess"
        );
    }
}
