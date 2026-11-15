//! 控制状态的规则：控制产出者与心跳、动作块的校验与调度、权威检查、持续状态、许可与释放。
//!
//! 时刻由测试注入（`tick`、`submit` 等的 `now`）；交给输入后端的输入由记录后端（`ledger::tests::Recorder`）
//! 记下。只有 `run_scheduler` 的测试读真实的时钟。

use super::{Control, Limits, Notices, Rejection, check_block};
use crate::frames::{FrameStatus, Geometry};
use crate::ledger::tests::{Recorder, axis, key};
use core_protocol::messages::{
    Action, ActionBlock, ActionOutcome, ActionSubmitResult, Click, ControlStep, HeldInput,
    HeldMouseButton, KeyPress, LatestFrame, LeaseGrantParams, LeaseStatus, MouseButton, Producer,
    ProducerHeartbeatParams, RejectReason, ReleaseReason, Scroll,
};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

const MS: u64 = 1_000_000;
/// 测试里的“现在”的起点；数值任意，只要与帧的采集时刻同一刻度。
const T0: u64 = 1_000_000 * MS;
const HEARTBEAT_TIMEOUT: u64 = 500 * MS;
const MAX_FRAME_AGE: u64 = 100 * MS;
const GEOMETRY: Geometry = Geometry {
    width: 320,
    height: 180,
    dpi: 96,
};

fn limits() -> Limits {
    Limits {
        heartbeat_timeout: Duration::from_nanos(HEARTBEAT_TIMEOUT),
        max_frame_age: Duration::from_nanos(MAX_FRAME_AGE),
    }
}

fn producer(id: &str, generation: u64) -> Producer {
    Producer {
        id: id.to_owned(),
        generation,
    }
}

fn beat(id: &str, generation: u64, seq: u64) -> ProducerHeartbeatParams {
    ProducerHeartbeatParams {
        producer: producer(id, generation),
        seq,
    }
}

fn step(offset_ms: u64, held: Vec<HeldInput>, actions: Vec<Action>) -> ControlStep {
    ControlStep {
        offset_ns: offset_ms * MS,
        held,
        actions,
    }
}

fn click(id: u64) -> Action {
    Action::Click(Click {
        id,
        x: 10,
        y: 20,
        button: MouseButton::Left,
    })
}

fn press(id: u64, scan_code: u16) -> Action {
    Action::KeyPress(KeyPress { id, scan_code })
}

fn mouse(button: MouseButton) -> HeldInput {
    HeldInput::MouseButton(HeldMouseButton { button })
}

/// 产出者 a、代次 1 的动作块，基准帧 1，从 `start` 起、`valid_ms` 毫秒内有效。
fn block(seq: u64, start: u64, valid_ms: u64, steps: Vec<ControlStep>) -> ActionBlock {
    ActionBlock {
        producer: producer("a", 1),
        seq,
        base_frame: 1,
        start_ns: start,
        deadline_ns: start + valid_ms * MS,
        steps,
    }
}

fn released(notices: &Notices) -> Vec<ReleaseReason> {
    notices.released()
}

/// 回执的内容：每条为（块序号，["executed 1"、"dropped 2 Superseded" ……]）。
fn receipts(notices: &Notices) -> Vec<(u64, Vec<String>)> {
    notices
        .receipts()
        .into_iter()
        .map(|receipt| {
            let outcomes = receipt
                .outcomes
                .iter()
                .map(|outcome| match outcome {
                    ActionOutcome::Executed(executed) => format!("executed {}", executed.id),
                    ActionOutcome::Dropped(dropped) => {
                        format!("dropped {} {:?}", dropped.id, dropped.reason)
                    }
                    ActionOutcome::Uncertain(uncertain) => format!("uncertain {}", uncertain.id),
                })
                .collect();
            (receipt.block_seq, outcomes)
        })
        .collect()
}

fn accepted(result: &ActionSubmitResult) -> bool {
    matches!(result, ActionSubmitResult::Accepted(_))
}

fn rejection(result: &ActionSubmitResult) -> Option<RejectReason> {
    match result {
        ActionSubmitResult::Rejected(rejected) => Some(rejected.reason),
        ActionSubmitResult::Accepted(_) => None,
    }
}

/// 三个条件都成立的控制状态：许可有效到 T0+10s，产出者 a 代次 1 在 T0 有心跳，T0 采到第 1 帧。
struct Armed {
    control: Control,
    frames: Arc<FrameStatus>,
    backend: Recorder,
}

impl Armed {
    fn new() -> Self {
        let frames = Arc::new(FrameStatus::default());
        let backend = Recorder::default();
        let control = Control::new(limits(), Arc::clone(&frames), backend.backend(), false);
        let armed = Self {
            control,
            frames,
            backend,
        };
        armed.frame(1, T0, GEOMETRY);
        armed.grant(1, T0 + 10_000 * MS, T0);
        assert!(
            armed
                .control
                .heartbeat(&beat("a", 1, 1), T0)
                .unwrap()
                .is_empty()
        );
        armed.backend.take();
        armed
    }

    fn frame(&self, frame_number: u64, capture_ns: u64, geometry: Geometry) {
        self.frames.record(
            LatestFrame {
                frame_number,
                capture_ns,
            },
            geometry,
            None,
        );
    }

    fn grant(&self, token: u64, expires_ns: u64, now: u64) -> Notices {
        let (result, notices) = self
            .control
            .grant_lease(&LeaseGrantParams { token, expires_ns }, now);
        result.unwrap();
        notices
    }

    fn submit(&self, block: ActionBlock, now: u64) -> (ActionSubmitResult, Notices) {
        self.control.submit(block, now)
    }

    fn inputs(&self) -> Vec<String> {
        self.backend.take()
    }
}

// ---------------------------------------------------------------- 控制产出者与心跳

#[test]
fn start_releases_without_a_producer() {
    let backend = Recorder::default();
    let control = Control::new(limits(), Arc::default(), backend.backend(), false);
    let released = control.release_on_start();
    assert_eq!(released.reason, ReleaseReason::CoreStart);
    assert_eq!(released.producer, None);
    assert!(released.released_at_ns > 0);
    assert_eq!(backend.take(), ["release all CoreStart"]);
    let health = control.health();
    assert!(!health.inputs_held);
    assert_eq!(health.producer, None);
    assert_eq!(health.lease, LeaseStatus::NotGranted);
}

#[test]
fn first_heartbeat_sets_the_producer_without_releasing() {
    let control = Control::new(limits(), Arc::default(), None, false);
    assert!(control.heartbeat(&beat("a", 1, 7), T0).unwrap().is_empty());
    assert_eq!(control.health().producer, Some(producer("a", 1)));
}

#[test]
fn seq_must_increase_within_a_generation() {
    let control = Control::new(limits(), Arc::default(), None, false);
    control.heartbeat(&beat("a", 1, 5), T0).unwrap();
    for stale in [5, 4, 0] {
        assert_eq!(
            control
                .heartbeat(&beat("a", 1, stale), T0 + 400 * MS)
                .unwrap_err(),
            Rejection::StaleSeq
        );
    }
    // 不算数的心跳不刷新截止时刻：仍按 seq=5 那次的时刻超时。
    assert!(control.tick(T0 + HEARTBEAT_TIMEOUT - 1).is_empty());
    assert_eq!(
        released(&control.tick(T0 + HEARTBEAT_TIMEOUT)),
        [ReleaseReason::HeartbeatTimeout]
    );
    // 递增的心跳算数，并重新开始计时。
    let t1 = T0 + 1000 * MS;
    assert!(control.heartbeat(&beat("a", 1, 6), t1).unwrap().is_empty());
    assert!(control.tick(t1 + HEARTBEAT_TIMEOUT / 2).is_empty());
}

#[test]
fn heartbeat_timeout_releases_once_and_stands_by() {
    let control = Control::new(limits(), Arc::default(), None, false);
    control.heartbeat(&beat("a", 3, 1), T0).unwrap();
    let notices = control.tick(T0 + HEARTBEAT_TIMEOUT);
    assert_eq!(released(&notices), [ReleaseReason::HeartbeatTimeout]);
    // 待命：不再重复释放；产出者仍是当前的，恢复心跳后重新计时。
    assert!(control.tick(T0 + 10 * HEARTBEAT_TIMEOUT).is_empty());
    assert_eq!(control.health().producer, Some(producer("a", 3)));
    let t1 = T0 + 20 * HEARTBEAT_TIMEOUT;
    control.heartbeat(&beat("a", 3, 2), t1).unwrap();
    assert_eq!(
        released(&control.tick(t1 + HEARTBEAT_TIMEOUT)),
        [ReleaseReason::HeartbeatTimeout]
    );
}

#[test]
fn old_generations_and_other_identities_are_rejected() {
    let control = Control::new(limits(), Arc::default(), None, false);
    control.heartbeat(&beat("a", 2, 1), T0).unwrap();
    assert_eq!(
        control.heartbeat(&beat("a", 1, 9), T0).unwrap_err(),
        Rejection::StaleGeneration
    );
    assert_eq!(
        control.heartbeat(&beat("b", 2, 9), T0).unwrap_err(),
        Rejection::ProducerMismatch
    );
    assert_eq!(control.health().producer, Some(producer("a", 2)));
}

#[test]
fn higher_generation_releases_before_switching() {
    let control = Control::new(limits(), Arc::default(), None, false);
    control.heartbeat(&beat("a", 1, 10), T0).unwrap();
    let notices = control.heartbeat(&beat("b", 2, 1), T0).unwrap();
    assert_eq!(released(&notices), [ReleaseReason::ProducerChanged]);
    assert_eq!(control.health().producer, Some(producer("b", 2)));
    // 新代次的 seq 从它自己的第一次心跳算起。
    assert!(control.heartbeat(&beat("b", 2, 2), T0).unwrap().is_empty());
}

#[test]
fn revoke_releases_the_current_generation_and_blocks_it() {
    let control = Control::new(limits(), Arc::default(), None, false);
    control.heartbeat(&beat("a", 4, 1), T0).unwrap();
    // 作废更早的代次不影响当前产出者。
    assert!(control.revoke(3).1.is_empty());
    assert_eq!(control.health().producer, Some(producer("a", 4)));
    assert_eq!(
        control.heartbeat(&beat("a", 3, 1), T0).unwrap_err(),
        Rejection::Revoked
    );
    assert_eq!(
        released(&control.revoke(4).1),
        [ReleaseReason::GenerationRevoked]
    );
    assert_eq!(control.health().producer, None);
    assert_eq!(
        control.heartbeat(&beat("a", 4, 2), T0).unwrap_err(),
        Rejection::Revoked
    );
    // 已作废的最高代次不回退。
    assert!(control.revoke(1).1.is_empty());
    assert_eq!(
        control.heartbeat(&beat("a", 4, 3), T0).unwrap_err(),
        Rejection::Revoked
    );
    // 更新的代次照常接受，没有当前产出者时不需要释放。
    assert!(control.heartbeat(&beat("b", 5, 1), T0).unwrap().is_empty());
    // 作废未来的代次也覆盖当前代次。
    assert_eq!(
        released(&control.revoke(9).1),
        [ReleaseReason::GenerationRevoked]
    );
    assert_eq!(
        control.heartbeat(&beat("c", 8, 1), T0).unwrap_err(),
        Rejection::Revoked
    );
}

#[test]
fn stop_releases_and_refuses_producers_and_blocks() {
    let armed = Armed::new();
    let notices = armed.control.stop();
    assert_eq!(released(&notices), [ReleaseReason::Stop]);
    assert_eq!(armed.inputs(), ["release all Stop"]);
    assert_eq!(
        armed.control.heartbeat(&beat("a", 2, 1), T0).unwrap_err(),
        Rejection::Stopping
    );
    let (result, _) = armed.submit(block(1, T0, 100, vec![step(0, vec![], vec![click(1)])]), T0);
    assert_eq!(rejection(&result), Some(RejectReason::Stopping));
    assert!(armed.control.wait_stopping(Duration::ZERO));
}

// ---------------------------------------------------------------- 动作块的校验

#[test]
fn every_block_rule_is_checked() {
    let ok = block(1, T0, 100, vec![step(0, vec![key(0x11)], vec![click(1)])]);
    assert_eq!(check_block(&ok), Ok(()));
    let cases: Vec<(&str, ActionBlock)> = vec![
        ("no steps", block(1, T0, 100, vec![])),
        (
            "offsets go backwards",
            block(
                1,
                T0,
                100,
                vec![step(20, vec![], vec![]), step(10, vec![], vec![])],
            ),
        ),
        (
            "a step after the deadline",
            block(1, T0, 100, vec![step(101, vec![], vec![])]),
        ),
        (
            "start plus offset overflows",
            ActionBlock {
                start_ns: u64::MAX,
                deadline_ns: u64::MAX,
                ..block(1, T0, 100, vec![step(1, vec![], vec![])])
            },
        ),
        (
            "a key held twice",
            block(
                1,
                T0,
                100,
                vec![step(0, vec![key(0x11), key(0x11)], vec![])],
            ),
        ),
        (
            "a mouse button held twice",
            block(
                1,
                T0,
                100,
                vec![step(
                    0,
                    vec![mouse(MouseButton::Left), mouse(MouseButton::Left)],
                    vec![],
                )],
            ),
        ),
        (
            "an axis held twice with different values",
            block(
                1,
                T0,
                100,
                vec![step(0, vec![axis("yaw", 0.1), axis("yaw", 0.2)], vec![])],
            ),
        ),
        (
            "an axis value outside [-1, 1]",
            block(1, T0, 100, vec![step(0, vec![axis("yaw", 1.5)], vec![])]),
        ),
        (
            "an action id repeated within the block",
            block(
                1,
                T0,
                100,
                vec![
                    step(0, vec![], vec![click(1)]),
                    step(10, vec![], vec![press(1, 0x1C)]),
                ],
            ),
        ),
    ];
    for (name, bad) in cases {
        assert!(check_block(&bad).is_err(), "{name}");
        let armed = Armed::new();
        let (result, notices) = armed.submit(bad, T0);
        assert_eq!(rejection(&result), Some(RejectReason::Invalid), "{name}");
        assert!(notices.is_empty(), "{name}");
    }
    // 同一步里不同的键、鼠标键与轴可以同时按住；同一键在不同步里也可以。
    let fine = block(
        1,
        T0,
        100,
        vec![
            step(
                0,
                vec![
                    key(0x11),
                    key(0x1E),
                    mouse(MouseButton::Left),
                    axis("yaw", -1.0),
                ],
                vec![],
            ),
            step(10, vec![key(0x11)], vec![]),
        ],
    );
    assert_eq!(check_block(&fine), Ok(()));
}

#[test]
fn producer_seq_and_expiry_are_checked_without_side_effects() {
    let armed = Armed::new();
    let one = || vec![step(0, vec![], vec![])];
    let (result, _) = armed.submit(block(5, T0, 100, one()), T0);
    assert!(accepted(&result));
    // 序号不大于已接受的。
    for seq in [5, 4] {
        let (result, _) = armed.submit(block(seq, T0, 100, one()), T0);
        assert_eq!(rejection(&result), Some(RejectReason::StaleSeq));
    }
    // 截止时刻已过（含恰好等于现在）。
    let (result, _) = armed.submit(block(6, T0 - 100 * MS, 100, one()), T0);
    assert_eq!(rejection(&result), Some(RejectReason::Expired));
    // 同代次的其它身份。
    let other = ActionBlock {
        producer: producer("b", 1),
        ..block(7, T0, 100, one())
    };
    assert_eq!(
        rejection(&armed.submit(other, T0).0),
        Some(RejectReason::ProducerMismatch)
    );
    // 已作废与更早的代次：拒绝，当前产出者不变，什么都不执行。
    armed.control.heartbeat(&beat("a", 3, 2), T0).unwrap();
    armed.control.revoke(2);
    for generation in [1, 2] {
        let old = ActionBlock {
            producer: producer("a", generation),
            ..block(100, T0, 100, vec![step(0, vec![key(0x11)], vec![click(1)])])
        };
        let (result, notices) = armed.submit(old, T0);
        assert_eq!(rejection(&result), Some(RejectReason::StaleGeneration));
        assert!(notices.is_empty());
    }
    assert!(armed.control.tick(T0 + 10 * MS).is_empty());
    assert!(
        armed
            .inputs()
            .iter()
            .all(|input| input.starts_with("release all"))
    );
    assert_eq!(armed.control.health().producer, Some(producer("a", 3)));
}

// ---------------------------------------------------------------- 执行、去重、取代与截止时刻

#[test]
fn steps_run_at_their_time_and_held_state_is_complete_each_step() {
    let armed = Armed::new();
    let steps = vec![
        step(0, vec![key(0x11), mouse(MouseButton::Left)], vec![click(1)]),
        step(10, vec![key(0x11), axis("yaw", 0.5)], vec![]),
        step(20, vec![axis("yaw", -0.5)], vec![press(2, 0x1C)]),
        step(30, vec![], vec![]),
    ];
    let (result, notices) = armed.submit(block(1, T0, 50, steps), T0);
    assert!(accepted(&result));
    assert!(notices.is_empty(), "the handler does not run any step");
    assert_eq!(armed.inputs(), Vec::<String>::new());

    let notices = armed.control.tick(T0);
    assert_eq!(
        armed.inputs(),
        ["down key 0x11", "down mouse Left", "click 1 at 10,20"]
    );
    assert_eq!(receipts(&notices), [(1, vec!["executed 1".to_owned()])]);
    assert!(armed.control.health().inputs_held);
    // 没到时刻的步不执行。
    assert!(armed.control.tick(T0 + 9 * MS).is_empty());
    assert_eq!(armed.inputs(), Vec::<String>::new());
    // 缺项即释放：这一步没列出鼠标左键。
    armed.control.tick(T0 + 10 * MS);
    assert_eq!(armed.inputs(), ["up mouse Left", "down axis yaw=0.5"]);
    armed.control.tick(T0 + 20 * MS);
    assert_eq!(
        armed.inputs(),
        ["up key 0x11", "down axis yaw=-0.5", "press 2 0x1c"]
    );
    armed.control.tick(T0 + 30 * MS);
    assert_eq!(armed.inputs(), ["up axis yaw=-0.5"]);
    assert!(!armed.control.health().inputs_held);
    // 到截止时刻没有新块：释放。
    let notices = armed.control.tick(T0 + 50 * MS);
    assert_eq!(released(&notices), [ReleaseReason::DeadlineExpired]);
    assert_eq!(armed.inputs(), ["release all DeadlineExpired"]);
    // 之后不再有计划。
    assert!(armed.control.tick(T0 + 60 * MS).is_empty());
}

#[test]
fn the_last_step_holds_until_the_deadline() {
    let armed = Armed::new();
    // 帧一直新鲜，只看截止时刻。
    let steps = vec![step(0, vec![key(0x11)], vec![])];
    armed.submit(block(1, T0, 80, steps), T0);
    armed.control.tick(T0);
    armed.frame(2, T0 + 70 * MS, GEOMETRY);
    assert!(armed.control.tick(T0 + 79 * MS).is_empty());
    assert_eq!(armed.inputs(), ["down key 0x11"]);
    let notices = armed.control.tick(T0 + 80 * MS);
    assert_eq!(released(&notices), [ReleaseReason::DeadlineExpired]);
    assert_eq!(
        armed.inputs(),
        ["up key 0x11", "release all DeadlineExpired"]
    );
}

#[test]
fn a_late_block_runs_its_past_steps_immediately() {
    let armed = Armed::new();
    let steps = vec![
        step(0, vec![], vec![press(1, 0x1C)]),
        step(10, vec![], vec![press(2, 0x39)]),
    ];
    // 到达时两步的时刻都已过，截止时刻还没到。
    armed.submit(block(1, T0 - 20 * MS, 90, steps), T0);
    let notices = armed.control.tick(T0);
    assert_eq!(armed.inputs(), ["press 1 0x1c", "press 2 0x39"]);
    // 每执行一步回执一次。
    assert_eq!(
        receipts(&notices),
        [
            (1, vec!["executed 1".to_owned()]),
            (1, vec!["executed 2".to_owned()])
        ]
    );
}

#[test]
fn one_shot_actions_are_deduplicated_within_a_generation() {
    let armed = Armed::new();
    armed.submit(block(1, T0, 100, vec![step(0, vec![], vec![click(1)])]), T0);
    armed.control.tick(T0);
    // 重传同一身份：不再执行。
    armed.submit(
        block(
            2,
            T0 + 5 * MS,
            100,
            vec![step(0, vec![], vec![click(1), press(2, 0x1C)])],
        ),
        T0 + 5 * MS,
    );
    let notices = armed.control.tick(T0 + 5 * MS);
    assert_eq!(
        receipts(&notices),
        [(
            2,
            vec!["dropped 1 Duplicate".to_owned(), "executed 2".to_owned()]
        )]
    );
    assert_eq!(armed.inputs(), ["click 1 at 10,20", "press 2 0x1c"]);
    // 新代次从头计：同一身份照常执行。
    armed
        .control
        .heartbeat(&beat("a", 2, 1), T0 + 6 * MS)
        .unwrap();
    armed.inputs();
    let next = ActionBlock {
        producer: producer("a", 2),
        ..block(1, T0 + 6 * MS, 100, vec![step(0, vec![], vec![click(1)])])
    };
    armed.submit(next, T0 + 6 * MS);
    armed.control.tick(T0 + 6 * MS);
    assert_eq!(armed.inputs(), ["click 1 at 10,20"]);
}

#[test]
fn a_new_block_supersedes_the_unexecuted_rest_from_its_start() {
    let armed = Armed::new();
    let steps = vec![
        step(0, vec![key(0x11)], vec![press(1, 0x1C)]),
        step(20, vec![key(0x11)], vec![press(2, 0x1C)]),
        step(40, vec![key(0x11)], vec![press(3, 0x1C)]),
    ];
    armed.submit(block(1, T0, 100, steps), T0);
    armed.control.tick(T0);
    armed.inputs();
    // 新块从 T0+30 起：旧块 T0+40 那步被取代，T0+20 那步照常执行。
    let (result, notices) = armed.submit(
        block(
            2,
            T0 + 30 * MS,
            100,
            vec![step(0, vec![], vec![press(4, 0x39)])],
        ),
        T0 + 10 * MS,
    );
    assert!(accepted(&result));
    assert_eq!(
        receipts(&notices),
        [(1, vec!["dropped 3 Superseded".to_owned()])]
    );
    let notices = armed.control.tick(T0 + 20 * MS);
    assert_eq!(receipts(&notices), [(1, vec!["executed 2".to_owned()])]);
    assert_eq!(armed.inputs(), ["press 2 0x1c"]);
    // 新块的第一步给出完整的持续状态：旧块按住的键抬起。
    let notices = armed.control.tick(T0 + 30 * MS);
    assert_eq!(receipts(&notices), [(2, vec!["executed 4".to_owned()])]);
    assert_eq!(armed.inputs(), ["up key 0x11", "press 4 0x39"]);
    // 旧块的截止时刻不再起作用；新块的截止时刻照常。
    armed.frame(2, T0 + 120 * MS, GEOMETRY);
    assert!(armed.control.tick(T0 + 100 * MS).is_empty());
    assert_eq!(
        released(&armed.control.tick(T0 + 130 * MS)),
        [ReleaseReason::DeadlineExpired]
    );
}

#[test]
fn held_state_does_not_outlive_the_old_deadline_before_a_later_block_starts() {
    let armed = Armed::new();
    armed.submit(block(1, T0, 30, vec![step(0, vec![key(0x11)], vec![])]), T0);
    armed.control.tick(T0);
    armed.inputs();
    // 新块在旧块截止之后才开始：旧块的持续状态在它的截止时刻抬起，不发释放全部。
    armed.submit(
        block(2, T0 + 60 * MS, 30, vec![step(0, vec![key(0x1E)], vec![])]),
        T0 + 10 * MS,
    );
    assert!(armed.control.tick(T0 + 29 * MS).is_empty());
    let notices = armed.control.tick(T0 + 30 * MS);
    assert_eq!(released(&notices), []);
    assert_eq!(armed.inputs(), ["up key 0x11"]);
    armed.frame(2, T0 + 60 * MS, GEOMETRY);
    armed
        .control
        .heartbeat(&beat("a", 1, 2), T0 + 60 * MS)
        .unwrap();
    armed.control.tick(T0 + 60 * MS);
    assert_eq!(armed.inputs(), ["down key 0x1e"]);
}

#[test]
fn regression_expired_old_block_cannot_press_held_input() {
    let armed = Armed::new_real();
    armed.configure(&input_policy());
    let (old, _) = armed.submit(
        block(1, T0 + 20 * MS, 20, vec![step(0, vec![key(0x11)], vec![])]),
        T0,
    );
    assert!(accepted(&old));
    let (new, _) = armed.submit(
        block(2, T0 + 60 * MS, 30, vec![step(0, vec![], vec![])]),
        T0 + 10 * MS,
    );
    assert!(accepted(&new));
    armed.control.tick(T0 + 50 * MS);
    assert_eq!(armed.inputs(), Vec::<String>::new());
}

// ---------------------------------------------------------------- 权威检查

/// 三个条件任一不成立：按下与一次性动作都不交给后端，释放全部输入并结束这一块。
#[test]
fn every_authority_condition_gates_every_input() {
    type Break = fn(&Armed) -> u64;
    let cases: [(&str, Break, ReleaseReason); 6] = [
        (
            "no lease was ever granted",
            |_| unreachable!("covered by a fresh control below"),
            ReleaseReason::NoLease,
        ),
        (
            "the lease expired",
            |armed| {
                // 同一令牌续约成更早的到期时刻，然后不再续约。
                armed.grant(1, T0 + 20 * MS, T0);
                T0 + 20 * MS
            },
            ReleaseReason::LeaseExpired,
        ),
        (
            "the lease was revoked",
            |armed| {
                armed.control.revoke_lease(1, T0);
                T0
            },
            ReleaseReason::LeaseRevoked,
        ),
        (
            "the lease source went away",
            |armed| {
                armed.control.lease_source_lost();
                T0
            },
            ReleaseReason::LeaseSourceLost,
        ),
        (
            "the heartbeat timed out",
            |_| T0 + HEARTBEAT_TIMEOUT,
            ReleaseReason::HeartbeatTimeout,
        ),
        (
            "the latest frame is too old",
            |_| T0 + MAX_FRAME_AGE + 1,
            ReleaseReason::FrameStale,
        ),
    ];
    for (name, breaker, reason) in cases.into_iter().skip(1) {
        let armed = Armed::new();
        let now = breaker(&armed);
        armed.inputs();
        let steps = vec![
            step(0, vec![key(0x11)], vec![click(1)]),
            step(10, vec![], vec![press(2, 0x1C)]),
        ];
        let (result, mut notices) = armed.submit(block(1, now, 100, steps), now);
        assert!(accepted(&result), "{name}");
        let more = armed.control.tick(now);
        notices.0.extend(more.0);
        assert!(released(&notices).contains(&reason), "{name}: {notices:?}");
        // 按下与动作一个都没交给后端；只有释放。
        let inputs = armed.inputs();
        assert!(
            inputs.iter().all(|input| input.starts_with("release all")),
            "{name}: {inputs:?}"
        );
        // 整块结束：两个动作都回执 released，之后的步不再执行。
        let all: Vec<String> = receipts(&notices)
            .into_iter()
            .flat_map(|(_, o)| o)
            .collect();
        assert_eq!(all, ["dropped 1 Released", "dropped 2 Released"], "{name}");
        assert!(
            armed
                .control
                .tick(now + 50 * MS)
                .0
                .iter()
                .all(|notice| matches!(notice, super::Notice::Released(_)))
        );
        assert!(
            !armed
                .inputs()
                .iter()
                .any(|input| input.starts_with("press"))
        );
    }
    // 从未授予许可（含没有后端、没有许可源）。
    for backend in [Recorder::default().backend(), None] {
        let frames = Arc::new(FrameStatus::default());
        frames.record(
            LatestFrame {
                frame_number: 1,
                capture_ns: T0,
            },
            GEOMETRY,
            None,
        );
        let control = Control::new(limits(), frames, backend, false);
        control.heartbeat(&beat("a", 1, 1), T0).unwrap();
        control.submit(block(1, T0, 100, vec![step(0, vec![], vec![click(1)])]), T0);
        let notices = control.tick(T0);
        assert_eq!(released(&notices), [ReleaseReason::NoLease]);
        assert_eq!(
            receipts(&notices),
            [(1, vec!["dropped 1 Released".to_owned()])]
        );
    }
}

#[test]
fn a_block_of_a_new_generation_needs_its_own_heartbeat() {
    let armed = Armed::new();
    armed.control.tick(T0);
    let next = ActionBlock {
        producer: producer("a", 2),
        ..block(1, T0, 100, vec![step(0, vec![], vec![click(1)])])
    };
    let (result, notices) = armed.submit(next.clone(), T0);
    assert!(accepted(&result));
    assert_eq!(released(&notices), [ReleaseReason::ProducerChanged]);
    // 这一代次还没有心跳：不发。
    let notices = armed.control.tick(T0);
    assert_eq!(released(&notices), [ReleaseReason::HeartbeatTimeout]);
    // 心跳到了，新的一块照常执行；上一块的点击没有执行过，不算重复。
    armed.control.heartbeat(&beat("a", 2, 1), T0 + MS).unwrap();
    armed.inputs();
    let again = ActionBlock {
        producer: producer("a", 2),
        ..block(2, T0 + MS, 100, vec![step(0, vec![], vec![click(1)])])
    };
    armed.submit(again, T0 + MS);
    armed.control.tick(T0 + MS);
    assert_eq!(armed.inputs(), ["click 1 at 10,20"]);
}

#[test]
fn held_inputs_are_released_when_a_condition_fails_without_a_new_action() {
    // 每种情形都先按住一个键，不提交新动作，只靠定时检查。
    type Break = fn(&Armed) -> u64;
    let cases: [(&str, Break, ReleaseReason); 3] = [
        (
            "frame freeze",
            |_| T0 + MAX_FRAME_AGE + 1,
            ReleaseReason::FrameStale,
        ),
        (
            "lease expiry",
            |armed| {
                armed.grant(1, T0 + 50 * MS, T0);
                T0 + 50 * MS
            },
            ReleaseReason::LeaseExpired,
        ),
        (
            "heartbeat timeout",
            |armed| {
                // 帧一直新鲜，只看心跳。
                armed.frame(2, T0 + HEARTBEAT_TIMEOUT, GEOMETRY);
                T0 + HEARTBEAT_TIMEOUT
            },
            ReleaseReason::HeartbeatTimeout,
        ),
    ];
    for (name, breaker, reason) in cases {
        let armed = Armed::new();
        armed.submit(
            block(1, T0, 10_000, vec![step(0, vec![key(0x11)], vec![])]),
            T0,
        );
        armed.control.tick(T0);
        assert_eq!(armed.inputs(), ["down key 0x11"], "{name}");
        let at = breaker(&armed);
        assert!(armed.control.tick(at - 1).is_empty(), "{name}");
        let notices = armed.control.tick(at);
        assert_eq!(released(&notices), [reason], "{name}");
        assert_eq!(
            armed.inputs(),
            ["up key 0x11".to_owned(), format!("release all {reason:?}")],
            "{name}"
        );
        assert!(!armed.control.health().inputs_held, "{name}");
    }
}

#[test]
fn frame_age_counts_negative_ages_as_zero() {
    let armed = Armed::new();
    // 采集时刻晚于现在（WGC 可能如此）：帧龄按 0。
    armed.frame(2, T0 + 5 * MS, GEOMETRY);
    armed.submit(block(1, T0, 100, vec![step(0, vec![], vec![click(1)])]), T0);
    armed.control.tick(T0);
    assert_eq!(armed.inputs(), ["click 1 at 10,20"]);
}

#[test]
fn revoking_the_generation_ends_its_block() {
    let armed = Armed::new();
    let steps = vec![
        step(0, vec![key(0x11)], vec![]),
        step(50, vec![key(0x11)], vec![press(1, 0x1C)]),
    ];
    armed.submit(block(1, T0, 100, steps), T0);
    armed.control.tick(T0);
    armed.inputs();
    let (_, notices) = armed.control.revoke(1);
    assert_eq!(released(&notices), [ReleaseReason::GenerationRevoked]);
    assert_eq!(
        receipts(&notices),
        [(1, vec!["dropped 1 Released".to_owned()])]
    );
    assert_eq!(
        armed.inputs(),
        ["up key 0x11", "release all GenerationRevoked"]
    );
    assert!(armed.control.tick(T0 + 60 * MS).is_empty());
    assert_eq!(armed.inputs(), Vec::<String>::new());
}

#[test]
fn a_higher_generation_releases_the_old_block_before_switching() {
    let armed = Armed::new();
    armed.submit(
        block(1, T0, 100, vec![step(0, vec![key(0x11)], vec![])]),
        T0,
    );
    armed.control.tick(T0);
    armed.inputs();
    let notices = armed.control.heartbeat(&beat("b", 2, 1), T0 + MS).unwrap();
    assert_eq!(released(&notices), [ReleaseReason::ProducerChanged]);
    assert_eq!(
        armed.inputs(),
        ["up key 0x11", "release all ProducerChanged"]
    );
    assert!(armed.control.tick(T0 + 99 * MS).is_empty());
}

#[test]
fn a_newer_lease_token_releases_what_the_old_one_held() {
    let armed = Armed::new();
    armed.submit(
        block(1, T0, 100, vec![step(0, vec![key(0x11)], vec![])]),
        T0,
    );
    armed.control.tick(T0);
    armed.inputs();
    // 同一令牌续约：照常保持。
    assert!(armed.grant(1, T0 + 20_000 * MS, T0 + MS).is_empty());
    let notices = armed.grant(2, T0 + 20_000 * MS, T0 + MS);
    assert_eq!(released(&notices), [ReleaseReason::LeaseRevoked]);
    assert_eq!(armed.inputs(), ["up key 0x11", "release all LeaseRevoked"]);
}

#[test]
fn a_renewal_with_a_stale_token_does_not_extend_the_lease() {
    let armed = Armed::new();
    armed.grant(5, T0 + 50 * MS, T0);
    let (result, _) = armed.control.grant_lease(
        &LeaseGrantParams {
            token: 4,
            expires_ns: T0 + 10_000 * MS,
        },
        T0 + MS,
    );
    assert!(result.is_err());
    assert_eq!(
        released(&armed.control.tick(T0 + 50 * MS)),
        [ReleaseReason::LeaseExpired]
    );
    armed.submit(
        block(1, T0 + 50 * MS, 100, vec![step(0, vec![], vec![click(1)])]),
        T0 + 50 * MS,
    );
    let notices = armed.control.tick(T0 + 50 * MS);
    assert_eq!(released(&notices), [ReleaseReason::LeaseExpired]);
}

// ---------------------------------------------------------------- 坐标与几何

#[test]
fn coordinate_actions_are_dropped_when_the_client_area_changes() {
    let armed = Armed::new();
    let scroll = Action::Scroll(Scroll {
        id: 3,
        x: 5,
        y: 5,
        notches: 1,
    });
    let actions = || vec![click(1), press(2, 0x1C), scroll.clone()];
    // 只移动窗口：尺寸与 DPI 不变，照常执行。
    armed.frame(2, T0, GEOMETRY);
    armed.submit(block(1, T0, 100, vec![step(0, vec![], actions())]), T0);
    let notices = armed.control.tick(T0);
    assert_eq!(
        receipts(&notices),
        [(
            1,
            vec![
                "executed 1".to_owned(),
                "executed 2".to_owned(),
                "executed 3".to_owned()
            ]
        )]
    );
    // 基准帧之后尺寸变了：带坐标的动作不执行，短按照常。
    for (name, changed) in [
        (
            "size",
            Geometry {
                width: 640,
                ..GEOMETRY
            },
        ),
        (
            "dpi",
            Geometry {
                dpi: 144,
                ..GEOMETRY
            },
        ),
    ] {
        let armed = Armed::new();
        armed.frame(2, T0, changed);
        armed.submit(block(1, T0, 100, vec![step(0, vec![], actions())]), T0);
        let notices = armed.control.tick(T0);
        assert_eq!(
            receipts(&notices),
            [(
                1,
                vec![
                    "dropped 1 GeometryChanged".to_owned(),
                    "executed 2".to_owned(),
                    "dropped 3 GeometryChanged".to_owned()
                ]
            )],
            "{name}"
        );
        assert!(
            released(&notices).is_empty(),
            "{name}: not an authority failure"
        );
        assert_eq!(armed.inputs(), ["press 2 0x1c"], "{name}");
    }
    // 基准帧的几何查不到（早于本实例写出的第一帧、尚未写出）：同样不执行。
    for base_frame in [0, 9] {
        let armed = Armed::new();
        let unknown = ActionBlock {
            base_frame,
            ..block(1, T0, 100, vec![step(0, vec![], vec![click(1)])])
        };
        armed.submit(unknown, T0);
        assert_eq!(
            receipts(&armed.control.tick(T0)),
            [(1, vec!["dropped 1 GeometryChanged".to_owned()])],
            "{base_frame}"
        );
    }
}

// ---------------------------------------------------------------- 线程

#[test]
fn the_scheduler_runs_steps_and_timers_in_real_time() {
    let frames = Arc::new(FrameStatus::default());
    let backend = Recorder::default();
    let control = Arc::new(Control::new(
        limits(),
        Arc::clone(&frames),
        backend.backend(),
        false,
    ));
    let (sender, received) = mpsc::channel();
    let scheduler = thread::spawn({
        let control = Arc::clone(&control);
        move || control.run_scheduler(|notices| sender.send(notices).unwrap())
    });
    let now = mono_clock::now_ns();
    frames.record(
        LatestFrame {
            frame_number: 1,
            capture_ns: now,
        },
        GEOMETRY,
        None,
    );
    let (result, _) = control.grant_lease(
        &LeaseGrantParams {
            token: 1,
            expires_ns: now + 10_000 * MS,
        },
        now,
    );
    result.unwrap();
    control.heartbeat(&beat("a", 1, 1), now).unwrap();
    let submitted = Instant::now();
    let steps = vec![step(30, vec![], vec![press(1, 0x1C)])];
    control.submit(block(1, now, 60, steps), now);
    let first = received.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!(receipts(&first), [(1, vec!["executed 1".to_owned()])]);
    assert!(submitted.elapsed() >= Duration::from_millis(29));
    let second = received.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!(released(&second), [ReleaseReason::DeadlineExpired]);
    assert!(submitted.elapsed() >= Duration::from_millis(59));
    // 心跳超时也由调度线程释放。
    let third = received.recv_timeout(Duration::from_secs(2)).unwrap();
    assert_eq!(released(&third), [ReleaseReason::HeartbeatTimeout]);
    control.release_on_exit(ReleaseReason::StdinClosed);
    scheduler.join().unwrap();
    assert!(received.try_recv().is_err());
}

#[test]
fn releasing_never_waits_for_the_capture_side() {
    let frames = Arc::new(FrameStatus::default());
    frames.record(
        LatestFrame {
            frame_number: 1,
            capture_ns: T0,
        },
        GEOMETRY,
        None,
    );
    let backend = Recorder::default();
    let control = Arc::new(Control::new(
        limits(),
        Arc::clone(&frames),
        backend.backend(),
        false,
    ));
    // 采集一侧一直占着它的锁：每条释放的路径照样完成，持有输入时的帧龄检查也不等它。
    let held = frames.hold();
    let reasons = Arc::new(Mutex::new(Vec::new()));
    let (done, finished) = mpsc::channel();
    let releaser = thread::spawn({
        let (control, reasons) = (Arc::clone(&control), Arc::clone(&reasons));
        move || {
            let push = |notices: Notices| reasons.lock().unwrap().extend(notices.released());
            reasons
                .lock()
                .unwrap()
                .push(control.release_on_start().reason);
            control.heartbeat(&beat("a", 1, 1), T0).unwrap();
            push(control.heartbeat(&beat("b", 2, 1), T0).unwrap());
            push(control.tick(T0 + HEARTBEAT_TIMEOUT));
            push(control.revoke(2).1);
            let first = LeaseGrantParams {
                token: 1,
                expires_ns: T0 + 10 * MS,
            };
            control.grant_lease(&first, T0).0.unwrap();
            push(control.tick(T0 + 10 * MS));
            let second = LeaseGrantParams {
                token: 2,
                expires_ns: T0 + 100 * MS,
            };
            control.grant_lease(&second, T0 + 10 * MS).0.unwrap();
            push(control.revoke_lease(2, T0 + 20 * MS));
            push(control.lease_source_lost());
            push(control.stop());
            push(control.release_on_exit(ReleaseReason::CoreConnectionLost));
            done.send(()).unwrap();
        }
    });
    finished.recv_timeout(Duration::from_secs(5)).unwrap();
    assert_eq!(
        *reasons.lock().unwrap(),
        [
            ReleaseReason::CoreStart,
            ReleaseReason::ProducerChanged,
            ReleaseReason::HeartbeatTimeout,
            ReleaseReason::GenerationRevoked,
            ReleaseReason::LeaseExpired,
            ReleaseReason::LeaseRevoked,
            ReleaseReason::LeaseSourceLost,
            ReleaseReason::Stop,
            ReleaseReason::CoreConnectionLost,
        ]
    );
    drop(held);
    releaser.join().unwrap();
}

#[test]
fn the_frame_age_check_does_not_take_the_capture_lock() {
    let armed = Armed::new();
    armed.submit(
        block(1, T0, 10_000, vec![step(0, vec![key(0x11)], vec![])]),
        T0,
    );
    armed.control.tick(T0);
    armed.inputs();
    let held = armed.frames.hold();
    // 持有输入、帧龄超限：定时检查不拿采集一侧的锁就能释放。
    let notices = armed.control.tick(T0 + MAX_FRAME_AGE + 1);
    assert_eq!(released(&notices), [ReleaseReason::FrameStale]);
    drop(held);
}

// ---------------------------------------------------------------- 输入策略

use super::Configured;
use core_protocol::messages::{InputPolicy, NormRect};

fn full_region() -> NormRect {
    NormRect {
        left: 0.0,
        top: 0.0,
        right: 1.0,
        bottom: 1.0,
    }
}

/// 一个合规的输入策略：左键点击、短按 0x1C/0x39、滚动，按住 0x11，按住时长 50 ms。
fn input_policy() -> InputPolicy {
    InputPolicy {
        click_buttons: vec![MouseButton::Left],
        press_keys: vec![0x1C, 0x39],
        scroll: true,
        click_regions: vec![full_region()],
        forbidden_zones: vec![],
        press_duration_ms: 50,
        max_rate: 2.0,
        hold_keys: vec![0x11],
        hold_buttons: vec![],
        max_hold_ms: Some(300),
    }
}

impl Armed {
    /// 真实后端的控制状态（`requires_policy`），仍用记录后端，便于核对交给后端的输入。
    fn new_real() -> Self {
        let frames = Arc::new(FrameStatus::default());
        let backend = Recorder::default();
        let control = Control::new(limits(), Arc::clone(&frames), backend.backend(), true);
        let armed = Self {
            control,
            frames,
            backend,
        };
        armed.frame(1, T0, GEOMETRY);
        armed.grant(1, T0 + 10_000 * MS, T0);
        armed.control.heartbeat(&beat("a", 1, 1), T0).unwrap();
        armed.backend.take();
        armed
    }

    fn configure(&self, policy: &InputPolicy) -> Configured {
        self.control.configure(policy).unwrap()
    }
}

fn click_at(id: u64, x: u32, y: u32) -> Action {
    Action::Click(Click {
        id,
        x,
        y,
        button: MouseButton::Left,
    })
}

#[test]
fn configure_is_once_and_validated() {
    let armed = Armed::new();
    assert_eq!(armed.configure(&input_policy()), Configured::Accepted);
    // 再设定即冲突（-32006）。
    assert_eq!(
        armed.control.configure(&input_policy()).unwrap(),
        Configured::Conflict
    );
    // 不合规则的策略在未设定过的实例上回参数无效。
    let other = Armed::new();
    let mut bad = input_policy();
    bad.press_duration_ms = 0;
    assert!(other.control.configure(&bad).is_err());
}

#[test]
fn the_real_backend_rejects_input_without_a_policy() {
    let armed = Armed::new_real();
    // 含输入的动作块：没有策略即拒绝。
    let (result, _) = armed.submit(
        block(1, T0, 10_000, vec![step(0, vec![], vec![press(1, 0x1C)])]),
        T0,
    );
    assert_eq!(rejection(&result), Some(RejectReason::NotAllowed));
    // 不含输入的动作块（空步）照常接受。
    let (result, _) = armed.submit(block(2, T0, 10_000, vec![step(0, vec![], vec![])]), T0);
    assert!(accepted(&result), "{result:?}");
}

#[test]
fn the_whitelist_is_checked_at_submit() {
    let armed = Armed::new_real();
    armed.configure(&input_policy());
    // 持续状态的键不在 hold_keys 内。
    let (result, _) = armed.submit(
        block(1, T0, 10_000, vec![step(0, vec![key(0x20)], vec![])]),
        T0,
    );
    assert_eq!(rejection(&result), Some(RejectReason::NotAllowed));
    // 右键不在 click_buttons 内。
    let right = Action::Click(Click {
        id: 1,
        x: 10,
        y: 20,
        button: MouseButton::Right,
    });
    let (result, _) = armed.submit(block(2, T0, 10_000, vec![step(0, vec![], vec![right])]), T0);
    assert_eq!(rejection(&result), Some(RejectReason::NotAllowed));
    // 白名单内的块接受。
    let (result, _) = armed.submit(
        block(
            3,
            T0,
            10_000,
            vec![step(0, vec![key(0x11)], vec![press(4, 0x1C)])],
        ),
        T0,
    );
    assert!(accepted(&result), "{result:?}");
}

#[test]
fn a_click_outside_the_regions_is_dropped_at_execution() {
    let armed = Armed::new_real();
    let mut policy = input_policy();
    // 只允许客户区左上角 1/4。
    policy.click_regions = vec![NormRect {
        left: 0.0,
        top: 0.0,
        right: 0.5,
        bottom: 0.5,
    }];
    armed.configure(&policy);
    // 320×180：允许区在 x<160、y<90。(300, 20) 在区外。
    armed.submit(
        block(
            1,
            T0,
            10_000,
            vec![step(0, vec![], vec![click_at(1, 300, 20)])],
        ),
        T0,
    );
    let notices = armed.control.tick(T0);
    assert_eq!(
        receipts(&notices),
        [(1, vec!["dropped 1 NotAllowed".to_owned()])]
    );
    assert_eq!(armed.inputs(), Vec::<String>::new(), "落点不合即不注入");
}

#[test]
fn a_forbidden_zone_drops_the_click() {
    let armed = Armed::new_real();
    let mut policy = input_policy();
    policy.forbidden_zones = vec![NormRect {
        left: 0.0,
        top: 0.0,
        right: 0.2,
        bottom: 0.2,
    }];
    armed.configure(&policy);
    // (10, 20) 归一化到 (0.03, 0.11) 在禁区内。
    armed.submit(
        block(
            1,
            T0,
            10_000,
            vec![step(0, vec![], vec![click_at(1, 10, 20)])],
        ),
        T0,
    );
    let notices = armed.control.tick(T0);
    assert_eq!(
        receipts(&notices),
        [(1, vec!["dropped 1 NotAllowed".to_owned()])]
    );
}

#[test]
fn the_rate_limit_drops_extra_one_shots() {
    let armed = Armed::new_real();
    armed.configure(&input_policy()); // max_rate = 2
    // 同一步三个短按：前两个执行，第三个超速丢弃。
    armed.submit(
        block(
            1,
            T0,
            10_000,
            vec![step(
                0,
                vec![],
                vec![press(1, 0x1C), press(2, 0x1C), press(3, 0x1C)],
            )],
        ),
        T0,
    );
    // 第一个按下后要等它抬起（50 ms）才做下一个：逐个推进时钟。
    armed.control.tick(T0);
    armed.control.tick(T0 + 50 * MS + 1);
    let notices = armed.control.tick(T0 + 100 * MS + 2);
    let joined: Vec<String> = receipts(&notices)
        .into_iter()
        .flat_map(|(_, o)| o)
        .collect();
    assert!(
        joined.contains(&"dropped 3 RateLimited".to_owned()),
        "{joined:?}"
    );
}

#[test]
fn a_one_shot_is_a_momentary_press_lifted_after_the_duration() {
    let armed = Armed::new_real();
    armed.configure(&input_policy());
    armed.submit(
        block(1, T0, 10_000, vec![step(0, vec![], vec![press(1, 0x1C)])]),
        T0,
    );
    armed.control.tick(T0);
    // 按下作为瞬时按住注入，还没抬起。
    assert_eq!(armed.inputs(), ["begin press 0x1c"]);
    // 到按住时长（50 ms）由调度抬起。
    armed.control.tick(T0 + 50 * MS + 1);
    assert_eq!(armed.inputs(), ["end press 0x1c"]);
}

#[test]
fn max_hold_releases_a_key_held_too_long() {
    let armed = Armed::new_real();
    armed.configure(&input_policy()); // max_hold = 300 ms
    armed.submit(
        block(1, T0, 10_000, vec![step(0, vec![key(0x11)], vec![])]),
        T0,
    );
    armed.control.tick(T0);
    assert_eq!(armed.inputs(), ["down key 0x11"]);
    // 帧要够新，否则先因 frame_stale 释放。补一帧。
    armed.frame(2, T0 + 290 * MS, GEOMETRY);
    let notices = armed.control.tick(T0 + 300 * MS + 1);
    assert_eq!(released(&notices), [ReleaseReason::MaxHold]);
    assert_eq!(armed.inputs(), ["up key 0x11", "release all MaxHold"]);
}

#[test]
fn a_local_revoke_releases_and_invalidates_the_token() {
    let armed = Armed::new_real();
    armed.configure(&input_policy());
    armed.submit(
        block(1, T0, 10_000, vec![step(0, vec![key(0x11)], vec![])]),
        T0,
    );
    armed.control.tick(T0);
    armed.inputs();
    // 接管即本地撤权并释放。
    let notices = armed.control.local_revoke(ReleaseReason::Takeover);
    assert_eq!(released(&notices), [ReleaseReason::Takeover]);
    assert_eq!(armed.inputs(), ["up key 0x11", "release all Takeover"]);
    // 旧令牌不能再续约，须换发更大的令牌。
    let (result, _) = armed.control.grant_lease(
        &LeaseGrantParams {
            token: 1,
            expires_ns: T0 + 20_000 * MS,
        },
        T0 + MS,
    );
    assert!(result.is_err());
    let (result, _) = armed.control.grant_lease(
        &LeaseGrantParams {
            token: 2,
            expires_ns: T0 + 20_000 * MS,
        },
        T0 + MS,
    );
    assert!(result.is_ok(), "换发更大的令牌被接受");
}

#[test]
fn regression_hook_lost_cannot_be_rearmed_in_the_same_instance() {
    let armed = Armed::new_real();
    armed.configure(&input_policy());
    assert_eq!(
        released(&armed.control.local_revoke(ReleaseReason::HookLost)),
        [ReleaseReason::HookLost]
    );
    let (result, _) = armed.control.grant_lease(
        &LeaseGrantParams {
            token: 2,
            expires_ns: T0 + 10_000 * MS,
        },
        T0 + MS,
    );
    assert!(result.is_err(), "钩子失效的实例不能接受新许可");
}

#[test]
fn regression_core_start_marks_unchecked_physical_release_as_unverified() {
    let armed = Armed::new_real();
    let released = serde_json::to_value(armed.control.release_on_start()).unwrap();
    assert_eq!(released["verification"], "unverified");
}
