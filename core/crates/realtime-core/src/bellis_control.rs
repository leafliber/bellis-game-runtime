//! 正式 Bellis 许可源。这个连接直接读原始帧，核连接和独立运行使用 `RpcPeer`。

mod releases;

use std::io;
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use bellis_channel::{
    ChannelError, Connection, ErrorCode, FrameKind, RPC_CANCEL, RpcKind, classify, is_cancel_params,
};
use core_protocol::messages::{LeaseGrantParams, LeaseStatus};
use game_bridge_protocol::{
    Ack, BellisRequest, ControlCall, ControlEvent, ControlResult, CoreDescription, CoreHealth,
    GAME_BRIDGE_MAJOR, LEASE_REFUSED, LeaseState, MonotonicMs, ROLE, RpcError, RuntimeNotification,
    RuntimeResponse,
};
use serde::Serialize;
use serde_json::Value;

pub(crate) use releases::ReleaseQueue;

use crate::bellis_session::Session;
use crate::config::{BellisLaunch, HELLO_TIMEOUT};
use crate::control::{Control, LeaseGrant};
use crate::session::StartError;
use crate::side::{Post, connect_side};

/// 读超时让同一线程有机会送出事件。
const READ_POLL: Duration = Duration::from_millis(20);
/// 对端无法读完一帧时关闭连接，许可源随之失效。
const WRITE_TIMEOUT: Duration = Duration::from_millis(200);
/// 续约前另查一次。空闲时也能发现启动器覆盖或删除当前会话。
const SESSION_POLL: Duration = Duration::from_millis(100);

pub(crate) struct BellisControl {
    connection: Arc<Connection>,
    releases: Arc<ReleaseQueue>,
    thread: Option<JoinHandle<()>>,
}

impl BellisControl {
    pub(crate) fn start(
        launch: BellisLaunch,
        control: Arc<Control>,
        post: Post,
        releases: Arc<ReleaseQueue>,
        health_interval: Duration,
    ) -> Result<Self, StartError> {
        let session = launch.source.read().map_err(StartError::BellisSession)?;
        if session.id != launch.expected_session_id {
            return Err(StartError::BellisSession(io::Error::new(
                io::ErrorKind::InvalidData,
                "Bellis SessionInfo does not match the Runtime session",
            )));
        }
        let connection =
            Arc::new(connect_side(&session.credentials, ROLE).map_err(StartError::Bellis)?);
        let mut worker = Worker {
            connection: Arc::clone(&connection),
            releases: Arc::clone(&releases),
            control,
            post,
            health_interval,
            launch,
            session,
            phase: Phase::AwaitingDescription,
        };
        let thread = thread::Builder::new()
            .name("bellis-control".into())
            .spawn(move || {
                let stopped = worker.run();
                let notices = worker.control.lease_source_lost();
                worker.connection.close();
                worker.releases.close();
                if !notices.is_empty()
                    && let Err(error) = stopped
                {
                    log!("Bellis control connection closed: {error}");
                }
                (worker.post)(notices);
            })
            .map_err(StartError::Thread)?;
        Ok(Self {
            connection,
            releases,
            thread: Some(thread),
        })
    }

    pub(crate) fn finish(self, timeout: Duration) {
        if !self.releases.flush(timeout) {
            log!(
                "Bellis release reports not flushed within {}ms",
                timeout.as_millis()
            );
        }
    }
}

impl Drop for BellisControl {
    fn drop(&mut self) {
        self.connection.close();
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

#[derive(PartialEq, Eq)]
enum Phase {
    AwaitingDescription,
    Ready,
    Incompatible,
}

struct Worker {
    connection: Arc<Connection>,
    releases: Arc<ReleaseQueue>,
    control: Arc<Control>,
    post: Post,
    health_interval: Duration,
    launch: BellisLaunch,
    session: Session,
    phase: Phase,
}

impl Worker {
    fn run(&mut self) -> Result<(), ChannelError> {
        let mut next_session_check = Instant::now();
        let mut next_health = next_session_check;
        let describe_deadline = next_session_check + HELLO_TIMEOUT;
        while self.connection.is_open() {
            let now = Instant::now();
            if self.phase == Phase::AwaitingDescription && now >= describe_deadline {
                return Err(protocol_error("Bellis game.describe timed out"));
            }
            if now >= next_session_check {
                self.check_session()?;
                next_session_check = now + SESSION_POLL;
            }
            if self.phase == Phase::Ready {
                for release in self.releases.ready(clock_now()?) {
                    self.notify(ControlEvent::InputReleased(release))?;
                }
                self.releases.sent();
                if now >= next_health {
                    self.report_health()?;
                    next_health = now + self.health_interval;
                }
            }
            let frame = match self.connection.recv_frame(Some(READ_POLL)) {
                Ok(frame) => frame,
                Err(error) if error.code() == ErrorCode::Timeout => continue,
                Err(error) => return Err(error),
            };
            if frame.kind != FrameKind::Json {
                return Err(protocol_error("Bellis control requires JSON frames"));
            }
            match BellisRequest::decode(&frame.payload) {
                Ok(request) => self.handle(request)?,
                Err(_) if rpc_cancel(&frame.payload) => {}
                Err(_) => return Err(protocol_error("invalid Bellis control request")),
            }
        }
        Ok(())
    }

    fn handle(&mut self, request: BellisRequest) -> Result<(), ChannelError> {
        let response = match (&self.phase, request.call) {
            (Phase::AwaitingDescription, ControlCall::Describe(params)) => {
                self.check_session()?;
                let description = CoreDescription::new(
                    self.launch.runtime_id.clone(),
                    MonotonicMs::from_ns(clock_now()?),
                );
                self.send(&RuntimeResponse::success(
                    request.id,
                    ControlResult::Description(description),
                ))?;
                // 必须先写出 Describe 的应答。主版本不符时保留连接供 Bellis 报告 WAITING。
                if params.major == GAME_BRIDGE_MAJOR {
                    self.phase = Phase::Ready;
                } else {
                    self.phase = Phase::Incompatible;
                    self.releases.close();
                }
                return Ok(());
            }
            (Phase::Ready, ControlCall::LeaseRenew(params)) => {
                self.check_session()?;
                let grant = LeaseGrantParams {
                    token: params.token.get(),
                    expires_ns: params.expires_at_ms.to_ns(),
                };
                let scope = params.local_dialogue.as_ref().or_else(|| {
                    params
                        .local_recovery
                        .as_ref()
                        .map(|recovery| &recovery.scope)
                });
                if let Some(scope) = scope
                    && (scope.session_id != self.session.id
                        || scope.runtime_id != self.launch.runtime_id
                        || self.launch.source.is_test())
                {
                    return Err(protocol_error(
                        "local input lease is for another Runtime session",
                    ));
                }
                let (result, notices) = if let Some(recovery) = &params.local_recovery {
                    self.control
                        .grant_bellis_recovery_lease(&grant, recovery, mono_clock::now_ns)
                } else {
                    self.control.grant_bellis_lease(
                        &grant,
                        params.local_dialogue.as_ref(),
                        mono_clock::now_ns,
                    )
                };
                (self.post)(notices);
                let result = match result {
                    Ok(LeaseGrant::Applied) => Ok(()),
                    Ok(LeaseGrant::Verifying(release_id)) => {
                        let verified = self.control.wait_for_lease_proof(release_id);
                        self.check_session()?;
                        verified.and_then(|()| {
                            self.control
                                .confirm_lease_grant(&grant, release_id, mono_clock::now_ns)
                        })
                    }
                    Err(refusal) => Err(refusal),
                };
                match result {
                    Ok(()) => RuntimeResponse::success(request.id, ControlResult::Ack(Ack {})),
                    Err(refusal) => {
                        let error = RpcError::new(LEASE_REFUSED, refusal.to_string(), None)
                            .expect("lease refusal is a valid RPC error code");
                        RuntimeResponse::failure(Some(request.id), error)
                    }
                }
            }
            (Phase::Ready, ControlCall::InputStop(params)) => {
                self.check_session()?;
                (self.post)(
                    self.control
                        .stop_bellis_input(params.through_token.get(), mono_clock::now_ns),
                );
                RuntimeResponse::success(request.id, ControlResult::Ack(Ack {}))
            }
            _ => {
                return Err(protocol_error(
                    "Bellis control request outside a compatible session",
                ));
            }
        };
        self.send(&response)
    }

    fn check_session(&self) -> Result<(), ChannelError> {
        match self.launch.source.read() {
            Ok(current) if current == self.session => Ok(()),
            _ => Err(protocol_error(
                "Bellis SessionInfo changed or is unavailable",
            )),
        }
    }

    fn report_health(&self) -> Result<(), ChannelError> {
        let (health, queue_depth) = self.control.health_with_queue_depth();
        if health.now_ns == 0 {
            return Err(protocol_error("system monotonic clock is unavailable"));
        }
        let lease_state = match health.lease {
            LeaseStatus::NotGranted => LeaseState::NotGranted,
            LeaseStatus::Valid => LeaseState::Valid,
            LeaseStatus::Expired => LeaseState::Expired,
            LeaseStatus::Revoked => LeaseState::Revoked,
            LeaseStatus::SourceLost => LeaseState::SourceLost,
        };
        let frame_age_ms = health.latest_frame.map(|frame| {
            let age = health.now_ns.saturating_sub(frame.capture_ns) / 1_000_000;
            u32::try_from(age).unwrap_or(u32::MAX)
        });
        self.notify(ControlEvent::HealthReported(CoreHealth {
            at_ms: MonotonicMs::from_ns(health.now_ns),
            lease_state,
            frame_age_ms,
            queue_depth,
        }))
    }

    fn notify(&self, event: ControlEvent) -> Result<(), ChannelError> {
        self.send(&RuntimeNotification::new(event))
    }

    fn send(&self, message: &impl Serialize) -> Result<(), ChannelError> {
        let json = serde_json::to_value(message)
            .map_err(|_| protocol_error("cannot encode Bellis control reply"))?;
        self.connection.send_with_timeout(&json, WRITE_TIMEOUT)
    }
}

fn protocol_error(message: &'static str) -> ChannelError {
    ChannelError::new(ErrorCode::ProtocolError, message)
}

fn clock_now() -> Result<u64, ChannelError> {
    let now = mono_clock::now_ns();
    if now == 0 {
        Err(protocol_error("system monotonic clock is unavailable"))
    } else {
        Ok(now)
    }
}

/// 按同步 `RpcPeer` 的约定处理取消。许可处理只接受原始帧的类型化结果。
fn rpc_cancel(payload: &[u8]) -> bool {
    let Ok(message) = serde_json::from_slice::<Value>(payload) else {
        return false;
    };
    classify(&message) == RpcKind::Notification
        && message["method"] == RPC_CANCEL
        && is_cancel_params(&message["params"])
        && (message["params"]["id"].is_string() || message["params"]["id"].is_i64())
}
