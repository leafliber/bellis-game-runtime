//! Bellis 游戏桥控制角色的线协议（GD-03、GR-05）。
//! 解码入口接受认证后的原始 JSON 帧。响应关联由调用方提供同一连接上仍在等待的请求。

mod payload;
mod wire;

use std::num::NonZeroU16;

use serde::Serialize;
use serde_json::value::RawValue;

pub use payload::{
    Ack, CoreDescription, CoreHealth, DescribeParams, InputReleased, InputStopParams,
    LeaseRenewParams, LeaseState, LeaseToken, LocalDialogue, LocalRecovery, LocalRecoveryKind,
    MAX_MONOTONIC_MS, MAX_TOKEN, MonotonicMs, ReleaseReason, ReleaseVerification, RuntimeId,
    StopReason, ThroughToken,
};
pub use wire::{DecodeError, RequestId};
use wire::{Object, is_safe_integer};

pub const ROLE: &str = "game-runtime.core";
pub const GAME_BRIDGE_MAJOR: NonZeroU16 = NonZeroU16::MIN;
pub const LEASE_REFUSED: i64 = -32_004;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
enum RpcVersion {
    #[serde(rename = "2.0")]
    V2,
}

fn envelope(raw: &[u8]) -> Result<Object<'_>, DecodeError> {
    let mut object = Object::read(raw)?;
    if object.field::<String>("jsonrpc")? != "2.0" {
        return Err(DecodeError::InvalidField("jsonrpc"));
    }
    Ok(object)
}

/// 控制角色只接受 Bellis 下行的应用请求。
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct BellisRequest {
    jsonrpc: RpcVersion,
    pub id: RequestId,
    #[serde(flatten)]
    pub call: ControlCall,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(tag = "method", content = "params")]
pub enum ControlCall {
    #[serde(rename = "game.describe")]
    Describe(DescribeParams),
    #[serde(rename = "lease.renew")]
    LeaseRenew(LeaseRenewParams),
    #[serde(rename = "input.stop")]
    InputStop(InputStopParams),
}

impl BellisRequest {
    #[must_use]
    pub const fn new(id: RequestId, call: ControlCall) -> Self {
        Self {
            jsonrpc: RpcVersion::V2,
            id,
            call,
        }
    }

    /// # Errors
    /// JSON-RPC 信封、方法方向、消息类别或正文不符合控制契约。
    pub fn decode(raw: &[u8]) -> Result<Self, DecodeError> {
        let mut object = envelope(raw)?;
        let id = RequestId::decode(object.take("id")?)?;
        let method = object.field::<String>("method")?;
        let params = object.take("params")?;
        let call = match method.as_str() {
            "game.describe" => ControlCall::Describe(DescribeParams::decode(params)?),
            "lease.renew" => ControlCall::LeaseRenew(LeaseRenewParams::decode(params)?),
            "input.stop" => ControlCall::InputStop(InputStopParams::decode(params)?),
            _ => return Err(DecodeError::UnexpectedMethod),
        };
        object.finish(Self::new(id, call))
    }
}

/// 控制角色从实时核发出的应用通知。
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub struct RuntimeNotification {
    jsonrpc: RpcVersion,
    #[serde(flatten)]
    pub event: ControlEvent,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(tag = "method", content = "params")]
pub enum ControlEvent {
    #[serde(rename = "input.released")]
    InputReleased(InputReleased),
    #[serde(rename = "health.reported")]
    HealthReported(CoreHealth),
}

impl RuntimeNotification {
    #[must_use]
    pub const fn new(event: ControlEvent) -> Self {
        Self {
            jsonrpc: RpcVersion::V2,
            event,
        }
    }

    /// # Errors
    /// JSON-RPC 信封、方法方向、消息类别或正文不符合控制契约。
    pub fn decode(raw: &[u8]) -> Result<Self, DecodeError> {
        let mut object = envelope(raw)?;
        let method = object.field::<String>("method")?;
        let params = object.take("params")?;
        let event = match method.as_str() {
            "input.released" => ControlEvent::InputReleased(InputReleased::decode(params)?),
            "health.reported" => ControlEvent::HealthReported(CoreHealth::decode(params)?),
            _ => return Err(DecodeError::UnexpectedMethod),
        };
        object.finish(Self::new(event))
    }
}

#[derive(Clone, Debug, Serialize)]
#[serde(untagged)]
pub enum RuntimeResponse {
    Success(RuntimeSuccess),
    Failure(RuntimeFailure),
}

#[derive(Clone, Debug, Serialize)]
pub struct RuntimeSuccess {
    jsonrpc: RpcVersion,
    pub id: RequestId,
    pub result: ControlResult,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(untagged)]
pub enum ControlResult {
    Description(CoreDescription),
    Ack(Ack),
}

#[derive(Clone, Debug, Serialize)]
pub struct RuntimeFailure {
    jsonrpc: RpcVersion,
    pub id: Option<RequestId>,
    pub error: RpcError,
}

/// 任意 JSON 的 data 原样保留，缺失与显式 null 分开表示。
#[derive(Clone, Debug, Serialize)]
pub struct RpcError {
    code: i64,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub data: Option<Box<RawValue>>,
}

impl RpcError {
    #[must_use]
    pub fn new(code: i64, message: String, data: Option<Box<RawValue>>) -> Option<Self> {
        is_safe_integer(code).then_some(Self {
            code,
            message,
            data,
        })
    }

    #[must_use]
    pub const fn code(&self) -> i64 {
        self.code
    }

    fn decode(raw: &RawValue) -> Result<Self, DecodeError> {
        let mut object = Object::payload(raw)?;
        let code = object.integer("code")?;
        let message = object.field("message")?;
        let data = object.remove("data").map(ToOwned::to_owned);
        let error = Self::new(code, message, data).ok_or(DecodeError::InvalidField("code"))?;
        object.finish(error)
    }
}

impl RuntimeResponse {
    #[must_use]
    pub const fn success(id: RequestId, result: ControlResult) -> Self {
        Self::Success(RuntimeSuccess {
            jsonrpc: RpcVersion::V2,
            id,
            result,
        })
    }

    #[must_use]
    pub const fn failure(id: Option<RequestId>, error: RpcError) -> Self {
        Self::Failure(RuntimeFailure {
            jsonrpc: RpcVersion::V2,
            id,
            error,
        })
    }

    /// 未知主版本的握手只读取公共 major。未关联的错误仅允许 id 为 null。
    ///
    /// # Errors
    /// 信封或载荷无效、应答无法关联，或关联的握手应答主版本不兼容。
    pub fn decode(raw: &[u8], request: Option<&BellisRequest>) -> Result<Self, DecodeError> {
        let mut object = envelope(raw)?;
        let id = object.nullable("id")?.map(RequestId::decode).transpose()?;
        let result = object.remove("result");
        let error = object.remove("error");
        let (result, error) = object.finish((result, error))?;
        if id.as_ref() != request.map(|request| &request.id) {
            return Err(DecodeError::UnmatchedResponse);
        }
        match (result, error) {
            (Some(result), None) => {
                let (Some(id), Some(request)) = (id, request) else {
                    return Err(DecodeError::UnmatchedResponse);
                };
                let decoded = match request.call {
                    ControlCall::Describe(_) => {
                        ControlResult::Description(CoreDescription::decode(result)?)
                    }
                    ControlCall::LeaseRenew(_) | ControlCall::InputStop(_) => {
                        ControlResult::Ack(Ack::decode(result)?)
                    }
                };
                Ok(Self::success(id, decoded))
            }
            (None, Some(error)) => Ok(Self::failure(id, RpcError::decode(error)?)),
            _ => Err(DecodeError::InvalidField("result / error")),
        }
    }
}
