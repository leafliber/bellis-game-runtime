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

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LocalDialogue {
    pub session_id: String,
    pub runtime_id: RuntimeId,
    pub authorization_id: RuntimeId,
    pub adapter_id: String,
    pub max_actions: u32,
    pub expires_at_ms: MonotonicMs,
}

impl LocalDialogue {
    fn decode(raw: &RawValue) -> Result<Self, DecodeError> {
        let mut object = Object::payload(raw)?;
        let scope = Self::read(&mut object)?;
        object.finish(scope)
    }

    fn read(object: &mut Object<'_>) -> Result<Self, DecodeError> {
        let session_id: String = object.field("sessionId")?;
        if !(1..=16).contains(&session_id.len())
            || !session_id
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
        {
            return Err(DecodeError::InvalidField("sessionId"));
        }
        let runtime_id = RuntimeId::new(object.field("runtimeId")?)
            .ok_or(DecodeError::InvalidField("runtimeId"))?;
        let authorization_id = RuntimeId::new(object.field("authorizationId")?)
            .ok_or(DecodeError::InvalidField("authorizationId"))?;
        let adapter_id: String = object.field("adapterId")?;
        if !(1..=48).contains(&adapter_id.len())
            || !adapter_id
                .as_bytes()
                .first()
                .is_some_and(u8::is_ascii_lowercase)
            || !adapter_id
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
        {
            return Err(DecodeError::InvalidField("adapterId"));
        }
        let max_actions = object.integer("maxActions")?;
        if !(1..=20).contains(&max_actions) {
            return Err(DecodeError::InvalidField("maxActions"));
        }
        let expires_at_ms = MonotonicMs::read(object, "expiresAtMs")?;
        Ok(Self {
            session_id,
            runtime_id,
            authorization_id,
            adapter_id,
            max_actions,
            expires_at_ms,
        })
    }

    /// 内核动作块用完整、无歧义的范围值关联两条独立连接的授权。
    #[must_use]
    pub fn action_scope(&self) -> String {
        format!(
            "{}/{}/{}/{}/{}/{}",
            self.session_id,
            self.runtime_id.0,
            self.authorization_id.0,
            self.adapter_id,
            self.max_actions,
            self.expires_at_ms.0,
        )
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(
    tag = "kind",
    rename_all = "snake_case",
    rename_all_fields = "camelCase"
)]
pub enum LocalRecoveryKind {
    Recover,
    Load {
        open_authorization_id: RuntimeId,
        open_authorization_seq: LeaseToken,
    },
    Prefix {
        decision_point_id: RuntimeId,
        option_id: RuntimeId,
    },
    Untried {
        decision_point_id: RuntimeId,
        option_id: RuntimeId,
    },
    Named {
        decision_point_id: RuntimeId,
        option_id: RuntimeId,
        options_digest: String,
        frame_number: LeaseToken,
        transform_version: u32,
    },
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct LocalRecovery {
    #[serde(flatten)]
    pub scope: LocalDialogue,
    #[serde(flatten)]
    pub kind: LocalRecoveryKind,
}

impl LocalRecovery {
    fn decode(raw: &RawValue) -> Result<Self, DecodeError> {
        let mut object = Object::payload(raw)?;
        let scope = LocalDialogue::read(&mut object)?;
        let kind: String = object.field("kind")?;
        let kind = match kind.as_str() {
            "recover" if scope.max_actions == 2 => LocalRecoveryKind::Recover,
            "load" if scope.max_actions == 1 => {
                let open_authorization_id = RuntimeId::new(object.field("openAuthorizationId")?)
                    .ok_or(DecodeError::InvalidField("openAuthorizationId"))?;
                let open_authorization_seq =
                    LeaseToken::decode(object.take("openAuthorizationSeq")?)?;
                if open_authorization_id == scope.authorization_id {
                    return Err(DecodeError::InvalidField("openAuthorizationId"));
                }
                LocalRecoveryKind::Load {
                    open_authorization_id,
                    open_authorization_seq,
                }
            }
            "prefix" | "untried" | "named" if scope.max_actions == 1 => {
                let decision_point_id = RuntimeId::new(object.field("decisionPointId")?)
                    .ok_or(DecodeError::InvalidField("decisionPointId"))?;
                let option_id = RuntimeId::new(object.field("optionId")?)
                    .ok_or(DecodeError::InvalidField("optionId"))?;
                match kind.as_str() {
                    "prefix" => LocalRecoveryKind::Prefix {
                        decision_point_id,
                        option_id,
                    },
                    "untried" => LocalRecoveryKind::Untried {
                        decision_point_id,
                        option_id,
                    },
                    _ => {
                        let options_digest: String = object.field("optionsDigest")?;
                        if options_digest.len() != 64
                            || !options_digest
                                .bytes()
                                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
                        {
                            return Err(DecodeError::InvalidField("optionsDigest"));
                        }
                        LocalRecoveryKind::Named {
                            decision_point_id,
                            option_id,
                            options_digest,
                            frame_number: LeaseToken::decode(object.take("frameNumber")?)?,
                            transform_version: object.integer("transformVersion")?,
                        }
                    }
                }
            }
            "recover" | "load" | "prefix" | "untried" | "named" => {
                return Err(DecodeError::InvalidField("maxActions"));
            }
            _ => return Err(DecodeError::InvalidField("kind")),
        };
        object.finish(Self { scope, kind })
    }

    /// 动作块绑定本次授权及操作所需的全部证据。
    #[must_use]
    pub fn action_scope(&self) -> String {
        let scope = self.scope.action_scope();
        match &self.kind {
            LocalRecoveryKind::Recover => format!("recover/{scope}"),
            LocalRecoveryKind::Load {
                open_authorization_id,
                open_authorization_seq,
            } => format!(
                "load/{scope}/{}/{}",
                open_authorization_id.0,
                open_authorization_seq.get()
            ),
            LocalRecoveryKind::Prefix {
                decision_point_id,
                option_id,
            } => format!("prefix/{scope}/{}/{}", decision_point_id.0, option_id.0),
            LocalRecoveryKind::Untried {
                decision_point_id,
                option_id,
            } => format!("untried/{scope}/{}/{}", decision_point_id.0, option_id.0),
            LocalRecoveryKind::Named {
                decision_point_id,
                option_id,
                options_digest,
                frame_number,
                transform_version,
            } => format!(
                "named/{scope}/{}/{}/{options_digest}/{}/{transform_version}",
                decision_point_id.0,
                option_id.0,
                frame_number.get(),
            ),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LeaseRenewParams {
    pub token: LeaseToken,
    pub expires_at_ms: MonotonicMs,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub local_dialogue: Option<LocalDialogue>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub local_recovery: Option<Box<LocalRecovery>>,
}

impl LeaseRenewParams {
    pub(crate) fn decode(raw: &RawValue) -> Result<Self, DecodeError> {
        let mut object = Object::payload(raw)?;
        let token = LeaseToken::decode(object.take("token")?)?;
        let expires_at_ms = MonotonicMs::read(&mut object, "expiresAtMs")?;
        let local_dialogue = object
            .remove("localDialogue")
            .map(LocalDialogue::decode)
            .transpose()?;
        let local_recovery = object
            .remove("localRecovery")
            .map(LocalRecovery::decode)
            .transpose()?
            .map(Box::new);
        if local_dialogue.is_some() && local_recovery.is_some() {
            return Err(DecodeError::InvalidField("localRecovery"));
        }
        object.finish(Self {
            token,
            expires_at_ms,
            local_dialogue,
            local_recovery,
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
