//! 已知图案窗口：真机测试与探测程序用来核对采到的像素。只用于测试与探测，不是假采集源。
//!
//! 窗口在自己的线程上创建并处理消息。客户区画 1 像素宽的品红边框，内部按中线分成四块纯色；`variant`
//! 轮换四块的颜色。动画模式下每次 DWM 合成后重画一次，并在左上角用 8 个灰度方块写入重画时的 QPC 纳秒数，
//! 供探测程序计算从重画到采集的时延。

use super::qpc_now_ns;
use super::window::query_geometry;
use super::{byte_size, os};
use crate::{CaptureError, Frame, Point, Size};
use std::cell::RefCell;
use std::sync::mpsc;
use std::sync::{Arc, Mutex, MutexGuard, OnceLock, PoisonError};
use std::thread::JoinHandle;
use windows::Win32::Foundation::{COLORREF, HINSTANCE, HWND, LPARAM, LRESULT, RECT, WPARAM};
use windows::Win32::Graphics::Dwm::DwmFlush;
use windows::Win32::Graphics::Gdi::{
    BeginPaint, BitBlt, CreateCompatibleBitmap, CreateCompatibleDC, CreateSolidBrush, DeleteDC,
    DeleteObject, EndPaint, FillRect, HDC, InvalidateRect, PAINTSTRUCT, SRCCOPY, SelectObject,
};
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::UI::HiDpi::{DPI_AWARENESS_CONTEXT_UNAWARE, SetThreadDpiAwarenessContext};
use windows::Win32::UI::WindowsAndMessaging::{
    CS_HREDRAW, CS_VREDRAW, CreateWindowExW, DefWindowProcW, DispatchMessageW, GetClientRect,
    GetMessageW, GetWindowRect, HWND_TOPMOST, IDC_ARROW, LoadCursorW, MSG, PM_REMOVE, PeekMessageW,
    PostMessageW, PostQuitMessage, RegisterClassExW, SW_RESTORE, SW_SHOWMINNOACTIVE,
    SW_SHOWNOACTIVATE, SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER, SetWindowPos,
    ShowWindow, TranslateMessage, WINDOW_EX_STYLE, WINDOW_STYLE, WM_CLOSE, WM_DESTROY,
    WM_ERASEBKGND, WM_NULL, WM_PAINT, WM_QUIT, WNDCLASSEXW, WS_OVERLAPPEDWINDOW, WS_POPUP,
};
use windows::core::{HSTRING, PCWSTR, w};

const CLASS_NAME: PCWSTR = w!("BellisCapturePatternWindow");
/// 边框颜色（RGB）：品红。
const BORDER: [u8; 3] = [255, 0, 255];
/// 四块的颜色（RGB）：都不是灰色，所以不会被误读成时间戳。
const PALETTE: [[u8; 3]; 4] = [[255, 0, 0], [0, 255, 0], [0, 0, 255], [255, 255, 0]];
/// 时间戳方块的位置与大小（客户区像素）。
const STAMP_LEFT: u32 = 2;
const STAMP_TOP: u32 = 2;
const STAMP_CELL: u32 = 8;
const STAMP_CELLS: u32 = 8;

/// 窗口样式。缺省是带标题栏与边框、按显示器感知 DPI v2 的普通窗口。
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct PatternStyle {
    /// 无边框窗口：窗口即客户区。
    pub borderless: bool,
    /// 窗口不感知 DPI：系统按显示器缩放把它拉伸显示，客户坐标是逻辑像素。
    pub dpi_unaware: bool,
}

/// 一次核对的结果。
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct PatternCheck {
    /// 核对的像素数（不含时间戳区域）。
    pub checked: u64,
    /// 底部两角之外颜色不符的像素数。
    pub mismatches: u64,
    /// 底部两角之内颜色不符的像素数（窗口圆角所致）。
    pub corner_mismatches: u64,
    /// 底部两角之外第一个不符的像素：位置、采到的 BGRA 与期望的 RGB。
    pub first_mismatch: Option<(u32, u32, [u8; 4], [u8; 3])>,
    /// 采到的 alpha 的最小值与最大值。
    pub alpha_range: (u8, u8),
}

impl PatternCheck {
    /// 底部两角之外的像素都与图案一致。
    #[must_use]
    pub fn is_exact(&self) -> bool {
        self.checked > 0 && self.mismatches == 0
    }
}

/// 已知图案窗口。丢弃时关闭窗口并结束其线程。
pub struct PatternWindow {
    hwnd: HWND,
    shared: Arc<Mutex<Shared>>,
    thread: Option<JoinHandle<()>>,
}

#[derive(Clone, Copy, Debug, Default)]
struct Shared {
    variant: u32,
    animate: bool,
}

/// 把窗口句柄从窗口线程交给创建者。
struct SendHwnd(HWND);
// SAFETY: 窗口句柄是进程内通用的标识，不是只属于某个线程的指针；跨线程使用窗口由系统负责同步。
unsafe impl Send for SendHwnd {}

thread_local! {
    /// 窗口线程上的共享状态，供窗口过程读取。每个窗口有自己的线程。
    static SHARED: RefCell<Option<Arc<Mutex<Shared>>>> = const { RefCell::new(None) };
}

impl PatternWindow {
    /// 打开窗口，使客户区位于 `origin`（屏幕物理像素）、大小为 `size`，显示但不激活。
    ///
    /// # Errors
    ///
    /// 注册窗口类、创建线程或窗口失败时返回错误。
    pub fn open(
        title: &str,
        style: PatternStyle,
        origin: Point,
        size: Size,
    ) -> Result<Self, CaptureError> {
        register_class()?;
        let shared = Arc::new(Mutex::new(Shared::default()));
        let (ready, created) = mpsc::channel();
        let thread_shared = Arc::clone(&shared);
        let title = title.to_owned();
        let thread = std::thread::Builder::new()
            .name("pattern-window".into())
            .spawn(move || run(&title, style, &thread_shared, &ready))
            .map_err(|error| CaptureError::Os {
                context: "spawn pattern window thread",
                code: error.raw_os_error().unwrap_or(0),
                message: error.to_string(),
            })?;
        let hwnd = match created.recv() {
            Ok(Ok(SendHwnd(hwnd))) => hwnd,
            Ok(Err(error)) => {
                let _ = thread.join();
                return Err(error);
            }
            Err(_) => {
                let _ = thread.join();
                return Err(CaptureError::Os {
                    context: "pattern window thread",
                    code: 0,
                    message: "the thread exited before creating the window".into(),
                });
            }
        };
        let window = Self {
            hwnd,
            shared,
            thread: Some(thread),
        };
        // SAFETY: 句柄属于刚创建的窗口；显示但不激活，不抢前台。
        let _ = unsafe { ShowWindow(window.hwnd, SW_SHOWNOACTIVATE) };
        window.set_client_rect(origin, size)?;
        Ok(window)
    }

    /// 窗口句柄。
    #[must_use]
    pub fn hwnd(&self) -> HWND {
        self.hwnd
    }

    /// 移动并改变大小，使客户区位于 `origin`、大小为 `size`（屏幕物理像素）。
    ///
    /// # Errors
    ///
    /// 查询几何或移动窗口失败时返回错误。
    pub fn set_client_rect(&self, origin: Point, size: Size) -> Result<(), CaptureError> {
        let geometry = query_geometry(self.hwnd)?;
        let mut outer = RECT::default();
        // SAFETY: 只写入局部的 RECT。
        unsafe { GetWindowRect(self.hwnd, &raw mut outer) }.map_err(os("GetWindowRect"))?;
        let left = geometry.client.left - outer.left;
        let top = geometry.client.top - outer.top;
        let right = outer.right - geometry.client.right;
        let bottom = outer.bottom - geometry.client.bottom;
        let width = i32::try_from(size.width).unwrap_or(i32::MAX);
        let height = i32::try_from(size.height).unwrap_or(i32::MAX);
        // SAFETY: 句柄属于本对象的窗口；跨线程调用由系统发给窗口线程处理。
        unsafe {
            SetWindowPos(
                self.hwnd,
                None,
                origin.x - left,
                origin.y - top,
                width + left + right,
                height + top + bottom,
                SWP_NOZORDER | SWP_NOACTIVATE,
            )
        }
        .map_err(os("SetWindowPos"))
    }

    /// 轮换四块的颜色并重画。
    pub fn set_variant(&self, variant: u32) {
        lock(&self.shared).variant = variant;
        self.repaint();
    }

    /// 打开或关闭动画模式。
    pub fn set_animate(&self, animate: bool) {
        lock(&self.shared).animate = animate;
        // SAFETY: 句柄属于本对象的窗口；空消息只为唤醒窗口线程。
        let _ = unsafe { PostMessageW(Some(self.hwnd), WM_NULL, WPARAM(0), LPARAM(0)) };
    }

    /// 请求重画整个客户区。
    pub fn repaint(&self) {
        // SAFETY: 句柄属于本对象的窗口；只把客户区标记为需要重画。
        let _ = unsafe { InvalidateRect(Some(self.hwnd), None, false) };
    }

    /// 最小化，不激活别的窗口。
    pub fn minimize(&self) {
        // SAFETY: 句柄属于本对象的窗口。
        let _ = unsafe { ShowWindow(self.hwnd, SW_SHOWMINNOACTIVE) };
    }

    /// 从最小化还原。
    pub fn restore(&self) {
        // SAFETY: 句柄属于本对象的窗口。
        let _ = unsafe { ShowWindow(self.hwnd, SW_RESTORE) };
    }

    /// 置于所有普通窗口之上，不激活。
    ///
    /// # Errors
    ///
    /// 系统拒绝时返回错误。
    pub fn set_topmost(&self) -> Result<(), CaptureError> {
        // SAFETY: 句柄属于本对象的窗口；不移动、不改变大小、不激活。
        unsafe {
            SetWindowPos(
                self.hwnd,
                Some(HWND_TOPMOST),
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )
        }
        .map_err(os("SetWindowPos(HWND_TOPMOST)"))
    }

    /// 客户区大小为 `size`、颜色轮换为 `variant` 时，`(x, y)` 处应有的 RGB。
    #[must_use]
    pub fn expected_rgb(size: Size, variant: u32, x: u32, y: u32) -> [u8; 3] {
        let (width, height) = (size.width, size.height);
        if x == 0 || y == 0 || x + 1 >= width || y + 1 >= height {
            return BORDER;
        }
        let quadrant = usize::from(x >= width / 2) + 2 * usize::from(y >= height / 2);
        PALETTE[(quadrant + variant as usize) % PALETTE.len()]
    }

    /// 核对一帧是否就是颜色轮换为 `variant` 的图案；时间戳区域不核对。
    ///
    /// Windows 11 给有边框的窗口画圆角，客户区底部两角的像素被 DWM 裁成半透明。客户区左下与右下各
    /// `corner × corner` 的方块内不符的像素单独计入 [`PatternCheck::corner_mismatches`]；无边框窗口传 0。
    #[must_use]
    pub fn verify(frame: &Frame, variant: u32, corner: u32) -> PatternCheck {
        let size = Size {
            width: frame.meta.width,
            height: frame.meta.height,
        };
        let mut check = PatternCheck {
            alpha_range: (u8::MAX, u8::MIN),
            ..PatternCheck::default()
        };
        for y in 0..size.height {
            for x in 0..size.width {
                if in_stamp(x, y) {
                    continue;
                }
                let Some(pixel) = frame.pixel(x, y) else {
                    continue;
                };
                let [b, g, r, a] = pixel;
                check.checked += 1;
                check.alpha_range = (check.alpha_range.0.min(a), check.alpha_range.1.max(a));
                let expected = Self::expected_rgb(size, variant, x, y);
                if [r, g, b] == expected {
                    continue;
                }
                let in_corner =
                    y + corner >= size.height && (x < corner || x + corner >= size.width);
                if in_corner {
                    check.corner_mismatches += 1;
                } else {
                    check.mismatches += 1;
                    check.first_mismatch.get_or_insert((x, y, pixel, expected));
                }
            }
        }
        check
    }

    /// 动画模式下写入帧中的重画时刻（QPC 纳秒）；帧中没有时间戳时为 `None`。
    #[must_use]
    pub fn paint_time_ns(frame: &Frame) -> Option<u64> {
        let mut bytes = [0u8; STAMP_CELLS as usize];
        for (cell, byte) in (0u32..).zip(bytes.iter_mut()) {
            let x = STAMP_LEFT + cell * STAMP_CELL + STAMP_CELL / 2;
            let [b, g, r, _] = frame.pixel(x, STAMP_TOP + STAMP_CELL / 2)?;
            if b != g || g != r {
                return None;
            }
            *byte = b;
        }
        Some(u64::from_le_bytes(bytes))
    }
}

impl Drop for PatternWindow {
    fn drop(&mut self) {
        // SAFETY: 句柄属于本对象的窗口；窗口已销毁时调用失败，无害。
        let _ = unsafe { PostMessageW(Some(self.hwnd), WM_CLOSE, WPARAM(0), LPARAM(0)) };
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

fn in_stamp(x: u32, y: u32) -> bool {
    (STAMP_LEFT..STAMP_LEFT + STAMP_CELL * STAMP_CELLS).contains(&x)
        && (STAMP_TOP..STAMP_TOP + STAMP_CELL).contains(&y)
}

fn lock(shared: &Mutex<Shared>) -> MutexGuard<'_, Shared> {
    shared.lock().unwrap_or_else(PoisonError::into_inner)
}

fn register_class() -> Result<(), CaptureError> {
    static REGISTERED: OnceLock<Result<(), CaptureError>> = OnceLock::new();
    REGISTERED
        .get_or_init(|| {
            // SAFETY: 取本模块的句柄，不增加引用计数。
            let instance = unsafe { GetModuleHandleW(None) }.map_err(os("GetModuleHandleW"))?;
            // SAFETY: 加载系统预定义的箭头光标。
            let cursor = unsafe { LoadCursorW(None, IDC_ARROW) }.map_err(os("LoadCursorW"))?;
            let class = WNDCLASSEXW {
                cbSize: byte_size::<WNDCLASSEXW>(),
                style: CS_HREDRAW | CS_VREDRAW,
                lpfnWndProc: Some(window_proc),
                hInstance: HINSTANCE::from(instance),
                hCursor: cursor,
                lpszClassName: CLASS_NAME,
                ..WNDCLASSEXW::default()
            };
            // SAFETY: 描述结构在调用期间有效；类名是静态字符串。
            if unsafe { RegisterClassExW(&raw const class) } == 0 {
                return Err(os("RegisterClassExW")(windows::core::Error::from_thread()));
            }
            Ok(())
        })
        .clone()
}

/// 窗口线程：创建窗口，把句柄交给创建者，然后处理消息直到窗口销毁。
fn run(
    title: &str,
    style: PatternStyle,
    shared: &Arc<Mutex<Shared>>,
    ready: &mpsc::Sender<Result<SendHwnd, CaptureError>>,
) {
    SHARED.with(|cell| *cell.borrow_mut() = Some(Arc::clone(shared)));
    let window_style: WINDOW_STYLE = if style.borderless {
        WS_POPUP
    } else {
        WS_OVERLAPPEDWINDOW
    };
    if style.dpi_unaware {
        // 窗口的 DPI 感知取决于创建它的线程。只改本线程，不影响进程的声明。
        // SAFETY: 参数是系统定义的伪句柄常量。
        unsafe { SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_UNAWARE) };
    }
    // SAFETY: 取本模块的句柄，不增加引用计数。
    let created = unsafe { GetModuleHandleW(None) }
        .map_err(os("GetModuleHandleW"))
        .and_then(|instance| {
            // SAFETY: 类已注册；标题在调用期间有效。窗口先不显示，由创建者决定位置后再显示。
            unsafe {
                CreateWindowExW(
                    WINDOW_EX_STYLE::default(),
                    CLASS_NAME,
                    &HSTRING::from(title),
                    window_style,
                    0,
                    0,
                    320,
                    240,
                    None,
                    None,
                    Some(HINSTANCE::from(instance)),
                    None,
                )
            }
            .map_err(os("CreateWindowExW"))
        });
    let hwnd = match created {
        Ok(hwnd) => hwnd,
        Err(error) => {
            let _ = ready.send(Err(error));
            return;
        }
    };
    if ready.send(Ok(SendHwnd(hwnd))).is_err() {
        return;
    }

    let mut message = MSG::default();
    loop {
        if lock(shared).animate {
            // SAFETY: 只读写局部的 MSG。
            while unsafe { PeekMessageW(&raw mut message, None, 0, 0, PM_REMOVE) }.as_bool() {
                if message.message == WM_QUIT {
                    return;
                }
                dispatch(&message);
            }
            // SAFETY: 句柄属于本线程创建的窗口；重画后等到下一次 DWM 合成。
            unsafe {
                let _ = InvalidateRect(Some(hwnd), None, false);
                let _ = windows::Win32::Graphics::Gdi::UpdateWindow(hwnd);
                let _ = DwmFlush();
            }
        } else {
            // SAFETY: 只读写局部的 MSG；返回 0 表示 WM_QUIT，-1 表示出错。
            if unsafe { GetMessageW(&raw mut message, None, 0, 0) }.0 <= 0 {
                return;
            }
            dispatch(&message);
        }
    }
}

fn dispatch(message: &MSG) {
    // SAFETY: 消息由本线程刚取得。
    unsafe {
        let _ = TranslateMessage(message);
        DispatchMessageW(message);
    }
}

extern "system" fn window_proc(
    hwnd: HWND,
    message: u32,
    wparam: WPARAM,
    lparam: LPARAM,
) -> LRESULT {
    match message {
        WM_PAINT => {
            paint(hwnd);
            LRESULT(0)
        }
        // 每次都画满整个客户区，不需要擦背景。
        WM_ERASEBKGND => LRESULT(1),
        WM_DESTROY => {
            // SAFETY: 在窗口线程上结束它的消息循环。
            unsafe { PostQuitMessage(0) };
            LRESULT(0)
        }
        // SAFETY: 原样交给系统的缺省处理。
        _ => unsafe { DefWindowProcW(hwnd, message, wparam, lparam) },
    }
}

/// 先画到内存位图，再一次性复制到窗口，免得 DWM 合成到画了一半的内容。
fn paint(hwnd: HWND) {
    let state = SHARED
        .with(|cell| cell.borrow().as_ref().map(|shared| *lock(shared)))
        .unwrap_or_default();
    let mut paint = PAINTSTRUCT::default();
    // SAFETY: 在本窗口的 WM_PAINT 中调用，与下面的 EndPaint 配对。
    let hdc = unsafe { BeginPaint(hwnd, &raw mut paint) };
    let mut client = RECT::default();
    // SAFETY: 只写入局部的 RECT。
    let _ = unsafe { GetClientRect(hwnd, &raw mut client) };
    let (width, height) = (client.right, client.bottom);
    if width > 0 && height > 0 {
        // SAFETY: 内存设备上下文与位图在这里创建，用完后选回原对象再删除。
        unsafe {
            let memory = CreateCompatibleDC(Some(hdc));
            let bitmap = CreateCompatibleBitmap(hdc, width, height);
            let previous = SelectObject(memory, bitmap.into());
            for (rect, rgb) in pattern_rects(width, height, state.variant) {
                fill(memory, &rect, rgb);
            }
            if state.animate {
                stamp(memory, qpc_now_ns());
            }
            let _ = BitBlt(hdc, 0, 0, width, height, Some(memory), 0, 0, SRCCOPY);
            SelectObject(memory, previous);
            let _ = DeleteObject(bitmap.into());
            let _ = DeleteDC(memory);
        }
    }
    // SAFETY: 与上面的 BeginPaint 配对。
    let _ = unsafe { EndPaint(hwnd, &raw const paint) };
}

/// 图案的 8 个矩形：4 条边框与 4 块，互不重叠，合起来正好铺满客户区。
fn pattern_rects(width: i32, height: i32, variant: u32) -> [(RECT, [u8; 3]); 8] {
    let rect = |left, top, right, bottom| RECT {
        left,
        top,
        right,
        bottom,
    };
    let (mid_x, mid_y) = (width / 2, height / 2);
    let color = |quadrant: usize| PALETTE[(quadrant + variant as usize) % PALETTE.len()];
    [
        (rect(0, 0, width, 1), BORDER),
        (rect(0, height - 1, width, height), BORDER),
        (rect(0, 1, 1, height - 1), BORDER),
        (rect(width - 1, 1, width, height - 1), BORDER),
        (rect(1, 1, mid_x, mid_y), color(0)),
        (rect(mid_x, 1, width - 1, mid_y), color(1)),
        (rect(1, mid_y, mid_x, height - 1), color(2)),
        (rect(mid_x, mid_y, width - 1, height - 1), color(3)),
    ]
}

/// 用 8 个灰度方块写入 `time_ns` 的小端字节。
fn stamp(hdc: HDC, time_ns: u64) {
    for (cell, byte) in (0i32..).zip(time_ns.to_le_bytes()) {
        let cell_size = STAMP_CELL.cast_signed();
        let left = STAMP_LEFT.cast_signed() + cell * cell_size;
        let top = STAMP_TOP.cast_signed();
        let rect = RECT {
            left,
            top,
            right: left + cell_size,
            bottom: top + cell_size,
        };
        fill(hdc, &rect, [byte, byte, byte]);
    }
}

fn fill(hdc: HDC, rect: &RECT, [r, g, b]: [u8; 3]) {
    let color = COLORREF(u32::from(r) | (u32::from(g) << 8) | (u32::from(b) << 16));
    // SAFETY: 画刷在这里创建，用完即删除；矩形在调用期间有效。
    unsafe {
        let brush = CreateSolidBrush(color);
        FillRect(hdc, rect, brush);
        let _ = DeleteObject(brush.into());
    }
}

#[cfg(test)]
mod tests {
    use super::{PatternWindow, pattern_rects};
    use crate::Size;

    #[test]
    fn rects_tile_the_client_area_exactly_as_expected() {
        for (width, height) in [(9, 7), (640, 480), (3, 3)] {
            for variant in 0..4 {
                let size = Size {
                    width: u32::try_from(width).unwrap(),
                    height: u32::try_from(height).unwrap(),
                };
                for y in 0..height {
                    for x in 0..width {
                        let covering: Vec<[u8; 3]> = pattern_rects(width, height, variant)
                            .iter()
                            .filter(|(r, _)| {
                                x >= r.left && x < r.right && y >= r.top && y < r.bottom
                            })
                            .map(|(_, rgb)| *rgb)
                            .collect();
                        let (xu, yu) = (u32::try_from(x).unwrap(), u32::try_from(y).unwrap());
                        assert_eq!(
                            covering,
                            vec![PatternWindow::expected_rgb(size, variant, xu, yu)],
                            "{width}x{height} variant {variant} at ({x}, {y})"
                        );
                    }
                }
            }
        }
    }
}
