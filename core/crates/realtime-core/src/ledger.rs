//! 输入后端接口、持键账目与释放全部输入（`docs/modules/realtime-core.md` 的“持键账目与释放”）。
//!
//! 释放是唯一的安全路径：任何失效都调用 [`InputLedger::release_all`]，它只动本地状态与输入后端，
//! 不依赖核连接，也不等任何写入，调用方在释放完成之后才通知主进程。
//!
//! 输入后端有只用于测试的假后端（`fake` 模块）与真实后端（`sendinput` 模块，SendInput 注入，只在
//! Windows 上）。没有输入后端时账目照常记账，但没有许可源，权威检查从不通过，账目因而总是空的。
//!
//! 持续状态经 [`InputBackend::press`]、[`InputBackend::release`] 按下与抬起；一次性的点击与短按在有
//! 输入策略时作为“瞬时按住”注入（[`InputBackend::begin_momentary`]、[`InputBackend::end_momentary`]），
//! 按住输入策略的 `press_duration` 后由调度线程抬起；没有输入策略时（假后端的 G0 行为）一次性动作是
//! 单个事件（[`InputBackend::act`]）。注入类的方法返回 [`Inject`]：真实后端据前台核对与 `SendInput` 的回执
//! 给出结果，假后端一律 [`Inject::Done`]。

use core_protocol::messages::{Action, HeldInput, InputPolicy, MouseButton, ReleaseReason};

/// 一次注入的结果。按下类（`press`、`begin_momentary`、`act`）返回它；抬起类是安全方向，不返回。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Inject {
    /// 已注入，注入前后前台核对都通过。
    Done,
    /// 注入前的前台核对不通过，没有注入任何事件。
    FocusLost,
    /// 已注入，但注入过程中前台窗口变了，是否收到不确定。
    FocusChanged,
    /// `SendInput` 注入的事件少于给出的（被拦截）。
    Blocked,
}

impl Inject {
    /// 是否有事件被注入（据此决定是否记进账目，好让释放把它抬起）。只有 [`Inject::FocusLost`] 没有。
    pub(crate) fn injected(self) -> bool {
        !matches!(self, Inject::FocusLost)
    }
}

/// 一次性的点击或短按作为“瞬时按住”注入时的内容。滚动不经这里（它是瞬时的单个事件）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Momentary {
    /// 点击：先移到客户区 `(x, y)` 再按下鼠标键，抬起时放开。
    Click { x: u32, y: u32, button: MouseButton },
    /// 短按：按下扫描码，抬起时放开。
    Key { scan_code: u16 },
}

/// 输入后端：实时核把输入交给它。每个方法都在控制状态的锁内调用，须很快返回，不能等网络写入。
///
/// 按下类（`press`、`begin_momentary`、`act`）只在权威检查通过之后调用，返回 [`Inject`]；抬起类
/// （`release`、`end_momentary`、`released_all`）随时可以调用，是安全方向。
pub(crate) trait InputBackend: Send {
    /// 按下一个键或鼠标键并保持，或把轴设为给定的取值。
    fn press(&mut self, input: &HeldInput) -> Inject;
    /// 抬起一个按住的键或鼠标键，或释放一个轴。
    fn release(&mut self, input: &HeldInput);
    /// 没有输入策略时执行一个一次性动作（单个事件，G0 的行为）。
    fn act(&mut self, action: &Action) -> Inject;
    /// 有输入策略时按下一个瞬时动作（点击或短按）。
    fn begin_momentary(&mut self, momentary: &Momentary) -> Inject;
    /// 抬起一个瞬时动作。
    fn end_momentary(&mut self, momentary: &Momentary);
    /// 释放全部输入已完成：此前按住的每一项都已逐一抬起。
    fn released_all(&mut self, reason: ReleaseReason);
    /// 收到输入策略：真实后端据此知道要核验哪些键与鼠标键，并做一次 `core_start` 核验。缺省不做。
    fn configured(&mut self, policy: &InputPolicy) {
        let _ = policy;
    }
}

/// 持续状态一项的身份：键按扫描码、鼠标键按键、轴按轴名，取值不算身份。
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub(crate) enum HeldId {
    Key(u16),
    MouseButton(MouseButton),
    Axis(String),
}

impl HeldId {
    pub(crate) fn of(input: &HeldInput) -> Self {
        match input {
            HeldInput::Key(key) => Self::Key(key.scan_code),
            HeldInput::MouseButton(button) => Self::MouseButton(button.button),
            HeldInput::Axis(axis) => Self::Axis(axis.axis.clone()),
        }
    }
}

/// 一项持续状态与它第一次按下的时刻（供最长按住时间核对）。
struct Held {
    input: HeldInput,
    pressed_ns: u64,
}

/// 一个瞬时按住与它应被抬起的时刻。
struct Transient {
    momentary: Momentary,
    lift_ns: u64,
}

/// 本实例按下且尚未抬起的输入，按按下的顺序排列；另有一次性动作的瞬时按住。
pub(crate) struct InputLedger {
    backend: Option<Box<dyn InputBackend>>,
    held: Vec<Held>,
    transients: Vec<Transient>,
}

impl InputLedger {
    pub(crate) fn new(backend: Option<Box<dyn InputBackend>>) -> Self {
        Self {
            backend,
            held: Vec::new(),
            transients: Vec::new(),
        }
    }

    /// 是否持有任何输入（持续状态或瞬时按住）。
    pub(crate) fn is_empty(&self) -> bool {
        self.held.is_empty() && self.transients.is_empty()
    }

    /// 是否正持有恰好这一项持续状态（身份与取值都相同）。
    pub(crate) fn holds(&self, input: &HeldInput) -> bool {
        self.held.iter().any(|held| held.input == *input)
    }

    /// 是否有瞬时按住还没抬起：有时同一步的下一个一次性动作要等它抬起。
    pub(crate) fn has_transient(&self) -> bool {
        !self.transients.is_empty()
    }

    /// 抬起身份不在 `keep` 中的每一项持续状态。抬起不需要权威检查，瞬时按住不受它影响。
    pub(crate) fn release_missing(&mut self, keep: &[HeldInput]) {
        let keep: Vec<HeldId> = keep.iter().map(HeldId::of).collect();
        let backend = &mut self.backend;
        self.held.retain(|held| {
            let kept = keep.contains(&HeldId::of(&held.input));
            if !kept && let Some(backend) = backend.as_mut() {
                backend.release(&held.input);
            }
            kept
        });
    }

    /// 按下一项持续状态，或把同名的轴改为新的取值。调用方已做过权威检查；`now` 用于最长按住时间核对。
    pub(crate) fn press(&mut self, input: HeldInput, now: u64) -> Inject {
        let result = self
            .backend
            .as_mut()
            .map_or(Inject::Done, |backend| backend.press(&input));
        if result.injected() {
            let id = HeldId::of(&input);
            match self
                .held
                .iter_mut()
                .find(|held| HeldId::of(&held.input) == id)
            {
                Some(held) => held.input = input,
                None => self.held.push(Held {
                    input,
                    pressed_ns: now,
                }),
            }
        }
        result
    }

    /// 收到输入策略：转交给输入后端（真实后端据此核验残留）。
    pub(crate) fn configured(&mut self, policy: &InputPolicy) {
        if let Some(backend) = self.backend.as_mut() {
            backend.configured(policy);
        }
    }

    /// 没有输入策略时执行一个一次性动作（单个事件）。调用方已做过权威检查。
    pub(crate) fn act(&mut self, action: &Action) -> Inject {
        self.backend
            .as_mut()
            .map_or(Inject::Done, |backend| backend.act(action))
    }

    /// 有输入策略时按下一个瞬时动作，到 `lift_ns` 由调度线程抬起。调用方已做过权威检查。
    pub(crate) fn begin_momentary(&mut self, momentary: Momentary, lift_ns: u64) -> Inject {
        let result = self
            .backend
            .as_mut()
            .map_or(Inject::Done, |backend| backend.begin_momentary(&momentary));
        if result.injected() {
            self.transients.push(Transient { momentary, lift_ns });
        }
        result
    }

    /// 抬起到 `now` 为止到期的瞬时按住（按按下的顺序）。
    pub(crate) fn lift_due(&mut self, now: u64) {
        let backend = &mut self.backend;
        self.transients.retain(|transient| {
            let due = now >= transient.lift_ns;
            if due && let Some(backend) = backend.as_mut() {
                backend.end_momentary(&transient.momentary);
            }
            !due
        });
    }

    /// 最早一个瞬时按住要抬起的时刻，供调度线程定时。
    pub(crate) fn next_lift(&self) -> Option<u64> {
        self.transients.iter().map(|t| t.lift_ns).min()
    }

    /// 最早一项持续状态的按下时刻，供最长按住时间核对。
    pub(crate) fn earliest_held_ns(&self) -> Option<u64> {
        self.held.iter().map(|held| held.pressed_ns).min()
    }

    /// 释放全部输入：先抬起全部瞬时按住，再按与按下相反的顺序逐项抬起持续状态，告诉后端已全部释放，
    /// 再清空账目。
    pub(crate) fn release_all(&mut self, reason: ReleaseReason) {
        if let Some(backend) = self.backend.as_mut() {
            for transient in self.transients.iter().rev() {
                backend.end_momentary(&transient.momentary);
            }
            for held in self.held.iter().rev() {
                backend.release(&held.input);
            }
            backend.released_all(reason);
        }
        self.transients.clear();
        self.held.clear();
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::{Inject, InputBackend, InputLedger, Momentary};
    use core_protocol::messages::{
        Action, HeldAxis, HeldInput, HeldKey, HeldMouseButton, KeyPress, MouseButton, ReleaseReason,
    };
    use std::sync::{Arc, Mutex};

    /// 记下每次调用的后端，供控制状态的单元测试核对交给后端的输入。按下类一律 [`Inject::Done`]。
    #[derive(Clone, Default)]
    pub(crate) struct Recorder(pub(crate) Arc<Mutex<Vec<String>>>);

    impl Recorder {
        pub(crate) fn take(&self) -> Vec<String> {
            std::mem::take(&mut *self.0.lock().unwrap())
        }

        /// 交给控制状态或账目的后端，与本记录器共用记录。
        #[allow(
            clippy::unnecessary_wraps,
            reason = "Control::new 与 InputLedger::new 收的是 Option"
        )]
        pub(crate) fn backend(&self) -> Option<Box<dyn InputBackend>> {
            Some(Box::new(self.clone()))
        }
    }

    pub(crate) fn describe(input: &HeldInput) -> String {
        match input {
            HeldInput::Key(key) => format!("key {:#x}", key.scan_code),
            HeldInput::MouseButton(button) => format!("mouse {:?}", button.button),
            HeldInput::Axis(axis) => format!("axis {}={}", axis.axis, axis.value),
        }
    }

    fn describe_momentary(momentary: &Momentary) -> String {
        match momentary {
            Momentary::Click { x, y, button } => format!("click {button:?} at {x},{y}"),
            Momentary::Key { scan_code } => format!("press {scan_code:#x}"),
        }
    }

    impl InputBackend for Recorder {
        fn press(&mut self, input: &HeldInput) -> Inject {
            self.0
                .lock()
                .unwrap()
                .push(format!("down {}", describe(input)));
            Inject::Done
        }

        fn release(&mut self, input: &HeldInput) {
            self.0
                .lock()
                .unwrap()
                .push(format!("up {}", describe(input)));
        }

        fn act(&mut self, action: &Action) -> Inject {
            let text = match action {
                Action::Click(click) => format!("click {} at {},{}", click.id, click.x, click.y),
                Action::KeyPress(press) => format!("press {} {:#x}", press.id, press.scan_code),
                Action::Scroll(scroll) => format!("scroll {} {}", scroll.id, scroll.notches),
                Action::MouseMove(moved) => format!("move {} {},{}", moved.id, moved.dx, moved.dy),
            };
            self.0.lock().unwrap().push(text);
            Inject::Done
        }

        fn begin_momentary(&mut self, momentary: &Momentary) -> Inject {
            self.0
                .lock()
                .unwrap()
                .push(format!("begin {}", describe_momentary(momentary)));
            Inject::Done
        }

        fn end_momentary(&mut self, momentary: &Momentary) {
            self.0
                .lock()
                .unwrap()
                .push(format!("end {}", describe_momentary(momentary)));
        }

        fn released_all(&mut self, reason: ReleaseReason) {
            self.0
                .lock()
                .unwrap()
                .push(format!("release all {reason:?}"));
        }
    }

    pub(crate) fn key(scan_code: u16) -> HeldInput {
        HeldInput::Key(HeldKey { scan_code })
    }

    pub(crate) fn axis(name: &str, value: f64) -> HeldInput {
        HeldInput::Axis(HeldAxis {
            axis: name.into(),
            value,
        })
    }

    #[test]
    fn releasing_lifts_every_held_input_in_reverse_order() {
        let recorder = Recorder::default();
        let mut ledger = InputLedger::new(recorder.backend());
        ledger.press(key(0x11), 0);
        ledger.press(
            HeldInput::MouseButton(HeldMouseButton {
                button: MouseButton::Left,
            }),
            0,
        );
        ledger.press(axis("yaw", 0.5), 0);
        ledger.release_all(ReleaseReason::LeaseExpired);
        assert!(ledger.is_empty());
        assert_eq!(
            recorder.take(),
            [
                "down key 0x11",
                "down mouse Left",
                "down axis yaw=0.5",
                "up axis yaw=0.5",
                "up mouse Left",
                "up key 0x11",
                "release all LeaseExpired",
            ]
        );
        // 账目为空时仍告诉后端释放了全部输入。
        ledger.release_all(ReleaseReason::Stop);
        assert_eq!(recorder.take(), ["release all Stop"]);
    }

    #[test]
    fn missing_items_are_released_and_axes_change_in_place() {
        let recorder = Recorder::default();
        let mut ledger = InputLedger::new(recorder.backend());
        ledger.press(key(0x11), 0);
        ledger.press(axis("yaw", 0.5), 0);
        ledger.release_missing(&[axis("yaw", -1.0)]);
        assert_eq!(
            recorder.take(),
            ["down key 0x11", "down axis yaw=0.5", "up key 0x11"]
        );
        assert!(!ledger.holds(&axis("yaw", -1.0)));
        ledger.press(axis("yaw", -1.0), 0);
        assert!(ledger.holds(&axis("yaw", -1.0)));
        ledger.release_all(ReleaseReason::Stop);
        assert_eq!(
            recorder.take(),
            ["down axis yaw=-1", "up axis yaw=-1", "release all Stop"]
        );
    }

    #[test]
    fn momentary_presses_lift_on_time_and_on_release() {
        let recorder = Recorder::default();
        let mut ledger = InputLedger::new(recorder.backend());
        ledger.begin_momentary(Momentary::Key { scan_code: 0x1C }, 100);
        ledger.begin_momentary(
            Momentary::Click {
                x: 5,
                y: 6,
                button: MouseButton::Left,
            },
            150,
        );
        assert!(ledger.has_transient());
        assert_eq!(ledger.next_lift(), Some(100));
        ledger.lift_due(100);
        assert_eq!(
            recorder.take(),
            [
                "begin press 0x1c",
                "begin click Left at 5,6",
                "end press 0x1c"
            ]
        );
        // 第二个瞬时按住由释放全部抬起。
        ledger.release_all(ReleaseReason::MaxHold);
        assert!(ledger.is_empty());
        assert_eq!(
            recorder.take(),
            ["end click Left at 5,6", "release all MaxHold"]
        );
    }

    #[test]
    fn without_a_backend_the_ledger_only_keeps_accounts() {
        let mut ledger = InputLedger::new(None);
        ledger.press(key(0x1E), 0);
        ledger.act(&Action::KeyPress(KeyPress {
            id: 1,
            scan_code: 0x1C,
        }));
        assert!(!ledger.is_empty());
        ledger.release_all(ReleaseReason::Stop);
        assert!(ledger.is_empty());
    }
}
