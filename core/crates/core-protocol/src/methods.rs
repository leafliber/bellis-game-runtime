//! JSON-RPC 方法目录：每个方法的名字、方向与 params、result 的类型。
//!
//! 核连接上实时核是通道客户端，主进程是服务端，两边都可以发请求与通知（JSON-RPC 2.0）。
//! 有 result 的方法是请求，没有的是通知。
//!
//! 两个目录：[`catalog`] 是核连接上的方法；[`test_device_catalog`] 是只用于测试的测试设备连接上的方法
//! （实时核以假输入后端启动时连到测试一方），见 `docs/modules/core-protocol.md` 的“测试设备连接”。

use schemars::{JsonSchema, Schema, SchemaGenerator};
use serde::Serialize;
use serde::de::DeserializeOwned;

use crate::messages::{
    ActionBlock, ActionReceiptParams, ActionSubmitResult, CaptureSetTargetParams, CoreHealthParams,
    CoreHelloParams, CoreHelloResult, Empty, FakeInputEventParams, FakeLeaseGrantParams,
    FakeLeaseRevokeParams, FrameRingAttachParams, InputReleasedParams, ProducerHeartbeatParams,
    ProducerRevokeParams,
};

/// 消息的发送方。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Direction {
    /// 实时核发往主进程。
    CoreToMain,
    /// 主进程发往实时核。
    MainToCore,
    /// 实时核发往测试一方（测试设备连接，只用于测试）。
    CoreToTest,
    /// 测试一方发往实时核（测试设备连接，只用于测试）。
    TestToCore,
}

/// 一个 JSON-RPC 方法。
pub trait Method {
    /// JSON-RPC 的 `method`。
    const NAME: &'static str;
    /// 发送方。
    const DIRECTION: Direction;
    /// `params` 的类型；不带参数的方法也发 `{}`。
    type Params: Serialize + DeserializeOwned + JsonSchema;
}

/// 有响应的方法（JSON-RPC 请求）。
pub trait Request: Method {
    /// 响应中 `result` 的类型。
    type Result: Serialize + DeserializeOwned + JsonSchema;
}

/// 没有响应的方法（JSON-RPC 通知）。
pub trait Notification: Method {}

/// 目录中的一个方法，供导出 Schema。
#[derive(Debug, Clone, Copy)]
pub struct MethodInfo {
    /// JSON-RPC 的 `method`。
    pub name: &'static str,
    /// 发送方。
    pub direction: Direction,
    /// 生成 `params` 的子 Schema。
    pub params: fn(&mut SchemaGenerator) -> Schema,
    /// 生成 `result` 的子 Schema；通知为 `None`。
    pub result: Option<fn(&mut SchemaGenerator) -> Schema>,
}

fn subschema<T: JsonSchema>(generator: &mut SchemaGenerator) -> Schema {
    generator.subschema_for::<T>()
}

/// 声明一组方法：为每个方法定义标记类型并实现 [`Method`] 与 [`Request`] 或 [`Notification`]，
/// 同时生成这一组的目录函数，使目录与类型不会脱节。
macro_rules! methods {
    (@kind $marker:ident => $result:ty) => {
        impl Request for $marker {
            type Result = $result;
        }
    };
    (@kind $marker:ident) => {
        impl Notification for $marker {}
    };
    (@result $result:ty) => {
        Some(subschema::<$result>)
    };
    (@result) => {
        None
    };
    (
        $(#[$catalog_attr:meta])*
        fn $catalog:ident;
        $(
            $(#[$attr:meta])*
            $marker:ident = $name:literal, $direction:ident, $params:ty $(=> $result:ty)?;
        )*
    ) => {
        $(
            $(#[$attr])*
            #[derive(Debug, Clone, Copy)]
            pub struct $marker;

            impl Method for $marker {
                const NAME: &'static str = $name;
                const DIRECTION: Direction = Direction::$direction;
                type Params = $params;
            }

            methods!(@kind $marker $(=> $result)?);
        )*

        $(#[$catalog_attr])*
        #[must_use]
        pub fn $catalog() -> Vec<MethodInfo> {
            vec![$(
                MethodInfo {
                    name: $name,
                    direction: Direction::$direction,
                    params: subschema::<$params>,
                    result: methods!(@result $($result)?),
                }
            ),*]
        }
    };
}

methods! {
    /// 核连接上的全部方法，按声明顺序。
    fn catalog;
    /// 握手：实时核通过通道认证后发出的第一条消息。
    CoreHello = "core.hello", CoreToMain, CoreHelloParams => CoreHelloResult;
    /// 帧环挂接：告诉实时核主进程创建的帧环。
    FrameRingAttach = "frame_ring.attach", MainToCore, FrameRingAttachParams => Empty;
    /// 设定采集目标。
    CaptureSetTarget = "capture.set_target", MainToCore, CaptureSetTargetParams => Empty;
    /// 控制产出者心跳。
    ProducerHeartbeat = "producer.heartbeat", MainToCore, ProducerHeartbeatParams;
    /// 提交动作块。
    ActionSubmit = "action.submit", MainToCore, ActionBlock => ActionSubmitResult;
    /// 作废代次；释放完成后才回应，回应即输入已释放的确认。
    ProducerRevoke = "producer.revoke", MainToCore, ProducerRevokeParams => Empty;
    /// 正常停止（Bellis R-06 停止顺序中的 RPC 一步）。
    CoreStop = "core.stop", MainToCore, Empty => Empty;
    /// 执行回执。
    ActionReceipt = "action.receipt", CoreToMain, ActionReceiptParams;
    /// 输入已释放的确认。
    InputReleased = "input.released", CoreToMain, InputReleasedParams;
    /// 实时核健康。
    CoreHealth = "core.health", CoreToMain, CoreHealthParams;
}

methods! {
    /// 测试设备连接上的全部方法，按声明顺序。只用于测试：实时核以假输入后端启动时才有这条连接。
    fn test_device_catalog;
    /// 测试许可源授予或续约输入许可。
    TestLeaseGrant = "test_lease.grant", TestToCore, FakeLeaseGrantParams => Empty;
    /// 测试许可源吊销输入许可；释放完成后才回应。
    TestLeaseRevoke = "test_lease.revoke", TestToCore, FakeLeaseRevokeParams => Empty;
    /// 假输入后端交给假输入设备的一个事件。
    FakeInput = "fake_input.event", CoreToTest, FakeInputEventParams;
}

/// 核协议自定义的 JSON-RPC 错误码；其它错误用 JSON-RPC 2.0 的标准错误码。
pub mod error_code {
    /// `core.hello` 的协议版本与主进程的不相等。
    pub const PROTOCOL_VERSION_MISMATCH: i32 = -32001;
    /// `frame_ring.attach` 打不开共享内存，或帧环头部与参数不符；`capture.set_target` 的假显示面同样。
    pub const FRAME_RING_MISMATCH: i32 = -32002;
    /// `capture.set_target` 的目标在这个平台或系统上不支持。
    pub const CAPTURE_UNSUPPORTED: i32 = -32003;
    /// `test_lease.grant` 不被接受：令牌小于当前令牌、已吊销或已过期，或到期时刻已过。只用于测试。
    pub const LEASE_REFUSED: i32 = -32004;

    /// 全部自定义错误码及其名字，供导出 Schema。
    pub const ALL: [(&str, i32); 4] = [
        ("protocol_version_mismatch", PROTOCOL_VERSION_MISMATCH),
        ("frame_ring_mismatch", FRAME_RING_MISMATCH),
        ("capture_unsupported", CAPTURE_UNSUPPORTED),
        ("lease_refused", LEASE_REFUSED),
    ];
}
