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

use core_protocol::messages::{
    Action, Click, HeldInput, HeldKey, HeldMouseButton, KeyPress, MouseButton, ReleaseReason,
    ResidualCause, Scroll,
};

use super::{
    Class, Combo, Desktop, Event, HookState, PhysicalButton, RawEvent, SelfCheck, Target,
    VerifyKey, VerifyKeys, Win32, absolute, normalize_vk, physical_button, self_check_count,
    verify_residual,
};
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
    down_buttons: Vec<MouseButton>,
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

    fn is_down(&self, key: VerifyKey) -> bool {
        let inner = self.inner();
        match key {
            VerifyKey::Scan(scan) => inner.down_scans.contains(&scan),
            VerifyKey::Mouse(button) => inner.down_buttons.contains(&button),
        }
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
            down_buttons: vec![MouseButton::Left],
            ..FakeInner::default()
        }),
    });
    let items = verify_residual(win32.as_ref(), &[0x11, 0x1C], &[MouseButton::Left]);
    // 只有读作按下的两项（0x1C 没按下不补）。
    assert_eq!(items.len(), 2);
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
fn verify_sends_nothing_when_nothing_is_held() {
    let win32 = Arc::new(FakeWin32::default());
    let items = verify_residual(win32.as_ref(), &[0x11], &[MouseButton::Left]);
    assert!(items.is_empty());
    assert!(win32.inner().sent.is_empty());
}

#[test]
fn released_all_and_configured_request_verification() {
    let win32 = FakeWin32::with_target(target(1));
    let (mut backend, rx) = backend(&win32);
    backend.released_all(ReleaseReason::EmergencyStop);
    let request = rx.try_recv().unwrap();
    assert_eq!(request.cause, ResidualCause::Release);
    assert_eq!(request.reason, Some(ReleaseReason::EmergencyStop));
    backend.configured(&core_protocol::messages::InputPolicy {
        click_buttons: vec![MouseButton::Left],
        press_keys: vec![0x1C],
        scroll: false,
        click_regions: vec![],
        forbidden_zones: vec![],
        press_duration_ms: 50,
        max_rate: 1.0,
        hold_keys: vec![0x11],
        hold_buttons: vec![],
        max_hold_ms: Some(1000),
    });
    let request = rx.try_recv().unwrap();
    assert_eq!(request.cause, ResidualCause::CoreStart);
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
    let real = super::TargetWindow::new(true);
    assert!(!real.accepts(&fake));
    assert!(real.accepts(&window));
    // 没有真实后端时两种都接受。
    let other = super::TargetWindow::new(false);
    assert!(other.accepts(&fake));
    assert!(other.accepts(&window));
}
