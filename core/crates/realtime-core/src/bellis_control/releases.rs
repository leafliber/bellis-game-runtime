use std::collections::BTreeMap;
use std::sync::{Condvar, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use core_protocol::messages::{
    InputVerificationParams, ReleaseReason, ReleaseVerification, VerificationResult,
};
use game_bridge_protocol::{
    InputReleased, LeaseToken, MonotonicMs, ReleaseReason as BridgeReason,
    ReleaseVerification as BridgeVerification,
};

use crate::control::ReleasedInput;

#[derive(Default)]
struct Pending {
    released: Option<ReleasedInput>,
    checked: Option<InputVerificationParams>,
}

#[derive(Default)]
struct State {
    pending: BTreeMap<u64, Pending>,
    sending: bool,
    closed: bool,
}

/// 一个控制连接只属于一个实时核实例。按该实例的 `release_id` 关联，并容许两条线程反序投递。
pub(crate) struct ReleaseQueue {
    state: Mutex<State>,
    changed: Condvar,
    wait_ns: u64,
}

impl ReleaseQueue {
    pub(crate) fn new(wait: Duration) -> Self {
        Self {
            state: Mutex::default(),
            changed: Condvar::new(),
            wait_ns: u64::try_from(wait.as_nanos()).unwrap_or(u64::MAX),
        }
    }

    fn lock(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    pub(crate) fn released(&self, release: &ReleasedInput) {
        let mut state = self.lock();
        if !state.closed {
            state
                .pending
                .entry(release.params.release_id)
                .or_default()
                .released = Some(release.clone());
        }
    }

    pub(crate) fn checked(&self, checked: InputVerificationParams) {
        let mut state = self.lock();
        if !state.closed {
            state.pending.entry(checked.release_id).or_default().checked = Some(checked);
        }
    }

    pub(super) fn ready(&self, now: u64) -> Vec<InputReleased> {
        let mut state = self.lock();
        let mut events = Vec::new();
        state.pending.retain(|_, pending| {
            let Some(released) = &pending.released else {
                return pending.checked.is_some_and(|checked| {
                    checked.checked_at_ns.saturating_add(self.wait_ns) > now
                });
            };
            let start = released.params.released_at_ns;
            let deadline = start.saturating_add(self.wait_ns);
            let checked = pending
                .checked
                .filter(|checked| (start..=deadline).contains(&checked.checked_at_ns));
            if released.awaits_verification && checked.is_none() && now < deadline {
                return true;
            }
            events.push(project(released, checked));
            false
        });
        state.sending = !events.is_empty();
        events
    }

    pub(super) fn sent(&self) {
        self.lock().sending = false;
        self.changed.notify_all();
    }

    pub(super) fn close(&self) {
        let mut state = self.lock();
        state.closed = true;
        state.pending.clear();
        state.sending = false;
        self.changed.notify_all();
    }

    pub(super) fn flush(&self, timeout: Duration) -> bool {
        let (state, _) = self
            .changed
            .wait_timeout_while(self.lock(), timeout, |state| {
                !state.pending.is_empty() || state.sending
            })
            .unwrap_or_else(PoisonError::into_inner);
        state.pending.is_empty() && !state.sending
    }
}

fn project(released: &ReleasedInput, checked: Option<InputVerificationParams>) -> InputReleased {
    let verification = match checked
        .filter(|_| released.awaits_verification)
        .map(|checked| checked.result)
    {
        Some(VerificationResult::Verified)
            if released.params.verification != ReleaseVerification::UpFailed =>
        {
            BridgeVerification::Verified
        }
        Some(VerificationResult::Residual) => BridgeVerification::Residual,
        _ => BridgeVerification::Unverified,
    };
    InputReleased {
        token: released.token.and_then(LeaseToken::new),
        at_ms: MonotonicMs::from_ns(released.params.released_at_ns),
        reason: bridge_reason(released.params.reason),
        verification,
    }
}

fn bridge_reason(reason: ReleaseReason) -> BridgeReason {
    match reason {
        ReleaseReason::CoreStart => BridgeReason::CoreStart,
        ReleaseReason::GenerationRevoked => BridgeReason::GenerationRevoked,
        ReleaseReason::ProducerChanged => BridgeReason::ProducerChanged,
        ReleaseReason::HeartbeatTimeout => BridgeReason::HeartbeatTimeout,
        ReleaseReason::DeadlineExpired => BridgeReason::DeadlineExpired,
        ReleaseReason::FrameStale => BridgeReason::FrameStale,
        ReleaseReason::LeaseExpired => BridgeReason::LeaseExpired,
        ReleaseReason::LeaseRevoked => BridgeReason::LeaseRevoked,
        ReleaseReason::LeaseSourceLost => BridgeReason::LeaseSourceLost,
        ReleaseReason::NoLease => BridgeReason::NoLease,
        ReleaseReason::FocusLost => BridgeReason::FocusLost,
        ReleaseReason::Takeover => BridgeReason::Takeover,
        ReleaseReason::EmergencyStop => BridgeReason::EmergencyStop,
        ReleaseReason::InjectionBlocked => BridgeReason::InjectionBlocked,
        ReleaseReason::HookLost => BridgeReason::HookLost,
        ReleaseReason::MaxHold => BridgeReason::MaxHold,
        ReleaseReason::Stop => BridgeReason::Stop,
        ReleaseReason::CoreConnectionLost => BridgeReason::CoreConnectionLost,
        ReleaseReason::StdinClosed => BridgeReason::StdinClosed,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core_protocol::messages::InputReleasedParams;

    const WAIT: Duration = Duration::from_nanos(100);

    fn release(id: u64, token: u64, at: u64) -> ReleasedInput {
        ReleasedInput {
            params: InputReleasedParams {
                release_id: id,
                reason: ReleaseReason::LeaseRevoked,
                producer: None,
                released_at_ns: at,
                verification: ReleaseVerification::Unverified,
            },
            token: Some(token),
            awaits_verification: true,
        }
    }

    fn check(id: u64, result: VerificationResult, at: u64) -> InputVerificationParams {
        InputVerificationParams {
            release_id: id,
            result,
            checked_at_ns: at,
        }
    }

    #[test]
    fn projects_only_matching_evidence_even_when_checks_arrive_first() {
        let queue = ReleaseQueue::new(WAIT);
        queue.checked(check(2, VerificationResult::Verified, 1_000_040));
        queue.released(&release(1, 7, 1_000_010));
        queue.released(&release(2, 8, 1_000_020));
        let [matching] = queue
            .ready(1_000_041)
            .try_into()
            .expect("only release 2 should be ready");
        assert_eq!(matching.token.map(LeaseToken::get), Some(8));
        assert_eq!(matching.verification, BridgeVerification::Verified);
        assert_eq!(matching.at_ms, MonotonicMs::from_ns(1_000_020));
        queue.checked(check(1, VerificationResult::Superseded, 1_000_050));
        let [older] = queue
            .ready(1_000_051)
            .try_into()
            .expect("release 1 should be ready");
        assert_eq!(older.token.map(LeaseToken::get), Some(7));
        assert_eq!(older.verification, BridgeVerification::Unverified);
    }

    #[test]
    fn unavailable_fake_and_failed_ups_have_no_verified_evidence() {
        for (awaits, up, result, expected) in [
            (
                true,
                ReleaseVerification::Unverified,
                VerificationResult::Unavailable,
                BridgeVerification::Unverified,
            ),
            (
                false,
                ReleaseVerification::Unverified,
                VerificationResult::Verified,
                BridgeVerification::Unverified,
            ),
            (
                true,
                ReleaseVerification::UpFailed,
                VerificationResult::Verified,
                BridgeVerification::Unverified,
            ),
            (
                true,
                ReleaseVerification::Unverified,
                VerificationResult::Residual,
                BridgeVerification::Residual,
            ),
        ] {
            let queue = ReleaseQueue::new(WAIT);
            let mut released = release(1, 3, 100);
            released.awaits_verification = awaits;
            released.params.verification = up;
            queue.released(&released);
            queue.checked(check(1, result, 110));
            assert_eq!(queue.ready(120)[0].verification, expected);
        }
    }

    #[test]
    fn old_or_late_observations_do_not_replace_an_unverified_report() {
        let queue = ReleaseQueue::new(WAIT);
        queue.released(&release(2, 8, 100));
        queue.checked(check(2, VerificationResult::Verified, 99));
        assert_eq!(queue.ready(199), []);
        let released = queue.ready(200);
        assert_eq!(released[0].verification, BridgeVerification::Unverified);
        queue.checked(check(2, VerificationResult::Verified, 201));
        assert_eq!(queue.ready(202), []);
        queue.sent();
        assert_eq!(queue.ready(400), []);
        assert!(queue.flush(Duration::ZERO));
    }
}
