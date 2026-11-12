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
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
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
    IsWindow, IsWindowVisible, KBDLLHOOKSTRUCT, LLKHF_INJECTED, LLMHF_INJECTED, MSG,
    MSLLHOOKSTRUCT, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN, SM_SWAPBUTTON, SM_XVIRTUALSCREEN,
    SM_YVIRTUALSCREEN, SetWindowsHookExW, UnhookWindowsHookEx, WH_KEYBOARD_LL, WH_MOUSE_LL,
    WM_KEYDOWN, WM_KEYUP, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MBUTTONDOWN, WM_MBUTTONUP,
    WM_MOUSEHWHEEL, WM_MOUSEMOVE, WM_MOUSEWHEEL, WM_RBUTTONDOWN, WM_RBUTTONUP, WM_SYSKEYDOWN,
    WM_SYSKEYUP, WM_XBUTTONDOWN, WM_XBUTTONUP,
};

use super::{
    Class, Combo, Desktop, Event, HookState, PhysicalButton, RawEvent, SelfCheck, Target,
    TargetWindow, VerifyKey, VerifyKeys, VerifyRequest, Win32, self_check_count, verify_residual,
};
use crate::control::{Control, Notice, Notices};
use crate::ledger::InputBackend;
use crate::side::Post;

/// 释放后到核验的时限（50 ms）：注入的抬起要经系统输入线程处理后才反映在按键状态里（GS-02 实测）。
const VERIFY_DELAY: Duration = Duration::from_millis(50);

/// 钩子自检的周期（100 ms）：本实例注入的事件应在这之内经过自己的钩子。
const SELF_CHECK_INTERVAL: Duration = Duration::from_millis(100);

/// 等两个低层钩子装上的时限：钩子线程装好（或装失败）即回报，超时按装不上处理。
const HOOK_INSTALL_TIMEOUT: Duration = Duration::from_secs(2);

/// 真实的 Win32 层：注入、前台核对、绝对坐标、按键状态。持有本实例的会话标记与目标窗口，并记下钩子
/// 自检要看到的注入事件数（所有注入都经 [`Win32::send`]，含核验线程补发的抬起）。
struct RealWin32 {
    marker: usize,
    target: Arc<TargetWindow>,
    injected: Arc<AtomicU64>,
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
        let sent = send_events(events, self.marker);
        self.injected
            .fetch_add(self_check_count(events, sent), Ordering::Release);
        sent
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
            _ => RawEvent::Other,
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
            WM_LBUTTONDOWN | WM_RBUTTONDOWN | WM_MBUTTONDOWN | WM_XBUTTONDOWN => {
                RawEvent::MouseDown
            }
            WM_LBUTTONUP | WM_RBUTTONUP | WM_MBUTTONUP | WM_XBUTTONUP => RawEvent::MouseUp,
            WM_MOUSEWHEEL | WM_MOUSEHWHEEL => RawEvent::Wheel,
            WM_MOUSEMOVE => mouse_move(info),
            _ => RawEvent::Other,
        };
        // 鼠标钩子的注入标志是 LLMHF_INJECTED（1），不是键盘的 LLKHF_INJECTED（0x10）。
        let injected = info.flags & LLMHF_INJECTED != 0;
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

/// 在本线程上装两个低层钩子，经 `ready` 回报装上与否；装上了就跑消息循环（回调在这个线程上执行），
/// 循环结束时卸载。装不上时卸掉已装的那个并返回。
fn run_hook_loop(ready: &Sender<io::Result<()>>) {
    // SAFETY: 回调是本模块的 `extern "system"` 函数；低层钩子不需要模块句柄。
    let keyboard = match unsafe { SetWindowsHookExW(WH_KEYBOARD_LL, Some(keyboard_hook), None, 0) }
    {
        Ok(handle) => handle,
        Err(error) => {
            let _ = ready.send(Err(io::Error::other(format!(
                "SetWindowsHookExW(WH_KEYBOARD_LL): {error}"
            ))));
            return;
        }
    };
    // SAFETY: 同上。
    let mouse = match unsafe { SetWindowsHookExW(WH_MOUSE_LL, Some(mouse_hook), None, 0) } {
        Ok(handle) => handle,
        Err(error) => {
            // SAFETY: 卸载刚装上的键盘钩子。
            let _ = unsafe { UnhookWindowsHookEx(keyboard) };
            let _ = ready.send(Err(io::Error::other(format!(
                "SetWindowsHookExW(WH_MOUSE_LL): {error}"
            ))));
            return;
        }
    };
    let _ = ready.send(Ok(()));
    let mut message = MSG::default();
    // SAFETY: 标准的消息循环；GetMessageW 填入本地 MSG。
    while unsafe { GetMessageW(&raw mut message, None, 0, 0) }.as_bool() {}
    // SAFETY: 卸载刚装的钩子。
    unsafe {
        let _ = UnhookWindowsHookEx(keyboard);
        let _ = UnhookWindowsHookEx(mouse);
    }
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

/// 起钩子线程，等它回报两个钩子都装上；装失败或 [`HOOK_INSTALL_TIMEOUT`] 内没有回报即返回错误。
fn spawn_hook_thread() -> io::Result<()> {
    let (ready_tx, ready_rx) = mpsc::channel();
    thread::Builder::new()
        .name("input-hook".into())
        .spawn(move || run_hook_loop(&ready_tx))?;
    match ready_rx.recv_timeout(HOOK_INSTALL_TIMEOUT) {
        Ok(result) => result,
        Err(_) => Err(io::Error::other(
            "the low-level hooks were not installed in time",
        )),
    }
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
    let win32: Arc<dyn Win32> = Arc::new(RealWin32 {
        marker,
        target,
        injected: Arc::clone(&injected),
    });
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
    spawn_hook_thread().map_err(|error| {
        io::Error::other(format!("failed to install the low-level hooks: {error}"))
    })?;
    let backend = Box::new(super::SendInputBackend::new(
        Arc::clone(&win32),
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

/// 本实例的会话标记：启动时从系统随机数源取的随机数，放进每个注入事件的 `dwExtraInfo`。不取 0，
/// 以免与不带标记的注入（`dwExtraInfo` 为 0）混同。
fn session_marker() -> usize {
    loop {
        let mut bytes = [0u8; size_of::<usize>()];
        getrandom::fill(&mut bytes).expect("系统随机数源不可用");
        let marker = usize::from_ne_bytes(bytes);
        if marker != 0 {
            return marker;
        }
    }
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
            let mut check = SelfCheck::default();
            loop {
                thread::sleep(SELF_CHECK_INTERVAL);
                let sent = injected.load(Ordering::Acquire);
                let passed = seen.load(Ordering::Acquire);
                // 本实例注入的事件应在 100 ms 内经过自己的钩子；上一轮的注入到这一轮还没被看到即判失效。
                if check.tick(sent, passed) {
                    log!("self-check: own injections did not pass the hook; hooks may be lost");
                    post(control.local_revoke(core_protocol::messages::ReleaseReason::HookLost));
                }
            }
        })?;
    Ok(())
}
