use std::num::NonZeroU16;

use serde::{Deserialize, Serialize};
use serde_json::value::RawValue;

use crate::GAME_BRIDGE_MAJOR;
use crate::wire::{DecodeError, MAX_SAFE_INTEGER, Object, integer};

const NS_PER_MS: u64 = 1_000_000;
/// 游戏桥时刻乘以一百万后仍能放入 u64。
pub const MAX_MONOTONIC_MS: u64 = u64::MAX / NS_PER_MS;
pub const MAX_TOKEN: u64 = MAX_SAFE_INTEGER.unsigned_abs();

/// 同机系统单调时钟的整毫秒。
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(transparent)]
pub struct MonotonicMs(u64);

impl MonotonicMs {
    #[must_use]
    pub fn new(ms: u64) -> Option<Self> {
        (ms <= MAX_MONOTONIC_MS).then_some(Self(ms))
    }

    /// 将同一时钟域的纳秒读数向下取整。
    #[must_use]
    pub const fn from_ns(ns: u64) -> Self {
        Self(ns / NS_PER_MS)
    }

    #[must_use]
    pub const fn to_ns(self) -> u64 {
        self.0 * NS_PER_MS
    }

    fn read(object: &mut Object<'_>, field: &'static str) -> Result<Self, DecodeError> {
        Self::new(object.integer(field)?).ok_or(DecodeError::InvalidField(field))
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(transparent)]
pub struct LeaseToken(u64);

impl LeaseToken {
    #[must_use]
    pub fn new(token: u64) -> Option<Self> {
        (1..=MAX_TOKEN).contains(&token).then_some(Self(token))
    }

    #[must_use]
    pub const fn get(self) -> u64 {
        self.0
    }

    fn decode(raw: &RawValue) -> Result<Self, DecodeError> {
        Self::new(integer(raw, "token")?).ok_or(DecodeError::InvalidField("token"))
    }
}

/// 包含上界的撤销标记，可以为 0。
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(transparent)]
pub struct ThroughToken(u64);

impl ThroughToken {
    #[must_use]
    pub fn new(token: u64) -> Option<Self> {
        (token <= MAX_TOKEN).then_some(Self(token))
    }

    #[must_use]
    pub const fn get(self) -> u64 {
        self.0
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(transparent)]
pub struct RuntimeId(String);

impl RuntimeId {
    #[must_use]
    pub fn new(id: String) -> Option<Self> {
        let (first, rest) = id.as_bytes().split_first()?;
        (first.is_ascii_alphanumeric()
            && rest.len() < 64
            && rest
                .iter()
                .all(|b| b.is_ascii_alphanumeric() || b"_.:-".contains(b)))
        .then_some(Self(id))
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(transparent)]
pub struct StopReason(String);

impl StopReason {
    #[must_use]
    pub fn new(reason: String) -> Option<Self> {
        (1..=200)
            .contains(&reason.chars().count())
            .then_some(Self(reason))
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub struct DescribeParams {
    pub major: NonZeroU16,
}

impl DescribeParams {
    pub(crate) fn decode(raw: &RawValue) -> Result<Self, DecodeError> {
        let mut object = Object::payload(raw)?;
        let major = object.integer("major")?;
        object.finish(Self { major })
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CoreDescription {
    major: NonZeroU16,
    pub runtime_id: RuntimeId,
    pub now_ms: MonotonicMs,
}

impl CoreDescription {
    #[must_use]
    pub const fn new(runtime_id: RuntimeId, now_ms: MonotonicMs) -> Self {
        Self {
            major: GAME_BRIDGE_MAJOR,
            runtime_id,
            now_ms,
        }
    }

    pub(crate) fn decode(raw: &RawValue) -> Result<Self, DecodeError> {
        let mut object = Object::payload(raw)?;
        let major: NonZeroU16 = object.integer("major")?;
        if major != GAME_BRIDGE_MAJOR {
            return Err(DecodeError::IncompatibleMajor(major.get()));
        }
        let runtime_id = RuntimeId::new(object.field("runtimeId")?)
            .ok_or(DecodeError::InvalidField("runtimeId"))?;
        let now_ms = MonotonicMs::read(&mut object, "nowMs")?;
        object.finish(Self::new(runtime_id, now_ms))
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub struct Ack {}

impl Ack {
    pub(crate) fn decode(raw: &RawValue) -> Result<Self, DecodeError> {
        Object::payload(raw)?.finish(Self {})
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LeaseRenewParams {
    pub token: LeaseToken,
    pub expires_at_ms: MonotonicMs,
}

impl LeaseRenewParams {
    pub(crate) fn decode(raw: &RawValue) -> Result<Self, DecodeError> {
        let mut object = Object::payload(raw)?;
        let token = LeaseToken::decode(object.take("token")?)?;
        let expires_at_ms = MonotonicMs::read(&mut object, "expiresAtMs")?;
        object.finish(Self {
            token,
            expires_at_ms,
        })
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct InputStopParams {
    pub through_token: ThroughToken,
    pub reason: StopReason,
}

impl InputStopParams {
    pub(crate) fn decode(raw: &RawValue) -> Result<Self, DecodeError> {
        let mut object = Object::payload(raw)?;
        let through_token = ThroughToken::new(object.integer("throughToken")?)
            .ok_or(DecodeError::InvalidField("throughToken"))?;
        let reason =
            StopReason::new(object.field("reason")?).ok_or(DecodeError::InvalidField("reason"))?;
        object.finish(Self {
            through_token,
            reason,
        })
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReleaseReason {
    CoreStart,
    GenerationRevoked,
    ProducerChanged,
    HeartbeatTimeout,
    DeadlineExpired,
    FrameStale,
    LeaseExpired,
    LeaseRevoked,
    LeaseSourceLost,
    NoLease,
    FocusLost,
    Takeover,
    EmergencyStop,
    InjectionBlocked,
    HookLost,
    MaxHold,
    Stop,
    CoreConnectionLost,
    StdinClosed,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReleaseVerification {
    Verified,
    Residual,
    Unverified,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct InputReleased {
    pub token: Option<LeaseToken>,
    pub at_ms: MonotonicMs,
    pub reason: ReleaseReason,
    pub verification: ReleaseVerification,
}

impl InputReleased {
    pub(crate) fn decode(raw: &RawValue) -> Result<Self, DecodeError> {
        let mut object = Object::payload(raw)?;
        let token = object
            .nullable("token")?
            .map(LeaseToken::decode)
            .transpose()?;
        let at_ms = MonotonicMs::read(&mut object, "atMs")?;
        let reason = object.field("reason")?;
        let verification = object.field("verification")?;
        object.finish(Self {
            token,
            at_ms,
            reason,
            verification,
        })
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LeaseState {
    NotGranted,
    Valid,
    Expired,
    Revoked,
    SourceLost,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CoreHealth {
    pub at_ms: MonotonicMs,
    pub lease_state: LeaseState,
    pub frame_age_ms: Option<u32>,
    pub queue_depth: u32,
}

impl CoreHealth {
    pub(crate) fn decode(raw: &RawValue) -> Result<Self, DecodeError> {
        let mut object = Object::payload(raw)?;
        let at_ms = MonotonicMs::read(&mut object, "atMs")?;
        let lease_state = object.field("leaseState")?;
        let frame_age_ms = object
            .nullable("frameAgeMs")?
            .map(|raw| integer(raw, "frameAgeMs"))
            .transpose()?;
        let queue_depth = object.integer("queueDepth")?;
        object.finish(Self {
            at_ms,
            lease_state,
            frame_age_ms,
            queue_depth,
        })
    }
}
