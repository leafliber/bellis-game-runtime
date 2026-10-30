//! Windows 上的实现：DPI 感知声明、窗口识别与几何、WGC 采集源，以及测试与探测用的已知图案窗口。

pub mod dpi;
mod pattern_window;
mod wgc;
mod window;

pub use pattern_window::{PatternCheck, PatternStyle, PatternWindow};
pub use wgc::{BorderRequest, BorderStatus, WgcCapture, WgcOptions, WgcStats, is_supported};
pub use window::{TopLevelWindow, find_window, list_windows, query_geometry};

use crate::CaptureError;

/// 现在的 QPC 时刻（纳秒），与主进程 `time.perf_counter_ns()` 同一时钟，见 [`mono_clock::now_ns`]。
pub use mono_clock::now_ns as qpc_now_ns;

/// `T` 的字节数，供 Win32 结构的大小参数使用。
#[allow(
    clippy::cast_possible_truncation,
    reason = "Win32 的结构都远小于 4 GiB"
)]
const fn byte_size<T>() -> u32 {
    size_of::<T>() as u32
}

/// 把系统调用的错误包装为 [`CaptureError::Os`]。
fn os(context: &'static str) -> impl FnOnce(windows::core::Error) -> CaptureError {
    move |error| CaptureError::Os {
        context,
        code: error.code().0,
        message: error.message(),
    }
}
