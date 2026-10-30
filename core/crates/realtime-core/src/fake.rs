//! 假输入后端与测试设备连接，只用于测试（`docs/modules/realtime-core.md` 的“假输入后端与测试设备连接”）。
//!
//! 实时核以 `--input-backend fake` 启动时，另作为 `bellis-channel/1` 客户端连到测试一方。这条连接承载：
//!
//! - 测试许可源：测试一方以 `test_lease.grant` 授予与续约许可，以 `test_lease.revoke` 吊销；连接断开即
//!   没有许可（`lease_source_lost`）。
//! - 假输入事件：假后端把每个按下、抬起、轴、一次性动作与释放全部作为 `fake_input.event` 发给测试一方。
//!
//! 许可不来自主进程：主进程只能让输入停下，不能让输入继续。测试一方能独立看到实时核是否发出了抬起，
//! 主进程崩溃时也一样。生产中没有这条连接；测试许可源只在这里构造，只能与假后端一起出现。
//!
//! 假后端把事件放进发送队列就返回，从不在控制状态的锁内等网络写入；队列由专门的线程写出。

use std::io;
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::Duration;

use bellis_channel::{
    ChannelCredentials, ChannelError, Connection, ConnectionOptions, Handlers, RpcError,
    RpcErrorCode, RpcOptions, RpcPeer, connect,
};
use core_protocol::messages::{
    Action, Empty, FakeAxisRelease, FakeAxisSet, FakeClick, FakeInputEvent, FakeInputEventParams,
    FakeKeyDown, FakeKeyPress, FakeKeyUp, FakeLeaseGrantParams, FakeLeaseRevokeParams,
    FakeMouseDown, FakeMouseMove, FakeMouseUp, FakeReleaseAll, FakeScroll, HeldInput,
    ReleaseReason,
};
use core_protocol::methods::{FakeInput, Method, TestLeaseGrant, TestLeaseRevoke, error_code};
use serde_json::{Map, Value};

use crate::control::{Control, Notices};
use crate::ledger::InputBackend;
use crate::queue::SendQueue;

/// 测试设备连接上请求的截止时间。实时核在这条连接上不发请求，只用于 `RpcPeer` 的配置。
const RPC_TIMEOUT: Duration = Duration::from_secs(2);

/// 假输入后端：每个输入都作为一条 `fake_input.event` 放进发送队列。
pub(crate) struct FakeBackend {
    queue: Arc<SendQueue>,
    seq: u64,
}

impl FakeBackend {
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
    queue: Arc<SendQueue>,
}

/// 以 `role` 连上测试一方并握手，交出连接与写往它的假输入后端。
///
/// # Errors
///
/// 连不上或握手失败时返回通道错误。
pub(crate) fn connect_test_device(
    credentials: &ChannelCredentials,
    role: &str,
) -> Result<(TestDeviceConnection, FakeBackend), ChannelError> {
    let connection = connect(
        &credentials.path,
        role,
        &credentials.key,
        &ConnectionOptions::default(),
    )?;
    let queue = Arc::new(SendQueue::default());
    let backend = FakeBackend {
        queue: Arc::clone(&queue),
        seq: 0,
    };
    Ok((TestDeviceConnection { connection, queue }, backend))
}

/// 把通知交给核连接的发送队列；测试设备连接的线程不直接写核连接。
pub(crate) type Post = Arc<dyn Fn(Notices) + Send + Sync>;

fn parse<T: serde::de::DeserializeOwned>(
    method: &str,
    params: &Map<String, Value>,
) -> Result<T, RpcError> {
    serde_json::from_value(Value::Object(params.clone())).map_err(|error| {
        RpcError::new(
            RpcErrorCode::INVALID_PARAMS,
            format!("invalid params for {method}: {error}"),
        )
    })
}

fn empty() -> Value {
    serde_json::to_value(Empty {}).unwrap_or_else(|_| Value::Object(Map::new()))
}

impl TestDeviceConnection {
    /// 开始处理测试许可源的请求、写出假输入事件，并在连接断开时让许可失效。
    ///
    /// # Errors
    ///
    /// 无法创建线程时返回 IO 错误。
    pub(crate) fn start(self, control: &Arc<Control>, post: &Post) -> io::Result<TestDevice> {
        let grant = {
            let (control, post) = (Arc::clone(control), Arc::clone(post));
            move |params: &Map<String, Value>| {
                let grant: FakeLeaseGrantParams = parse(TestLeaseGrant::NAME, params)?;
                let (result, notices) = control.grant_lease(&grant, mono_clock::now_ns());
                post(notices);
                match result {
                    Ok(()) => Ok(empty()),
                    Err(refusal) => {
                        log!("{} refused: {refusal}", TestLeaseGrant::NAME);
                        Err(RpcError::new(
                            i64::from(error_code::LEASE_REFUSED),
                            refusal.to_string(),
                        ))
                    }
                }
            }
        };
        let revoke = {
            let (control, post) = (Arc::clone(control), Arc::clone(post));
            move |params: &Map<String, Value>| {
                let revoke: FakeLeaseRevokeParams = parse(TestLeaseRevoke::NAME, params)?;
                // 释放完成后才回应。
                post(control.revoke_lease(revoke.token, mono_clock::now_ns()));
                Ok(empty())
            }
        };
        let handlers = Handlers::new()
            .request(TestLeaseGrant::NAME, grant)
            .request(TestLeaseRevoke::NAME, revoke);
        let peer = Arc::new(RpcPeer::spawn(
            self.connection,
            handlers,
            &RpcOptions {
                timeout: RPC_TIMEOUT,
            },
        )?);
        let mut device = TestDevice {
            peer: Arc::clone(&peer),
            queue: Arc::clone(&self.queue),
            sender: None,
        };
        let sender = thread::Builder::new()
            .name("test-device-sender".into())
            .spawn({
                let (queue, peer) = (Arc::clone(&self.queue), Arc::clone(&peer));
                move || {
                    let mut failed = false;
                    queue.run(|method, params| {
                        if let Err(error) = peer.notify(method, Some(params))
                            && !failed
                        {
                            log!("test device: {method} not sent: {error}");
                            failed = true;
                        }
                    });
                }
            })?;
        device.sender = Some(sender);
        // 阻塞在等连接关闭上，进程退出时随之结束，不等待。
        thread::Builder::new()
            .name("test-device-lifeline".into())
            .spawn({
                let (control, post) = (Arc::clone(control), Arc::clone(post));
                move || {
                    let reason = peer.wait_closed(None).flatten();
                    let notices = control.lease_source_lost();
                    if !notices.is_empty() {
                        log!(
                            "test device connection closed ({}); no lease from now on",
                            reason.map_or_else(|| "by the peer".to_owned(), |e| e.to_string())
                        );
                    }
                    post(notices);
                }
            })?;
        Ok(device)
    }
}

/// 运行中的测试设备连接。
pub(crate) struct TestDevice {
    peer: Arc<RpcPeer>,
    queue: Arc<SendQueue>,
    sender: Option<JoinHandle<()>>,
}

impl TestDevice {
    /// 等已放进队列的事件写出（至多 `timeout`），再关闭连接。实时核退出前调用：释放全部输入的抬起
    /// 由此送到测试一方。
    pub(crate) fn finish(mut self, timeout: Duration) {
        if !self.queue.flush(timeout) {
            log!(
                "test device: input events not flushed within {}ms",
                timeout.as_millis()
            );
        }
        self.close();
    }

    fn close(&mut self) {
        self.queue.abandon();
        self.peer.close();
        if let Some(sender) = self.sender.take() {
            let _ = sender.join();
        }
    }
}

impl Drop for TestDevice {
    fn drop(&mut self) {
        self.close();
    }
}
