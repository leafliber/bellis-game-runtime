//! Windows 上的实现：DPI 感知声明、窗口识别与几何、WGC 采集源，以及测试与探测用的已知图案窗口。

pub mod dpi;
mod pattern_window;
mod wgc;
mod window;

pub use pattern_window::{PatternCheck, PatternStyle, PatternWindow};
pub use wgc::{BorderRequest, BorderStatus, WgcCapture, WgcOptions, WgcStats};
pub use window::{TopLevelWindow, find_window, list_windows, query_geometry};

use crate::{CaptureError, qpc_ticks_to_ns};
use std::sync::OnceLock;
use windows::Win32::System::Performance::{QueryPerformanceCounter, QueryPerformanceFrequency};

/// 现在的 QPC 时刻，按 [`qpc_ticks_to_ns`] 换算为纳秒，与主进程 `time.perf_counter_ns()` 同一时钟。
#[must_use]
pub fn qpc_now_ns() -> u64 {
    static FREQUENCY: OnceLock<i64> = OnceLock::new();
    let frequency = *FREQUENCY.get_or_init(|| {
        let mut frequency = 0;
        // SAFETY: 只写入局部变量。Windows XP 起这个调用不会失败；万一失败，频率为 0，换算结果为 0。
        let _ = unsafe { QueryPerformanceFrequency(&raw mut frequency) };
        frequency
    });
    let mut ticks = 0;
    // SAFETY: 只写入局部变量。Windows XP 起这个调用不会失败；万一失败，刻度为 0，换算结果为 0。
    let _ = unsafe { QueryPerformanceCounter(&raw mut ticks) };
    qpc_ticks_to_ns(ticks, frequency).unwrap_or(0)
}

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
