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
    CaptureState, CoreHealthParams, DropReason, Dropped, Executed, HeldInput, InputReleasedParams,
    LeaseGrantParams, Producer, ProducerHeartbeatParams, RejectReason, Rejected, ReleaseReason,
};

use crate::frames::FrameStatus;
use crate::lease::{Granted, Lease, Refusal};
use crate::ledger::{HeldId, InputBackend, InputLedger};

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
    Released(InputReleasedParams),
    Receipt(ActionReceiptParams),
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
                Notice::Released(released) => Some(released.reason),
                Notice::Receipt(_) => None,
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
                Notice::Released(_) => None,
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

/// 待执行的一步。
#[derive(Debug)]
struct Entry {
    /// 生效时刻：`start_ns + offset_ns`。
    at_ns: u64,
    /// 所属动作块的有效截止时刻；过了它这一步不再执行。
    deadline_ns: u64,
    block_seq: u64,
    base_frame: u64,
    held: Vec<HeldInput>,
    actions: Vec<Action>,
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
                actions: Vec::new(),
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
                actions: step.actions,
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
    plan: Plan,
}

impl State {
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
    ) {
        self.ledger.release_all(reason);
        let released_at_ns = mono_clock::now_ns();
        let mut outcomes = first;
        self.plan.clear(&mut outcomes);
        notices.0.push(Notice::Released(InputReleasedParams {
            reason,
            producer: producer.cloned(),
            released_at_ns,
        }));
        outcomes.emit(producer, notices);
    }

    fn release_current(&mut self, reason: ReleaseReason, notices: &mut Notices) {
        let producer = self.current_producer();
        self.release(reason, producer.as_ref(), Outcomes::default(), notices);
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

    /// 处理到 `now` 为止到期的一切：心跳超时、许可到期、持有输入时帧龄超限、到时的步与截止时刻。
    fn tick(&mut self, now: u64, frames: &FrameStatus, limits: &Limits, notices: &mut Notices) {
        if self.stopping {
            return;
        }
        if let Some(current) = self.current.as_mut()
            && current.deadline.is_some_and(|deadline| now >= deadline)
        {
            current.deadline = None;
            self.release_current(ReleaseReason::HeartbeatTimeout, notices);
        }
        if self.lease.expire(now) {
            self.release_current(ReleaseReason::LeaseExpired, notices);
        }
        // 持续状态不只在发新动作时检查：持有输入期间帧龄一超限就释放。
        if !self.ledger.is_empty() && !frame_is_fresh(frames, now, limits) {
            self.release_current(ReleaseReason::FrameStale, notices);
        }
        self.run_due_entries(now, frames, limits, notices);
        if let Some(active) = self.plan.active
            && now >= active.deadline_ns
        {
            self.release_current(ReleaseReason::DeadlineExpired, notices);
        }
    }

    fn run_due_entries(
        &mut self,
        now: u64,
        frames: &FrameStatus,
        limits: &Limits,
        notices: &mut Notices,
    ) {
        while let Some(front) = self.plan.entries.front() {
            if front.at_ns > now {
                return;
            }
            let active = self.plan.active.map(|a| a.seq);
            if now > front.deadline_ns && active == Some(front.block_seq) {
                // 在效块的截止时刻已过：由截止时刻的释放结束它，这一步不再执行。
                return;
            }
            let Some(entry) = self.plan.entries.pop_front() else {
                return;
            };
            let late = now > entry.deadline_ns;
            match self.execute(&entry, late, now, frames, limits) {
                Ok(outcomes) => outcomes.emit(self.current_producer().as_ref(), notices),
                Err((reason, outcomes)) => {
                    let producer = self.current_producer();
                    self.release(reason, producer.as_ref(), outcomes, notices);
                    return;
                }
            }
        }
    }

    /// 执行一步：先抬起不在持续状态里的项，再逐项按下、逐个执行一次性动作，每次交给后端之前都做权威
    /// 检查。`late` 表示所属的旧块已过了截止时刻（调度线程醒得晚了）：只做抬起，动作回执 `superseded`。
    fn execute(
        &mut self,
        entry: &Entry,
        late: bool,
        now: u64,
        frames: &FrameStatus,
        limits: &Limits,
    ) -> Result<Outcomes, (ReleaseReason, Outcomes)> {
        let mut outcomes = Outcomes::default();
        let Some(producer) = self.current_producer() else {
            outcomes.drop_all(entry, DropReason::Released);
            return Err((ReleaseReason::ProducerChanged, outcomes));
        };
        self.ledger.release_missing(&entry.held);
        if late {
            outcomes.drop_all(entry, DropReason::Superseded);
            return Ok(outcomes);
        }
        for input in &entry.held {
            if self.ledger.holds(input) {
                continue;
            }
            if let Err(reason) = self.authority(&producer, now, frames, limits) {
                outcomes.drop_all(entry, DropReason::Released);
                return Err((reason, outcomes));
            }
            self.ledger.press(input.clone());
        }
        let mut failed = None;
        for action in &entry.actions {
            let id = action_id(action);
            let outcome = if failed.is_some() {
                dropped(action, DropReason::Released)
            } else if self
                .current
                .as_ref()
                .is_some_and(|current| current.executed.contains(&id))
            {
                dropped(action, DropReason::Duplicate)
            } else if has_coordinates(action) && frames.geometry_changed_since(entry.base_frame) {
                dropped(action, DropReason::GeometryChanged)
            } else if let Err(reason) = self.authority(&producer, now, frames, limits) {
                failed = Some(reason);
                dropped(action, DropReason::Released)
            } else {
                self.ledger.act(action);
                if let Some(current) = self.current.as_mut() {
                    current.executed.insert(id);
                }
                ActionOutcome::Executed(Executed {
                    id,
                    at_ns: mono_clock::now_ns(),
                })
            };
            outcomes.add(entry.block_seq, outcome);
        }
        match failed {
            Some(reason) => Err((reason, outcomes)),
            None => Ok(outcomes),
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

fn dropped(action: &Action, reason: DropReason) -> ActionOutcome {
    ActionOutcome::Dropped(Dropped {
        id: action_id(action),
        reason,
    })
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
    /// 测试许可源。
    pub(crate) fn new(
        limits: Limits,
        frames: Arc<FrameStatus>,
        backend: Option<Box<dyn InputBackend>>,
    ) -> Self {
        let has_source = backend.is_some();
        Self {
            limits,
            state: Mutex::new(State {
                ledger: InputLedger::new(backend),
                lease: Lease::new(has_source),
                current: None,
                revoked_through: None,
                stopping: false,
                plan: Plan::default(),
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
    pub(crate) fn release_on_start(&self) -> InputReleasedParams {
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
    pub(crate) fn revoke(&self, generation: u64) -> Notices {
        let mut state = self.lock();
        let through = state
            .revoked_through
            .map_or(generation, |r| r.max(generation));
        state.revoked_through = Some(through);
        let mut notices = Notices::default();
        if state
            .current
            .as_ref()
            .is_some_and(|current| current.producer.generation <= through)
        {
            let producer = state.current.take().map(|c| c.producer);
            state.release(
                ReleaseReason::GenerationRevoked,
                producer.as_ref(),
                Outcomes::default(),
                &mut notices,
            );
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
        let result = Self::accept(&mut state, block, now, &mut notices);
        self.done(state);
        (result, notices)
    }

    fn accept(
        state: &mut State,
        block: ActionBlock,
        now: u64,
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
        if same_generation.is_none() {
            if state.current.is_some() {
                state.release_current(ReleaseReason::ProducerChanged, notices);
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

    /// 许可源的授予（`operator_lease.grant` 或 `test_lease.grant`）。换发了更大的令牌时先释放旧许可负责的
    /// 输入（`lease_revoked`）。
    pub(crate) fn grant_lease(
        &self,
        grant: &LeaseGrantParams,
        now: u64,
    ) -> (Result<(), Refusal>, Notices) {
        let mut state = self.lock();
        let mut notices = Notices::default();
        state.tick(now, &self.frames, &self.limits, &mut notices);
        let result = state.lease.grant(grant.token, grant.expires_ns, now);
        if result == Ok(Granted::Replaced) {
            state.release_current(ReleaseReason::LeaseRevoked, &mut notices);
        }
        self.done(state);
        (result.map(|_| ()), notices)
    }

    /// 许可源的吊销（`operator_lease.revoke` 或 `test_lease.revoke`）：吊销 `token` 及更早的令牌；吊销了
    /// 有效的许可时先释放（`lease_revoked`）。
    /// 返回时释放已完成。
    pub(crate) fn revoke_lease(&self, token: u64, now: u64) -> Notices {
        let mut state = self.lock();
        let mut notices = Notices::default();
        state.tick(now, &self.frames, &self.limits, &mut notices);
        if state.lease.revoke(token) {
            state.release_current(ReleaseReason::LeaseRevoked, &mut notices);
        }
        self.done(state);
        notices
    }

    /// 与许可源的连接断开：此后没有许可，释放全部输入（`lease_source_lost`）。正在停止时什么也不做。
    pub(crate) fn lease_source_lost(&self) -> Notices {
        let mut state = self.lock();
        let mut notices = Notices::default();
        if !state.stopping {
            state.lease.source_lost();
            state.release_current(ReleaseReason::LeaseSourceLost, &mut notices);
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
        let now_ns = mono_clock::now_ns();
        let (capture, latest_frame) = self.frames.snapshot();
        let state = self.lock();
        CoreHealthParams {
            now_ns,
            capture,
            inputs_held: !state.ledger.is_empty(),
            producer: state.current_producer(),
            latest_frame,
            lease: state.lease.status(now_ns),
            audio: CaptureState::Idle,
            latest_audio: None,
        }
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
        [
            state.current.as_ref().and_then(|c| c.deadline),
            state.lease.expires_at(),
            state.plan.entries.front().map(|e| e.at_ns),
            state.plan.active.map(|a| a.deadline_ns),
            frame_stale_at,
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
