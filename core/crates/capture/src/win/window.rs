//! 窗口识别、窗口几何与不支持情形的判断（`docs/modules/realtime-core.md` 的“窗口识别”与“客户区与变换版本”）。

use super::dpi::thread_is_per_monitor_aware_v2;
use super::{byte_size, os};
use crate::{CaptureError, Rect, WindowGeometry, WindowInfo, WindowSelector};
use std::ffi::c_void;
use windows::Win32::Foundation::{CloseHandle, HWND, LPARAM, POINT, RECT};
use windows::Win32::Graphics::Dwm::{
    DWMWA_CLOAKED, DWMWA_EXTENDED_FRAME_BOUNDS, DwmGetWindowAttribute,
};
use windows::Win32::Graphics::Gdi::{ClientToScreen, MONITOR_DEFAULTTONEAREST, MonitorFromWindow};
use windows::Win32::System::Threading::{
    OpenProcess, PROCESS_NAME_WIN32, PROCESS_QUERY_LIMITED_INFORMATION, QueryFullProcessImageNameW,
};
use windows::Win32::UI::HiDpi::{GetDpiForMonitor, MDT_EFFECTIVE_DPI};
use windows::Win32::UI::Shell::{QUNS_RUNNING_D3D_FULL_SCREEN, SHQueryUserNotificationState};
use windows::Win32::UI::WindowsAndMessaging::{
    EnumWindows, GetClassNameW, GetClientRect, GetForegroundWindow, GetWindowTextLengthW,
    GetWindowTextW, GetWindowThreadProcessId, IsIconic, IsWindow, IsWindowVisible,
};
use windows::core::{BOOL, PWSTR};

/// 一个可见的顶层窗口。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TopLevelWindow {
    /// 窗口句柄。
    pub hwnd: HWND,
    /// 所属进程的 ID。
    pub process_id: u32,
    /// 识别信息。
    pub info: WindowInfo,
}

/// 列出可见、未隐藏（cloaked）的顶层窗口。最小化的窗口也在其中，以便如实报告最小化。
///
/// # Errors
///
/// 枚举窗口失败时返回 [`CaptureError::Os`]。
pub fn list_windows() -> Result<Vec<TopLevelWindow>, CaptureError> {
    unsafe extern "system" fn collect(hwnd: HWND, lparam: LPARAM) -> BOOL {
        // SAFETY: `lparam` 是下面 `EnumWindows` 调用传入的 `&mut Vec<HWND>`，枚举期间一直有效且不被别处访问。
        let handles = unsafe { &mut *(lparam.0 as *mut Vec<HWND>) };
        handles.push(hwnd);
        BOOL::from(true)
    }
    let mut handles: Vec<HWND> = Vec::new();
    // SAFETY: 回调只把句柄追加进 `handles`；`EnumWindows` 同步返回，之后不再使用这个指针。
    unsafe { EnumWindows(Some(collect), LPARAM(&raw mut handles as isize)) }
        .map_err(os("EnumWindows"))?;
    Ok(handles
        .into_iter()
        .filter(|hwnd| is_candidate(*hwnd))
        .map(describe)
        .collect())
}

/// 按识别条件找唯一的窗口。找不到或不止一个都返回错误，不猜测。
///
/// # Errors
///
/// 没有窗口符合时返回 [`CaptureError::WindowNotFound`]，不止一个时返回 [`CaptureError::AmbiguousWindow`]。
pub fn find_window(selector: &WindowSelector) -> Result<TopLevelWindow, CaptureError> {
    let mut matches: Vec<TopLevelWindow> = list_windows()?
        .into_iter()
        .filter(|window| selector.matches(&window.info))
        .collect();
    match matches.len() {
        0 => Err(CaptureError::WindowNotFound {
            selector: selector.to_string(),
        }),
        1 => Ok(matches.remove(0)),
        count => Err(CaptureError::AmbiguousWindow {
            selector: selector.to_string(),
            matches: count,
        }),
    }
}

/// 查询窗口现在的几何：DWM 窗口边界、客户区在屏幕上的范围与所在显示器的有效 DPI，都是物理像素。
///
/// # Errors
///
/// 调用线程不是按显示器感知 DPI v2 时返回 [`CaptureError::NotPerMonitorDpiAwareV2`]；窗口已关闭时
/// 返回 [`CaptureError::WindowClosed`]；其余失败返回 [`CaptureError::Os`]。
pub fn query_geometry(hwnd: HWND) -> Result<WindowGeometry, CaptureError> {
    if !thread_is_per_monitor_aware_v2() {
        return Err(CaptureError::NotPerMonitorDpiAwareV2);
    }
    let closed_or = |context| {
        move |error| {
            if is_window(hwnd) {
                os(context)(error)
            } else {
                CaptureError::WindowClosed
            }
        }
    };

    let mut bounds = RECT::default();
    // SAFETY: 输出缓冲是一个 RECT，大小与 DWMWA_EXTENDED_FRAME_BOUNDS 要求的一致。
    unsafe {
        DwmGetWindowAttribute(
            hwnd,
            DWMWA_EXTENDED_FRAME_BOUNDS,
            (&raw mut bounds).cast::<c_void>(),
            byte_size::<RECT>(),
        )
    }
    .map_err(closed_or(
        "DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)",
    ))?;

    // 调用线程是按显示器感知 DPI v2，系统把客户区尺寸换算成物理像素交给它（对不感知 DPI 的窗口按缩放比
    // 换算）。原点用 ClientToScreen 取；不用它换算右下角，因为它对不感知 DPI 的窗口先把点取整到逻辑像素，
    // 实测在 175% 缩放下少算 2 个物理像素。
    let mut client = RECT::default();
    // SAFETY: 只写入局部的 RECT。
    unsafe { GetClientRect(hwnd, &raw mut client) }.map_err(closed_or("GetClientRect"))?;
    let (width, height) = (client.right - client.left, client.bottom - client.top);
    // 两个角都换到屏幕坐标，取左上的一个作原点，从右到左布局（WS_EX_LAYOUTRTL）的窗口也成立。
    let mut corners = [
        POINT {
            x: client.left,
            y: client.top,
        },
        POINT {
            x: client.right,
            y: client.bottom,
        },
    ];
    for corner in &mut corners {
        // SAFETY: 只读写局部的 POINT。
        if !unsafe { ClientToScreen(hwnd, corner) }.as_bool() {
            return Err(closed_or("ClientToScreen")(
                windows::core::Error::from_thread(),
            ));
        }
    }
    let [a, b] = corners;
    let (left, top) = (a.x.min(b.x), a.y.min(b.y));
    let client = Rect {
        left,
        top,
        right: left + width,
        bottom: top + height,
    };

    // SAFETY: 窗口句柄由调用方给出；MONITOR_DEFAULTTONEAREST 保证返回一个有效的显示器。
    let monitor = unsafe { MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST) };
    let (mut dpi_x, mut dpi_y) = (0, 0);
    // SAFETY: 只写入两个局部变量。
    unsafe { GetDpiForMonitor(monitor, MDT_EFFECTIVE_DPI, &raw mut dpi_x, &raw mut dpi_y) }
        .map_err(os("GetDpiForMonitor"))?;

    Ok(WindowGeometry {
        frame_bounds: Rect {
            left: bounds.left,
            top: bounds.top,
            right: bounds.right,
            bottom: bounds.bottom,
        },
        client,
        dpi: dpi_x,
    })
}

/// 检查窗口能否采集：已关闭、最小化与独占全屏都明确报错。
pub(crate) fn check_capturable(hwnd: HWND) -> Result<(), CaptureError> {
    if !is_window(hwnd) {
        return Err(CaptureError::WindowClosed);
    }
    // SAFETY: 只读窗口状态；句柄无效时返回假。
    if unsafe { IsIconic(hwnd) }.as_bool() {
        return Err(CaptureError::WindowMinimized);
    }
    if is_exclusive_fullscreen(hwnd)? {
        return Err(CaptureError::ExclusiveFullscreen);
    }
    Ok(())
}

/// 系统报告有 Direct3D 独占全屏程序，且前台窗口属于目标窗口的进程。
///
/// 独占全屏只在程序处于前台时成立，所以前台窗口不属于目标进程时，即使系统报告独占全屏，也不是目标窗口。
fn is_exclusive_fullscreen(hwnd: HWND) -> Result<bool, CaptureError> {
    // SAFETY: 只读系统状态。
    let state =
        unsafe { SHQueryUserNotificationState() }.map_err(os("SHQueryUserNotificationState"))?;
    if state != QUNS_RUNNING_D3D_FULL_SCREEN {
        return Ok(false);
    }
    // SAFETY: 只读系统状态；没有前台窗口时返回空句柄，进程 ID 为 0。
    let foreground = unsafe { GetForegroundWindow() };
    Ok(!foreground.is_invalid() && process_id(foreground) == process_id(hwnd))
}

pub(crate) fn is_window(hwnd: HWND) -> bool {
    // SAFETY: 只读窗口状态；句柄无效时返回假。
    unsafe { IsWindow(Some(hwnd)) }.as_bool()
}

fn is_candidate(hwnd: HWND) -> bool {
    // SAFETY: 只读窗口状态。
    if !unsafe { IsWindowVisible(hwnd) }.as_bool() {
        return false;
    }
    let mut cloaked: u32 = 0;
    // SAFETY: 输出缓冲是一个 u32，大小与 DWMWA_CLOAKED 要求的一致。
    let queried = unsafe {
        DwmGetWindowAttribute(
            hwnd,
            DWMWA_CLOAKED,
            (&raw mut cloaked).cast::<c_void>(),
            byte_size::<u32>(),
        )
    };
    queried.is_err() || cloaked == 0
}

fn describe(hwnd: HWND) -> TopLevelWindow {
    let process_id = process_id(hwnd);
    TopLevelWindow {
        hwnd,
        process_id,
        info: WindowInfo {
            process_image: process_image(process_id),
            class_name: class_name(hwnd),
            title: title(hwnd),
        },
    }
}

fn process_id(hwnd: HWND) -> u32 {
    let mut process_id = 0;
    // SAFETY: 只写入局部变量；句柄无效时进程 ID 保持 0。
    unsafe { GetWindowThreadProcessId(hwnd, Some(&raw mut process_id)) };
    process_id
}

fn title(hwnd: HWND) -> String {
    // SAFETY: 只读窗口状态。
    let len = unsafe { GetWindowTextLengthW(hwnd) };
    let mut buffer = vec![0u16; usize::try_from(len).unwrap_or(0) + 1];
    // SAFETY: 缓冲的长度由切片给出，系统最多写入这么多个 UTF-16 单元（含结尾的 0）。
    let copied = unsafe { GetWindowTextW(hwnd, &mut buffer) };
    String::from_utf16_lossy(&buffer[..usize::try_from(copied).unwrap_or(0)])
}

fn class_name(hwnd: HWND) -> String {
    // 窗口类名最长 256 个字符。
    let mut buffer = [0u16; 257];
    // SAFETY: 缓冲的长度由切片给出。
    let copied = unsafe { GetClassNameW(hwnd, &mut buffer) };
    String::from_utf16_lossy(&buffer[..usize::try_from(copied).unwrap_or(0)])
}

/// 进程映像的完整路径；无权打开进程（如受保护进程）时为 `None`。
fn process_image(process_id: u32) -> Option<String> {
    // PROCESS_QUERY_LIMITED_INFORMATION 可以跨完整性级别打开，提权的游戏进程也能查到映像路径。
    // SAFETY: 只请求查询权限；得到的句柄在下面关闭。
    let process =
        unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, process_id) }.ok()?;
    let mut buffer = vec![0u16; 32_768];
    let mut len = u32::try_from(buffer.len()).unwrap_or(u32::MAX);
    // SAFETY: `len` 是缓冲的长度（UTF-16 单元），系统据此写入并把实际长度写回 `len`。
    let queried = unsafe {
        QueryFullProcessImageNameW(
            process,
            PROCESS_NAME_WIN32,
            PWSTR(buffer.as_mut_ptr()),
            &raw mut len,
        )
    };
    // SAFETY: 句柄由上面的 OpenProcess 取得，只关闭一次。
    let _ = unsafe { CloseHandle(process) };
    queried.ok()?;
    Some(String::from_utf16_lossy(
        &buffer[..usize::try_from(len).unwrap_or(0)],
    ))
}

#[cfg(test)]
mod tests {
    use super::{find_window, list_windows};
    use crate::{CaptureError, WindowSelector};

    #[test]
    fn missing_window_is_reported_not_guessed() {
        let selector = WindowSelector {
            process_name: Some("no-such-process-7f3a9c.exe".into()),
            ..WindowSelector::default()
        };
        assert_eq!(
            find_window(&selector),
            Err(CaptureError::WindowNotFound {
                selector: r#"process "no-such-process-7f3a9c.exe""#.into()
            })
        );
    }

    #[test]
    fn empty_selector_finds_nothing() {
        assert!(matches!(
            find_window(&WindowSelector::default()),
            Err(CaptureError::WindowNotFound { .. })
        ));
        // 枚举本身可用，与是否有交互桌面无关。
        list_windows().expect("enumerate windows");
    }
}
