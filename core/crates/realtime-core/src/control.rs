//! 控制状态：控制产出者与心跳、输入许可、动作块的调度与权威检查，以及释放全部输入。
//!
//! 规则见 `docs/modules/core-protocol.md` 的“控制产出者”“动作块”与“输入许可”，实现见
//! `docs/modules/realtime-core.md` 的“动作块的执行”。全部状态在一把锁里：每个改变状态的操作都在锁内
//! 先完成释放，再把要发出的通知（[`Notices`]）交给调用方，由调用方在锁外发出：释放从不等待写入，
//! 写失败也已释放。释放也不碰采集一侧：帧龄读 [`FrameStatus`] 的原子量，不拿它的锁。
//!
//! 时刻一律是 `mono_clock::now_ns()` 的读数，与动作块的 `start_ns`、`deadline_ns`、许可的到期时刻和
//! 帧环的采集时刻同一时钟。

use std::collections::{HashSet, VecDeque};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant};

use core_protocol::messages::{
    Accepted, Action, ActionBlock, ActionOutcome, ActionReceiptParams, ActionSubmitResult,
    CaptureState, CoreHealthParams, DropReason, Dropped, Executed, HeldInput, InputPolicy,
    InputReleasedParams, InputResidualParams, InputVerificationParams, LeaseGrantParams, Producer,
    ProducerHeartbeatParams, RejectReason, Rejected, ReleaseReason, ReleaseVerification, Uncertain,
    VerificationResult,
};

use crate::frames::FrameStatus;
use crate::lease::{Granted, Lease, Refusal};
use crate::ledger::{HeldId, Inject, InputBackend, InputLedger, Momentary};
use crate::policy::Policy;

/// `input.configure` 已被接受的情况。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Configured {
    /// 第一次设定，接受。
    Accepted,
    /// 本实例已设定过（回 -32006）。
    Conflict,
}

/// 心跳不算数的原因。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Rejection {
    /// 代次不大于已作废的最高代次。
    Revoked,
    /// 代次小于当前代次。
    StaleGeneration,
    /// 代次等于当前代次，身份却不同。
    ProducerMismatch,
    /// 同一代次内 `seq` 没有递增。
    StaleSeq,
    /// 正在停止，不再接受控制产出者。
    Stopping,
}

/// 控制状态的时限。
#[derive(Clone, Copy, Debug)]
pub(crate) struct Limits {
    /// 心跳超过这么久没有到达即释放全部输入并待命。
    pub(crate) heartbeat_timeout: Duration,
    /// 动作允许的帧龄：最新写入帧的帧龄超过它即不发输入并释放。
    pub(crate) max_frame_age: Duration,
}

/// 状态变化产生的通知，按产生的顺序；由调用方在锁外发出。
#[derive(Debug, Default)]
pub(crate) struct Notices(pub(crate) Vec<Notice>);

/// 一条要发给主进程的通知。
#[derive(Debug)]
pub(crate) enum Notice {
    Released(ReleasedInput),
    Receipt(ActionReceiptParams),
    /// 真实后端核验出的残留（也发往操作员控制台），见 `docs/modules/realtime-core.md`。
    #[cfg_attr(
        not(windows),
        allow(dead_code, reason = "只由 Windows 上真实后端的核验线程构造")
    )]
    Residual(InputResidualParams),
    #[cfg_attr(
        not(windows),
        allow(dead_code, reason = "只有真实后端发出物理核验结果")
    )]
    Verification(InputVerificationParams),
}

/// 同一把控制锁内记录一次释放及其关联令牌。核协议只上报 params。
#[derive(Clone, Debug)]
pub(crate) struct ReleasedInput {
    pub(crate) params: InputReleasedParams,
    pub(crate) token: Option<u64>,
    pub(crate) awaits_verification: bool,
}

/// 可立即应答的授予，或仍须等待某次旧许可释放核验的请求。
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum LeaseGrant {
    Applied,
    Verifying(u64),
}

#[derive(Clone, Copy)]
struct PendingVerification {
    release_id: u64,
    blocks_new_lease: bool,
}

impl Notices {
    pub(crate) fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    /// 其中的释放，按顺序。
    #[cfg(test)]
    pub(crate) fn released(&self) -> Vec<ReleaseReason> {
        self.0
            .iter()
            .filter_map(|notice| match notice {
                Notice::Released(released) => Some(released.params.reason),
                Notice::Receipt(_) | Notice::Residual(_) | Notice::Verification(_) => None,
            })
            .collect()
    }

    /// 其中的回执，按顺序。
    #[cfg(test)]
    pub(crate) fn receipts(&self) -> Vec<&ActionReceiptParams> {
        self.0
            .iter()
            .filter_map(|notice| match notice {
                Notice::Receipt(receipt) => Some(receipt),
                Notice::Released(_) | Notice::Residual(_) | Notice::Verification(_) => None,
            })
            .collect()
    }
}

#[derive(Debug)]
struct Current {
    producer: Producer,
    /// 上一个算数的心跳 `seq`；这一代次还没有心跳时为 `None`。
    last_seq: Option<u64>,
    /// 心跳截止时刻。超时释放后与这一代次还没有心跳时为 `None`（待命）。
    deadline: Option<u64>,
    /// 上一个接受的动作块的 `seq`。
    last_block_seq: Option<u64>,
    /// 这一代次已执行过的一次性动作的身份，用于去重。
    executed: HashSet<u64>,
}

impl Current {
    fn new(producer: Producer) -> Self {
        Self {
            producer,
            last_seq: None,
            deadline: None,
            last_block_seq: None,
            executed: HashSet::new(),
        }
    }
}

/// 待执行的一步。一次性动作在有输入策略时作为瞬时按住注入、按住时长后抬起，同一步的下一个一次性动作
/// 等它抬起（[`State::step`]）；因此一步可能跨多次调度，`held_done` 与 `done` 记下已完成的部分。
#[derive(Debug)]
struct Entry {
    /// 生效时刻：`start_ns + offset_ns`；等瞬时按住抬起时改成抬起时刻。
    at_ns: u64,
    /// 所属动作块的有效截止时刻；过了它这一步不再执行。
    deadline_ns: u64,
    block_seq: u64,
    base_frame: u64,
    held: Vec<HeldInput>,
    actions: VecDeque<Action>,
    /// 这一步的持续状态已按下。
    held_done: bool,
    /// 这一步已给出结果的一次性动作，等整步完成或释放时一并回执。
    done: Vec<ActionOutcome>,
}

/// 当前在效的动作块。
#[derive(Clone, Copy, Debug)]
struct ActiveBlock {
    seq: u64,
    deadline_ns: u64,
}

/// 调度计划：按时刻排列的待执行步与在效的动作块。全部属于当前控制产出者。
#[derive(Debug, Default)]
struct Plan {
    entries: VecDeque<Entry>,
    active: Option<ActiveBlock>,
}

/// 按动作块分组的一次性动作结果，用于组装回执。
#[derive(Default)]
struct Outcomes(Vec<(u64, Vec<ActionOutcome>)>);

impl Outcomes {
    fn add(&mut self, block_seq: u64, outcome: ActionOutcome) {
        match self.0.last_mut() {
            Some((seq, outcomes)) if *seq == block_seq => outcomes.push(outcome),
            _ => self.0.push((block_seq, vec![outcome])),
        }
    }

    fn drop_all(&mut self, entry: &Entry, reason: DropReason) {
        for outcome in &entry.done {
            self.add(entry.block_seq, outcome.clone());
        }
        for action in &entry.actions {
            self.add(entry.block_seq, dropped(action, reason));
        }
    }

    fn emit(self, producer: Option<&Producer>, notices: &mut Notices) {
        let Some(producer) = producer else {
            return;
        };
        for (block_seq, outcomes) in self.0 {
            notices.0.push(Notice::Receipt(ActionReceiptParams {
                producer: producer.clone(),
                block_seq,
                outcomes,
            }));
        }
    }
}

impl Plan {
    /// 装入新接受的动作块：它从起始时刻起取代上一块未执行的部分（回执 `superseded`）。上一块的截止
    /// 时刻早于新块的起始时刻时，在那一刻插入一步空的持续状态，不让上一块的持续状态越过它的截止时刻。
    fn install(&mut self, block: ActionBlock, superseded: &mut Outcomes) {
        let start = block.start_ns;
        let mut kept = VecDeque::with_capacity(self.entries.len() + block.steps.len() + 1);
        for entry in self.entries.drain(..) {
            if entry.at_ns >= start {
                superseded.drop_all(&entry, DropReason::Superseded);
            } else {
                kept.push_back(entry);
            }
        }
        if let Some(old) = self.active
            && old.deadline_ns < start
        {
            kept.push_back(Entry {
                at_ns: old.deadline_ns,
                deadline_ns: old.deadline_ns,
                block_seq: old.seq,
                base_frame: block.base_frame,
                held: Vec::new(),
                actions: VecDeque::new(),
                held_done: false,
                done: Vec::new(),
            });
        }
        for step in block.steps {
            kept.push_back(Entry {
                // 校验已保证不溢出、不晚于截止时刻。
                at_ns: start.saturating_add(step.offset_ns),
                deadline_ns: block.deadline_ns,
                block_seq: block.seq,
                base_frame: block.base_frame,
                held: step.held,
                actions: step.actions.into(),
                held_done: false,
                done: Vec::new(),
            });
        }
        self.entries = kept;
        self.active = Some(ActiveBlock {
            seq: block.seq,
            deadline_ns: block.deadline_ns,
        });
    }

    /// 结束全部计划：未执行的动作回执 `released`。
    fn clear(&mut self, outcomes: &mut Outcomes) {
        for entry in self.entries.drain(..) {
            outcomes.drop_all(&entry, DropReason::Released);
        }
        self.active = None;
    }
}

struct State {
    ledger: InputLedger,
    lease: Lease,
    current: Option<Current>,
    revoked_through: Option<u64>,
    stopping: bool,
    input_fault: Option<Refusal>,
    last_release_id: u64,
    verifies_release: bool,
    pending_verification: Option<PendingVerification>,
    last_verification_posted: Option<InputVerificationParams>,
    plan: Plan,
    /// 输入策略；收到 `input.configure` 后有。
    policy: Option<Policy>,
    /// 真实后端：没有输入策略时拒绝含输入的动作块。
    requires_policy: bool,
    /// 过去 1 秒已执行的一次性动作的时刻，用于按 `max_rate` 限速。
    rate: VecDeque<u64>,
}

/// 调度一步的进展。
enum Step {
    /// 这一步做完了。
    Done,
    /// 有瞬时按住还没抬起，等到 `at_ns` 再接着做这一步的其余动作。
    Defer(u64),
    /// 要释放全部输入并结束这一块；`revoke` 为真时同时本地撤权。
    Release { reason: ReleaseReason, revoke: bool },
}

impl State {
    fn input_unavailable(&self) -> bool {
        self.input_fault.is_some() || self.pending_verification.is_some()
    }

    fn current_producer(&self) -> Option<Producer> {
        self.current.as_ref().map(|c| c.producer.clone())
    }

    /// 释放全部输入并结束计划；释放完成后才读时刻。`producer` 是释放时的当前产出者，计划里的步都属于它。
    /// `first` 是触发释放的那一步已有的结果，排在其余未执行的动作之前。
    fn release(
        &mut self,
        reason: ReleaseReason,
        producer: Option<&Producer>,
        first: Outcomes,
        notices: &mut Notices,
    ) -> bool {
        self.release_for_token(reason, producer, first, self.lease.active_token(), notices)
    }

    fn release_for_token(
        &mut self,
        reason: ReleaseReason,
        producer: Option<&Producer>,
        first: Outcomes,
        token: Option<u64>,
        notices: &mut Notices,
    ) -> bool {
        self.last_release_id += 1;
        let release_id = self.last_release_id;
        let awaits_verification = self.verifies_release
            && (self.policy.is_some()
                || matches!(
                    reason,
                    ReleaseReason::CoreStart
                        | ReleaseReason::Stop
                        | ReleaseReason::StdinClosed
                        | ReleaseReason::CoreConnectionLost
                ));
        if awaits_verification {
            // 新的全释放也覆盖旧许可仍待核验的输入。
            let blocks_new_lease = token.is_some()
                || self
                    .pending_verification
                    .is_some_and(|pending| pending.blocks_new_lease);
            self.pending_verification = Some(PendingVerification {
                release_id,
                blocks_new_lease,
            });
        }
        let accepted = self.ledger.release_all(reason, release_id);
        let verification = if accepted {
            ReleaseVerification::Unverified
        } else {
            self.latch_input_fault(Refusal::ReleaseFailed);
            ReleaseVerification::UpFailed
        };
        let released_at_ns = mono_clock::now_ns();
        let mut outcomes = first;
        self.plan.clear(&mut outcomes);
        notices.0.push(Notice::Released(ReleasedInput {
            params: InputReleasedParams {
                release_id,
                reason,
                producer: producer.cloned(),
                released_at_ns,
                verification,
            },
            token,
            awaits_verification,
        }));
        outcomes.emit(producer, notices);
        accepted
    }

    fn latch_input_fault(&mut self, fault: Refusal) {
        self.input_fault.get_or_insert(fault);
        self.lease.local_revoke();
    }

    fn pending_verification_id(&self) -> Option<u64> {
        self.pending_verification.map(|release| release.release_id)
    }

    fn check_lease_grant(&self, grant: &LeaseGrantParams, now: u64) -> Result<Granted, Refusal> {
        if now == 0 {
            return Err(Refusal::ClockUnavailable);
        }
        if self.stopping {
            return Err(Refusal::NoSource);
        }
        if let Some(fault) = self.input_fault {
            return Err(fault);
        }
        self.lease.check_grant(grant.token, grant.expires_ns, now)
    }

    fn grant_lease(
        &mut self,
        grant: &LeaseGrantParams,
        read_clock: impl Fn() -> u64,
        notices: &mut Notices,
    ) -> Result<LeaseGrant, Refusal> {
        let now = read_clock();
        if !self.stopping {
            self.expire_lease(now, notices);
        }
        let granted = self.check_lease_grant(grant, now)?;
        if granted == Granted::Replaced {
            let old_token = self.lease.active_token().ok_or(Refusal::NoSource)?;
            self.lease.revoke(old_token);
            if !self.release_current_for_token(
                ReleaseReason::LeaseRevoked,
                Some(old_token),
                notices,
            ) || (self.verifies_release
                && self.pending_verification_id() != Some(self.last_release_id))
            {
                return Err(Refusal::ReleaseFailed);
            }
        }
        if granted != Granted::Renewed
            && let Some(pending) = self.pending_verification
            && pending.blocks_new_lease
        {
            return Ok(LeaseGrant::Verifying(pending.release_id));
        }
        // 释放调用可能耗时，安装令牌前重新核对绝对截止时间。
        let now = read_clock();
        self.check_lease_grant(grant, now)?;
        self.lease.grant(grant.token, grant.expires_ns, now)?;
        Ok(LeaseGrant::Applied)
    }

    fn require_verified_release(&self, release_id: u64) -> Result<(), Refusal> {
        if self.stopping {
            return Err(Refusal::NoSource);
        }
        if let Some(fault) = self.input_fault {
            return Err(fault);
        }
        if self.last_release_id == release_id
            && self.pending_verification_id().is_none()
            && self.last_verification_posted.is_some_and(|checked| {
                checked.release_id == release_id && checked.result == VerificationResult::Verified
            })
        {
            Ok(())
        } else {
            Err(Refusal::ReleaseFailed)
        }
    }

    fn release_current(&mut self, reason: ReleaseReason, notices: &mut Notices) -> bool {
        self.release_current_for_token(reason, self.lease.active_token(), notices)
    }

    fn release_current_for_token(
        &mut self,
        reason: ReleaseReason,
        token: Option<u64>,
        notices: &mut Notices,
    ) -> bool {
        let producer = self.current_producer();
        self.release_for_token(
            reason,
            producer.as_ref(),
            Outcomes::default(),
            token,
            notices,
        )
    }

    fn revoke_through(&mut self, generation: u64, notices: &mut Notices) {
        let through = self
            .revoked_through
            .map_or(generation, |r| r.max(generation));
        self.revoked_through = Some(through);
        if self
            .current
            .as_ref()
            .is_some_and(|c| c.producer.generation <= through)
        {
            let producer = self.current.take().map(|c| c.producer);
            self.release(
                ReleaseReason::GenerationRevoked,
                producer.as_ref(),
                Outcomes::default(),
                notices,
            );
        }
    }

    /// 执行一步时触发的释放：`first` 是这一步已有的结果与其余动作的 `released`。`revoke` 为真时先本地
    /// 撤权（接管、急停、注入被拦截、钩子失效），许可源须换发更大的令牌。
    fn release_block(
        &mut self,
        reason: ReleaseReason,
        revoke: bool,
        first: Outcomes,
        notices: &mut Notices,
    ) {
        let token = self.lease.active_token();
        if revoke {
            self.lease.local_revoke();
        }
        let producer = self.current_producer();
        self.release_for_token(reason, producer.as_ref(), first, token, notices);
    }

    /// 丢掉过去 1 秒之前的一次性动作记录。
    fn prune_rate(&mut self, now: u64) {
        let window = 1_000_000_000;
        while let Some(&front) = self.rate.front() {
            if now.saturating_sub(front) >= window {
                self.rate.pop_front();
            } else {
                break;
            }
        }
    }

    /// 权威检查：输入许可有效；`producer` 是当前控制产出者且心跳有效；最新写入帧的帧龄不超过上限。
    /// 每次把一个输入交给后端之前调用；不成立时给出释放的原因。
    fn authority(
        &self,
        producer: &Producer,
        now: u64,
        frames: &FrameStatus,
        limits: &Limits,
    ) -> Result<(), ReleaseReason> {
        self.lease.check(now)?;
        match &self.current {
            Some(current) if current.producer == *producer => {
                if current.deadline.is_none_or(|deadline| now >= deadline) {
                    return Err(ReleaseReason::HeartbeatTimeout);
                }
            }
            _ => return Err(ReleaseReason::ProducerChanged),
        }
        if frame_is_fresh(frames, now, limits) {
            Ok(())
        } else {
            Err(ReleaseReason::FrameStale)
        }
    }

    /// 处理到 `now` 为止到期的一切：抬起到期的瞬时按住、心跳超时、许可到期、持有输入时帧龄超限、
    /// 最长按住、到时的步与截止时刻。
    fn tick(&mut self, now: u64, frames: &FrameStatus, limits: &Limits, notices: &mut Notices) {
        if self.stopping {
            return;
        }
        // 先抬起到期的瞬时按住：它不是失效，不释放全部；抬起后同一步的下一个一次性动作可以接着做。
        if !self.ledger.lift_due(now) {
            let token = self.lease.active_token();
            self.lease.local_revoke();
            self.release_current_for_token(ReleaseReason::InjectionBlocked, token, notices);
            return;
        }
        if let Some(current) = self.current.as_mut()
            && current.deadline.is_some_and(|deadline| now >= deadline)
        {
            current.deadline = None;
            self.release_current(ReleaseReason::HeartbeatTimeout, notices);
        }
        self.expire_lease(now, notices);
        // 持续状态不只在发新动作时检查：持有输入期间帧龄一超限就释放。
        if !self.ledger.is_empty() && !frame_is_fresh(frames, now, limits) {
            self.release_current(ReleaseReason::FrameStale, notices);
        }
        if self.max_hold_exceeded(now) {
            self.release_current(ReleaseReason::MaxHold, notices);
        }
        self.run_due_entries(now, frames, limits, notices);
        if let Some(active) = self.plan.active
            && now >= active.deadline_ns
        {
            self.release_current(ReleaseReason::DeadlineExpired, notices);
        }
    }

    fn expire_lease(&mut self, now: u64, notices: &mut Notices) {
        let token = self.lease.active_token();
        if self.lease.expire(now) {
            self.release_current_for_token(ReleaseReason::LeaseExpired, token, notices);
        }
    }

    /// 有输入策略、持续状态里最早一项按住超过 `max_hold` 时为真。
    fn max_hold_exceeded(&self, now: u64) -> bool {
        let Some(max_hold) = self.policy.as_ref().and_then(Policy::max_hold) else {
            return false;
        };
        let max_hold = nanos(max_hold);
        self.ledger
            .earliest_held_ns()
            .is_some_and(|pressed| now.saturating_sub(pressed) >= max_hold)
    }

    fn run_due_entries(
        &mut self,
        now: u64,
        frames: &FrameStatus,
        limits: &Limits,
        notices: &mut Notices,
    ) {
        // 一次提交可能在验完块后切换产出者并触发释放。调度也须等这次释放核验。
        if self.input_unavailable() {
            return;
        }
        loop {
            let Some(front) = self.plan.entries.front() else {
                return;
            };
            if front.at_ns > now {
                return;
            }
            let active = self.plan.active.map(|a| a.seq);
            if now > front.deadline_ns && active == Some(front.block_seq) {
                // 在效块的截止时刻已过：由截止时刻的释放结束它，这一步不再执行。
                return;
            }
            let mut entry = self
                .plan
                .entries
                .pop_front()
                .expect("front just checked above");
            let late = now > entry.deadline_ns;
            match self.step(&mut entry, late, now, frames, limits) {
                Step::Done => {
                    let producer = self.current_producer();
                    let mut outcomes = Outcomes::default();
                    for outcome in entry.done {
                        outcomes.add(entry.block_seq, outcome);
                    }
                    outcomes.emit(producer.as_ref(), notices);
                }
                Step::Defer(at) => {
                    entry.at_ns = at;
                    self.plan.entries.push_front(entry);
                    return;
                }
                Step::Release { reason, revoke } => {
                    // 这一步已有的结果加上其余动作的 released，由释放一并回执。
                    let mut first = Outcomes::default();
                    for outcome in &entry.done {
                        first.add(entry.block_seq, outcome.clone());
                    }
                    for action in &entry.actions {
                        first.add(entry.block_seq, dropped(action, DropReason::Released));
                    }
                    self.release_block(reason, revoke, first, notices);
                    return;
                }
            }
        }
    }

    /// 执行一步，结果累进 `entry.done`；返回这一步的进展。先抬起不在持续状态里的项，再逐项按下、逐个
    /// 执行一次性动作，每次交给后端之前都做权威检查。`late` 表示所属的旧块已过了截止时刻（调度线程醒得
    /// 晚了）：只做抬起，动作回执 `superseded`。
    #[allow(
        clippy::too_many_lines,
        reason = "一步的执行是一条直线的流程，拆开反而难读"
    )]
    fn step(
        &mut self,
        entry: &mut Entry,
        late: bool,
        now: u64,
        frames: &FrameStatus,
        limits: &Limits,
    ) -> Step {
        let Some(producer) = self.current_producer() else {
            while let Some(action) = entry.actions.pop_front() {
                entry.done.push(dropped(&action, DropReason::Released));
            }
            return Step::Release {
                reason: ReleaseReason::ProducerChanged,
                revoke: false,
            };
        };
        if !entry.held_done && !self.ledger.release_missing(&entry.held) {
            return drain_released(entry, ReleaseReason::InjectionBlocked, true);
        }
        if late {
            while let Some(action) = entry.actions.pop_front() {
                entry.done.push(dropped(&action, DropReason::Superseded));
            }
            return Step::Done;
        }
        if !entry.held_done {
            for input in &entry.held {
                if self.ledger.holds(input) {
                    continue;
                }
                if let Err(reason) = self.authority(&producer, now, frames, limits) {
                    return drain_released(entry, reason, false);
                }
                match self.ledger.press(input.clone(), now) {
                    Inject::Done => {}
                    Inject::FocusLost | Inject::FocusChanged => {
                        return drain_released(entry, ReleaseReason::FocusLost, false);
                    }
                    Inject::Blocked => {
                        return drain_released(entry, ReleaseReason::InjectionBlocked, true);
                    }
                }
            }
            entry.held_done = true;
        }
        while !entry.actions.is_empty() {
            // 瞬时按住还没抬起：同一步的下一个一次性动作等它抬起。
            if self.ledger.has_transient()
                && let Some(at) = self.ledger.next_lift()
            {
                return Step::Defer(at);
            }
            let action = entry.actions.pop_front().expect("not empty just checked");
            let id = action_id(&action);
            if self
                .current
                .as_ref()
                .is_some_and(|current| current.executed.contains(&id))
            {
                entry.done.push(dropped(&action, DropReason::Duplicate));
                continue;
            }
            if has_coordinates(&action) && frames.geometry_changed_since(entry.base_frame) {
                entry
                    .done
                    .push(dropped(&action, DropReason::GeometryChanged));
                continue;
            }
            self.prune_rate(now);
            if let Some(reason) = self.policy_drop(&action, frames) {
                entry.done.push(dropped(&action, reason));
                continue;
            }
            if let Err(reason) = self.authority(&producer, now, frames, limits) {
                entry.done.push(dropped(&action, DropReason::Released));
                return Step::Release {
                    reason,
                    revoke: false,
                };
            }
            match self.inject(&action, now) {
                Inject::Done => {
                    entry.done.push(ActionOutcome::Executed(Executed {
                        id,
                        at_ns: mono_clock::now_ns(),
                    }));
                    if let Some(current) = self.current.as_mut() {
                        current.executed.insert(id);
                    }
                    self.rate.push_back(now);
                }
                Inject::FocusChanged => {
                    entry.done.push(ActionOutcome::Uncertain(Uncertain {
                        id,
                        at_ns: mono_clock::now_ns(),
                    }));
                    return Step::Release {
                        reason: ReleaseReason::FocusLost,
                        revoke: false,
                    };
                }
                Inject::FocusLost => {
                    entry.done.push(dropped(&action, DropReason::Released));
                    return Step::Release {
                        reason: ReleaseReason::FocusLost,
                        revoke: false,
                    };
                }
                Inject::Blocked => {
                    entry.done.push(dropped(&action, DropReason::Released));
                    return Step::Release {
                        reason: ReleaseReason::InjectionBlocked,
                        revoke: true,
                    };
                }
            }
        }
        Step::Done
    }

    /// 执行时的输入策略核对（带坐标的落点、一次性动作的速率）；不通过时给出丢弃原因。没有输入策略时不限制。
    /// 调用方须已按 `now` 调用过 [`Self::prune_rate`]。
    fn policy_drop(&self, action: &Action, frames: &FrameStatus) -> Option<DropReason> {
        let policy = self.policy.as_ref()?;
        if let Some((x, y)) = coordinates(action) {
            let geometry = frames.latest_geometry();
            let allowed = geometry.is_some_and(|geometry| {
                policy.point_allowed(x, y, geometry.width, geometry.height)
            });
            if !allowed {
                return Some(DropReason::NotAllowed);
            }
        }
        // 速率：过去 1 秒内的一次性动作数到了上限即丢弃。
        (!policy.within_rate(self.rate.len())).then_some(DropReason::RateLimited)
    }

    /// 把一个一次性动作交给后端：有输入策略时点击与短按作为瞬时按住注入（按住 `press_duration`），
    /// 滚动是单个事件；没有输入策略时都是单个事件（G0）。
    fn inject(&mut self, action: &Action, now: u64) -> Inject {
        match &self.policy {
            Some(policy) => {
                let lift_ns = now.saturating_add(nanos(policy.press_duration()));
                match action {
                    Action::Click(click) => self.ledger.begin_momentary(
                        Momentary::Click {
                            x: click.x,
                            y: click.y,
                            button: click.button,
                        },
                        lift_ns,
                    ),
                    Action::KeyPress(press) => self.ledger.begin_momentary(
                        Momentary::Key {
                            scan_code: press.scan_code,
                        },
                        lift_ns,
                    ),
                    Action::Scroll(_) | Action::MouseMove(_) => self.ledger.act(action),
                }
            }
            None => self.ledger.act(action),
        }
    }
}

/// 最新写入帧的帧龄不超过上限。帧龄为负按 0；本实例还没写过帧时不算新鲜。
fn frame_is_fresh(frames: &FrameStatus, now: u64, limits: &Limits) -> bool {
    let max_age = u64::try_from(limits.max_frame_age.as_nanos()).unwrap_or(u64::MAX);
    frames
        .latest_capture_ns()
        .is_some_and(|capture_ns| now.saturating_sub(capture_ns) <= max_age)
}

fn action_id(action: &Action) -> u64 {
    match action {
        Action::Click(click) => click.id,
        Action::KeyPress(press) => press.id,
        Action::Scroll(scroll) => scroll.id,
        Action::MouseMove(moved) => moved.id,
    }
}

/// 坐标按基准帧的客户区解释的动作。
fn has_coordinates(action: &Action) -> bool {
    matches!(action, Action::Click(_) | Action::Scroll(_))
}

/// 带坐标的动作的客户区落点（物理像素）。
fn coordinates(action: &Action) -> Option<(u32, u32)> {
    match action {
        Action::Click(click) => Some((click.x, click.y)),
        Action::Scroll(scroll) => Some((scroll.x, scroll.y)),
        Action::KeyPress(_) | Action::MouseMove(_) => None,
    }
}

fn dropped(action: &Action, reason: DropReason) -> ActionOutcome {
    ActionOutcome::Dropped(Dropped {
        id: action_id(action),
        reason,
    })
}

/// 把一步里剩下的动作都回执 `released`，返回要释放的进展。
fn drain_released(entry: &mut Entry, reason: ReleaseReason, revoke: bool) -> Step {
    while let Some(action) = entry.actions.pop_front() {
        entry.done.push(dropped(&action, DropReason::Released));
    }
    Step::Release { reason, revoke }
}

/// 动作块的内容规则（不含产出者、序号与截止时刻）：至少一步；步的偏移不减，生效时刻不溢出、不晚于
/// 截止时刻；同一步中同一键、鼠标键或轴只出现一次，轴的取值在 [-1, 1]；动作身份在块内不重复。
pub(crate) fn check_block(block: &ActionBlock) -> Result<(), String> {
    if block.steps.is_empty() {
        return Err("an action block needs at least one step".into());
    }
    let mut previous = 0;
    let mut ids = HashSet::new();
    for (index, step) in block.steps.iter().enumerate() {
        if step.offset_ns < previous {
            return Err(format!("step {index}: offset_ns goes backwards"));
        }
        previous = step.offset_ns;
        let Some(at) = block.start_ns.checked_add(step.offset_ns) else {
            return Err(format!("step {index}: start_ns + offset_ns overflows"));
        };
        if at > block.deadline_ns {
            return Err(format!("step {index}: takes effect after deadline_ns"));
        }
        let mut held = HashSet::new();
        for input in &step.held {
            if !held.insert(HeldId::of(input)) {
                return Err(format!(
                    "step {index}: {:?} is held twice",
                    HeldId::of(input)
                ));
            }
            if let HeldInput::Axis(axis) = input
                && !(-1.0..=1.0).contains(&axis.value)
            {
                return Err(format!(
                    "step {index}: axis {} value {} is outside [-1, 1]",
                    axis.axis, axis.value
                ));
            }
        }
        for action in &step.actions {
            if !ids.insert(action_id(action)) {
                return Err(format!(
                    "step {index}: action id {} repeats",
                    action_id(action)
                ));
            }
        }
    }
    Ok(())
}

fn rejected(reason: RejectReason, message: impl Into<String>) -> ActionSubmitResult {
    ActionSubmitResult::Rejected(Rejected {
        reason,
        message: message.into(),
    })
}

/// 实时核的控制状态，供读线程、调度线程、健康线程与测试设备连接的线程共用。
pub(crate) struct Control {
    limits: Limits,
    state: Mutex<State>,
    changed: Condvar,
    /// 采集状态、最近写入的一帧与客户区几何，由采集线程更新。帧龄读它的原子量；几何查询拿它的锁，
    /// 锁序是先控制状态、后它，采集线程只拿它的锁。
    frames: Arc<FrameStatus>,
}

fn nanos(duration: Duration) -> u64 {
    u64::try_from(duration.as_nanos()).unwrap_or(u64::MAX)
}

impl Control {
    /// `backend` 是输入后端；有它才有许可源：操作员许可连接，或只用于测试、与假后端同生同灭的
    /// 测试许可源。`requires_policy` 为真（真实后端）时，没有输入策略即拒绝含输入的动作块。
    pub(crate) fn new(
        limits: Limits,
        frames: Arc<FrameStatus>,
        backend: Option<Box<dyn InputBackend>>,
        requires_policy: bool,
    ) -> Self {
        let has_source = backend.is_some();
        let verifies_release = backend
            .as_ref()
            .is_some_and(|backend| backend.verifies_release());
        Self {
            limits,
            state: Mutex::new(State {
                ledger: InputLedger::new(backend),
                lease: Lease::new(has_source),
                current: None,
                revoked_through: None,
                stopping: false,
                input_fault: None,
                last_release_id: 0,
                verifies_release,
                pending_verification: None,
                last_verification_posted: None,
                plan: Plan::default(),
                policy: None,
                requires_policy,
                rate: VecDeque::new(),
            }),
            changed: Condvar::new(),
            frames,
        }
    }

    fn lock(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// 改变了状态的操作结束时调用：调度线程据此重新计算下一次醒来的时刻。
    fn done(&self, state: MutexGuard<'_, State>) {
        drop(state);
        self.changed.notify_all();
    }

    /// 启动时先释放全部输入（`core_start`），之后才处理产出者与动作。
    pub(crate) fn release_on_start(&self) -> ReleasedInput {
        let mut notices = Notices::default();
        self.lock().release(
            ReleaseReason::CoreStart,
            None,
            Outcomes::default(),
            &mut notices,
        );
        match notices.0.into_iter().next() {
            Some(Notice::Released(released)) => released,
            _ => unreachable!("release always reports the release first"),
        }
    }

    /// 只核验当前释放。检查可报告补发被拒，在同一把锁内本地撤权。
    #[cfg(any(windows, test))]
    pub(crate) fn with_verification<T>(
        &self,
        release_id: u64,
        check: impl FnOnce() -> (T, bool),
    ) -> Option<T> {
        let mut state = self.lock();
        if state.pending_verification_id() != Some(release_id) {
            return None;
        }
        let (observed, repair_rejected) = check();
        if repair_rejected {
            state.latch_input_fault(Refusal::ReleaseFailed);
        }
        Some(observed)
    }

    /// 核验期间可能发生了更新的释放。只有当前释放的成功结果能打开输入门禁。
    #[cfg(any(windows, test))]
    pub(crate) fn finish_verification(
        &self,
        release_id: u64,
        observed: VerificationResult,
    ) -> InputVerificationParams {
        let mut state = self.lock();
        let result = if state.pending_verification_id() == Some(release_id) {
            match observed {
                VerificationResult::Verified if state.input_fault.is_some() => {
                    VerificationResult::Unavailable
                }
                VerificationResult::Residual | VerificationResult::Unavailable => {
                    state.latch_input_fault(Refusal::ReleaseFailed);
                    observed
                }
                _ => observed,
            }
        } else {
            VerificationResult::Superseded
        };
        let checked_at_ns = mono_clock::now_ns();
        self.done(state);
        InputVerificationParams {
            release_id,
            result,
            checked_at_ns,
        }
    }

    /// 回执入队后才放开对应的门禁；旧回执不能放开新的。
    #[cfg(any(windows, test))]
    pub(crate) fn verification_posted(&self, observed: InputVerificationParams) {
        let mut state = self.lock();
        if state
            .last_verification_posted
            .is_none_or(|last| last.release_id < observed.release_id)
        {
            state.last_verification_posted = Some(observed);
        }
        if state.pending_verification_id() == Some(observed.release_id) {
            state.pending_verification = None;
        }
        self.done(state);
    }

    /// 退出前给最后一次释放的物理核验一段有限时间。
    pub(crate) fn wait_for_final_verification(&self, timeout: Duration) -> bool {
        let has_report = |state: &State| {
            state
                .last_verification_posted
                .map_or(0, |checked| checked.release_id)
                == state.last_release_id
        };
        let (state, _) = self
            .changed
            .wait_timeout_while(self.lock(), timeout, |state| {
                state.verifies_release && !has_report(state)
            })
            .unwrap_or_else(PoisonError::into_inner);
        state.input_fault.is_none() && (!state.verifies_release || has_report(&state))
    }

    /// `producer.heartbeat`。有效的心跳刷新截止时刻；更高的代次先释放（`producer_changed`）再切换。
    pub(crate) fn heartbeat(
        &self,
        heartbeat: &ProducerHeartbeatParams,
        now: u64,
    ) -> Result<Notices, Rejection> {
        let mut state = self.lock();
        if state.stopping {
            return Err(Rejection::Stopping);
        }
        let producer = &heartbeat.producer;
        if state
            .revoked_through
            .is_some_and(|revoked| producer.generation <= revoked)
        {
            return Err(Rejection::Revoked);
        }
        let deadline = Some(now.saturating_add(nanos(self.limits.heartbeat_timeout)));
        let mut notices = Notices::default();
        match &mut state.current {
            Some(current) if producer.generation < current.producer.generation => {
                return Err(Rejection::StaleGeneration);
            }
            Some(current) if producer.generation == current.producer.generation => {
                if producer.id != current.producer.id {
                    return Err(Rejection::ProducerMismatch);
                }
                if current.last_seq.is_some_and(|last| heartbeat.seq <= last) {
                    return Err(Rejection::StaleSeq);
                }
                current.last_seq = Some(heartbeat.seq);
                current.deadline = deadline;
            }
            _ => {
                if state.current.is_some() {
                    state.release_current(ReleaseReason::ProducerChanged, &mut notices);
                }
                let mut current = Current::new(producer.clone());
                current.last_seq = Some(heartbeat.seq);
                current.deadline = deadline;
                state.current = Some(current);
            }
        }
        self.done(state);
        Ok(notices)
    }

    /// `producer.revoke`：作废 `generation` 及更早的全部代次；当前代次在其中时先释放
    /// （`generation_revoked`）并不再有当前产出者。返回时释放已完成。
    pub(crate) fn revoke(&self, generation: u64) -> (Option<u64>, Notices) {
        let mut state = self.lock();
        let previous_release = state.last_release_id;
        let mut notices = Notices::default();
        state.revoke_through(generation, &mut notices);
        // 空闲后至今本实例未发出新输入，仍可关联最后一次释放。
        let has_release = state.last_release_id != previous_release
            || state.verifies_release && state.current.is_none() && state.last_release_id > 0;
        let release_id = has_release.then_some(state.last_release_id);
        self.done(state);
        (release_id, notices)
    }

    /// 采集线程在切换目标、帧环或丢弃采集源时调用。清除帧与作废代次之间不执行输入。
    pub(crate) fn capture_reset(&self) -> Notices {
        let mut state = self.lock();
        self.frames.forget();
        let mut notices = Notices::default();
        if let Some(producer) = state.current_producer() {
            state.revoke_through(producer.generation, &mut notices);
        }
        self.done(state);
        notices
    }

    /// `action.submit`：先处理到 `now` 为止到期的一切，再按产出者、内容、序号与截止时刻的顺序校验；
    /// 被拒绝的动作块不改变任何状态。接受的动作块交给调度线程执行，这里不执行任何一步。
    pub(crate) fn submit(&self, block: ActionBlock, now: u64) -> (ActionSubmitResult, Notices) {
        let mut state = self.lock();
        let mut notices = Notices::default();
        state.tick(now, &self.frames, &self.limits, &mut notices);
        let result = Self::accept(&mut state, block, now, &self.frames, &mut notices);
        self.done(state);
        (result, notices)
    }

    fn accept(
        state: &mut State,
        block: ActionBlock,
        now: u64,
        frames: &FrameStatus,
        notices: &mut Notices,
    ) -> ActionSubmitResult {
        if state.stopping {
            return rejected(RejectReason::Stopping, "the core is stopping");
        }
        let producer = &block.producer;
        if let Some(revoked) = state.revoked_through
            && producer.generation <= revoked
        {
            return rejected(
                RejectReason::StaleGeneration,
                format!(
                    "generation {} is revoked (up to {revoked})",
                    producer.generation
                ),
            );
        }
        let same_generation = match &state.current {
            Some(current) if producer.generation < current.producer.generation => {
                return rejected(
                    RejectReason::StaleGeneration,
                    format!(
                        "generation {} is older than the current generation {}",
                        producer.generation, current.producer.generation
                    ),
                );
            }
            Some(current) if producer.generation == current.producer.generation => {
                if producer.id != current.producer.id {
                    return rejected(
                        RejectReason::ProducerMismatch,
                        format!(
                            "generation {} belongs to {:?}, not {:?}",
                            producer.generation, current.producer.id, producer.id
                        ),
                    );
                }
                Some(current.last_block_seq)
            }
            _ => None,
        };
        if let Err(message) = check_block(&block) {
            return rejected(RejectReason::Invalid, message);
        }
        if let Err(message) = Self::check_whitelist(state, &block) {
            return rejected(RejectReason::NotAllowed, message);
        }
        if let Some(Some(last)) = same_generation
            && block.seq <= last
        {
            return rejected(
                RejectReason::StaleSeq,
                format!("seq {} is not after {last}", block.seq),
            );
        }
        if block.deadline_ns <= now {
            return rejected(
                RejectReason::Expired,
                format!("deadline_ns {} is not after now ({now})", block.deadline_ns),
            );
        }
        if block
            .steps
            .iter()
            .any(|step| !step.held.is_empty() || !step.actions.is_empty())
        {
            if state.input_unavailable() {
                return rejected(
                    RejectReason::NotAllowed,
                    "input release has not been verified",
                );
            }
            if state.requires_policy && !frames.contains_frame(block.base_frame) {
                return rejected(
                    RejectReason::NotAllowed,
                    "the base frame is outside the current capture",
                );
            }
        }
        if same_generation.is_none() {
            if state.current.is_some()
                && !state.release_current(ReleaseReason::ProducerChanged, notices)
            {
                return rejected(
                    RejectReason::NotAllowed,
                    "input release failed during producer change",
                );
            }
            state.current = Some(Current::new(producer.clone()));
        }
        if let Some(current) = state.current.as_mut() {
            current.last_block_seq = Some(block.seq);
        }
        let producer = producer.clone();
        let mut superseded = Outcomes::default();
        state.plan.install(block, &mut superseded);
        superseded.emit(Some(&producer), notices);
        ActionSubmitResult::Accepted(Accepted {})
    }

    /// 从同一把锁内读时钟并尝试授予。入站许可请求只处理到期与释放，执行计划由调度线程负责。
    pub(crate) fn grant_lease(
        &self,
        grant: &LeaseGrantParams,
        read_clock: impl Fn() -> u64,
    ) -> (Result<LeaseGrant, Refusal>, Notices) {
        let mut state = self.lock();
        let mut notices = Notices::default();
        let result = state.grant_lease(grant, read_clock, &mut notices);
        self.done(state);
        (result, notices)
    }

    /// 给核验线程有限时间。等待期间不持控制锁，旧令牌已失效且新令牌尚未安装。
    pub(crate) fn wait_for_lease_proof(&self, release_id: u64) -> Result<(), Refusal> {
        let (state, _) = self
            .changed
            .wait_timeout_while(self.lock(), Duration::from_millis(200), |state| {
                !state.stopping
                    && state.input_fault.is_none()
                    && state.last_release_id == release_id
                    && state.pending_verification_id() == Some(release_id)
            })
            .unwrap_or_else(PoisonError::into_inner);
        state.require_verified_release(release_id)
    }

    /// 核验已发布后在锁内复查释放编号、许可状态与本次授予的绝对截止，再安装令牌。
    pub(crate) fn confirm_lease_grant(
        &self,
        grant: &LeaseGrantParams,
        release_id: u64,
        read_clock: impl FnOnce() -> u64,
    ) -> Result<(), Refusal> {
        let mut state = self.lock();
        state.require_verified_release(release_id)?;
        let now = read_clock();
        if state.check_lease_grant(grant, now)? != Granted::New {
            return Err(Refusal::ReleaseFailed);
        }
        state.lease.grant(grant.token, grant.expires_ns, now)?;
        self.done(state);
        Ok(())
    }

    /// 许可源的吊销（`operator_lease.revoke` 或 `test_lease.revoke`）：吊销 `token` 及更早的令牌；吊销了
    /// 有效的许可时先释放（`lease_revoked`）。
    /// 返回时释放已完成。
    pub(crate) fn revoke_lease(&self, token: u64, read_clock: impl FnOnce() -> u64) -> Notices {
        let mut state = self.lock();
        let mut notices = Notices::default();
        state.expire_lease(read_clock(), &mut notices);
        let previous = state.lease.active_token();
        if state.lease.revoke(token) {
            state.release_current_for_token(ReleaseReason::LeaseRevoked, previous, &mut notices);
        }
        self.done(state);
        notices
    }

    /// Bellis 要求在本次停止后重新核验，即使这个令牌已经停止。旧停止不得触碰更新的有效许可。
    pub(crate) fn stop_bellis_input(
        &self,
        through: u64,
        read_clock: impl FnOnce() -> u64,
    ) -> Notices {
        let mut state = self.lock();
        let mut notices = Notices::default();
        if !state.stopping {
            state.expire_lease(read_clock(), &mut notices);
            let newer = state
                .lease
                .active_token()
                .is_some_and(|token| token > through);
            state.lease.revoke(through);
            if !newer {
                state.release_current_for_token(
                    ReleaseReason::LeaseRevoked,
                    (through != 0).then_some(through),
                    &mut notices,
                );
            }
        }
        self.done(state);
        notices
    }

    /// `input.configure`：核对并存下输入策略，每个实例一次。设定过回 [`Configured::Conflict`]（-32006），
    /// 不合规则回 `Err`（-32602）。接受时告诉输入后端白名单里的键与鼠标键，供真实后端核验残留。
    pub(crate) fn configure(&self, params: &InputPolicy) -> Result<Configured, String> {
        let mut state = self.lock();
        if state.policy.is_some() {
            return Ok(Configured::Conflict);
        }
        let policy = Policy::parse(params)?;
        state.policy = Some(policy);
        state.ledger.configured(params);
        self.done(state);
        Ok(Configured::Accepted)
    }

    /// 提交时的输入策略核对：持续状态与一次性动作都须在白名单内（真实后端没有策略时拒绝含输入的块）。
    fn check_whitelist(state: &State, block: &ActionBlock) -> Result<(), String> {
        match &state.policy {
            Some(policy) => {
                for step in &block.steps {
                    for held in &step.held {
                        policy.allows_held(held).map_err(|error| error.0)?;
                    }
                    for action in &step.actions {
                        policy.allows_action(action).map_err(|error| error.0)?;
                    }
                }
                Ok(())
            }
            None if state.requires_policy => {
                let has_input = block
                    .steps
                    .iter()
                    .any(|step| !step.held.is_empty() || !step.actions.is_empty());
                if has_input {
                    Err("the real input backend has no input policy yet".into())
                } else {
                    Ok(())
                }
            }
            None => Ok(()),
        }
    }

    /// 真实后端的本地撤权：吊销令牌并释放全部输入。钩子失效后本实例拒绝后续令牌。
    /// 由钩子线程调用，通知经发送队列发出，不阻塞回调。
    #[cfg_attr(
        not(windows),
        allow(dead_code, reason = "只由 Windows 上真实后端的钩子线程调用")
    )]
    pub(crate) fn local_revoke(&self, reason: ReleaseReason) -> Notices {
        let mut state = self.lock();
        let mut notices = Notices::default();
        let hook_lost = reason == ReleaseReason::HookLost;
        if !(state.stopping || hook_lost && state.input_fault.is_some()) {
            let token = state.lease.active_token();
            if hook_lost {
                state.latch_input_fault(Refusal::HookLost);
            } else {
                state.lease.local_revoke();
            }
            state.release_current_for_token(reason, token, &mut notices);
        }
        self.done(state);
        notices
    }

    /// 许可现在是否有效：钩子据此判断是否算接管（许可无效时没有可接管的输入）。
    #[cfg_attr(
        not(windows),
        allow(dead_code, reason = "只由 Windows 上真实后端的钩子线程调用")
    )]
    pub(crate) fn lease_valid(&self) -> bool {
        self.lock().lease.check(mono_clock::now_ns()).is_ok()
    }

    /// 与许可源的连接断开：此后没有许可，释放全部输入（`lease_source_lost`）。正在停止时什么也不做。
    pub(crate) fn lease_source_lost(&self) -> Notices {
        let mut state = self.lock();
        let mut notices = Notices::default();
        let token = state.lease.active_token();
        if !state.stopping && state.lease.source_lost() {
            state.release_current_for_token(ReleaseReason::LeaseSourceLost, token, &mut notices);
        }
        self.done(state);
        notices
    }

    /// 处理到 `now` 为止到期的一切，供测试注入时刻；调度线程在锁内做同样的事（`run_scheduler`）。
    #[cfg(test)]
    pub(crate) fn tick(&self, now: u64) -> Notices {
        let mut state = self.lock();
        let mut notices = Notices::default();
        state.tick(now, &self.frames, &self.limits, &mut notices);
        self.done(state);
        notices
    }

    /// `core.stop`：释放全部输入（`stop`）并进入停止，此后不再接受控制产出者与动作块。
    pub(crate) fn stop(&self) -> Notices {
        self.release_and_stop(ReleaseReason::Stop)
    }

    /// 生命线：核连接断开（`core_connection_lost`）或 stdin 读到 EOF（`stdin_closed`）。释放全部输入并
    /// 进入停止。
    pub(crate) fn release_on_exit(&self, reason: ReleaseReason) -> Notices {
        self.release_and_stop(reason)
    }

    fn release_and_stop(&self, reason: ReleaseReason) -> Notices {
        let mut state = self.lock();
        let mut notices = Notices::default();
        let producer = state.current.take().map(|c| c.producer);
        state.release(reason, producer.as_ref(), Outcomes::default(), &mut notices);
        state.stopping = true;
        self.done(state);
        notices
    }

    /// `core.health` 的内容。先读时刻再读状态：`now_ns` 不早于某个时刻的一条，其中的状态也不早于那个时刻。
    ///
    /// 控制状态不知道音频：`audio` 为 `idle`、`latest_audio` 为 `null`，由会话按音频线程的状态填写
    /// （在这之后读，所以同样不早于 `now_ns`）。
    pub(crate) fn health(&self) -> CoreHealthParams {
        self.health_with_queue_depth().0
    }

    pub(crate) fn health_with_queue_depth(&self) -> (CoreHealthParams, u32) {
        let now_ns = mono_clock::now_ns();
        let (capture, latest_frame) = self.frames.snapshot();
        let state = self.lock();
        let depth = u32::try_from(state.plan.entries.len()).unwrap_or(u32::MAX);
        let health = CoreHealthParams {
            now_ns,
            capture,
            inputs_held: !state.ledger.is_empty(),
            producer: state.current_producer(),
            latest_frame,
            lease: state.lease.status(now_ns),
            audio: CaptureState::Idle,
            latest_audio: None,
        };
        (health, depth)
    }

    /// 下一次需要醒来的时刻：心跳截止、许可到期、下一步、在效块的截止时刻，以及持有输入时帧龄超限
    /// 的时刻。没有时为 `None`。
    fn next_wake(&self, state: &State) -> Option<u64> {
        let frame_stale_at = (!state.ledger.is_empty()).then(|| {
            self.frames.latest_capture_ns().map_or(0, |capture_ns| {
                capture_ns
                    .saturating_add(nanos(self.limits.max_frame_age))
                    .saturating_add(1)
            })
        });
        let max_hold_at = state
            .policy
            .as_ref()
            .and_then(Policy::max_hold)
            .zip(state.ledger.earliest_held_ns())
            .map(|(max_hold, pressed)| pressed.saturating_add(nanos(max_hold)));
        let next_entry = state
            .plan
            .entries
            .front()
            .filter(|_| !state.input_unavailable());
        [
            state.current.as_ref().and_then(|c| c.deadline),
            state.lease.expires_at(),
            next_entry.map(|entry| entry.at_ns),
            state.plan.active.map(|a| a.deadline_ns),
            frame_stale_at,
            state.ledger.next_lift(),
            max_hold_at,
        ]
        .into_iter()
        .flatten()
        .min()
    }

    /// 调度线程：在调用线程上运行到进入停止。按时刻执行动作块的步，并负责全部定时的释放（心跳超时、
    /// 许可到期、持有输入时帧龄超限、截止时刻）；每轮产生的通知在锁外交给 `send`。
    pub(crate) fn run_scheduler(&self, mut send: impl FnMut(Notices)) {
        let mut state = self.lock();
        loop {
            if state.stopping {
                return;
            }
            let now = mono_clock::now_ns();
            let mut notices = Notices::default();
            state.tick(now, &self.frames, &self.limits, &mut notices);
            if !notices.is_empty() {
                drop(state);
                send(notices);
                state = self.lock();
                continue;
            }
            state = match self.next_wake(&state) {
                None => self
                    .changed
                    .wait(state)
                    .unwrap_or_else(PoisonError::into_inner),
                Some(at) if at <= now => continue,
                Some(at) => {
                    self.changed
                        .wait_timeout(state, Duration::from_nanos(at - now))
                        .unwrap_or_else(PoisonError::into_inner)
                        .0
                }
            };
        }
    }

    /// 等到进入停止或过了 `timeout`；返回是否已进入停止。
    pub(crate) fn wait_stopping(&self, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        let mut state = self.lock();
        while !state.stopping {
            let now = Instant::now();
            if now >= deadline {
                return false;
            }
            state = self
                .changed
                .wait_timeout(state, deadline - now)
                .unwrap_or_else(PoisonError::into_inner)
                .0;
        }
        true
    }
}

#[cfg(test)]
mod tests;
