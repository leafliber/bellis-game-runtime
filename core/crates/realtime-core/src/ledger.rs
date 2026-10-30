//! 输入后端接口、持键账目与释放全部输入（`docs/modules/realtime-core.md` 的“持键账目与释放”）。
//!
//! 释放是唯一的安全路径：任何失效都调用 [`InputLedger::release_all`]，它只动本地状态与输入后端，
//! 不依赖核连接，也不等任何写入，调用方在释放完成之后才通知主进程。
//!
//! 输入后端只有只用于测试的假后端（`fake` 模块），它把事件放进发送队列就返回，从不等网络写入；
//! 真实后端（`SendInput` 与 `GetAsyncKeyState` 核验）在 G2 实现，现在 unsupported。没有输入后端时
//! 账目照常记账，但没有许可源，权威检查从不通过，账目因而总是空的。

use core_protocol::messages::{Action, HeldInput, MouseButton, ReleaseReason};

/// 输入后端：实时核把输入交给它。每个方法都在控制状态的锁内调用，须很快返回，不能等网络写入。
///
/// 按下类（`press`、`act`）只在权威检查通过之后调用；抬起类（`release`、`released_all`）随时可以调用。
pub(crate) trait InputBackend: Send {
    /// 按下一个键或鼠标键并保持，或把轴设为给定的取值。
    fn press(&mut self, input: &HeldInput);
    /// 抬起一个按住的键或鼠标键，或释放一个轴。
    fn release(&mut self, input: &HeldInput);
    /// 执行一个一次性动作。
    fn act(&mut self, action: &Action);
    /// 释放全部输入已完成：此前按住的每一项都已逐一 `release`。
    fn released_all(&mut self, reason: ReleaseReason);
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

/// 本实例按下且尚未抬起的输入，按按下的顺序排列。
pub(crate) struct InputLedger {
    backend: Option<Box<dyn InputBackend>>,
    held: Vec<HeldInput>,
}

impl InputLedger {
    pub(crate) fn new(backend: Option<Box<dyn InputBackend>>) -> Self {
        Self {
            backend,
            held: Vec::new(),
        }
    }

    /// 是否持有任何输入。
    pub(crate) fn is_empty(&self) -> bool {
        self.held.is_empty()
    }

    /// 是否正持有恰好这一项（身份与取值都相同）。
    pub(crate) fn holds(&self, input: &HeldInput) -> bool {
        self.held.contains(input)
    }

    /// 抬起身份不在 `keep` 中的每一项。抬起不需要权威检查。
    pub(crate) fn release_missing(&mut self, keep: &[HeldInput]) {
        let keep: Vec<HeldId> = keep.iter().map(HeldId::of).collect();
        let backend = &mut self.backend;
        self.held.retain(|input| {
            let kept = keep.contains(&HeldId::of(input));
            if !kept && let Some(backend) = backend.as_mut() {
                backend.release(input);
            }
            kept
        });
    }

    /// 按下一项，或把同名的轴改为新的取值。调用方已做过权威检查。
    pub(crate) fn press(&mut self, input: HeldInput) {
        if let Some(backend) = self.backend.as_mut() {
            backend.press(&input);
        }
        let id = HeldId::of(&input);
        match self.held.iter_mut().find(|held| HeldId::of(held) == id) {
            Some(held) => *held = input,
            None => self.held.push(input),
        }
    }

    /// 执行一个一次性动作。调用方已做过权威检查。
    pub(crate) fn act(&mut self, action: &Action) {
        if let Some(backend) = self.backend.as_mut() {
            backend.act(action);
        }
    }

    /// 释放全部输入：按与按下相反的顺序逐项抬起，告诉后端已全部释放，再清空账目。
    pub(crate) fn release_all(&mut self, reason: ReleaseReason) {
        if let Some(backend) = self.backend.as_mut() {
            for input in self.held.iter().rev() {
                backend.release(input);
            }
            backend.released_all(reason);
        }
        self.held.clear();
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::{InputBackend, InputLedger};
    use core_protocol::messages::{
        Action, HeldAxis, HeldInput, HeldKey, HeldMouseButton, KeyPress, MouseButton, ReleaseReason,
    };
    use std::sync::{Arc, Mutex};

    /// 记下每次调用的后端，供控制状态的单元测试核对交给后端的输入。
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

    impl InputBackend for Recorder {
        fn press(&mut self, input: &HeldInput) {
            self.0
                .lock()
                .unwrap()
                .push(format!("down {}", describe(input)));
        }

        fn release(&mut self, input: &HeldInput) {
            self.0
                .lock()
                .unwrap()
                .push(format!("up {}", describe(input)));
        }

        fn act(&mut self, action: &Action) {
            let text = match action {
                Action::Click(click) => format!("click {} at {},{}", click.id, click.x, click.y),
                Action::KeyPress(press) => format!("press {} {:#x}", press.id, press.scan_code),
                Action::Scroll(scroll) => format!("scroll {} {}", scroll.id, scroll.notches),
                Action::MouseMove(moved) => format!("move {} {},{}", moved.id, moved.dx, moved.dy),
            };
            self.0.lock().unwrap().push(text);
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
        ledger.press(key(0x11));
        ledger.press(HeldInput::MouseButton(HeldMouseButton {
            button: MouseButton::Left,
        }));
        ledger.press(axis("yaw", 0.5));
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
        ledger.press(key(0x11));
        ledger.press(axis("yaw", 0.5));
        ledger.release_missing(&[axis("yaw", -1.0)]);
        assert_eq!(
            recorder.take(),
            ["down key 0x11", "down axis yaw=0.5", "up key 0x11"]
        );
        assert!(!ledger.holds(&axis("yaw", -1.0)));
        ledger.press(axis("yaw", -1.0));
        assert!(ledger.holds(&axis("yaw", -1.0)));
        ledger.release_all(ReleaseReason::Stop);
        assert_eq!(
            recorder.take(),
            ["down axis yaw=-1", "up axis yaw=-1", "release all Stop"]
        );
    }

    #[test]
    fn without_a_backend_the_ledger_only_keeps_accounts() {
        let mut ledger = InputLedger::new(None);
        ledger.press(key(0x1E));
        ledger.act(&Action::KeyPress(KeyPress {
            id: 1,
            scan_code: 0x1C,
        }));
        assert!(!ledger.is_empty());
        ledger.release_all(ReleaseReason::Stop);
        assert!(ledger.is_empty());
    }
}
