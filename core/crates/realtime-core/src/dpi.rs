//! 进程的 DPI 感知声明（`docs/modules/realtime-core.md` 的“画面采集”）。

use windows::Win32::Foundation::HANDLE;
use windows::Win32::UI::HiDpi::{
    AreDpiAwarenessContextsEqual, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
    GetDpiAwarenessContextForProcess, SetProcessDpiAwarenessContext,
};

/// 把当前进程声明为按显示器感知 DPI v2。
///
/// 进程的 DPI 感知只能设定一次：已经是 v2 时视为成功，已经是其它模式时返回系统的错误。
pub(crate) fn declare_per_monitor_aware_v2() -> windows::core::Result<()> {
    // SAFETY: 参数是系统定义的伪句柄常量，调用不读写任何调用方内存。
    let declared =
        unsafe { SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2) };
    if declared.is_err() && is_per_monitor_aware_v2() {
        return Ok(());
    }
    declared
}

/// 当前进程是否已是按显示器感知 DPI v2。
fn is_per_monitor_aware_v2() -> bool {
    // SAFETY: 空句柄表示查询当前进程；两个调用都只读系统状态，返回的上下文是不需要释放的伪句柄。
    unsafe {
        let current = GetDpiAwarenessContextForProcess(HANDLE::default());
        AreDpiAwarenessContextsEqual(current, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2).as_bool()
    }
}

#[cfg(test)]
mod tests {
    use super::{declare_per_monitor_aware_v2, is_per_monitor_aware_v2};

    #[test]
    fn declares_per_monitor_aware_v2_once_and_accepts_repeat() {
        declare_per_monitor_aware_v2().expect("first declaration");
        assert!(is_per_monitor_aware_v2());
        // 第二次调用时系统拒绝重设，但进程已是 v2，应视为成功。
        declare_per_monitor_aware_v2().expect("repeated declaration");
        assert!(is_per_monitor_aware_v2());
    }
}
