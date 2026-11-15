//! 输入许可：令牌、到期时刻与“为什么无效”（`docs/modules/core-protocol.md` 的“输入许可”）。
//!
//! 规则取自 Bellis D-31、D-34：令牌只增不减，只认等于当前令牌且未过期的许可。许可源只有一个：操作员许可
//! 连接（独立运行中的操作员控制台），或没有它时测试设备连接上的测试许可源（只用于测试，与假输入后端同生
//! 同灭）；没有许可源时许可永远无效。
//!
//! 这里只是状态机，不含锁与时钟：时刻由调用方给出（与帧环同一时钟的纳秒数），释放由控制状态完成。

use std::fmt;

use core_protocol::messages::{LeaseStatus, ReleaseReason};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum State {
    /// 许可源从未授予，或没有许可源。
    NotGranted,
    /// 令牌等于当前令牌；到期时刻之前有效。
    Valid { token: u64, expires_ns: u64 },
    /// 过了到期时刻，没有续约。这个令牌不能再续约，须换发更大的令牌。
    Expired { token: u64 },
    /// 被吊销，或换发了更大的令牌后旧许可不再有效（换发时直接成为新的 `Valid`）。
    Revoked,
    /// 与许可源的连接断开；此后不再有许可。
    SourceLost,
}

/// 授予（`operator_lease.grant` 或 `test_lease.grant`）接受后的结果。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Granted {
    /// 此前没有有效的许可，现在有了。
    New,
    /// 同一令牌续约，到期时刻换成新的。
    Renewed,
    /// 换发了更大的令牌：旧许可负责的输入须先释放（`lease_revoked`）。
    Replaced,
}

/// 授予不被接受的原因。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Refusal {
    /// 令牌小于当前令牌。
    Stale { token: u64, current: u64 },
    /// 令牌已被吊销。
    Revoked { token: u64, through: u64 },
    /// 这个令牌的许可已经过期，过期的许可不能续约。
    Lapsed { token: u64 },
    /// 到期时刻不晚于现在。
    PastExpiry { expires_ns: u64, now: u64 },
    /// 没有许可源，或与它的连接已断开。
    NoSource,
    /// 本实例的接管钩子失效。
    HookLost,
    /// 本实例无法可靠地完成输入释放。
    ReleaseFailed,
}

impl fmt::Display for Refusal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Stale { token, current } => {
                write!(f, "token {token} is older than the current token {current}")
            }
            Self::Revoked { token, through } => {
                write!(f, "token {token} is revoked (tokens up to {through} are)")
            }
            Self::Lapsed { token } => write!(
                f,
                "the lease with token {token} has expired; grant a newer token"
            ),
            Self::PastExpiry { expires_ns, now } => {
                write!(f, "expires_ns {expires_ns} is not after now ({now})")
            }
            Self::NoSource => f.write_str("there is no lease source"),
            Self::HookLost => f.write_str("the input hook has failed; restart the core"),
            Self::ReleaseFailed => f.write_str("input release failed; restart the core"),
        }
    }
}

/// 实时核持有的输入许可。
#[derive(Debug)]
pub(crate) struct Lease {
    has_source: bool,
    /// 见过的最大令牌（授予或吊销），只增不减。
    current_token: Option<u64>,
    /// 已吊销的最大令牌。
    revoked_through: Option<u64>,
    state: State,
}

impl Lease {
    /// `has_source` 为假时没有许可源：许可永远无效，授予一律拒绝。
    pub(crate) fn new(has_source: bool) -> Self {
        Self {
            has_source,
            current_token: None,
            revoked_through: None,
            state: State::NotGranted,
        }
    }

    /// 授予或续约。调用方须先以 [`Self::expire`] 处理到期，再调用这里。
    pub(crate) fn grant(
        &mut self,
        token: u64,
        expires_ns: u64,
        now: u64,
    ) -> Result<Granted, Refusal> {
        if !self.has_source || self.state == State::SourceLost {
            return Err(Refusal::NoSource);
        }
        if let Some(through) = self.revoked_through
            && token <= through
        {
            return Err(Refusal::Revoked { token, through });
        }
        if let Some(current) = self.current_token
            && token < current
        {
            return Err(Refusal::Stale { token, current });
        }
        if expires_ns <= now {
            return Err(Refusal::PastExpiry { expires_ns, now });
        }
        let granted = match self.state {
            State::Valid { token: held, .. } if held == token => Granted::Renewed,
            State::Valid { .. } => Granted::Replaced,
            State::Expired { token: lapsed } if lapsed == token => {
                return Err(Refusal::Lapsed { token });
            }
            State::NotGranted | State::Expired { .. } | State::Revoked | State::SourceLost => {
                Granted::New
            }
        };
        self.current_token = Some(token);
        self.state = State::Valid { token, expires_ns };
        Ok(granted)
    }

    /// 吊销 `token` 及更早的全部令牌；返回是否吊销了一个有效的许可（调用方随之释放）。
    pub(crate) fn revoke(&mut self, token: u64) -> bool {
        self.revoked_through = Some(self.revoked_through.map_or(token, |r| r.max(token)));
        self.current_token = Some(self.current_token.map_or(token, |c| c.max(token)));
        match self.state {
            State::Valid { token: held, .. } if held <= token => {
                self.state = State::Revoked;
                true
            }
            State::Expired { token: lapsed } if lapsed <= token => {
                self.state = State::Revoked;
                false
            }
            _ => false,
        }
    }

    /// 本地撤权（真实后端上的接管、急停、注入被拦截、钩子失效）：吊销当前令牌及更早的全部令牌，
    /// 同吊销。返回是否吊销了一个有效的许可（调用方随之释放）；没有授予过令牌时什么也不做。
    pub(crate) fn local_revoke(&mut self) -> bool {
        match self.current_token {
            Some(token) => self.revoke(token),
            None => false,
        }
    }

    /// 到了到期时刻即转为过期；返回是否刚刚过期（调用方随之释放）。
    pub(crate) fn expire(&mut self, now: u64) -> bool {
        match self.state {
            State::Valid { token, expires_ns } if now >= expires_ns => {
                self.state = State::Expired { token };
                true
            }
            _ => false,
        }
    }

    /// 与许可源的连接断开。
    pub(crate) fn source_lost(&mut self) {
        if self.has_source {
            self.state = State::SourceLost;
        }
    }

    /// 许可是否有效；无效时给出释放的原因。
    pub(crate) fn check(&self, now: u64) -> Result<(), ReleaseReason> {
        match self.state {
            State::Valid { expires_ns, .. } if now < expires_ns => Ok(()),
            State::Valid { .. } | State::Expired { .. } => Err(ReleaseReason::LeaseExpired),
            State::Revoked => Err(ReleaseReason::LeaseRevoked),
            State::SourceLost => Err(ReleaseReason::LeaseSourceLost),
            State::NotGranted => Err(ReleaseReason::NoLease),
        }
    }

    /// 有效许可的到期时刻，供调度线程定时。
    pub(crate) fn expires_at(&self) -> Option<u64> {
        match self.state {
            State::Valid { expires_ns, .. } => Some(expires_ns),
            _ => None,
        }
    }

    /// `core.health` 报的状态。
    pub(crate) fn status(&self, now: u64) -> LeaseStatus {
        match self.state {
            State::Valid { expires_ns, .. } if now < expires_ns => LeaseStatus::Valid,
            State::Valid { .. } | State::Expired { .. } => LeaseStatus::Expired,
            State::Revoked => LeaseStatus::Revoked,
            State::SourceLost => LeaseStatus::SourceLost,
            State::NotGranted => LeaseStatus::NotGranted,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{Granted, Lease, Refusal};
    use core_protocol::messages::{LeaseStatus, ReleaseReason};

    #[test]
    fn without_a_source_there_is_never_a_lease() {
        let mut lease = Lease::new(false);
        assert_eq!(lease.check(0), Err(ReleaseReason::NoLease));
        assert_eq!(lease.grant(1, 100, 0), Err(Refusal::NoSource));
        assert_eq!(lease.check(0), Err(ReleaseReason::NoLease));
        assert_eq!(lease.status(0), LeaseStatus::NotGranted);
    }

    #[test]
    fn a_grant_is_valid_until_it_expires() {
        let mut lease = Lease::new(true);
        assert_eq!(lease.check(0), Err(ReleaseReason::NoLease));
        assert_eq!(lease.grant(1, 500, 0), Ok(Granted::New));
        assert_eq!(lease.check(499), Ok(()));
        assert_eq!(lease.status(499), LeaseStatus::Valid);
        // 到期时刻本身已无效，即使还没有处理到期。
        assert_eq!(lease.check(500), Err(ReleaseReason::LeaseExpired));
        assert_eq!(lease.expires_at(), Some(500));
        assert!(!lease.expire(499));
        assert!(lease.expire(500));
        assert!(!lease.expire(600), "only once");
        assert_eq!(lease.expires_at(), None);
        assert_eq!(lease.status(600), LeaseStatus::Expired);
    }

    #[test]
    fn the_same_token_renews_and_a_lapsed_token_cannot() {
        let mut lease = Lease::new(true);
        lease.grant(3, 500, 0).unwrap();
        assert_eq!(lease.grant(3, 750, 250), Ok(Granted::Renewed));
        assert_eq!(lease.check(600), Ok(()));
        assert!(lease.expire(750));
        assert_eq!(lease.grant(3, 2000, 800), Err(Refusal::Lapsed { token: 3 }));
        assert_eq!(lease.check(800), Err(ReleaseReason::LeaseExpired));
        // 换发更大的令牌才重新有效。
        assert_eq!(lease.grant(4, 2000, 800), Ok(Granted::New));
        assert_eq!(lease.check(800), Ok(()));
    }

    #[test]
    fn tokens_only_increase() {
        let mut lease = Lease::new(true);
        lease.grant(5, 500, 0).unwrap();
        // 令牌不等于当前值的续约不算数，许可照常到期。
        assert_eq!(
            lease.grant(4, 900, 100),
            Err(Refusal::Stale {
                token: 4,
                current: 5
            })
        );
        assert_eq!(lease.check(500), Err(ReleaseReason::LeaseExpired));
        // 更大的令牌换掉仍然有效的许可：调用方据此先释放。
        let mut lease = Lease::new(true);
        lease.grant(5, 500, 0).unwrap();
        assert_eq!(lease.grant(6, 500, 0), Ok(Granted::Replaced));
        assert_eq!(
            lease.grant(5, 500, 0).unwrap_err(),
            Refusal::Stale {
                token: 5,
                current: 6
            }
        );
    }

    #[test]
    fn revoking_invalidates_the_token_and_everything_older() {
        let mut lease = Lease::new(true);
        lease.grant(2, 500, 0).unwrap();
        assert!(!lease.revoke(1), "an older token does not touch the lease");
        assert_eq!(lease.check(10), Ok(()));
        assert!(lease.revoke(2));
        assert_eq!(lease.check(10), Err(ReleaseReason::LeaseRevoked));
        assert_eq!(lease.status(10), LeaseStatus::Revoked);
        assert!(!lease.revoke(2), "only once");
        assert_eq!(
            lease.grant(2, 500, 10),
            Err(Refusal::Revoked {
                token: 2,
                through: 2
            })
        );
        // 吊销未来的令牌：它们也不能再授予。
        assert!(!lease.revoke(7));
        assert!(matches!(
            lease.grant(7, 500, 10),
            Err(Refusal::Revoked { .. })
        ));
        assert_eq!(lease.grant(8, 500, 10), Ok(Granted::New));
    }

    #[test]
    fn local_revoke_invalidates_the_current_token_like_a_revoke() {
        let mut lease = Lease::new(true);
        assert!(!lease.local_revoke(), "没有令牌时什么也不做");
        lease.grant(3, 500, 0).unwrap();
        assert!(lease.local_revoke());
        assert_eq!(lease.check(10), Err(ReleaseReason::LeaseRevoked));
        // 当前令牌及更早的不能再授予，须换发更大的令牌。
        assert!(matches!(
            lease.grant(3, 500, 10),
            Err(Refusal::Revoked { .. })
        ));
        assert_eq!(lease.grant(4, 500, 10), Ok(Granted::New));
        assert!(lease.local_revoke(), "刚换发的更大令牌又被本地撤权");
        assert_eq!(lease.check(10), Err(ReleaseReason::LeaseRevoked));
    }

    #[test]
    fn an_expiry_in_the_past_is_refused() {
        let mut lease = Lease::new(true);
        assert_eq!(
            lease.grant(1, 100, 100),
            Err(Refusal::PastExpiry {
                expires_ns: 100,
                now: 100
            })
        );
        assert_eq!(lease.check(100), Err(ReleaseReason::NoLease));
    }

    #[test]
    fn losing_the_source_ends_the_lease_for_good() {
        let mut lease = Lease::new(true);
        lease.grant(1, 500, 0).unwrap();
        lease.source_lost();
        assert_eq!(lease.check(10), Err(ReleaseReason::LeaseSourceLost));
        assert_eq!(lease.status(10), LeaseStatus::SourceLost);
        assert_eq!(lease.grant(2, 500, 10), Err(Refusal::NoSource));
        assert_eq!(lease.expires_at(), None);
    }
}
