//! 假输入后端与测试设备连接，只用于测试（`docs/modules/realtime-core.md` 的“假输入后端与测试设备连接”）。
//!
//! 实时核以 `--input-backend fake` 启动时，另作为 `bellis-channel/1` 客户端连到测试一方。这条连接承载：
//!
//! - 假输入事件：假后端把每个按下、抬起、轴、一次性动作与释放全部作为 `fake_input.event` 发给测试一方。
//! - 测试许可源，只在没有操作员许可连接时：测试一方以 `test_lease.grant` 授予与续约许可，以
//!   `test_lease.revoke` 吊销；连接断开即没有许可（`lease_source_lost`）。有操作员许可连接时这两个方法
//!   不登记，回“方法不存在”，许可只来自操作员控制台（`operator` 模块）。
//!
//! 许可不来自主进程：主进程只能让输入停下，不能让输入继续。测试一方能独立看到实时核是否发出了抬起，
//! 主进程崩溃时也一样。生产中没有这条连接；测试许可源只在这里构造，只能与假后端一起出现。
//!
//! 假后端把事件放进发送队列就返回，从不在控制状态的锁内等网络写入；队列由测试设备连接的发送线程写出。

use std::io;
use std::sync::Arc;
use std::time::Duration;

use bellis_channel::{ChannelCredentials, ChannelError, Connection, Handlers};
use core_protocol::messages::{
    Action, FakeAxisRelease, FakeAxisSet, FakeClick, FakeInputEvent, FakeInputEventParams,
    FakeKeyDown, FakeKeyPress, FakeKeyUp, FakeMouseDown, FakeMouseMove, FakeMouseUp,
    FakeReleaseAll, FakeScroll, FakeVerifyReleaseParams, FakeVerifyReleaseResult, HeldInput,
    MouseButton, ReleaseReason, VerificationResult,
};
use core_protocol::methods::{
    FakeInput, FakeVerifyRelease, Method, TestLeaseGrant, TestLeaseRevoke,
};
use serde_json::{Map, Value};

use crate::control::{Control, Notice, Notices};
use crate::ledger::{Inject, InputBackend, Momentary};
use crate::queue::SendQueue;
use crate::side::{Post, RequestSender, SideConnection, connect_side, with_lease_handlers};

const VERIFICATION_WINDOW_NS: u64 = 200_000_000;

/// 假输入后端：每个输入都作为一条 `fake_input.event` 放进发送队列。
pub(crate) struct FakeBackend {
    queue: Arc<SendQueue>,
    seq: u64,
    verify_release: bool,
}

impl FakeBackend {
    /// 新的假后端与它的发送队列；队列由测试设备连接的发送线程写出（[`TestDeviceConnection::start`]）。
    pub(crate) fn new(verify_release: bool) -> (Self, Arc<SendQueue>) {
        let queue = Arc::new(SendQueue::default());
        let backend = Self {
            queue: Arc::clone(&queue),
            seq: 0,
            verify_release,
        };
        (backend, queue)
    }

    fn emit(&mut self, event: FakeInputEvent) {
        self.seq += 1;
        let params = FakeInputEventParams {
            seq: self.seq,
            at_ns: mono_clock::now_ns(),
            event,
        };
        if let Ok(Value::Object(params)) = serde_json::to_value(params) {
            self.queue.push(FakeInput::NAME, params);
        } else {
            log!("{} not queued: params are not an object", FakeInput::NAME);
        }
    }
}

impl FakeBackend {
    fn key_down(&mut self, scan_code: u16) {
        self.emit(FakeInputEvent::KeyDown(FakeKeyDown { scan_code }));
    }

    fn key_up(&mut self, scan_code: u16) {
        self.emit(FakeInputEvent::KeyUp(FakeKeyUp { scan_code }));
    }

    fn mouse_down(&mut self, button: MouseButton) {
        self.emit(FakeInputEvent::MouseDown(FakeMouseDown { button }));
    }

    fn mouse_up(&mut self, button: MouseButton) {
        self.emit(FakeInputEvent::MouseUp(FakeMouseUp { button }));
    }
}

impl InputBackend for FakeBackend {
    fn press(&mut self, input: &HeldInput) -> Inject {
        match input {
            HeldInput::Key(key) => self.key_down(key.scan_code),
            HeldInput::MouseButton(button) => self.mouse_down(button.button),
            HeldInput::Axis(axis) => self.emit(FakeInputEvent::AxisSet(FakeAxisSet {
                axis: axis.axis.clone(),
                value: axis.value,
            })),
        }
        Inject::Done
    }

    fn release(&mut self, input: &HeldInput) -> bool {
        match input {
            HeldInput::Key(key) => self.key_up(key.scan_code),
            HeldInput::MouseButton(button) => self.mouse_up(button.button),
            HeldInput::Axis(axis) => self.emit(FakeInputEvent::AxisRelease(FakeAxisRelease {
                axis: axis.axis.clone(),
            })),
        }
        true
    }

    fn act(&mut self, action: &Action) -> Inject {
        self.emit(match action {
            Action::Click(click) => FakeInputEvent::Click(FakeClick {
                id: click.id,
                x: click.x,
                y: click.y,
                button: click.button,
            }),
            Action::KeyPress(press) => FakeInputEvent::KeyPress(FakeKeyPress {
                id: press.id,
                scan_code: press.scan_code,
            }),
            Action::Scroll(scroll) => FakeInputEvent::Scroll(FakeScroll {
                id: scroll.id,
                x: scroll.x,
                y: scroll.y,
                notches: scroll.notches,
            }),
            Action::MouseMove(moved) => FakeInputEvent::MouseMove(FakeMouseMove {
                id: moved.id,
                dx: moved.dx,
                dy: moved.dy,
            }),
        });
        Inject::Done
    }

    fn begin_momentary(&mut self, momentary: &Momentary) -> Inject {
        // 有输入策略时的瞬时按住：点击发鼠标按下，短按发键按下（坐标在真实后端用，假后端不记）。
        match momentary {
            Momentary::Click { button, .. } => self.mouse_down(*button),
            Momentary::Key { scan_code } => self.key_down(*scan_code),
        }
        Inject::Done
    }

    fn end_momentary(&mut self, momentary: &Momentary) -> bool {
        match momentary {
            Momentary::Click { button, .. } => self.mouse_up(*button),
            Momentary::Key { scan_code } => self.key_up(*scan_code),
        }
        true
    }

    fn released_all(&mut self, reason: ReleaseReason, release_id: u64) {
        let deadline_ns = mono_clock::now_ns().saturating_add(VERIFICATION_WINDOW_NS);
        self.emit(FakeInputEvent::ReleaseAll(FakeReleaseAll { reason }));
        if self.verify_release {
            let request = FakeVerifyReleaseParams {
                release_id,
                through_seq: self.seq,
                deadline_ns,
            };
            if let Ok(Value::Object(params)) = serde_json::to_value(request) {
                self.queue.push(FakeVerifyRelease::NAME, params);
            }
        }
    }

    fn verifies_release(&self) -> bool {
        self.verify_release
    }
}

/// 已握手、还没开始处理消息的测试设备连接。
pub(crate) struct TestDeviceConnection {
    connection: Connection,
}

/// 以 `role` 连上测试一方并握手。
///
/// # Errors
///
/// 连不上或握手失败时返回通道错误。
pub(crate) fn connect_test_device(
    credentials: &ChannelCredentials,
    role: &str,
) -> Result<TestDeviceConnection, ChannelError> {
    connect_side(credentials, role).map(|connection| TestDeviceConnection { connection })
}

/// 测试设备连接在许可上的角色。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum DeviceRole {
    /// 测试许可源：处理 `test_lease.*`，连接断开即没有许可。
    LeaseSource,
    /// 只收假输入事件：许可来自操作员许可连接，`test_lease.*` 回“方法不存在”。
    EventsOnly,
}

impl TestDeviceConnection {
    /// 开始写出假后端的事件（`events` 是 [`FakeBackend::new`] 交出的队列）；作为测试许可源时另处理
    /// `test_lease.*`，并在连接断开时让许可失效。
    ///
    /// # Errors
    ///
    /// 无法创建线程时返回 IO 错误。
    pub(crate) fn start(
        self,
        role: DeviceRole,
        events: Arc<SendQueue>,
        control: &Arc<Control>,
        post: &Post,
        verify_release: bool,
    ) -> io::Result<SideConnection> {
        let handlers = match role {
            DeviceRole::LeaseSource => with_lease_handlers(
                Handlers::new(),
                TestLeaseGrant::NAME,
                TestLeaseRevoke::NAME,
                control,
                post,
            ),
            DeviceRole::EventsOnly => Handlers::new(),
        };
        let request = verify_release.then(|| verification_sender(control, post));
        let device = SideConnection::spawn_with_requests(
            "test-device",
            self.connection,
            handlers,
            events,
            request,
        )?;
        if role == DeviceRole::LeaseSource || verify_release {
            device.watch_as_lease_source(control, post)?;
        }
        Ok(device)
    }
}

fn checked_result(
    request: &FakeVerifyReleaseParams,
    reply: Value,
    now_ns: u64,
    open: bool,
) -> VerificationResult {
    let Ok(reply) = serde_json::from_value::<FakeVerifyReleaseResult>(reply) else {
        return VerificationResult::Unavailable;
    };
    if !open
        || now_ns >= request.deadline_ns
        || reply.release_id != request.release_id
        || reply.through_seq != request.through_seq
    {
        return VerificationResult::Unavailable;
    }
    if reply.held.is_empty() {
        VerificationResult::Verified
    } else {
        VerificationResult::Residual
    }
}

fn verification_sender(control: &Arc<Control>, post: &Post) -> RequestSender {
    let (control, post) = (Arc::clone(control), Arc::clone(post));
    Box::new(move |peer, method, params: &Map<String, Value>| {
        if method != FakeVerifyRelease::NAME {
            return false;
        }
        let Ok(request) =
            serde_json::from_value::<FakeVerifyReleaseParams>(Value::Object(params.clone()))
        else {
            post(control.lease_source_lost());
            return true;
        };
        let now_ns = mono_clock::now_ns();
        let observed = if request.deadline_ns <= now_ns {
            VerificationResult::Unavailable
        } else {
            peer.request_with_timeout(
                method,
                Some(params),
                Duration::from_nanos(request.deadline_ns - now_ns),
            )
            .map_or(VerificationResult::Unavailable, |reply| {
                checked_result(&request, reply, mono_clock::now_ns(), !peer.is_closed())
            })
        };
        let proof = control.finish_verification(request.release_id, observed);
        post(Notices(vec![Notice::Verification(proof)]));
        control.verification_posted(proof);
        true
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn proof_needs_matching_watermark_open_peer_and_live_deadline() {
        let request = FakeVerifyReleaseParams {
            release_id: 4,
            through_seq: 9,
            deadline_ns: 200,
        };
        let clear = json!({"release_id": 4, "through_seq": 9, "held": []});
        assert_eq!(
            checked_result(&request, clear.clone(), 199, true),
            VerificationResult::Verified
        );
        assert_eq!(
            checked_result(&request, clear.clone(), 200, true),
            VerificationResult::Unavailable
        );
        assert_eq!(
            checked_result(&request, clear, 199, false),
            VerificationResult::Unavailable
        );
        for bad in [
            json!({"release_id": 3, "through_seq": 9, "held": []}),
            json!({"release_id": 4, "through_seq": 8, "held": []}),
            json!({"release_id": 4, "through_seq": 9, "held": [], "extra": true}),
            json!({"release_id": 4, "through_seq": 9, "held": [{"type": "unknown"}]}),
            json!({"release_id": 4, "through_seq": 9}),
        ] {
            assert_eq!(
                checked_result(&request, bad, 199, true),
                VerificationResult::Unavailable
            );
        }
        assert_eq!(
            checked_result(
                &request,
                json!({"release_id": 4, "through_seq": 9, "held": [{"type": "key", "scan_code": 17}]}),
                199,
                true
            ),
            VerificationResult::Residual
        );
    }

    #[test]
    fn verification_request_follows_release_event_on_the_same_fifo() {
        for enabled in [false, true] {
            let (mut backend, queue) = FakeBackend::new(enabled);
            backend.key_down(17);
            backend.key_up(17);
            backend.released_all(ReleaseReason::Stop, 7);
            let mut seen = Vec::new();
            queue.run(|name, params| {
                seen.push((name, params.clone()));
                if (enabled && name == FakeVerifyRelease::NAME) || (!enabled && seen.len() == 3) {
                    queue.abandon();
                }
            });
            assert_eq!(seen.len(), if enabled { 4 } else { 3 });
            assert_eq!(seen[2].1["seq"], 3);
            assert_eq!(seen[2].1["event"]["type"], "release_all");
            if enabled {
                assert_eq!(seen[3].1["release_id"], 7);
                assert_eq!(seen[3].1["through_seq"], 3);
            }
            assert_eq!(backend.verifies_release(), enabled);
        }
    }
}
