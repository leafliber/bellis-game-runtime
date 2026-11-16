//! 实时核作为通道客户端连出的旁路连接：操作员许可连接与只用于测试的测试设备连接
//! （`docs/modules/core-protocol.md` 的“操作员许可连接”与“测试设备连接”）。
//!
//! 两者的形状相同：一个 `RpcPeer`、一个写往对端的发送队列与它的发送线程。许可源（操作员控制台，或没有
//! 操作员许可连接时的测试许可源）另有授予与吊销的处理函数，以及一条生命线：连接断开即没有许可
//! （`lease_source_lost`），断开后不重连。
//!
//! 发往对端的通知（假输入事件、`input.released`）先放进发送队列，由发送线程写出：释放路径与读线程上的
//! 处理函数从不等网络写入。

use std::io;
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::Duration;

use bellis_channel::{
    ChannelCredentials, ChannelError, Connection, ConnectionOptions, Handlers, RpcError,
    RpcErrorCode, RpcOptions, RpcPeer, connect,
};
use core_protocol::messages::{Empty, LeaseGrantParams, LeaseRevokeParams};
use core_protocol::methods::error_code;
use serde_json::{Map, Value};

use crate::control::{Control, LeaseGrant, Notices};
use crate::queue::SendQueue;

/// 旁路连接上请求的截止时间。实时核在这些连接上不发请求，只用于 `RpcPeer` 的配置。
const RPC_TIMEOUT: Duration = Duration::from_secs(2);

/// 把通知交给核连接的出口（放进发送队列）；旁路连接的线程不直接写核连接。
pub(crate) type Post = Arc<dyn Fn(Notices) + Send + Sync>;

/// 测试设备的核验请求与事件共用一个发送队列；返回 true 表示该项已处理。
pub(crate) type RequestSender =
    Box<dyn Fn(&RpcPeer, &'static str, &Map<String, Value>) -> bool + Send>;

/// 以 `role` 连上对端并握手。
///
/// # Errors
///
/// 连不上或握手失败时返回通道错误。
pub(crate) fn connect_side(
    credentials: &ChannelCredentials,
    role: &str,
) -> Result<Connection, ChannelError> {
    connect(
        &credentials.path,
        role,
        &credentials.key,
        &ConnectionOptions::default(),
    )
}

/// 运行中的旁路连接：处理对端的请求，并由发送线程把 `queue` 里的通知写给对端。
pub(crate) struct SideConnection {
    name: &'static str,
    peer: Arc<RpcPeer>,
    queue: Arc<SendQueue>,
    sender: Option<JoinHandle<()>>,
}

impl SideConnection {
    /// 开始处理 `handlers` 里的请求，并启动写出 `queue` 的发送线程。`name` 只用于日志与线程名。
    ///
    /// # Errors
    ///
    /// 无法创建线程时返回 IO 错误。
    pub(crate) fn spawn(
        name: &'static str,
        connection: Connection,
        handlers: Handlers,
        queue: Arc<SendQueue>,
    ) -> io::Result<Self> {
        Self::spawn_with_requests(name, connection, handlers, queue, None)
    }

    /// 仅显式假设备模式使用请求处理接缝，普通旁路连接仍只发送通知。
    pub(crate) fn spawn_with_requests(
        name: &'static str,
        connection: Connection,
        handlers: Handlers,
        queue: Arc<SendQueue>,
        request: Option<RequestSender>,
    ) -> io::Result<Self> {
        let peer = Arc::new(RpcPeer::spawn(
            connection,
            handlers,
            &RpcOptions {
                timeout: RPC_TIMEOUT,
            },
        )?);
        let mut side = Self {
            name,
            peer: Arc::clone(&peer),
            queue: Arc::clone(&queue),
            sender: None,
        };
        let sender = thread::Builder::new()
            .name(format!("{name}-sender"))
            .spawn(move || {
                let mut failed = false;
                queue.run(|method, params| {
                    if request
                        .as_ref()
                        .is_some_and(|send| send(&peer, method, params))
                    {
                        return;
                    }
                    if let Err(error) = peer.notify(method, Some(params))
                        && !failed
                    {
                        log!("{name}: {method} not sent: {error}");
                        failed = true;
                    }
                });
            })?;
        side.sender = Some(sender);
        Ok(side)
    }

    /// 许可源的生命线：连接断开即此后没有许可，释放全部输入（`lease_source_lost`），通知经 `post` 发出。
    /// 不重连。
    ///
    /// # Errors
    ///
    /// 无法创建线程时返回 IO 错误。
    pub(crate) fn watch_as_lease_source(
        &self,
        control: &Arc<Control>,
        post: &Post,
    ) -> io::Result<()> {
        // 阻塞在等连接关闭上，进程退出时随之结束，不等待。
        let name = self.name;
        let (peer, control, post) = (
            Arc::clone(&self.peer),
            Arc::clone(control),
            Arc::clone(post),
        );
        thread::Builder::new()
            .name(format!("{name}-lifeline"))
            .spawn(move || {
                let reason = peer.wait_closed(None).flatten();
                let notices = control.lease_source_lost();
                if !notices.is_empty() {
                    log!(
                        "{name} connection closed ({}); no lease from now on",
                        reason.map_or_else(|| "by the peer".to_owned(), |e| e.to_string())
                    );
                }
                post(notices);
            })?;
        Ok(())
    }

    /// 等已放进队列的通知写出（至多 `timeout`），再关闭连接。实时核退出前调用。
    pub(crate) fn finish(mut self, timeout: Duration) {
        if !self.queue.flush(timeout) {
            log!(
                "{}: notifications not flushed within {}ms",
                self.name,
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

impl Drop for SideConnection {
    fn drop(&mut self) {
        self.close();
    }
}

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

/// 在 `handlers` 上登记许可源的两个请求：`grant` 授予或续约（不被接受回 -32004），`revoke` 吊销（释放
/// 完成后才回应）。操作员许可连接与测试许可源用同一套规则（[`Control::grant_lease`]、
/// [`Control::revoke_lease`]），只是方法名不同。
pub(crate) fn with_lease_handlers(
    handlers: Handlers,
    grant: &'static str,
    revoke: &'static str,
    control: &Arc<Control>,
    post: &Post,
) -> Handlers {
    let grant_handler = {
        let (control, post) = (Arc::clone(control), Arc::clone(post));
        move |params: &Map<String, Value>| {
            let params: LeaseGrantParams = parse(grant, params)?;
            let (result, notices) = control.grant_lease(&params, mono_clock::now_ns);
            post(notices);
            let result = result.and_then(|grant| match grant {
                LeaseGrant::Applied => Ok(()),
                LeaseGrant::Verifying(release_id) => {
                    control.wait_for_lease_proof(release_id)?;
                    control.confirm_lease_grant(&params, release_id, mono_clock::now_ns)
                }
            });
            match result {
                Ok(()) => Ok(empty()),
                Err(refusal) => {
                    log!("{grant} refused: {refusal}");
                    Err(RpcError::new(
                        i64::from(error_code::LEASE_REFUSED),
                        refusal.to_string(),
                    ))
                }
            }
        }
    };
    let revoke_handler = {
        let (control, post) = (Arc::clone(control), Arc::clone(post));
        move |params: &Map<String, Value>| {
            let params: LeaseRevokeParams = parse(revoke, params)?;
            // 释放完成后才回应。
            post(control.revoke_lease(params.token, mono_clock::now_ns));
            Ok(empty())
        }
    };
    handlers
        .request(grant, grant_handler)
        .request(revoke, revoke_handler)
}
