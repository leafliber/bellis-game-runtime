//! DPI 感知（`docs/modules/realtime-core.md` 的“画面采集”）：坐标一律是物理像素，进程须声明为按显示器
//! 感知 DPI v2。

use windows::Win32::Foundation::HANDLE;
use windows::Win32::UI::HiDpi::{
    AreDpiAwarenessContextsEqual, DPI_AWARENESS_CONTEXT,
    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2, GetDpiAwarenessContextForProcess,
    GetThreadDpiAwarenessContext, SetProcessDpiAwarenessContext,
};

/// 把当前进程声明为按显示器感知 DPI v2。须在创建任何窗口或读取任何坐标之前调用。
///
/// 进程的 DPI 感知只能设定一次：已经是 v2 时视为成功。
///
/// # Errors
///
/// 进程已是其它 DPI 感知模式时返回系统的错误。
pub fn declare_per_monitor_aware_v2() -> windows::core::Result<()> {
    // SAFETY: 参数是系统定义的伪句柄常量，调用不读写任何调用方内存。
    let declared =
        unsafe { SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2) };
    if declared.is_err() && process_is_per_monitor_aware_v2() {
        return Ok(());
    }
    declared
}

/// 当前进程是否已是按显示器感知 DPI v2。
#[must_use]
pub fn process_is_per_monitor_aware_v2() -> bool {
    // SAFETY: 空句柄表示查询当前进程；调用只读系统状态，返回的上下文是不需要释放的伪句柄。
    is_v2(unsafe { GetDpiAwarenessContextForProcess(HANDLE::default()) })
}

/// 调用线程是否是按显示器感知 DPI v2。线程可以覆盖进程的设定，坐标换算按线程的设定进行。
#[must_use]
pub fn thread_is_per_monitor_aware_v2() -> bool {
    // SAFETY: 调用只读当前线程的状态，返回的上下文是不需要释放的伪句柄。
    is_v2(unsafe { GetThreadDpiAwarenessContext() })
}

fn is_v2(context: DPI_AWARENESS_CONTEXT) -> bool {
    // SAFETY: 两个参数都是系统给出的 DPI 感知上下文伪句柄，调用只比较它们。
    unsafe { AreDpiAwarenessContextsEqual(context, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2) }
        .as_bool()
}

#[cfg(test)]
mod tests {
    use super::{
        declare_per_monitor_aware_v2, process_is_per_monitor_aware_v2,
        thread_is_per_monitor_aware_v2,
    };

    #[test]
    fn declares_per_monitor_aware_v2_once_and_accepts_repeat() {
        declare_per_monitor_aware_v2().expect("first declaration");
        assert!(process_is_per_monitor_aware_v2());
        assert!(thread_is_per_monitor_aware_v2());
        // 第二次调用时系统拒绝重设，但进程已是 v2，应视为成功。
        declare_per_monitor_aware_v2().expect("repeated declaration");
        assert!(process_is_per_monitor_aware_v2());
    }
}
