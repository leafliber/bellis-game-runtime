//! 操作员许可连接（`docs/modules/core-protocol.md` 的“操作员许可连接”）。
//!
//! 独立运行中（GR-36），实时核另作为 `bellis-channel/1` 客户端连到操作员控制台。这条连接只有输入许可：
//! 控制台以 `operator_lease.grant` 授予与续约，以 `operator_lease.revoke` 吊销；实时核每次释放全部输入都在
//! 这条连接上发 `input.released`（与核连接上的同名、同 params）。连接断开即没有许可
//! （`lease_source_lost`），不重连。
//!
//! 许可不来自主进程：主进程只能让输入停下，不能让输入继续。拓扑与 G3 的控制连接相同。

use std::io;
use std::sync::Arc;

use bellis_channel::{ChannelCredentials, ChannelError, Connection, Handlers};
use core_protocol::methods::{Method, OperatorLeaseGrant, OperatorLeaseRevoke};

use crate::control::Control;
use crate::queue::SendQueue;
use crate::side::{Post, SideConnection, connect_side, with_lease_handlers};

/// 已握手、还没开始处理消息的操作员许可连接。
pub(crate) struct OperatorConnection {
    connection: Connection,
}

/// 以 `role` 连上操作员控制台并握手。
///
/// # Errors
///
/// 连不上或握手失败时返回通道错误。
pub(crate) fn connect_operator(
    credentials: &ChannelCredentials,
    role: &str,
) -> Result<OperatorConnection, ChannelError> {
    connect_side(credentials, role).map(|connection| OperatorConnection { connection })
}

impl OperatorConnection {
    /// 开始处理控制台的授予与吊销，写出 `released` 队列里的 `input.released`，并在连接断开时让许可失效。
    ///
    /// # Errors
    ///
    /// 无法创建线程时返回 IO 错误。
    pub(crate) fn start(
        self,
        released: Arc<SendQueue>,
        control: &Arc<Control>,
        post: &Post,
    ) -> io::Result<SideConnection> {
        let handlers = with_lease_handlers(
            Handlers::new(),
            OperatorLeaseGrant::NAME,
            OperatorLeaseRevoke::NAME,
            control,
            post,
        );
        let operator = SideConnection::spawn("operator", self.connection, handlers, released)?;
        operator.watch_as_lease_source(control, post)?;
        Ok(operator)
    }
}
