//! 真实后端逻辑的单元测试：替身 [`Win32`] 在任何平台上核对注入事件的构造、前台核对、绝对坐标、钩子
//! 分类、急停组合键与残留核验。真实的 Win32 调用、钩子线程与核验线程只在 Windows 上，由真机手动测试
//! （GS-02）核对。

#![allow(
    clippy::cast_possible_truncation,
    reason = "替身 Win32 的事件计数是小整数"
)]
#![allow(
    clippy::assert_is_empty,
    reason = "断言某次没有注入任何事件，用 is_empty 最直白"
)]

use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use core_protocol::messages::{
    Action, ActionBlock, ActionSubmitResult, Click, ControlStep, HeldInput, HeldKey,
    HeldMouseButton, KeyPress, LatestFrame, LeaseGrantParams, MouseButton, Producer,
    ProducerHeartbeatParams, RejectReason, ReleaseReason, ReleaseVerification, ResidualCause,
    Scroll, VerificationResult,
};

use super::{
    Class, Combo, Desktop, Event, HookState, PhysicalButton, RawEvent, SelfCheck, Target,
    VerifyKey, VerifyKeys, Win32, absolute, check_release, normalize_vk, physical_button,
    self_check_count, verify_residual,
};
use crate::control::{Control, Limits, Notice};
use crate::frames::{FrameStatus, Geometry};
use crate::ledger::{Inject, InputBackend, Momentary};
use crate::sendinput::SendInputBackend;

/// 替身 Win32：按脚本给出前台目标与按键状态，记下注入的事件。
#[derive(Default)]
struct FakeWin32 {
    inner: Mutex<FakeInner>,
}

#[derive(Default)]
struct FakeInner {
    /// 每次 `foreground_target` 依次返回这些；用尽后返回最后一个。
    targets: Vec<Option<Target>>,
    target_cursor: usize,
    swapped: bool,
    /// `send` 至多接受这么多事件（模拟被拦截）；`None` 表示全部接受。
    accept: Option<usize>,
    /// 读作按下的键与鼠标键。
    down_scans: Vec<u16>,
    down_buttons: Vec<PhysicalButton>,
    desktop_unavailable: bool,
    sent: Vec<Vec<Event>>,
}

impl FakeWin32 {
    fn with_target(target: Target) -> Arc<Self> {
        Arc::new(Self {
            inner: Mutex::new(FakeInner {
                targets: vec![Some(target)],
                ..FakeInner::default()
            }),
        })
    }

    fn inner(&self) -> std::sync::MutexGuard<'_, FakeInner> {
        self.inner.lock().unwrap()
    }
}

impl Win32 for FakeWin32 {
    fn foreground_target(&self) -> Option<Target> {
        let mut inner = self.inner();
        let index = inner
            .target_cursor
            .min(inner.targets.len().saturating_sub(1));
        inner.target_cursor += 1;
        inner.targets.get(index).copied().flatten()
    }

    fn virtual_desktop(&self) -> Desktop {
        Desktop {
            left: 0,
            top: 0,
            width: 1920,
            height: 1080,
        }
    }

    fn buttons_swapped(&self) -> bool {
        self.inner().swapped
    }

    fn send(&self, events: &[Event]) -> u32 {
        let mut inner = self.inner();
        inner.sent.push(events.to_vec());
        let accepted = inner
            .accept
            .map_or(events.len(), |cap| cap.min(events.len()));
        accepted as u32
    }

    fn can_verify(&self) -> bool {
        !self.inner().desktop_unavailable
    }

    fn is_down(&self, key: VerifyKey) -> Option<bool> {
        let inner = self.inner();
        Some(match key {
            VerifyKey::Scan(scan) => inner.down_scans.contains(&scan),
            VerifyKey::Mouse(button) => inner.down_buttons.contains(&button),
        })
    }
}

fn target(window: isize) -> Target {
    Target {
        window,
        client_x: 100,
        client_y: 200,
    }
}

fn backend(win32: &Arc<FakeWin32>) -> (SendInputBackend, mpsc::Receiver<super::VerifyRequest>) {
    let (tx, rx) = mpsc::channel();
    let backend = SendInputBackend::new(
        Arc::clone(win32) as Arc<dyn Win32>,
        tx,
        Arc::new(Mutex::new(VerifyKeys::default())),
    );
    (backend, rx)
}

// ---------------------------------------------------------------- 坐标与鼠标键

#[test]
fn absolute_maps_client_pixels_to_the_virtual_desktop() {
    let desktop = Desktop {
        left: 0,
        top: 0,
        width: 1920,
        height: 1080,
    };
    // 客户区原点 (100, 200)，客户区点 (860, 340) → 屏幕 (960, 540)，正中。
    let (x, y) = absolute(860, 340, target(1), desktop);
    assert!((32_750..=32_790).contains(&x), "{x}");
    assert!((32_750..=32_790).contains(&y), "{y}");
    // 负偏移的虚拟桌面：原点左移。
    let desktop = Desktop {
        left: -1920,
        top: 0,
        width: 3840,
        height: 1080,
    };
    let (x, _) = absolute(0, 0, target(1), desktop);
    // 屏幕 x = 100，归一化到 [-1920, 1920]：约 (100+1920)/3840*65535。
    assert!((34_000..=34_800).contains(&x), "{x}");
}

#[test]
fn physical_button_follows_swap() {
    assert_eq!(
        physical_button(MouseButton::Left, false),
        PhysicalButton::Left
    );
    assert_eq!(
        physical_button(MouseButton::Left, true),
        PhysicalButton::Right
    );
    assert_eq!(
        physical_button(MouseButton::Right, true),
        PhysicalButton::Left
    );
    assert_eq!(
        physical_button(MouseButton::Middle, true),
        PhysicalButton::Middle
    );
}

// ---------------------------------------------------------------- 急停组合键

#[test]
fn combo_parses_and_triggers() {
    let combo = Combo::parse("ctrl+shift+f12").unwrap();
    assert_eq!(combo, Combo::default_hotkey());
    assert!(Combo::parse("ctrl+alt+end").is_ok());
    assert!(Combo::parse("a").is_ok());
    assert!(Combo::parse("ctrl+shift").is_err(), "没有最终键");
    assert!(Combo::parse("ctrl+ctrl+a").is_err(), "修饰键重复");
    assert!(Combo::parse("ctrl+a+b").is_err(), "多个最终键");
    assert!(Combo::parse("ctrl+nope").is_err());
}

#[test]
fn regression_emergency_modifier_survives_other_side_key_up() {
    let mut state = HookState::default();
    let combo = Combo::default_hotkey();
    for event in [
        RawEvent::KeyDown { vk: 0xA2 }, // VK_LCONTROL
        RawEvent::KeyDown { vk: 0xA3 }, // VK_RCONTROL
        RawEvent::KeyDown { vk: 0xA0 }, // VK_LSHIFT
        RawEvent::KeyUp { vk: 0xA2 },
    ] {
        state.classify(event, false, false, &combo, false);
    }
    assert_eq!(
        state.classify(RawEvent::KeyDown { vk: 0x7B }, false, false, &combo, false),
        Class::Emergency
    );
}

// ---------------------------------------------------------------- 钩子分类

const MARKER_MATCH: bool = true;

#[test]
fn own_injection_is_ignored_third_party_is_takeover() {
    let mut state = HookState::default();
    let combo = Combo::default_hotkey();
    // 带注入标志、本实例标记：忽略。
    assert_eq!(
        state.classify(
            RawEvent::KeyDown { vk: b'A'.into() },
            true,
            MARKER_MATCH,
            &combo,
            true
        ),
        Class::OwnInjection
    );
    // 带注入标志、不是本实例的标记：第三方注入，接管。
    assert_eq!(
        state.classify(
            RawEvent::KeyDown { vk: b'A'.into() },
            true,
            false,
            &combo,
            true
        ),
        Class::Takeover
    );
    // 不带注入标志的真人按下：接管。
    assert_eq!(
        state.classify(RawEvent::MouseDown, false, false, &combo, true),
        Class::Takeover
    );
    assert_eq!(
        state.classify(RawEvent::Wheel, false, false, &combo, true),
        Class::Takeover
    );
}

#[test]
fn key_up_is_never_takeover() {
    let mut state = HookState::default();
    let combo = Combo::default_hotkey();
    assert_eq!(
        state.classify(
            RawEvent::KeyUp { vk: b'A'.into() },
            false,
            false,
            &combo,
            true
        ),
        Class::Ignore
    );
    assert_eq!(
        state.classify(RawEvent::MouseUp, false, false, &combo, true),
        Class::Ignore
    );
}

#[test]
fn takeover_only_while_the_lease_is_valid() {
    let mut state = HookState::default();
    let combo = Combo::default_hotkey();
    assert_eq!(
        state.classify(RawEvent::MouseDown, false, false, &combo, false),
        Class::Ignore
    );
}

#[test]
fn mouse_movement_needs_to_pass_the_threshold() {
    let mut state = HookState::default();
    let combo = Combo::default_hotkey();
    // 累计不到 8 像素不算。
    assert_eq!(
        state.classify(
            RawEvent::MouseMove { dx: 3, dy: 2 },
            false,
            false,
            &combo,
            true
        ),
        Class::Ignore
    );
    // 再来一点，越过阈值即接管。
    assert_eq!(
        state.classify(
            RawEvent::MouseMove { dx: 3, dy: 3 },
            false,
            false,
            &combo,
            true
        ),
        Class::Takeover
    );
    // 许可重新生效即重置累计。
    state.lease_became_valid();
    assert_eq!(
        state.classify(
            RawEvent::MouseMove { dx: 3, dy: 2 },
            false,
            false,
            &combo,
            true
        ),
        Class::Ignore
    );
}

#[test]
fn emergency_takes_precedence_over_takeover() {
    let mut state = HookState::default();
    let combo = Combo::default_hotkey(); // Ctrl+Shift+F12
    let ctrl = 0x11u16;
    let shift = 0x10u16;
    let f12 = 0x6F + 12;
    // 按住 Ctrl、Shift（真人，接管），再按 F12 → 急停，不论许可是否有效。
    assert_eq!(
        state.classify(RawEvent::KeyDown { vk: ctrl }, false, false, &combo, true),
        Class::Takeover
    );
    assert_eq!(
        state.classify(RawEvent::KeyDown { vk: shift }, false, false, &combo, true),
        Class::Takeover
    );
    assert_eq!(
        state.classify(RawEvent::KeyDown { vk: f12 }, false, false, &combo, false),
        Class::Emergency
    );
    // 注入的急停也算（不论标记）。
    let mut state = HookState::default();
    state.classify(RawEvent::KeyDown { vk: ctrl }, true, true, &combo, false);
    state.classify(RawEvent::KeyDown { vk: shift }, true, true, &combo, false);
    assert_eq!(
        state.classify(RawEvent::KeyDown { vk: f12 }, true, true, &combo, false),
        Class::Emergency
    );
}

#[test]
fn emergency_accepts_left_and_right_modifiers() {
    // 低层钩子报的是分左右的键码：VK_LCONTROL（0xA2）、VK_RSHIFT（0xA1）。
    let combo = Combo::default_hotkey();
    let (lctrl, rctrl, rshift, f12) = (0xA2u16, 0xA3u16, 0xA1u16, 0x6F + 12);
    let mut state = HookState::default();
    state.classify(RawEvent::KeyDown { vk: lctrl }, false, false, &combo, false);
    state.classify(
        RawEvent::KeyDown { vk: rshift },
        false,
        false,
        &combo,
        false,
    );
    assert_eq!(
        state.classify(RawEvent::KeyDown { vk: f12 }, false, false, &combo, false),
        Class::Emergency
    );
    // 抬起左 Ctrl 后再按 F12 不是急停；换右 Ctrl 按住又是。
    state.classify(RawEvent::KeyUp { vk: f12 }, false, false, &combo, false);
    state.classify(RawEvent::KeyUp { vk: lctrl }, false, false, &combo, false);
    assert_eq!(
        state.classify(RawEvent::KeyDown { vk: f12 }, false, false, &combo, false),
        Class::Ignore
    );
    state.classify(RawEvent::KeyUp { vk: f12 }, false, false, &combo, false);
    state.classify(RawEvent::KeyDown { vk: rctrl }, false, false, &combo, false);
    assert_eq!(
        state.classify(RawEvent::KeyDown { vk: f12 }, false, false, &combo, false),
        Class::Emergency
    );
    // Alt 与 Win 同样归一。
    let combo = Combo::parse("alt+win+end").unwrap();
    let mut state = HookState::default();
    state.classify(RawEvent::KeyDown { vk: 0xA5 }, false, false, &combo, false); // VK_RMENU
    state.classify(RawEvent::KeyDown { vk: 0x5C }, false, false, &combo, false); // VK_RWIN
    assert_eq!(
        state.classify(RawEvent::KeyDown { vk: 0x23 }, false, false, &combo, false),
        Class::Emergency
    );
}

#[test]
fn normalize_vk_folds_only_the_sided_modifiers() {
    assert_eq!(normalize_vk(0xA0), 0x10);
    assert_eq!(normalize_vk(0xA1), 0x10);
    assert_eq!(normalize_vk(0xA2), 0x11);
    assert_eq!(normalize_vk(0xA3), 0x11);
    assert_eq!(normalize_vk(0xA4), 0x12);
    assert_eq!(normalize_vk(0xA5), 0x12);
    assert_eq!(normalize_vk(0x5C), 0x5B);
    assert_eq!(normalize_vk(0x5B), 0x5B);
    assert_eq!(normalize_vk(0x7B), 0x7B, "F12 不变");
    assert_eq!(normalize_vk(0x57), 0x57, "W 不变");
}

#[test]
fn own_injected_ups_and_buttons_count_for_the_self_check() {
    // 本实例注入的键与鼠标键的按下与抬起、滚轮都计入自检，许可有效与否都一样，从不算接管。
    let combo = Combo::default_hotkey();
    let mut state = HookState::default();
    for event in [
        RawEvent::KeyDown { vk: 0x57 },
        RawEvent::KeyUp { vk: 0x57 },
        RawEvent::MouseDown,
        RawEvent::MouseUp,
        RawEvent::Wheel,
    ] {
        for lease_valid in [true, false] {
            assert_eq!(
                state.classify(event, true, MARKER_MATCH, &combo, lease_valid),
                Class::OwnInjection,
                "{event:?}"
            );
        }
    }
    // 本实例注入的移动不计入自检（与注入一侧一致），也不算接管。
    assert_eq!(
        state.classify(
            RawEvent::MouseMove { dx: 500, dy: 500 },
            true,
            MARKER_MATCH,
            &combo,
            true
        ),
        Class::Ignore
    );
    // 第三方注入的抬起不算接管。
    assert_eq!(
        state.classify(RawEvent::MouseUp, true, false, &combo, true),
        Class::Ignore
    );
}

#[test]
fn other_messages_never_trigger_anything() {
    let combo = Combo::default_hotkey();
    let mut state = HookState::default();
    for (injected, own) in [(false, false), (true, false), (true, true)] {
        assert_eq!(
            state.classify(RawEvent::Other, injected, own, &combo, true),
            Class::Ignore
        );
    }
}

// ---------------------------------------------------------------- 钩子自检

#[test]
fn the_self_check_counts_accepted_events_but_not_moves() {
    let click = [
        Event::MouseMoveAbs { x: 1, y: 2 },
        Event::MouseDown {
            button: PhysicalButton::Left,
        },
    ];
    assert_eq!(self_check_count(&click, 2), 1, "只计按下，不计移动");
    assert_eq!(self_check_count(&click, 1), 0, "只接受了移动");
    assert_eq!(self_check_count(&click, 0), 0, "被拦截，一个也没注入");
    let ups = [
        Event::KeyUp {
            scan_code: 0x11,
            extended: false,
        },
        Event::MouseUp {
            button: PhysicalButton::Left,
        },
        Event::Wheel { notches: 1 },
    ];
    assert_eq!(self_check_count(&ups, 3), 3);
    assert_eq!(self_check_count(&ups, 2), 2);
}

#[test]
fn the_self_check_reports_missing_events_once() {
    let mut check = SelfCheck::default();
    // 第一轮：之前没有注入。
    assert!(!check.tick(2, 0));
    // 第二轮：上一轮时的 2 个都经过了钩子，又注入了 1 个。
    assert!(!check.tick(3, 2));
    // 第三轮：上一轮时的 3 个只看到 2 个：判失效。
    assert!(check.tick(3, 2));
    // 同一批没有经过的只报一次。
    assert!(!check.tick(3, 2));
    assert!(!check.tick(5, 2));
    // 之后注入的 2 个照常经过：不报。
    assert!(!check.tick(5, 4));
    // 之后注入的又有没经过的：再报。
    assert!(!check.tick(6, 4));
    assert!(check.tick(6, 4));
}

// ---------------------------------------------------------------- 注入

fn only_event(win32: &FakeWin32, index: usize) -> Vec<Event> {
    win32.inner().sent[index].clone()
}

#[test]
fn pressing_a_key_injects_a_scan_code_down() {
    let win32 = FakeWin32::with_target(target(1));
    let (mut backend, _rx) = backend(&win32);
    let result = backend.press(&HeldInput::Key(HeldKey { scan_code: 0x11 }));
    assert_eq!(result, Inject::Done);
    assert_eq!(
        only_event(&win32, 0),
        [Event::KeyDown {
            scan_code: 0x11,
            extended: false
        }]
    );
    // 扩展键带标志。
    backend.press(&HeldInput::Key(HeldKey { scan_code: 0xE04B }));
    assert_eq!(
        only_event(&win32, 1),
        [Event::KeyDown {
            scan_code: 0x4B,
            extended: true
        }]
    );
}

#[test]
fn a_click_moves_then_presses_and_lifts() {
    let win32 = FakeWin32::with_target(target(1));
    let (mut backend, _rx) = backend(&win32);
    let momentary = Momentary::Click {
        x: 860,
        y: 340,
        button: MouseButton::Left,
    };
    assert_eq!(backend.begin_momentary(&momentary), Inject::Done);
    let events = only_event(&win32, 0);
    assert_eq!(events.len(), 2);
    assert!(matches!(events[0], Event::MouseMoveAbs { .. }));
    assert_eq!(
        events[1],
        Event::MouseDown {
            button: PhysicalButton::Left
        }
    );
    backend.end_momentary(&momentary);
    assert_eq!(
        only_event(&win32, 1),
        [Event::MouseUp {
            button: PhysicalButton::Left
        }]
    );
}

#[test]
fn focus_lost_before_injection_sends_nothing() {
    let win32 = Arc::new(FakeWin32 {
        inner: Mutex::new(FakeInner {
            targets: vec![None],
            ..FakeInner::default()
        }),
    });
    let (mut backend, _rx) = backend(&win32);
    assert_eq!(
        backend.press(&HeldInput::Key(HeldKey { scan_code: 0x11 })),
        Inject::FocusLost
    );
    assert!(win32.inner().sent.is_empty());
}

#[test]
fn focus_changed_after_injection_is_uncertain() {
    // 注入前是窗口 1，注入后变成窗口 2。
    let win32 = Arc::new(FakeWin32 {
        inner: Mutex::new(FakeInner {
            targets: vec![Some(target(1)), Some(target(2))],
            ..FakeInner::default()
        }),
    });
    let (mut backend, _rx) = backend(&win32);
    assert_eq!(
        backend.press(&HeldInput::Key(HeldKey { scan_code: 0x11 })),
        Inject::FocusChanged
    );
}

#[test]
fn a_blocked_injection_reports_blocked() {
    let win32 = Arc::new(FakeWin32 {
        inner: Mutex::new(FakeInner {
            targets: vec![Some(target(1))],
            accept: Some(0), // 一个都不接受
            ..FakeInner::default()
        }),
    });
    let (mut backend, _rx) = backend(&win32);
    assert_eq!(
        backend.press(&HeldInput::Key(HeldKey { scan_code: 0x11 })),
        Inject::Blocked
    );
}

#[test]
fn scroll_injects_a_move_and_a_wheel() {
    let win32 = FakeWin32::with_target(target(1));
    let (mut backend, _rx) = backend(&win32);
    let action = Action::Scroll(Scroll {
        id: 1,
        x: 10,
        y: 20,
        notches: -2,
    });
    assert_eq!(backend.act(&action), Inject::Done);
    let events = only_event(&win32, 0);
    assert!(matches!(events[0], Event::MouseMoveAbs { .. }));
    assert_eq!(events[1], Event::Wheel { notches: -2 });
    // 点击与短按在真实后端不走 act（走瞬时按住）。
    assert_eq!(
        backend.act(&Action::Click(Click {
            id: 2,
            x: 0,
            y: 0,
            button: MouseButton::Left
        })),
        Inject::Done
    );
    assert_eq!(
        backend.act(&Action::KeyPress(KeyPress {
            id: 3,
            scan_code: 0x1C
        })),
        Inject::Done
    );
    // 只注入了滚动那一次。
    assert_eq!(win32.inner().sent.len(), 1);
}

// ---------------------------------------------------------------- 核验与请求

#[test]
fn verify_resends_ups_for_still_held_keys() {
    let win32 = Arc::new(FakeWin32 {
        inner: Mutex::new(FakeInner {
            down_scans: vec![0x11],
            down_buttons: vec![PhysicalButton::Left],
            ..FakeInner::default()
        }),
    });
    let (items, repair_accepted) =
        verify_residual(win32.as_ref(), &[0x11, 0x1C], &[MouseButton::Left]).unwrap();
    // 只有读作按下的两项（0x1C 没按下不补）。
    assert_eq!(items.len(), 2);
    assert!(repair_accepted);
    assert!(items.contains(&HeldInput::Key(HeldKey { scan_code: 0x11 })));
    assert!(items.contains(&HeldInput::MouseButton(HeldMouseButton {
        button: MouseButton::Left
    })));
    // 补发了抬起。
    let sent = &win32.inner().sent;
    assert_eq!(sent.len(), 1);
    assert!(sent[0].contains(&Event::KeyUp {
        scan_code: 0x11,
        extended: false
    }));
    assert!(sent[0].contains(&Event::MouseUp {
        button: PhysicalButton::Left
    }));
}

#[test]
fn regression_verify_queries_the_physical_button_when_swapped() {
    let win32 = FakeWin32 {
        inner: Mutex::new(FakeInner {
            swapped: true,
            down_buttons: vec![PhysicalButton::Right],
            ..FakeInner::default()
        }),
    };
    let (items, repair_accepted) = verify_residual(&win32, &[], &[MouseButton::Left]).unwrap();
    assert!(repair_accepted);
    assert_eq!(
        items,
        [HeldInput::MouseButton(HeldMouseButton {
            button: MouseButton::Left
        })]
    );
    assert_eq!(
        win32.inner().sent,
        [vec![Event::MouseUp {
            button: PhysicalButton::Right
        }]]
    );
}

#[test]
fn verify_sends_nothing_when_nothing_is_held() {
    let win32 = Arc::new(FakeWin32::default());
    let (items, _) = verify_residual(win32.as_ref(), &[0x11], &[MouseButton::Left]).unwrap();
    assert!(items.is_empty());
    assert!(win32.inner().sent.is_empty());
}

#[test]
fn verify_reports_a_rejected_repair() {
    let win32 = FakeWin32 {
        inner: Mutex::new(FakeInner {
            accept: Some(0),
            down_scans: vec![0x11],
            ..FakeInner::default()
        }),
    };
    let (items, repair_accepted) = verify_residual(&win32, &[0x11], &[]).unwrap();
    assert_eq!(items, [HeldInput::Key(HeldKey { scan_code: 0x11 })]);
    assert!(!repair_accepted);
}

#[test]
fn regression_rejected_key_up_is_reported() {
    let win32 = FakeWin32::with_target(target(1));
    let (backend, _verify) = backend(&win32);
    let frames = Arc::new(FrameStatus::default());
    frames.record(
        LatestFrame {
            frame_number: 1,
            capture_ns: 100,
        },
        Geometry {
            width: 320,
            height: 180,
            dpi: 96,
        },
        None,
    );
    let control = Control::new(
        Limits {
            heartbeat_timeout: Duration::from_secs(1),
            max_frame_age: Duration::from_secs(1),
        },
        frames,
        Some(Box::new(backend)),
        false,
    );
    let producer = Producer {
        id: "fake-win32".into(),
        generation: 1,
    };
    control
        .grant_lease(
            &LeaseGrantParams {
                token: 1,
                expires_ns: 500,
            },
            100,
        )
        .0
        .unwrap();
    control
        .heartbeat(
            &ProducerHeartbeatParams {
                producer: producer.clone(),
                seq: 1,
            },
            100,
        )
        .unwrap();
    let block = ActionBlock {
        producer,
        seq: 1,
        base_frame: 1,
        start_ns: 100,
        deadline_ns: 400,
        steps: vec![ControlStep {
            offset_ns: 0,
            held: vec![HeldInput::Key(HeldKey { scan_code: 0x11 })],
            actions: Vec::new(),
        }],
    };
    assert!(matches!(
        control.submit(block, 100).0,
        ActionSubmitResult::Accepted(_)
    ));
    control.tick(100);
    win32.inner().accept = Some(0);

    let (release_id, notices) = control.revoke(1);
    assert_eq!(
        win32.inner().sent,
        [
            vec![Event::KeyDown {
                scan_code: 0x11,
                extended: false
            }],
            vec![Event::KeyUp {
                scan_code: 0x11,
                extended: false
            }],
        ]
    );
    let [Notice::Released(released)] = notices.0.as_slice() else {
        panic!("expected one release, got {notices:?}");
    };
    assert_eq!(release_id, Some(released.release_id));
    assert_eq!(
        serde_json::to_value(released).unwrap()["verification"],
        "up_failed"
    );
    assert!(
        control
            .grant_lease(
                &LeaseGrantParams {
                    token: 2,
                    expires_ns: 600
                },
                200
            )
            .0
            .is_err(),
        "the same instance cannot accept a new lease after a rejected up"
    );
}

fn verification_policy() -> core_protocol::messages::InputPolicy {
    core_protocol::messages::InputPolicy {
        click_buttons: vec![MouseButton::Left],
        press_keys: vec![0x1C],
        scroll: false,
        click_regions: vec![core_protocol::messages::NormRect {
            left: 0.0,
            top: 0.0,
            right: 1.0,
            bottom: 1.0,
        }],
        forbidden_zones: vec![],
        press_duration_ms: 50,
        max_rate: 1.0,
        hold_keys: vec![0x11],
        hold_buttons: vec![],
        max_hold_ms: Some(1000),
    }
}

fn verifying_control(
    win32: &Arc<FakeWin32>,
) -> (
    Control,
    Arc<Mutex<VerifyKeys>>,
    mpsc::Receiver<super::VerifyRequest>,
) {
    let (tx, rx) = mpsc::channel();
    let keys = Arc::new(Mutex::new(VerifyKeys::default()));
    let backend = SendInputBackend::new(Arc::clone(win32) as Arc<dyn Win32>, tx, Arc::clone(&keys));
    let frames = Arc::new(FrameStatus::default());
    frames.record(
        LatestFrame {
            frame_number: 1,
            capture_ns: 100,
        },
        Geometry {
            width: 320,
            height: 180,
            dpi: 96,
        },
        None,
    );
    let control = Control::new(
        Limits {
            heartbeat_timeout: Duration::from_secs(1),
            max_frame_age: Duration::from_secs(1),
        },
        frames,
        Some(Box::new(backend)),
        true,
    );
    (control, keys, rx)
}

#[test]
fn an_old_check_never_opens_a_new_release_gate() {
    let win32 = FakeWin32::with_target(target(1));
    let (control, keys, rx) = verifying_control(&win32);
    let startup = control.release_on_start();
    assert_eq!(startup.verification, ReleaseVerification::Unverified);
    assert!(rx.try_recv().is_err());
    control.configure(&verification_policy()).unwrap();
    let old = rx.try_recv().unwrap();
    assert_eq!(old.release_id, startup.release_id);
    assert_eq!(old.cause, ResidualCause::CoreStart);
    control.local_revoke(ReleaseReason::Takeover);
    let new = rx.try_recv().unwrap();
    let keys = keys.lock().unwrap().clone();
    let observe = |request| {
        check_release(
            &control,
            win32.as_ref(),
            &keys,
            request,
            |_| unreachable!(),
            || unreachable!(),
        )
    };
    let old_result = observe(old);
    assert_eq!(old_result.result, VerificationResult::Superseded);
    control.verification_posted(old_result.release_id);

    let lease = LeaseGrantParams {
        token: 1,
        expires_ns: 600,
    };
    control.grant_lease(&lease, 100).0.unwrap();
    let producer = Producer {
        id: "fake-win32".into(),
        generation: 1,
    };
    control
        .heartbeat(
            &ProducerHeartbeatParams {
                producer: producer.clone(),
                seq: 1,
            },
            100,
        )
        .unwrap();
    let inputs = ActionBlock {
        producer,
        seq: 1,
        base_frame: 1,
        start_ns: 100,
        deadline_ns: 400,
        steps: vec![ControlStep {
            offset_ns: 0,
            held: vec![HeldInput::Key(HeldKey { scan_code: 0x11 })],
            actions: vec![],
        }],
    };
    assert!(matches!(control.submit(inputs.clone(), 100).0,
        ActionSubmitResult::Rejected(r) if r.reason == RejectReason::NotAllowed));
    let new_result = observe(new);
    assert_eq!(new_result.result, VerificationResult::Verified);
    assert!(matches!(
        control.submit(inputs.clone(), 100).0,
        ActionSubmitResult::Rejected(_)
    ));
    control.verification_posted(new_result.release_id);
    assert!(matches!(
        control.submit(inputs, 100).0,
        ActionSubmitResult::Accepted(_)
    ));
    control.tick(100);
    assert_eq!(win32.inner().sent.len(), 1);
    assert_eq!(observe(old).result, VerificationResult::Superseded);
}

#[test]
#[allow(
    clippy::too_many_lines,
    reason = "按顺序核对提交、心跳、许可替换共享的核验门禁"
)]
fn regression_a_replacement_producer_cannot_press_before_release_verification() {
    let win32 = FakeWin32::with_target(target(1));
    let (control, keys, rx) = verifying_control(&win32);
    control.release_on_start();
    control.configure(&verification_policy()).unwrap();
    let keys = keys.lock().unwrap().clone();
    let check = |request| {
        let checked = check_release(
            &control,
            win32.as_ref(),
            &keys,
            request,
            |_| unreachable!(),
            || unreachable!(),
        );
        assert_eq!(checked.result, VerificationResult::Verified);
        control.verification_posted(checked.release_id);
    };
    check(rx.try_recv().unwrap());
    let lease = LeaseGrantParams {
        token: 1,
        expires_ns: 600,
    };
    control.grant_lease(&lease, 100).0.unwrap();
    let old = Producer {
        id: "old".into(),
        generation: 1,
    };
    control
        .heartbeat(
            &ProducerHeartbeatParams {
                producer: old,
                seq: 1,
            },
            100,
        )
        .unwrap();
    let new = Producer {
        id: "new".into(),
        generation: 2,
    };
    let block = ActionBlock {
        producer: new.clone(),
        seq: 1,
        base_frame: 1,
        start_ns: 200,
        deadline_ns: 400,
        steps: vec![ControlStep {
            offset_ns: 0,
            held: vec![HeldInput::Key(HeldKey { scan_code: 0x11 })],
            actions: vec![],
        }],
    };
    let (result, notices) = control.submit(block.clone(), 200);
    assert!(matches!(result, ActionSubmitResult::Accepted(_)));
    assert_eq!(notices.released(), [ReleaseReason::ProducerChanged]);
    // 新代次在检查前也有心跳，确保不靠心跳缺失挡下它。
    control
        .heartbeat(
            &ProducerHeartbeatParams {
                producer: new,
                seq: 1,
            },
            200,
        )
        .unwrap();
    control.tick(200);
    assert!(
        win32.inner().sent.is_empty(),
        "new inputs must wait for this release check"
    );
    check(rx.try_recv().unwrap());
    control.tick(201);
    assert_eq!(win32.inner().sent.len(), 1);

    let next = Producer {
        id: "next".into(),
        generation: 3,
    };
    let heartbeat_release = control
        .heartbeat(
            &ProducerHeartbeatParams {
                producer: next.clone(),
                seq: 1,
            },
            202,
        )
        .unwrap();
    assert_eq!(
        heartbeat_release.released(),
        [ReleaseReason::ProducerChanged]
    );
    let next_block = ActionBlock {
        producer: next,
        ..block
    };
    assert!(matches!(
        control.submit(next_block.clone(), 202).0,
        ActionSubmitResult::Rejected(_)
    ));

    let (grant, lease_release) = control.grant_lease(
        &LeaseGrantParams {
            token: 2,
            expires_ns: 700,
        },
        203,
    );
    grant.unwrap();
    assert_eq!(lease_release.released(), [ReleaseReason::LeaseRevoked]);
    assert!(matches!(
        control.submit(next_block.clone(), 203).0,
        ActionSubmitResult::Rejected(_)
    ));
    control.tick(203);
    assert_eq!(
        win32.inner().sent.len(),
        2,
        "only the previous key up may follow"
    );

    let heartbeat_check = check_release(
        &control,
        win32.as_ref(),
        &keys,
        rx.try_recv().unwrap(),
        |_| unreachable!(),
        || unreachable!(),
    );
    assert_eq!(heartbeat_check.result, VerificationResult::Superseded);
    control.verification_posted(heartbeat_check.release_id);
    assert!(matches!(
        control.submit(next_block.clone(), 203).0,
        ActionSubmitResult::Rejected(_)
    ));
    check(rx.try_recv().unwrap());
    assert!(matches!(
        control.submit(next_block, 203).0,
        ActionSubmitResult::Accepted(_)
    ));
    control.tick(203);
    assert_eq!(win32.inner().sent.len(), 3);
}

#[test]
fn a_rejected_residual_repair_or_unreadable_desktop_blocks_new_leases() {
    for (unavailable, expected) in [
        (false, VerificationResult::Residual),
        (true, VerificationResult::Unavailable),
    ] {
        let win32 = FakeWin32::with_target(target(1));
        let (control, keys, rx) = verifying_control(&win32);
        control.release_on_start();
        control.configure(&verification_policy()).unwrap();
        let request = rx.try_recv().unwrap();
        {
            let mut state = win32.inner();
            state.desktop_unavailable = unavailable;
            state.down_scans = vec![0x11];
            state.accept = Some(0);
        }
        let mut residual = None;
        let checked = check_release(
            &control,
            win32.as_ref(),
            &keys.lock().unwrap(),
            request,
            |report| residual = Some(report),
            || unreachable!(),
        );
        assert_eq!(checked.release_id, request.release_id);
        assert_eq!(checked.result, expected);
        if unavailable {
            assert!(residual.is_none());
            assert!(win32.inner().sent.is_empty());
        } else {
            let residual = residual.unwrap();
            assert_eq!(residual.release_id, request.release_id);
            assert!(!residual.repair_accepted);
        }
        control.verification_posted(checked.release_id);
        assert!(
            control
                .grant_lease(
                    &LeaseGrantParams {
                        token: 2,
                        expires_ns: 600
                    },
                    200
                )
                .0
                .is_err()
        );
    }
}

#[test]
fn released_all_and_configured_request_verification() {
    let win32 = FakeWin32::with_target(target(1));
    let (mut backend, rx) = backend(&win32);
    backend.released_all(ReleaseReason::CoreStart, 1);
    assert!(rx.try_recv().is_err());
    backend.released_all(ReleaseReason::EmergencyStop, 2);
    let request = rx.try_recv().unwrap();
    assert_eq!(request.cause, ResidualCause::Release);
    assert_eq!(request.release_id, 2);
    assert_eq!(request.reason, Some(ReleaseReason::EmergencyStop));
    backend.configured(&verification_policy());
    let request = rx.try_recv().unwrap();
    assert_eq!(request.cause, ResidualCause::CoreStart);
    assert_eq!(request.release_id, 1);
    assert_eq!(request.reason, None);
    let _ = Arc::clone(&win32);
}

// ---------------------------------------------------------------- 采集目标

#[test]
fn the_real_backend_refuses_a_fake_display_target() {
    use core_protocol::messages::{CaptureTarget, FakeTarget, FrameRingAttachParams, WindowTarget};

    let fake = CaptureTarget::Fake(FakeTarget {
        surface: FrameRingAttachParams {
            name: "x".into(),
            size_bytes: 0,
            layout_version: 1,
            slot_count: 2,
            slot_stride: 0,
            max_width: 1,
            max_height: 1,
        },
    });
    let window = CaptureTarget::Window(WindowTarget {
        process_name: "game.exe".into(),
        window_class: None,
        title: Some("Game".into()),
    });
    // 真实后端：假显示面没有可注入的窗口（回 -32003），窗口目标照常。
    let real = super::TargetWindow::new(true, Arc::default());
    assert!(!real.accepts(&fake));
    assert!(real.accepts(&window));
    // 没有真实后端时两种都接受。
    let other = super::TargetWindow::new(false, Arc::default());
    assert!(other.accepts(&fake));
    assert!(other.accepts(&window));
}
