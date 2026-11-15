//! 真实输入后端（SendInput，GR-09）与接管检测、急停、残留核验（`docs/modules/realtime-core.md` 的
//! “注入”“接管与急停”“持键账目与释放”）。
//!
//! Win32 调用藏在 [`Win32`] 这层薄接口后面：注入的事件构造、前台核对、绝对坐标换算、钩子事件的分类、
//! 急停组合键与残留核验都是不依赖平台的逻辑，用替身 [`Win32`] 在任何平台上做单元测试。真实的 Win32
//! 实现、低层钩子线程与核验线程只在 Windows 上（[`windows`] 子模块）。

#![allow(
    dead_code,
    reason = "真实后端的构造只在 Windows 上编译，逻辑在所有平台测试"
)]

use std::collections::BTreeSet;
use std::sync::mpsc::Sender;
use std::sync::{Arc, Mutex};

use core_protocol::messages::{
    Action, HeldInput, HeldKey, HeldMouseButton, MouseButton, ReleaseReason, ResidualCause,
};
#[cfg(any(windows, test))]
use core_protocol::messages::{InputResidualParams, InputVerificationParams, VerificationResult};

#[cfg(any(windows, test))]
use crate::control::Control;
use crate::frames::FrameStatus;
use crate::ledger::{Inject, InputBackend, Momentary};

/// 真实输入只使用最近写入帧所对应的采集窗口。
pub(crate) struct TargetWindow {
    frames: Arc<FrameStatus>,
    /// 以真实后端运行：注入要有窗口，`capture.set_target` 的假显示面回 -32003。
    real: bool,
}

impl TargetWindow {
    /// `real` 为真表示以真实后端运行。
    pub(crate) fn new(real: bool, frames: Arc<FrameStatus>) -> Self {
        Self { frames, real }
    }

    /// 以真实后端运行时采集目标必须是窗口：假显示面没有可注入的窗口。
    pub(crate) fn accepts(&self, target: &core_protocol::messages::CaptureTarget) -> bool {
        !(self.real && matches!(target, core_protocol::messages::CaptureTarget::Fake(_)))
    }

    #[cfg(windows)]
    fn get(&self) -> Option<(capture::WindowIdentity, crate::frames::Geometry)> {
        self.frames.captured_window()
    }
}

/// 高字节为 `0xE0` 的扩展键。
fn is_extended(scan_code: u16) -> bool {
    scan_code >> 8 == 0xE0
}

/// 扫描码的低字节（Set 1 的按下码）。
fn low_scan(scan_code: u16) -> u16 {
    scan_code & 0xFF
}

// ---------------------------------------------------------------- 注入事件

/// 交给 [`Win32::send`] 的一个注入事件，已不含平台细节；每个都带本实例的会话标记。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Event {
    /// 键按下（扫描码，扩展键带标志）。
    KeyDown { scan_code: u16, extended: bool },
    /// 键抬起。
    KeyUp { scan_code: u16, extended: bool },
    /// 移到虚拟桌面绝对坐标（0–65535）。
    MouseMoveAbs { x: u16, y: u16 },
    /// 物理鼠标键按下（已按 `SM_SWAPBUTTON` 换算）。
    MouseDown { button: PhysicalButton },
    /// 物理鼠标键抬起。
    MouseUp { button: PhysicalButton },
    /// 滚轮，正数向前。
    Wheel { notches: i32 },
}

/// 注入时用的物理鼠标键（左右键已按系统设置换算）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum PhysicalButton {
    Left,
    Right,
    Middle,
}

/// 目标窗口现在适合注入的前提：客户区原点（屏幕物理像素）与窗口身份（判断注入前后前台是否变了）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct Target {
    /// 窗口身份，用来判断注入过程中前台有没有换窗口。
    pub(crate) window: isize,
    /// 客户区左上角在屏幕上的横坐标（物理像素）。
    pub(crate) client_x: i32,
    /// 客户区左上角在屏幕上的纵坐标（物理像素）。
    pub(crate) client_y: i32,
}

/// 虚拟桌面的范围（屏幕物理像素），供绝对坐标换算。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct Desktop {
    pub(crate) left: i32,
    pub(crate) top: i32,
    pub(crate) width: i32,
    pub(crate) height: i32,
}

/// 核验时要读按键状态的一项。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum VerifyKey {
    Scan(u16),
    Mouse(PhysicalButton),
}

/// 把客户区物理像素 `(cx, cy)` 加客户区原点换算成虚拟桌面的 0–65535 绝对坐标。
#[must_use]
pub(crate) fn absolute(cx: u32, cy: u32, target: Target, desktop: Desktop) -> (u16, u16) {
    let screen_x = i64::from(target.client_x) + i64::from(cx);
    let screen_y = i64::from(target.client_y) + i64::from(cy);
    let norm = |value: i64, origin: i32, span: i32| -> u16 {
        let span = i64::from(span.max(1));
        // 取像素中心，按 Windows 的惯例换算到 0–65535。
        let numerator = (value - i64::from(origin)) * 65_535 + span / 2;
        let scaled = (numerator / span).clamp(0, 65_535);
        u16::try_from(scaled).unwrap_or(u16::MAX)
    };
    (
        norm(screen_x, desktop.left, desktop.width),
        norm(screen_y, desktop.top, desktop.height),
    )
}

/// 把白名单的鼠标键按 `swapped`（`SM_SWAPBUTTON`）换算成注入用的物理键。
#[must_use]
pub(crate) fn physical_button(button: MouseButton, swapped: bool) -> PhysicalButton {
    match (button, swapped) {
        (MouseButton::Left, false) | (MouseButton::Right, true) => PhysicalButton::Left,
        (MouseButton::Right, false) | (MouseButton::Left, true) => PhysicalButton::Right,
        (MouseButton::Middle, _) => PhysicalButton::Middle,
    }
}

fn key_down_events(scan_code: u16) -> Event {
    Event::KeyDown {
        scan_code: low_scan(scan_code),
        extended: is_extended(scan_code),
    }
}

fn key_up_events(scan_code: u16) -> Event {
    Event::KeyUp {
        scan_code: low_scan(scan_code),
        extended: is_extended(scan_code),
    }
}

// ---------------------------------------------------------------- 急停组合键

/// 急停组合键：一组修饰键加一个最终键。缺省 `Ctrl+Shift+F12`。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Combo {
    /// 修饰键的虚拟键码集合（左右不分，用无边键码）。
    modifiers: BTreeSet<u16>,
    /// 最终键的虚拟键码。
    key: u16,
}

/// 虚拟键码常量（只列组合键用得到的）。
mod vk {
    pub(super) const CONTROL: u16 = 0x11;
    pub(super) const SHIFT: u16 = 0x10;
    pub(super) const ALT: u16 = 0x12;
    pub(super) const WIN: u16 = 0x5B;
    pub(super) const RWIN: u16 = 0x5C;
    pub(super) const LSHIFT: u16 = 0xA0;
    pub(super) const RSHIFT: u16 = 0xA1;
    pub(super) const LCONTROL: u16 = 0xA2;
    pub(super) const RCONTROL: u16 = 0xA3;
    pub(super) const LALT: u16 = 0xA4;
    pub(super) const RALT: u16 = 0xA5;
    pub(super) const END: u16 = 0x23;
    pub(super) const HOME: u16 = 0x24;
    pub(super) const INSERT: u16 = 0x2D;
    pub(super) const DELETE: u16 = 0x2E;
    pub(super) const ESCAPE: u16 = 0x1B;
    pub(super) const PAUSE: u16 = 0x13;
    pub(super) const SCROLL_LOCK: u16 = 0x91;
}

/// 把分左右的修饰键归成不分左右的键码：低层键盘钩子报 `VK_LCONTROL`、`VK_RSHIFT` 等，组合键存的是
/// `VK_CONTROL`、`VK_SHIFT` 等。其它键原样返回。
#[must_use]
pub(crate) fn normalize_vk(code: u16) -> u16 {
    match code {
        vk::LSHIFT | vk::RSHIFT => vk::SHIFT,
        vk::LCONTROL | vk::RCONTROL => vk::CONTROL,
        vk::LALT | vk::RALT => vk::ALT,
        vk::RWIN => vk::WIN,
        other => other,
    }
}

fn modifier_vk(name: &str) -> Option<u16> {
    match name {
        "ctrl" | "control" => Some(vk::CONTROL),
        "shift" => Some(vk::SHIFT),
        "alt" => Some(vk::ALT),
        "win" | "super" | "meta" => Some(vk::WIN),
        _ => None,
    }
}

fn key_vk(name: &str) -> Option<u16> {
    if let Some(number) = name.strip_prefix('f')
        && let Ok(index) = number.parse::<u16>()
        && (1..=24).contains(&index)
    {
        // F1..F24 的虚拟键码是 0x70 起。
        return Some(0x6F + index);
    }
    if name.len() == 1 {
        let ch = name.chars().next()?;
        if ch.is_ascii_alphabetic() {
            return Some(ch.to_ascii_uppercase() as u16);
        }
        if ch.is_ascii_digit() {
            return Some(ch as u16);
        }
    }
    match name {
        "end" => Some(vk::END),
        "home" => Some(vk::HOME),
        "insert" | "ins" => Some(vk::INSERT),
        "delete" | "del" => Some(vk::DELETE),
        "escape" | "esc" => Some(vk::ESCAPE),
        "pause" => Some(vk::PAUSE),
        "scrolllock" => Some(vk::SCROLL_LOCK),
        _ => None,
    }
}

impl Combo {
    /// 缺省急停组合键 `Ctrl+Shift+F12`（维护者 2026-11-02）。
    #[must_use]
    pub fn default_hotkey() -> Self {
        Self {
            modifiers: [vk::CONTROL, vk::SHIFT].into_iter().collect(),
            key: 0x6F + 12,
        }
    }

    /// 解析形如 `ctrl+shift+f12`、`ctrl+alt+end` 的组合键：几个修饰键加一个最终键，`+` 分隔，不分大小写。
    ///
    /// # Errors
    ///
    /// 没有最终键、有多个最终键、名字不认识或有重复时返回说明。
    pub fn parse(text: &str) -> Result<Self, String> {
        let mut modifiers = BTreeSet::new();
        let mut key: Option<u16> = None;
        for part in text.split('+') {
            let name = part.trim().to_ascii_lowercase();
            if name.is_empty() {
                return Err(format!("emergency hotkey {text:?} has an empty part"));
            }
            if let Some(modifier) = modifier_vk(&name) {
                if !modifiers.insert(modifier) {
                    return Err(format!("emergency hotkey {text:?} repeats a modifier"));
                }
            } else if let Some(code) = key_vk(&name) {
                if key.is_some() {
                    return Err(format!(
                        "emergency hotkey {text:?} has more than one non-modifier key"
                    ));
                }
                key = Some(code);
            } else {
                return Err(format!(
                    "emergency hotkey {text:?} has an unknown key {name:?}"
                ));
            }
        }
        match key {
            Some(key) => Ok(Self { modifiers, key }),
            None => Err(format!(
                "emergency hotkey {text:?} needs a non-modifier key"
            )),
        }
    }

    /// 所有修饰键都按住、且最终键正是刚按下的这个时，算触发。
    fn triggered(&self, final_key: u16, down: &BTreeSet<u16>) -> bool {
        normalize_vk(final_key) == self.key
            && self
                .modifiers
                .iter()
                .all(|modifier| down.iter().any(|vk| normalize_vk(*vk) == *modifier))
    }
}

// ---------------------------------------------------------------- 钩子分类

/// 低层钩子看到的一个原始事件（已归一化，不含平台类型）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum RawEvent {
    /// 键按下，`vk` 是虚拟键码。
    KeyDown { vk: u16 },
    /// 键抬起。
    KeyUp { vk: u16 },
    /// 鼠标键按下。
    MouseDown,
    /// 鼠标键抬起。
    MouseUp,
    /// 滚轮。
    Wheel,
    /// 鼠标移动，`dx`、`dy` 是屏幕物理像素的位移。
    MouseMove { dx: i32, dy: i32 },
    /// 其它消息：不触发任何动作，也不计入自检。
    Other,
}

/// 钩子对一个事件的判定。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Class {
    /// 本实例注入的键、鼠标键（按下与抬起）或滚轮：忽略，计入自检。本实例注入的移动判为 `Ignore`，不计。
    OwnInjection,
    /// 真人输入或第三方注入：接管（只在许可有效时算数，由调用方核对）。
    Takeover,
    /// 急停组合键：不论许可是否有效。
    Emergency,
    /// 钩子线程已退出。
    HookLost,
    /// 不触发任何动作（抬起、未过阈值的移动、许可无效时的输入等）。
    Ignore,
}

/// 鼠标移动算接管的累计阈值：8 个物理像素，避开光电鼠标的抖动（GS-02 实测）。
pub(crate) const MOVE_THRESHOLD_PX: i64 = 8;

/// 钩子的分类状态：按住的键（判急停）、鼠标移动的累计（判真人移动）。
#[derive(Debug, Default)]
pub(crate) struct HookState {
    down: BTreeSet<u16>,
    movement: i64,
}

impl HookState {
    /// 许可（重新）生效时重置鼠标移动的累计：阈值从许可生效起算。
    pub(crate) fn lease_became_valid(&mut self) {
        self.movement = 0;
    }

    /// 分类一个事件。`injected` 为真表示带注入标志；`own` 为真表示 `dwExtraInfo` 是本实例的会话标记；
    /// `combo` 是急停组合键；`lease_valid` 是许可现在是否有效。急停优先于接管。
    pub(crate) fn classify(
        &mut self,
        event: RawEvent,
        injected: bool,
        own: bool,
        combo: &Combo,
        lease_valid: bool,
    ) -> Class {
        // 按住集合保存左右两侧的原始键码，判断组合键时才归并。
        match event {
            RawEvent::KeyDown { vk } => {
                let emergency = combo.triggered(vk, &self.down);
                self.down.insert(vk);
                if emergency {
                    return Class::Emergency;
                }
            }
            RawEvent::KeyUp { vk } => {
                self.down.remove(&vk);
            }
            _ => {}
        }
        // 本实例的注入不算接管。键、鼠标键的按下与抬起、滚轮计入自检；移动不计（与注入一侧的计数一致）。
        if injected && own {
            return match event {
                RawEvent::MouseMove { .. } | RawEvent::Other => Class::Ignore,
                _ => Class::OwnInjection,
            };
        }
        // 第三方注入与真人输入：接管只在许可有效时算数。
        let human = match event {
            // 抬起不算接管；其它消息不触发任何动作。
            RawEvent::KeyUp { .. } | RawEvent::MouseUp | RawEvent::Other => false,
            // 没有注入标志的真人移动：累计越过阈值才算（避开抖动）。
            RawEvent::MouseMove { dx, dy } if !injected => {
                self.movement += i64::from(dx.unsigned_abs()) + i64::from(dy.unsigned_abs());
                self.movement > MOVE_THRESHOLD_PX
            }
            // 真人按下、滚轮，或带注入标志的第三方移动与按下。
            _ => true,
        };
        if human && lease_valid {
            Class::Takeover
        } else {
            Class::Ignore
        }
    }
}

// ---------------------------------------------------------------- Win32 这层薄接口

/// 真实后端用到的 Win32 调用，藏在这层接口后面以便用替身测试逻辑。实现须线程安全（注入线程与核验线程
/// 都用它）。
pub(crate) trait Win32: Send + Sync {
    /// 目标窗口现在适合注入吗：存在、可见、未最小化、是前台、输入桌面是当前桌面；是就给出它的身份与
    /// 客户区原点，否则 `None`。
    fn foreground_target(&self) -> Option<Target>;
    /// 虚拟桌面的范围，供绝对坐标换算。
    fn virtual_desktop(&self) -> Desktop;
    /// 鼠标左右键是否交换（`SM_SWAPBUTTON`）。
    fn buttons_swapped(&self) -> bool;
    /// 注入一批带会话标记的事件，返回被接受的事件数（少于给出的即被拦截）。真实实现同时按
    /// [`self_check_count`] 记下钩子自检要看到的事件数。
    fn send(&self, events: &[Event]) -> u32;
    /// 某个键或鼠标键现在是否按下（`GetAsyncKeyState` 的高位）。读物理鼠标键，不随左右键交换。
    /// 线程能读取当前活动输入桌面的状态。
    fn can_verify(&self) -> bool;
    /// `None` 表示这项输入的状态不可读。
    fn is_down(&self, key: VerifyKey) -> Option<bool>;
}

/// 钩子自检要计的事件数：SendInput 按顺序接受了前 `sent` 个事件，其中键、鼠标键与滚轮的个数。移动不计：
/// 移到光标已在的位置时钩子是否看得到没有核实；钩子一侧同样不计本实例的移动。
#[must_use]
pub(crate) fn self_check_count(events: &[Event], sent: u32) -> u64 {
    let counted = events
        .iter()
        .take(sent as usize)
        .filter(|event| !matches!(event, Event::MouseMoveAbs { .. }))
        .count();
    counted as u64
}

/// 钩子自检的状态：上一轮读到的注入数，与已经报过、不再追究的未经过的事件数。
#[derive(Debug, Default)]
pub(crate) struct SelfCheck {
    last_injected: u64,
    excused: u64,
}

impl SelfCheck {
    /// 每 100 ms 一轮。`injected` 是到现在为止要计的注入事件数（[`self_check_count`]），`seen` 是钩子看到的
    /// 本实例事件数。上一轮时已注入的到这一轮还没全部经过钩子即返回真（判钩子失效）；同一批没有经过的
    /// 只报一次，之后注入的照常核对。
    pub(crate) fn tick(&mut self, injected: u64, seen: u64) -> bool {
        let lost = self.last_injected > seen.saturating_add(self.excused);
        if lost {
            self.excused = self.last_injected - seen;
        }
        self.last_injected = injected;
        lost
    }
}

// ---------------------------------------------------------------- 核验残留

/// 读取白名单内按住的项，连同与读取使用同一物理键的抬起事件。
fn read_residual(
    win32: &dyn Win32,
    keys: &[u16],
    buttons: &[MouseButton],
) -> Option<Vec<(HeldInput, Event)>> {
    if !win32.can_verify() {
        return None;
    }
    let mut down = Vec::new();
    for &scan_code in keys {
        if win32.is_down(VerifyKey::Scan(scan_code))? {
            down.push((
                HeldInput::Key(HeldKey { scan_code }),
                key_up_events(scan_code),
            ));
        }
    }
    let swapped = win32.buttons_swapped();
    for &button in buttons {
        let physical = physical_button(button, swapped);
        if win32.is_down(VerifyKey::Mouse(physical))? {
            down.push((
                HeldInput::MouseButton(HeldMouseButton { button }),
                Event::MouseUp { button: physical },
            ));
        }
    }
    (win32.can_verify() && swapped == win32.buttons_swapped()).then_some(down)
}

/// 返回读作按住的项、以及系统是否接受全部补发的抬起事件。
pub(crate) fn verify_residual(
    win32: &dyn Win32,
    keys: &[u16],
    buttons: &[MouseButton],
) -> Option<(Vec<HeldInput>, bool)> {
    let (items, ups): (Vec<_>, Vec<_>) = read_residual(win32, keys, buttons)?.into_iter().unzip();
    let repair_accepted = ups.is_empty() || win32.send(&ups) as usize == ups.len();
    Some((items, repair_accepted))
}

/// 补发抬起后只复查状态。
pub(crate) fn no_residual(
    win32: &dyn Win32,
    keys: &[u16],
    buttons: &[MouseButton],
) -> Option<bool> {
    Some(read_residual(win32, keys, buttons)?.is_empty())
}

// ---------------------------------------------------------------- 真实后端

/// 请求一次残留核验（释放后或收到输入策略时），由核验线程处理。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct VerifyRequest {
    pub(crate) release_id: u64,
    pub(crate) cause: ResidualCause,
    /// `cause` 为 `release` 时那次释放的原因。
    pub(crate) reason: Option<ReleaseReason>,
}

/// 核验线程读写的策略里的键与鼠标键。
#[derive(Clone, Debug, Default)]
pub(crate) struct VerifyKeys {
    pub(crate) configured: bool,
    pub(crate) swapped: bool,
    pub(crate) keys: Vec<u16>,
    pub(crate) buttons: Vec<MouseButton>,
}

impl VerifyKeys {
    fn can_check(&self, win32: &dyn Win32) -> bool {
        self.configured && (self.buttons.is_empty() || win32.buttons_swapped() == self.swapped)
    }
}

#[cfg(any(windows, test))]
pub(crate) fn check_release(
    control: &Control,
    win32: &dyn Win32,
    keys: &VerifyKeys,
    request: VerifyRequest,
    report_residual: impl FnOnce(InputResidualParams),
    wait_for_repair: impl FnOnce(),
) -> InputVerificationParams {
    let id = request.release_id;
    let mut residual = None;
    let read = control
        .with_verification(id, || {
            let observed = keys
                .can_check(win32)
                .then(|| verify_residual(win32, &keys.keys, &keys.buttons))
                .flatten();
            let repair_rejected = observed.as_ref().is_some_and(|(_, accepted)| !accepted);
            (observed, repair_rejected)
        })
        .flatten();
    let result = match read {
        None => VerificationResult::Unavailable,
        Some((items, _)) if items.is_empty() => VerificationResult::Verified,
        Some((items, repair_accepted)) => {
            residual = Some(InputResidualParams {
                release_id: id,
                cause: request.cause,
                reason: request.reason,
                items,
                repair_accepted,
                checked_at_ns: mono_clock::now_ns(),
            });
            if repair_accepted {
                wait_for_repair();
                match control
                    .with_verification(id, || {
                        let clear = keys
                            .can_check(win32)
                            .then(|| no_residual(win32, &keys.keys, &keys.buttons))
                            .flatten();
                        (clear, false)
                    })
                    .flatten()
                {
                    Some(true) => VerificationResult::Verified,
                    Some(false) => VerificationResult::Residual,
                    None => VerificationResult::Unavailable,
                }
            } else {
                VerificationResult::Residual
            }
        }
    };
    let result = control.finish_verification(id, result);
    if let Some(residual) = residual {
        report_residual(residual);
    }
    result
}

/// 真实输入后端：用 `SendInput` 注入，注入前核对前台、注入后再核对一次，被拦截即 `Blocked`。释放后与收到
/// 输入策略时请核验线程核验残留。泛型于 [`Win32`]，便于用替身测试。钩子自检的注入计数由 [`Win32::send`]
/// 的实现记（核验线程补发的抬起也经它），见 [`self_check_count`]。
pub(crate) struct SendInputBackend {
    win32: Arc<dyn Win32>,
    verify: Sender<VerifyRequest>,
    verify_keys: Arc<Mutex<VerifyKeys>>,
    startup: Option<VerifyRequest>,
    configured_swap: Option<bool>,
}

impl SendInputBackend {
    pub(crate) fn new(
        win32: Arc<dyn Win32>,
        verify: Sender<VerifyRequest>,
        verify_keys: Arc<Mutex<VerifyKeys>>,
    ) -> Self {
        Self {
            win32,
            verify,
            verify_keys,
            startup: None,
            configured_swap: None,
        }
    }

    /// 注入一批按下类的事件：注入前核对前台（不成立 `FocusLost`），按当时的客户区原点换算绝对坐标，
    /// 发出后比较被接受的事件数（少即 `Blocked`），再核对一次前台（变了即 `FocusChanged`）。
    fn inject_down(&self, build: impl Fn(Target, Desktop) -> Vec<Event>) -> Inject {
        let Some(before) = self.win32.foreground_target() else {
            return Inject::FocusLost;
        };
        let events = build(before, self.win32.virtual_desktop());
        let sent = self.win32.send(&events);
        if (sent as usize) < events.len() {
            return Inject::Blocked;
        }
        match self.win32.foreground_target() {
            Some(after) if after.window == before.window => Inject::Done,
            _ => Inject::FocusChanged,
        }
    }

    /// 抬起类的事件：安全方向，不核对前台，直接注入。
    fn inject_up(&self, events: &[Event]) -> bool {
        self.win32.send(events) as usize == events.len()
    }

    fn mouse_button(&self, button: MouseButton) -> PhysicalButton {
        physical_button(
            button,
            self.configured_swap
                .unwrap_or_else(|| self.win32.buttons_swapped()),
        )
    }

    fn mouse_mapping_matches(&self) -> bool {
        self.configured_swap
            .is_none_or(|swapped| self.win32.buttons_swapped() == swapped)
    }
}

impl InputBackend for SendInputBackend {
    fn press(&mut self, input: &HeldInput) -> Inject {
        match input {
            HeldInput::Key(key) => {
                let scan = key.scan_code;
                self.inject_down(move |_, _| vec![key_down_events(scan)])
            }
            HeldInput::MouseButton(button) => {
                if !self.mouse_mapping_matches() {
                    return Inject::Blocked;
                }
                let physical = self.mouse_button(button.button);
                self.inject_down(move |_, _| vec![Event::MouseDown { button: physical }])
            }
            // 轴不在真实后端的白名单内（提交时已拒绝）；保险起见不注入。
            HeldInput::Axis(_) => Inject::Done,
        }
    }

    fn release(&mut self, input: &HeldInput) -> bool {
        match input {
            HeldInput::Key(key) => self.inject_up(&[key_up_events(key.scan_code)]),
            HeldInput::MouseButton(button) => self.inject_up(&[Event::MouseUp {
                button: self.mouse_button(button.button),
            }]),
            HeldInput::Axis(_) => true,
        }
    }

    fn act(&mut self, action: &Action) -> Inject {
        match action {
            Action::Scroll(scroll) => {
                let (cx, cy, notches) = (scroll.x, scroll.y, scroll.notches);
                self.inject_down(move |target, desktop| {
                    let (x, y) = absolute(cx, cy, target, desktop);
                    vec![Event::MouseMoveAbs { x, y }, Event::Wheel { notches }]
                })
            }
            // 点击与短按在真实后端走瞬时按住；相对位移不在白名单内。
            Action::Click(_) | Action::KeyPress(_) | Action::MouseMove(_) => Inject::Done,
        }
    }

    fn begin_momentary(&mut self, momentary: &Momentary) -> Inject {
        match *momentary {
            Momentary::Click { x, y, button } => {
                if !self.mouse_mapping_matches() {
                    return Inject::Blocked;
                }
                let physical = self.mouse_button(button);
                self.inject_down(move |target, desktop| {
                    let (ax, ay) = absolute(x, y, target, desktop);
                    vec![
                        Event::MouseMoveAbs { x: ax, y: ay },
                        Event::MouseDown { button: physical },
                    ]
                })
            }
            Momentary::Key { scan_code } => {
                self.inject_down(move |_, _| vec![key_down_events(scan_code)])
            }
        }
    }

    fn end_momentary(&mut self, momentary: &Momentary) -> bool {
        match *momentary {
            Momentary::Click { button, .. } => self.inject_up(&[Event::MouseUp {
                button: self.mouse_button(button),
            }]),
            Momentary::Key { scan_code } => self.inject_up(&[key_up_events(scan_code)]),
        }
    }

    fn verifies_release(&self) -> bool {
        true
    }

    fn released_all(&mut self, reason: ReleaseReason, release_id: u64) {
        if reason == ReleaseReason::CoreStart {
            // 启动时还没有输入策略，收到策略后才知道要查哪些键。
            self.startup = Some(VerifyRequest {
                release_id,
                cause: ResidualCause::CoreStart,
                reason: None,
            });
        } else {
            let _ = self.verify.send(VerifyRequest {
                release_id,
                cause: ResidualCause::Release,
                reason: Some(reason),
            });
        }
    }

    fn configured(&mut self, policy: &core_protocol::messages::InputPolicy) {
        let swapped = self.win32.buttons_swapped();
        self.configured_swap = Some(swapped);
        if let Ok(mut keys) = self.verify_keys.lock() {
            keys.keys = policy
                .press_keys
                .iter()
                .chain(&policy.hold_keys)
                .copied()
                .collect();
            keys.buttons = policy
                .click_buttons
                .iter()
                .chain(&policy.hold_buttons)
                .copied()
                .collect();
            keys.configured = true;
            keys.swapped = swapped;
        }
        // 新实例收到输入策略时核验一次：旧实例可能留下了按住的键。
        if let Some(request) = self.startup.take() {
            let _ = self.verify.send(request);
        }
    }
}

#[cfg(windows)]
pub(crate) mod windows;

#[cfg(test)]
mod tests;
