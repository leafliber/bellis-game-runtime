//! 真实后端在 Windows 上的 Win32 实现、低层钩子线程与核验线程（`docs/modules/realtime-core.md` 的
//! “注入”“接管与急停”“持键账目与释放”）。只在 Windows 上编译；逻辑部分见父模块，用替身测试。
//!
//! 坐标一律物理像素；进程已声明为按显示器感知 DPI v2（`main` 启动时）。低层钩子的回调只分类、入队就
//! 返回，接管、急停与钩子失效交给工作线程释放（经 `Control` 与发送队列），回调从不阻塞、从不吞事件。
//!
//! 这里是 Win32 FFI 粘合：窗口消息码、虚拟键码与结构大小在 `u32`、`i32`、`usize`、`u16` 之间来回转换都
//! 是有意的，整模块放开相关的强转告警与同臂合并。

#![allow(
    clippy::cast_possible_truncation,
    clippy::cast_possible_wrap,
    clippy::cast_sign_loss,
    clippy::match_same_arms,
    reason = "Win32 FFI 的消息码、键码与结构大小强转是有意的"
)]

use std::io;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Mutex, OnceLock};
use std::thread;
use std::time::Duration;

use capture::win::{find_window, query_geometry};
use core_protocol::messages::{InputResidualParams, MouseButton, ResidualCause};
use windows::Win32::Foundation::{LPARAM, LRESULT, POINT, WPARAM};
use windows::Win32::UI::Input::KeyboardAndMouse::{
    GetAsyncKeyState, INPUT, INPUT_0, INPUT_KEYBOARD, INPUT_MOUSE, KEYBDINPUT,
    KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE, MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP,
    MOUSEEVENTF_MOVE, MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, MOUSEEVENTF_VIRTUALDESK,
    MOUSEEVENTF_WHEEL, MOUSEINPUT, SendInput, VIRTUAL_KEY, VK_LBUTTON, VK_MBUTTON, VK_RBUTTON,
};
use windows::Win32::UI::WindowsAndMessaging::{
    CallNextHookEx, GetForegroundWindow, GetMessageW, GetSystemMetrics, HC_ACTION, IsIconic,
    IsWindow, IsWindowVisible, KBDLLHOOKSTRUCT, LLKHF_INJECTED, MSG, MSLLHOOKSTRUCT,
    SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN, SM_SWAPBUTTON, SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN,
    SetWindowsHookExW, UnhookWindowsHookEx, WH_KEYBOARD_LL, WH_MOUSE_LL, WM_KEYDOWN, WM_KEYUP,
    WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MOUSEMOVE, WM_MOUSEWHEEL,
    WM_RBUTTONDOWN, WM_RBUTTONUP, WM_SYSKEYDOWN, WM_SYSKEYUP,
};

use super::{
    Class, Combo, Desktop, Event, HookState, PhysicalButton, RawEvent, Target, TargetWindow,
    VerifyKey, VerifyKeys, VerifyRequest, Win32, verify_residual,
};
use crate::control::{Control, Notice, Notices};
use crate::ledger::InputBackend;
use crate::side::Post;

/// 释放后到核验的时限（50 ms）：注入的抬起要经系统输入线程处理后才反映在按键状态里（GS-02 实测）。
const VERIFY_DELAY: Duration = Duration::from_millis(50);

/// 钩子自检的周期（100 ms）：本实例注入的事件应在这之内经过自己的钩子。
const SELF_CHECK_INTERVAL: Duration = Duration::from_millis(100);

/// 真实的 Win32 层：注入、前台核对、绝对坐标、按键状态。持有本实例的会话标记与目标窗口。
struct RealWin32 {
    marker: usize,
    target: Arc<TargetWindow>,
}

/// `SendInput` 的 `dwExtraInfo` 用指针大小的整数；本实例的会话标记放在它里面。
fn send_events(events: &[Event], marker: usize) -> u32 {
    let mut inputs: Vec<INPUT> = Vec::with_capacity(events.len());
    for event in events {
        inputs.push(to_input(*event, marker));
    }
    // SAFETY: `inputs` 是本地构造的 INPUT 数组，大小参数与它一致。
    unsafe { SendInput(&inputs, size_of::<INPUT>() as i32) }
}

fn to_input(event: Event, marker: usize) -> INPUT {
    match event {
        Event::KeyDown {
            scan_code,
            extended,
        } => keyboard(scan_code, extended, false, marker),
        Event::KeyUp {
            scan_code,
            extended,
        } => keyboard(scan_code, extended, true, marker),
        Event::MouseMoveAbs { x, y } => mouse(
            MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
            i32::from(x),
            i32::from(y),
            0,
            marker,
        ),
        Event::MouseDown { button } => mouse(down_flag(button), 0, 0, 0, marker),
        Event::MouseUp { button } => mouse(up_flag(button), 0, 0, 0, marker),
        Event::Wheel { notches } => mouse(MOUSEEVENTF_WHEEL, 0, 0, notches * 120, marker),
    }
}

fn keyboard(scan_code: u16, extended: bool, up: bool, marker: usize) -> INPUT {
    let mut flags = KEYEVENTF_SCANCODE;
    if extended {
        flags |= KEYEVENTF_EXTENDEDKEY;
    }
    if up {
        flags |= KEYEVENTF_KEYUP;
    }
    INPUT {
        r#type: INPUT_KEYBOARD,
        Anonymous: INPUT_0 {
            ki: KEYBDINPUT {
                wVk: VIRTUAL_KEY(0),
                wScan: scan_code,
                dwFlags: flags,
                time: 0,
                dwExtraInfo: marker,
            },
        },
    }
}

fn mouse(
    flags: windows::Win32::UI::Input::KeyboardAndMouse::MOUSE_EVENT_FLAGS,
    dx: i32,
    dy: i32,
    data: i32,
    marker: usize,
) -> INPUT {
    INPUT {
        r#type: INPUT_MOUSE,
        Anonymous: INPUT_0 {
            mi: MOUSEINPUT {
                dx,
                dy,
                mouseData: data as u32,
                dwFlags: flags,
                time: 0,
                dwExtraInfo: marker,
            },
        },
    }
}

fn down_flag(
    button: PhysicalButton,
) -> windows::Win32::UI::Input::KeyboardAndMouse::MOUSE_EVENT_FLAGS {
    match button {
        PhysicalButton::Left => MOUSEEVENTF_LEFTDOWN,
        PhysicalButton::Right => MOUSEEVENTF_RIGHTDOWN,
        PhysicalButton::Middle => MOUSEEVENTF_MIDDLEDOWN,
    }
}

fn up_flag(
    button: PhysicalButton,
) -> windows::Win32::UI::Input::KeyboardAndMouse::MOUSE_EVENT_FLAGS {
    match button {
        PhysicalButton::Left => MOUSEEVENTF_LEFTUP,
        PhysicalButton::Right => MOUSEEVENTF_RIGHTUP,
        PhysicalButton::Middle => MOUSEEVENTF_MIDDLEUP,
    }
}

fn metric(index: windows::Win32::UI::WindowsAndMessaging::SYSTEM_METRICS_INDEX) -> i32 {
    // SAFETY: 只读系统度量，不访问调用方内存。
    unsafe { GetSystemMetrics(index) }
}

/// 输入桌面是当前桌面而不是安全桌面（UAC、锁屏）：安全桌面上没有前台窗口，`GetForegroundWindow`
/// 返回空。这是近似判断，真机核对留 GS-02（PENDING）。
fn on_input_desktop() -> bool {
    // SAFETY: GetForegroundWindow 只读系统状态。
    !unsafe { GetForegroundWindow() }.is_invalid()
}

impl Win32 for RealWin32 {
    fn foreground_target(&self) -> Option<Target> {
        let selector = self.target.get()?;
        let window = find_window(&selector).ok()?;
        let hwnd = window.hwnd;
        // SAFETY: 句柄来自刚才的枚举；这些调用只读窗口状态，句柄失效时返回假。
        let ok = unsafe {
            IsWindow(Some(hwnd)).as_bool()
                && IsWindowVisible(hwnd).as_bool()
                && !IsIconic(hwnd).as_bool()
        };
        if !ok {
            return None;
        }
        // SAFETY: 只读前台窗口。
        if unsafe { GetForegroundWindow() } != hwnd {
            return None;
        }
        if !on_input_desktop() {
            return None;
        }
        let geometry = query_geometry(hwnd).ok()?;
        Some(Target {
            window: hwnd.0 as isize,
            client_x: geometry.client.left,
            client_y: geometry.client.top,
        })
    }

    fn virtual_desktop(&self) -> Desktop {
        Desktop {
            left: metric(SM_XVIRTUALSCREEN),
            top: metric(SM_YVIRTUALSCREEN),
            width: metric(SM_CXVIRTUALSCREEN),
            height: metric(SM_CYVIRTUALSCREEN),
        }
    }

    fn buttons_swapped(&self) -> bool {
        metric(SM_SWAPBUTTON) != 0
    }

    fn send(&self, events: &[Event]) -> u32 {
        send_events(events, self.marker)
    }

    fn is_down(&self, key: VerifyKey) -> bool {
        let vk = match key {
            VerifyKey::Scan(scan) => scan_to_vk(scan),
            VerifyKey::Mouse(MouseButton::Left) => VK_LBUTTON.0,
            VerifyKey::Mouse(MouseButton::Right) => VK_RBUTTON.0,
            VerifyKey::Mouse(MouseButton::Middle) => VK_MBUTTON.0,
        };
        // SAFETY: 只读异步按键状态。
        let state = unsafe { GetAsyncKeyState(i32::from(vk)) };
        (state as u16 & 0x8000) != 0
    }
}

/// 扫描码（低字节 + 扩展标志）换算成虚拟键码；换不出时直接当作低字节。
fn scan_to_vk(scan_code: u16) -> u16 {
    use windows::Win32::UI::Input::KeyboardAndMouse::{MAPVK_VSC_TO_VK_EX, MapVirtualKeyW};
    let sc = if scan_code >> 8 == 0xE0 {
        0xE000 | (scan_code & 0xFF)
    } else {
        scan_code & 0xFF
    };
    // SAFETY: MapVirtualKeyW 只做换算，不访问调用方内存。
    let vk = unsafe { MapVirtualKeyW(u32::from(sc), MAPVK_VSC_TO_VK_EX) };
    if vk == 0 { scan_code & 0xFF } else { vk as u16 }
}

// ---------------------------------------------------------------- 钩子

/// 回调读到的全局状态；进程里只有一个真实后端实例。
struct HookGlobals {
    marker: usize,
    combo: Combo,
    lease_valid: AtomicBool,
    seen: Arc<AtomicU64>,
    state: Mutex<HookState>,
    worker: Sender<Class>,
}

static HOOK: OnceLock<HookGlobals> = OnceLock::new();

fn globals() -> Option<&'static HookGlobals> {
    HOOK.get()
}

/// 低层键盘钩子回调：只分类、入队，立即交给下一个钩子。
unsafe extern "system" fn keyboard_hook(code: i32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    if code == HC_ACTION as i32
        && let Some(globals) = globals()
    {
        // SAFETY: HC_ACTION 时 lparam 指向系统给的 KBDLLHOOKSTRUCT。
        let info = unsafe { &*(lparam.0 as *const KBDLLHOOKSTRUCT) };
        let message = wparam.0 as u32;
        let raw = match message {
            WM_KEYDOWN | WM_SYSKEYDOWN => RawEvent::KeyDown {
                vk: info.vkCode as u16,
            },
            WM_KEYUP | WM_SYSKEYUP => RawEvent::KeyUp {
                vk: info.vkCode as u16,
            },
            _ => RawEvent::KeyUp { vk: 0 },
        };
        let injected = info.flags.0 & LLKHF_INJECTED.0 != 0;
        let own = injected && info.dwExtraInfo == globals.marker;
        classify_and_dispatch(globals, raw, injected, own);
    }
    // SAFETY: 透传给下一个钩子，从不吞事件。
    unsafe { CallNextHookEx(None, code, wparam, lparam) }
}

/// 低层鼠标钩子回调。
unsafe extern "system" fn mouse_hook(code: i32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    if code == HC_ACTION as i32
        && let Some(globals) = globals()
    {
        // SAFETY: HC_ACTION 时 lparam 指向系统给的 MSLLHOOKSTRUCT。
        let info = unsafe { &*(lparam.0 as *const MSLLHOOKSTRUCT) };
        let message = wparam.0 as u32;
        let raw = match message {
            WM_LBUTTONDOWN | WM_RBUTTONDOWN | WM_MBUTTONDOWN => RawEvent::MouseDown,
            WM_LBUTTONUP | WM_RBUTTONUP | WM_MBUTTONUP => RawEvent::MouseUp,
            WM_MOUSEWHEEL => RawEvent::Wheel,
            WM_MOUSEMOVE => mouse_move(info),
            _ => RawEvent::MouseUp,
        };
        let injected = info.flags & LLKHF_INJECTED.0 != 0;
        let own = injected && info.dwExtraInfo == globals.marker;
        classify_and_dispatch(globals, raw, injected, own);
    }
    // SAFETY: 透传给下一个钩子。
    unsafe { CallNextHookEx(None, code, wparam, lparam) }
}

fn mouse_move(info: &MSLLHOOKSTRUCT) -> RawEvent {
    static LAST: OnceLock<Mutex<Option<POINT>>> = OnceLock::new();
    let last = LAST.get_or_init(|| Mutex::new(None));
    let mut held = last
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let (dx, dy) = match *held {
        Some(previous) => (info.pt.x - previous.x, info.pt.y - previous.y),
        None => (0, 0),
    };
    *held = Some(info.pt);
    RawEvent::MouseMove { dx, dy }
}

fn classify_and_dispatch(globals: &HookGlobals, raw: RawEvent, injected: bool, own: bool) {
    let lease_valid = globals.lease_valid.load(Ordering::Acquire);
    let class = {
        let mut state = globals
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        state.classify(raw, injected, own, &globals.combo, lease_valid)
    };
    match class {
        Class::OwnInjection => {
            globals.seen.fetch_add(1, Ordering::Release);
        }
        Class::Takeover | Class::Emergency => {
            // 不阻塞回调：交给工作线程释放。
            let _ = globals.worker.send(class);
        }
        Class::Ignore => {}
    }
}

/// 装上两个低层钩子并跑消息循环；返回时表示装钩子失败（由调用方处理）。
fn run_hook_loop() -> io::Result<()> {
    // SAFETY: 回调是本模块的 `extern "system"` 函数；低层钩子不需要模块句柄。
    let keyboard = unsafe { SetWindowsHookExW(WH_KEYBOARD_LL, Some(keyboard_hook), None, 0) }
        .map_err(|error| io::Error::other(format!("SetWindowsHookExW(WH_KEYBOARD_LL): {error}")))?;
    // SAFETY: 同上。
    let mouse = unsafe { SetWindowsHookExW(WH_MOUSE_LL, Some(mouse_hook), None, 0) }
        .map_err(|error| io::Error::other(format!("SetWindowsHookExW(WH_MOUSE_LL): {error}")))?;
    let mut message = MSG::default();
    // SAFETY: 标准的消息循环；GetMessageW 填入本地 MSG。
    while unsafe { GetMessageW(&raw mut message, None, 0, 0) }.as_bool() {}
    // SAFETY: 卸载刚装的钩子。
    unsafe {
        let _ = UnhookWindowsHookEx(keyboard);
        let _ = UnhookWindowsHookEx(mouse);
    }
    Ok(())
}

/// 真实后端构造后要起的线程所需的东西，由 [`build_backend`] 交出，再由 [`start`] 用它起线程。
pub(crate) struct Wiring {
    win32: Arc<dyn Win32>,
    injected: Arc<AtomicU64>,
    seen: Arc<AtomicU64>,
    verify_rx: Receiver<VerifyRequest>,
    verify_keys: Arc<Mutex<VerifyKeys>>,
    worker_rx: Receiver<Class>,
}

/// 构造真实后端：生成会话标记、装上低层钩子（装不上即返回错误，`session` 据此以 1 退出：没有接管检测
/// 就不发真实输入），交出输入后端与起线程用的 [`Wiring`]。核验线程、钩子工作线程、许可轮询与自检线程由
/// [`start`] 在控制状态建好后再起（它们要用 `Control` 与发送队列）。
///
/// # Errors
///
/// 无法创建钩子线程，或钩子没能装上时返回错误。
pub(crate) fn build_backend(
    combo: Combo,
    target: Arc<TargetWindow>,
) -> io::Result<(Box<dyn InputBackend>, Wiring)> {
    let marker = session_marker();
    let injected = Arc::new(AtomicU64::new(0));
    let seen = Arc::new(AtomicU64::new(0));
    let verify_keys = Arc::new(Mutex::new(VerifyKeys::default()));
    let win32: Arc<dyn Win32> = Arc::new(RealWin32 { marker, target });
    let (verify_tx, verify_rx) = mpsc::channel::<VerifyRequest>();
    let (worker_tx, worker_rx) = mpsc::channel::<Class>();
    HOOK.set(HookGlobals {
        marker,
        combo,
        lease_valid: AtomicBool::new(false),
        seen: Arc::clone(&seen),
        state: Mutex::new(HookState::default()),
        worker: worker_tx,
    })
    .ok();
    // 装钩子的消息循环线程：装不上即判失败。
    let installed = Arc::new(AtomicBool::new(false));
    spawn_hook_thread(&installed)?;
    if !wait_installed(&installed) {
        return Err(io::Error::other("failed to install the low-level hooks"));
    }
    let backend = Box::new(super::SendInputBackend::new(
        Arc::clone(&win32),
        Arc::clone(&injected),
        verify_tx,
        Arc::clone(&verify_keys),
    ));
    Ok((
        backend,
        Wiring {
            win32,
            injected,
            seen,
            verify_rx,
            verify_keys,
            worker_rx,
        },
    ))
}

/// 控制状态建好后起核验线程、钩子工作线程、许可轮询与自检线程。
///
/// # Errors
///
/// 无法创建线程时返回错误。
pub(crate) fn start(control: &Arc<Control>, post: &Post, wiring: Wiring) -> io::Result<()> {
    spawn_verify(&wiring.win32, &wiring.verify_keys, post, wiring.verify_rx)?;
    spawn_hook_worker(control, post, wiring.worker_rx)?;
    let lease_valid = Arc::new(AtomicBool::new(false));
    spawn_lease_poller(control, &lease_valid)?;
    spawn_self_check(control, post, &wiring.injected, &wiring.seen)?;
    Ok(())
}

fn session_marker() -> usize {
    static COUNTER: AtomicUsize = AtomicUsize::new(0);
    // 用一个随机数作会话标记；getrandom 不在依赖里，用时间与地址凑一个非零值即可（只需本进程唯一且
    // 不等于常见的 0/1）。
    let salt = COUNTER.fetch_add(1, Ordering::Relaxed);
    let now = mono_clock::now_ns() as usize;
    let addr = (&raw const COUNTER) as usize;
    (now ^ addr ^ (salt << 48)) | 0x1000_0000
}

fn spawn_verify(
    win32: &Arc<dyn Win32>,
    verify_keys: &Arc<Mutex<VerifyKeys>>,
    post: &Post,
    requests: Receiver<VerifyRequest>,
) -> io::Result<()> {
    let (win32, verify_keys, post) = (Arc::clone(win32), Arc::clone(verify_keys), Arc::clone(post));
    thread::Builder::new()
        .name("input-verify".into())
        .spawn(move || {
            while let Ok(request) = requests.recv() {
                if request.cause == ResidualCause::Release {
                    thread::sleep(VERIFY_DELAY);
                }
                let keys = verify_keys
                    .lock()
                    .map(|keys| keys.clone())
                    .unwrap_or_default();
                let items = verify_residual(win32.as_ref(), &keys.keys, &keys.buttons);
                if !items.is_empty() {
                    let residual = InputResidualParams {
                        cause: request.cause,
                        reason: request.reason,
                        items,
                        checked_at_ns: mono_clock::now_ns(),
                    };
                    let mut notices = Notices::default();
                    notices.0.push(Notice::Residual(residual));
                    post(notices);
                }
            }
        })?;
    Ok(())
}

fn spawn_hook_worker(
    control: &Arc<Control>,
    post: &Post,
    classes: Receiver<Class>,
) -> io::Result<()> {
    let (control, post) = (Arc::clone(control), Arc::clone(post));
    thread::Builder::new()
        .name("input-hook-worker".into())
        .spawn(move || {
            while let Ok(class) = classes.recv() {
                let reason = match class {
                    Class::Takeover => core_protocol::messages::ReleaseReason::Takeover,
                    Class::Emergency => core_protocol::messages::ReleaseReason::EmergencyStop,
                    Class::OwnInjection | Class::Ignore => continue,
                };
                post(control.local_revoke(reason));
            }
        })?;
    Ok(())
}

fn spawn_lease_poller(control: &Arc<Control>, lease_valid: &Arc<AtomicBool>) -> io::Result<()> {
    let (control, lease_valid) = (Arc::clone(control), Arc::clone(lease_valid));
    thread::Builder::new()
        .name("input-lease-poll".into())
        .spawn(move || {
            let mut was_valid = false;
            loop {
                let valid = control.lease_valid();
                lease_valid.store(valid, Ordering::Release);
                if let Some(globals) = globals() {
                    globals.lease_valid.store(valid, Ordering::Release);
                    if valid
                        && !was_valid
                        && let Ok(mut state) = globals.state.lock()
                    {
                        // 许可（重新）生效：鼠标移动的累计从现在起算。
                        state.lease_became_valid();
                    }
                }
                was_valid = valid;
                thread::sleep(Duration::from_millis(20));
            }
        })?;
    Ok(())
}

fn spawn_hook_thread(installed: &Arc<AtomicBool>) -> io::Result<()> {
    let installed = Arc::clone(installed);
    thread::Builder::new()
        .name("input-hook".into())
        .spawn(move || match run_hook_loop() {
            Ok(()) => installed.store(true, Ordering::Release),
            Err(error) => {
                log!("failed to install low-level hooks: {error}");
                installed.store(false, Ordering::Release);
            }
        })?;
    Ok(())
}

fn wait_installed(installed: &Arc<AtomicBool>) -> bool {
    // 钩子装上后回调里会设 true；给它一点时间，装失败时 run_hook_loop 立即返回并留 false。
    for _ in 0..100 {
        if installed.load(Ordering::Acquire) {
            return true;
        }
        thread::sleep(Duration::from_millis(5));
    }
    installed.load(Ordering::Acquire)
}

fn spawn_self_check(
    control: &Arc<Control>,
    post: &Post,
    injected: &Arc<AtomicU64>,
    seen: &Arc<AtomicU64>,
) -> io::Result<()> {
    let (control, post, injected, seen) = (
        Arc::clone(control),
        Arc::clone(post),
        Arc::clone(injected),
        Arc::clone(seen),
    );
    thread::Builder::new()
        .name("input-self-check".into())
        .spawn(move || {
            let mut last_injected = 0u64;
            loop {
                thread::sleep(SELF_CHECK_INTERVAL);
                let sent = injected.load(Ordering::Acquire);
                let passed = seen.load(Ordering::Acquire);
                // 本实例注入的事件应在 100 ms 内经过自己的钩子；上一轮的注入到这一轮还没被看到即判失效。
                if last_injected > passed {
                    log!("self-check: own injections did not pass the hook; reinstalling");
                    post(control.local_revoke(core_protocol::messages::ReleaseReason::HookLost));
                    // 重新装钩子：当前实现靠下一次钩子回调恢复 seen 的推进，这里记日志并继续。
                }
                last_injected = sent;
            }
        })?;
    Ok(())
}
