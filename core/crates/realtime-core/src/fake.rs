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

use bellis_channel::{ChannelCredentials, ChannelError, Connection, Handlers};
use core_protocol::messages::{
    Action, FakeAxisRelease, FakeAxisSet, FakeClick, FakeInputEvent, FakeInputEventParams,
    FakeKeyDown, FakeKeyPress, FakeKeyUp, FakeMouseDown, FakeMouseMove, FakeMouseUp,
    FakeReleaseAll, FakeScroll, HeldInput, ReleaseReason,
};
use core_protocol::methods::{FakeInput, Method, TestLeaseGrant, TestLeaseRevoke};
use serde_json::Value;

use crate::control::Control;
use crate::ledger::InputBackend;
use crate::queue::SendQueue;
use crate::side::{Post, SideConnection, connect_side, with_lease_handlers};

/// 假输入后端：每个输入都作为一条 `fake_input.event` 放进发送队列。
pub(crate) struct FakeBackend {
    queue: Arc<SendQueue>,
    seq: u64,
}

impl FakeBackend {
    /// 新的假后端与它的发送队列；队列由测试设备连接的发送线程写出（[`TestDeviceConnection::start`]）。
    pub(crate) fn new() -> (Self, Arc<SendQueue>) {
        let queue = Arc::new(SendQueue::default());
        let backend = Self {
            queue: Arc::clone(&queue),
            seq: 0,
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

impl InputBackend for FakeBackend {
    fn press(&mut self, input: &HeldInput) {
        self.emit(match input {
            HeldInput::Key(key) => FakeInputEvent::KeyDown(FakeKeyDown {
                scan_code: key.scan_code,
            }),
            HeldInput::MouseButton(button) => FakeInputEvent::MouseDown(FakeMouseDown {
                button: button.button,
            }),
            HeldInput::Axis(axis) => FakeInputEvent::AxisSet(FakeAxisSet {
                axis: axis.axis.clone(),
                value: axis.value,
            }),
        });
    }

    fn release(&mut self, input: &HeldInput) {
        self.emit(match input {
            HeldInput::Key(key) => FakeInputEvent::KeyUp(FakeKeyUp {
                scan_code: key.scan_code,
            }),
            HeldInput::MouseButton(button) => FakeInputEvent::MouseUp(FakeMouseUp {
                button: button.button,
            }),
            HeldInput::Axis(axis) => FakeInputEvent::AxisRelease(FakeAxisRelease {
                axis: axis.axis.clone(),
            }),
        });
    }

    fn act(&mut self, action: &Action) {
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
    }

    fn released_all(&mut self, reason: ReleaseReason) {
        self.emit(FakeInputEvent::ReleaseAll(FakeReleaseAll { reason }));
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
        let device = SideConnection::spawn("test-device", self.connection, handlers, events)?;
        if role == DeviceRole::LeaseSource {
            device.watch_as_lease_source(control, post)?;
        }
        Ok(device)
    }
}
