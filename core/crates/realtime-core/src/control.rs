//! 控制状态：当前控制产出者、当前代次、已作废的最高代次与心跳，以及释放全部输入。
//!
//! 规则见 `docs/modules/core-protocol.md` 的“控制产出者”。每个改变状态的操作都在锁内先完成释放，
//! 再把 `input.released` 的内容交给调用方，由调用方在锁外发出：释放从不等待写入，写失败也已释放。

use std::sync::{Condvar, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant};

use core_protocol::messages::{
    CaptureState, CoreHealthParams, InputReleasedParams, Producer, ProducerHeartbeatParams,
    ReleaseReason,
};

use crate::ledger::InputLedger;

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

#[derive(Debug)]
struct Current {
    producer: Producer,
    last_seq: u64,
    /// 心跳截止时刻。超时释放后为 `None`（待命），直到同一产出者的下一次有效心跳。
    deadline: Option<Instant>,
}

#[derive(Debug, Default)]
struct State {
    ledger: InputLedger,
    current: Option<Current>,
    revoked_through: Option<u64>,
    stopping: bool,
}

impl State {
    /// 释放全部输入；释放完成后才读时刻。
    fn release(
        &mut self,
        reason: ReleaseReason,
        producer: Option<Producer>,
    ) -> InputReleasedParams {
        self.ledger.release_all();
        InputReleasedParams {
            reason,
            producer,
            released_at_ns: mono_clock::now_ns(),
        }
    }

    fn current_producer(&self) -> Option<Producer> {
        self.current.as_ref().map(|c| c.producer.clone())
    }
}

/// 实时核的控制状态，供读线程、看门狗线程与健康线程共用。
pub(crate) struct Control {
    heartbeat_timeout: Duration,
    state: Mutex<State>,
    changed: Condvar,
}

impl Control {
    pub(crate) fn new(heartbeat_timeout: Duration) -> Self {
        Self {
            heartbeat_timeout,
            state: Mutex::new(State::default()),
            changed: Condvar::new(),
        }
    }

    fn lock(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// 启动时先释放全部输入（`core_start`），之后才处理产出者与动作。
    pub(crate) fn release_on_start(&self) -> InputReleasedParams {
        self.lock().release(ReleaseReason::CoreStart, None)
    }

    /// `producer.heartbeat`。有效的心跳刷新截止时刻；更高的代次先释放（`producer_changed`）再切换，
    /// 返回要发出的 `input.released`。
    pub(crate) fn heartbeat(
        &self,
        heartbeat: &ProducerHeartbeatParams,
        now: Instant,
    ) -> Result<Option<InputReleasedParams>, Rejection> {
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
        let deadline = Some(now + self.heartbeat_timeout);
        let mut released = None;
        match &mut state.current {
            Some(current) if producer.generation < current.producer.generation => {
                return Err(Rejection::StaleGeneration);
            }
            Some(current) if producer.generation == current.producer.generation => {
                if producer.id != current.producer.id {
                    return Err(Rejection::ProducerMismatch);
                }
                if heartbeat.seq <= current.last_seq {
                    return Err(Rejection::StaleSeq);
                }
                current.last_seq = heartbeat.seq;
                current.deadline = deadline;
            }
            _ => {
                if state.current.is_some() {
                    let old = state.current_producer();
                    released = Some(state.release(ReleaseReason::ProducerChanged, old));
                }
                state.current = Some(Current {
                    producer: producer.clone(),
                    last_seq: heartbeat.seq,
                    deadline,
                });
            }
        }
        drop(state);
        self.changed.notify_all();
        Ok(released)
    }

    /// `producer.revoke`：作废 `generation` 及更早的全部代次；当前代次在其中时先释放
    /// （`generation_revoked`）并不再有当前产出者。返回时释放已完成。
    pub(crate) fn revoke(&self, generation: u64) -> Option<InputReleasedParams> {
        let mut state = self.lock();
        let through = state
            .revoked_through
            .map_or(generation, |r| r.max(generation));
        state.revoked_through = Some(through);
        let released = state
            .current
            .as_ref()
            .is_some_and(|current| current.producer.generation <= through)
            .then(|| {
                let producer = state.current.take().map(|c| c.producer);
                state.release(ReleaseReason::GenerationRevoked, producer)
            });
        drop(state);
        self.changed.notify_all();
        released
    }

    /// 心跳超时检查：到了截止时刻即释放全部输入（`heartbeat_timeout`）并待命，产出者仍是当前的。
    /// 看门狗在锁内做同样的检查（`run_watchdog`）；这个入口供测试注入时刻。
    #[cfg(test)]
    pub(crate) fn expire(&self, now: Instant) -> Option<InputReleasedParams> {
        Self::expire_locked(&mut self.lock(), now)
    }

    fn expire_locked(state: &mut State, now: Instant) -> Option<InputReleasedParams> {
        let current = state.current.as_mut()?;
        if current.deadline.is_none_or(|deadline| now < deadline) {
            return None;
        }
        current.deadline = None;
        let producer = state.current_producer();
        Some(state.release(ReleaseReason::HeartbeatTimeout, producer))
    }

    /// `core.stop`：释放全部输入（`stop`）并进入停止，此后不再接受控制产出者。
    pub(crate) fn stop(&self) -> InputReleasedParams {
        let mut state = self.lock();
        let producer = state.current.take().map(|c| c.producer);
        let released = state.release(ReleaseReason::Stop, producer);
        state.stopping = true;
        drop(state);
        self.changed.notify_all();
        released
    }

    /// 生命线：核连接断开或 stdin 读到 EOF。释放全部输入并进入停止；无法通知主进程，不返回通知。
    pub(crate) fn release_on_exit(&self) {
        let mut state = self.lock();
        state.current = None;
        state.ledger.release_all();
        state.stopping = true;
        drop(state);
        self.changed.notify_all();
    }

    /// `core.health` 的内容。
    pub(crate) fn health(&self) -> CoreHealthParams {
        let state = self.lock();
        CoreHealthParams {
            now_ns: mono_clock::now_ns(),
            // 帧环与采集在第 7 步接入，此前总是没有挂接。
            capture: CaptureState::Idle,
            inputs_held: !state.ledger.is_empty(),
            producer: state.current_producer(),
        }
    }

    /// 心跳看门狗：在调用线程上运行到进入停止，每次超时释放后在锁外调用 `on_release`。
    pub(crate) fn run_watchdog(&self, mut on_release: impl FnMut(InputReleasedParams)) {
        let mut state = self.lock();
        loop {
            if state.stopping {
                return;
            }
            let deadline = state.current.as_ref().and_then(|c| c.deadline);
            let Some(deadline) = deadline else {
                state = self
                    .changed
                    .wait(state)
                    .unwrap_or_else(PoisonError::into_inner);
                continue;
            };
            let now = Instant::now();
            if now < deadline {
                state = self
                    .changed
                    .wait_timeout(state, deadline - now)
                    .unwrap_or_else(PoisonError::into_inner)
                    .0;
                continue;
            }
            let released = Self::expire_locked(&mut state, now);
            drop(state);
            if let Some(released) = released {
                on_release(released);
            }
            state = self.lock();
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
mod tests {
    use super::{Control, Rejection};
    use core_protocol::messages::{
        InputReleasedParams, Producer, ProducerHeartbeatParams, ReleaseReason,
    };
    use std::sync::Arc;
    use std::sync::mpsc;
    use std::thread;
    use std::time::{Duration, Instant};

    const TIMEOUT: Duration = Duration::from_millis(500);

    fn producer(id: &str, generation: u64) -> Producer {
        Producer {
            id: id.to_owned(),
            generation,
        }
    }

    fn beat(id: &str, generation: u64, seq: u64) -> ProducerHeartbeatParams {
        ProducerHeartbeatParams {
            producer: producer(id, generation),
            seq,
        }
    }

    fn reason(released: Option<InputReleasedParams>) -> Option<(ReleaseReason, Option<Producer>)> {
        released.map(|r| (r.reason, r.producer))
    }

    #[test]
    fn start_releases_without_a_producer() {
        let control = Control::new(TIMEOUT);
        let released = control.release_on_start();
        assert_eq!(released.reason, ReleaseReason::CoreStart);
        assert_eq!(released.producer, None);
        assert!(released.released_at_ns > 0);
        let health = control.health();
        assert!(!health.inputs_held);
        assert_eq!(health.producer, None);
    }

    #[test]
    fn first_heartbeat_sets_the_producer_without_releasing() {
        let control = Control::new(TIMEOUT);
        let now = Instant::now();
        assert_eq!(control.heartbeat(&beat("a", 1, 7), now), Ok(None));
        assert_eq!(control.health().producer, Some(producer("a", 1)));
    }

    #[test]
    fn seq_must_increase_within_a_generation() {
        let control = Control::new(TIMEOUT);
        let t0 = Instant::now();
        assert_eq!(control.heartbeat(&beat("a", 1, 5), t0), Ok(None));
        for stale in [5, 4, 0] {
            let later = t0 + Duration::from_millis(400);
            assert_eq!(
                control.heartbeat(&beat("a", 1, stale), later),
                Err(Rejection::StaleSeq)
            );
        }
        // 不算数的心跳不刷新截止时刻：仍按 seq=5 那次的时刻超时。
        assert_eq!(control.expire(t0 + Duration::from_millis(499)), None);
        assert_eq!(
            reason(control.expire(t0 + TIMEOUT)),
            Some((ReleaseReason::HeartbeatTimeout, Some(producer("a", 1))))
        );
        // 递增的心跳算数，并重新开始计时。
        let t1 = t0 + Duration::from_secs(1);
        assert_eq!(control.heartbeat(&beat("a", 1, 6), t1), Ok(None));
        assert_eq!(control.expire(t1 + TIMEOUT / 2), None);
    }

    #[test]
    fn heartbeat_timeout_releases_once_and_stands_by() {
        let control = Control::new(TIMEOUT);
        let t0 = Instant::now();
        control.heartbeat(&beat("a", 3, 1), t0).unwrap();
        assert_eq!(
            reason(control.expire(t0 + TIMEOUT)),
            Some((ReleaseReason::HeartbeatTimeout, Some(producer("a", 3))))
        );
        // 待命：不再重复释放；产出者仍是当前的，恢复心跳后重新计时。
        assert_eq!(control.expire(t0 + 10 * TIMEOUT), None);
        assert_eq!(control.health().producer, Some(producer("a", 3)));
        let t1 = t0 + 20 * TIMEOUT;
        assert_eq!(control.heartbeat(&beat("a", 3, 2), t1), Ok(None));
        assert!(control.expire(t1 + TIMEOUT).is_some());
    }

    #[test]
    fn old_generations_and_other_identities_are_rejected() {
        let control = Control::new(TIMEOUT);
        let now = Instant::now();
        control.heartbeat(&beat("a", 2, 1), now).unwrap();
        assert_eq!(
            control.heartbeat(&beat("a", 1, 9), now),
            Err(Rejection::StaleGeneration)
        );
        assert_eq!(
            control.heartbeat(&beat("b", 2, 9), now),
            Err(Rejection::ProducerMismatch)
        );
        assert_eq!(control.health().producer, Some(producer("a", 2)));
    }

    #[test]
    fn higher_generation_releases_before_switching() {
        let control = Control::new(TIMEOUT);
        let now = Instant::now();
        control.heartbeat(&beat("a", 1, 10), now).unwrap();
        let released = control.heartbeat(&beat("b", 2, 1), now).unwrap();
        assert_eq!(
            reason(released),
            Some((ReleaseReason::ProducerChanged, Some(producer("a", 1))))
        );
        assert_eq!(control.health().producer, Some(producer("b", 2)));
        // 新代次的 seq 从它自己的第一次心跳算起。
        assert_eq!(control.heartbeat(&beat("b", 2, 2), now), Ok(None));
    }

    #[test]
    fn revoke_releases_the_current_generation_and_blocks_it() {
        let control = Control::new(TIMEOUT);
        let now = Instant::now();
        control.heartbeat(&beat("a", 4, 1), now).unwrap();
        // 作废更早的代次不影响当前产出者。
        assert_eq!(control.revoke(3), None);
        assert_eq!(control.health().producer, Some(producer("a", 4)));
        assert_eq!(
            control.heartbeat(&beat("a", 3, 1), now),
            Err(Rejection::Revoked)
        );

        assert_eq!(
            reason(control.revoke(4)),
            Some((ReleaseReason::GenerationRevoked, Some(producer("a", 4))))
        );
        assert_eq!(control.health().producer, None);
        assert_eq!(
            control.heartbeat(&beat("a", 4, 2), now),
            Err(Rejection::Revoked)
        );
        // 已作废的最高代次不回退。
        assert_eq!(control.revoke(1), None);
        assert_eq!(
            control.heartbeat(&beat("a", 4, 3), now),
            Err(Rejection::Revoked)
        );
        // 更新的代次照常接受，没有当前产出者时不需要释放。
        assert_eq!(control.heartbeat(&beat("b", 5, 1), now), Ok(None));
        // 作废未来的代次也覆盖当前代次。
        assert_eq!(
            reason(control.revoke(9)),
            Some((ReleaseReason::GenerationRevoked, Some(producer("b", 5))))
        );
        assert_eq!(
            control.heartbeat(&beat("c", 8, 1), now),
            Err(Rejection::Revoked)
        );
    }

    #[test]
    fn stop_releases_and_refuses_producers() {
        let control = Control::new(TIMEOUT);
        let now = Instant::now();
        control.heartbeat(&beat("a", 1, 1), now).unwrap();
        let released = control.stop();
        assert_eq!(released.reason, ReleaseReason::Stop);
        assert_eq!(released.producer, Some(producer("a", 1)));
        assert_eq!(
            control.heartbeat(&beat("a", 2, 1), now),
            Err(Rejection::Stopping)
        );
        assert!(control.wait_stopping(Duration::ZERO));
    }

    #[test]
    fn watchdog_releases_in_real_time_and_stops() {
        let timeout = Duration::from_millis(50);
        let control = Arc::new(Control::new(timeout));
        let (sender, released) = mpsc::channel();
        let watchdog = thread::spawn({
            let control = Arc::clone(&control);
            move || control.run_watchdog(|r| sender.send((r, Instant::now())).unwrap())
        });
        let sent = Instant::now();
        control.heartbeat(&beat("a", 1, 1), sent).unwrap();
        let (release, at) = released.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(release.reason, ReleaseReason::HeartbeatTimeout);
        assert!(at - sent >= timeout, "released after {:?}", at - sent);
        control.release_on_exit();
        watchdog.join().unwrap();
        assert!(released.try_recv().is_err());
    }
}
