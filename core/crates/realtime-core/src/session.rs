//! 核连接上的一次会话：启动、握手、控制产出者的消息、动作块与生命线（`docs/modules/realtime-core.md` 的
//! “启动与生命线”与“动作块的执行”）。
//!
//! 线程：
//! - 调用 [`run`] 的线程：有操作员许可连接时先连上操作员控制台，以假后端启动时接着连上测试设备；释放全部
//!   输入（控制台随即收到 `input.released(core_start)`），连接主进程并握手，发出
//!   `input.released(core_start)`，再启动其余线程，然后等会话结束；
//! - `RpcPeer` 的读线程：处理 `producer.heartbeat`、`producer.revoke`、`core.stop`、`action.submit`、
//!   `frame_ring.attach` 与 `capture.set_target`，处理函数在读线程上同步执行，只动控制状态、发通知或把命令
//!   交给采集线程，很快返回，也不向主进程发请求；`action.submit` 只校验并交给调度线程，不执行任何一步；
//! - 调度线程：按时刻执行动作块的步，负责全部定时的释放（心跳超时、许可到期、帧龄超限、截止时刻）；
//! - 核连接的发送线程：写出调度线程与测试设备连接放进队列的通知，它们因此从不等网络写入；
//! - 健康：启动时立即发一次 `core.health`，之后按周期发；
//! - 采集：取帧写进帧环（`frames` 模块）；
//! - 两条生命线：一个线程等核连接关闭，一个线程读 stdin 直到 EOF；
//! - 操作员许可连接的读线程、发送线程与生命线（`operator` 模块）；以假后端启动时另有测试设备连接的读线程、
//!   发送线程，作为测试许可源时还有生命线（`fake` 模块）。
//!
//! 握手与 `core_start` 在启动读线程之前完成，所以读线程处理任何产出者消息之前，`core_start` 已经发出；
//! 发送队列也在 `core_start` 之后才开始写。

use std::fmt;
use std::io::{self, Read};
use std::sync::mpsc::{self, Sender};
use std::sync::{Arc, OnceLock, Weak};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use audio_ring::AudioRingWriter;
use bellis_channel::{
    ChannelError, Connection, ConnectionOptions, Handlers, Message, RpcError, RpcErrorCode,
    RpcKind, RpcOptions, RpcPeer, classify, connect,
};
use core_protocol::PROTOCOL_VERSION;
use core_protocol::messages::{
    ActionBlock, ActionSubmitResult, AudioRingAttachParams, AudioSetTargetParams,
    CaptureSetTargetParams, CoreHealthParams, CoreHelloParams, CoreHelloResult, Empty,
    FrameRingAttachParams, InputReleasedParams, ProducerHeartbeatParams, ProducerRevokeParams,
    ReleaseReason,
};
use core_protocol::methods::{
    ActionReceipt, ActionSubmit, AudioRingAttach, AudioSetTarget, CaptureSetTarget, CoreHealth,
    CoreHello, CoreStop, FrameRingAttach, InputReleased, Method, Notification,
    OperatorInputReleased, ProducerHeartbeat, ProducerRevoke, error_code,
};
use frame_ring::FrameRingWriter;
use serde_json::{Map, Value, json};

use crate::audio::{self, Audio, AudioStatus, AudioThread, audio_opener_for};
use crate::config::{FakePermit, HELLO_TIMEOUT, InputSetup, Launch, Options, PermitSource};
use crate::control::{Control, Limits, Notice, Notices};
use crate::fake::{DeviceRole, FakeBackend, connect_test_device};
use crate::frames::{Capture, CaptureThread, FrameStatus, TargetError, opener_for};
use crate::ledger::InputBackend;
use crate::operator::connect_operator;
use crate::queue::SendQueue;
use crate::side::{Post, SideConnection};

/// 实时核的程序版本，经 `core.hello` 报给主进程。
pub const CORE_VERSION: &str = env!("CARGO_PKG_VERSION");

/// 本端发出的请求的截止时间。实时核目前只在握手时发请求（`core.hello`，另有 [`HELLO_TIMEOUT`]）。
const RPC_TIMEOUT: Duration = Duration::from_secs(2);

/// 会话结束时等采集线程停掉采集源的时限。等不到也照常退出：输入此前已经释放。
const CAPTURE_STOP_WAIT: Duration = Duration::from_millis(500);

/// 会话结束时等音频线程停掉音频源的时限。等不到也照常退出：输入此前已经释放。
const AUDIO_STOP_WAIT: Duration = Duration::from_millis(500);

/// 会话结束时等核连接的发送队列写完的时限。等不到也照常退出：输入此前已经释放。
const OUTBOX_FLUSH_WAIT: Duration = Duration::from_millis(200);

/// 会话结束时等假输入事件（释放全部输入的抬起）写给测试一方的时限。只用于测试。
const DEVICE_FLUSH_WAIT: Duration = Duration::from_millis(500);

/// 会话结束时等 `input.released` 写给操作员控制台的时限。等不到也照常退出：输入此前已经释放。
const OPERATOR_FLUSH_WAIT: Duration = Duration::from_millis(200);

/// 会话怎样结束。每一种都已释放全部输入。
#[derive(Debug)]
pub enum Ended {
    /// 收到 `core.stop`，已回应。
    Stopped,
    /// 核连接断开；`None` 表示对端正常关闭。
    ConnectionClosed(Option<ChannelError>),
    /// stdin 读到 EOF 或读取失败：主进程要求停止，或已经退出。
    StdinClosed,
}

impl fmt::Display for Ended {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Stopped => f.write_str("core.stop"),
            Self::ConnectionClosed(None) => f.write_str("core connection closed"),
            Self::ConnectionClosed(Some(error)) => write!(f, "core connection lost: {error}"),
            Self::StdinClosed => f.write_str("stdin closed"),
        }
    }
}

/// 启动失败：没能建立会话。
#[derive(Debug)]
pub enum StartError {
    /// 连不上主进程或握手失败。
    Connect(ChannelError),
    /// 连不上操作员控制台或握手失败（只在给了操作员许可连接时连接）。
    Operator(ChannelError),
    /// 连不上测试设备或握手失败（只在假后端时连接）。
    TestDevice(ChannelError),
    /// `core.hello` 没有得到合法的接受。
    Hello(String),
    /// 无法创建线程。
    Thread(io::Error),
}

impl fmt::Display for StartError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Connect(error) => write!(f, "cannot connect to the main process: {error}"),
            Self::Operator(error) => write!(f, "cannot connect to the operator console: {error}"),
            Self::TestDevice(error) => write!(f, "cannot connect to the test device: {error}"),
            Self::Hello(reason) => write!(f, "core.hello failed: {reason}"),
            Self::Thread(error) => write!(f, "cannot start a thread: {error}"),
        }
    }
}

impl std::error::Error for StartError {}

/// 运行一次会话直到结束；返回前已释放全部输入。
///
/// `input` 是输入后端与许可源的组合（[`InputSetup::select`] 已核对门禁）；`stdin` 是生命线：读到 EOF
/// 即结束。
///
/// # Errors
///
/// 连不上主进程、操作员控制台或测试设备、握手失败或无法创建线程时返回 [`StartError`]；此时已释放全部
/// 输入。
pub fn run(
    options: &Options,
    launch: &Launch,
    input: InputSetup,
    stdin: impl Read + Send + 'static,
) -> Result<Ended, StartError> {
    let frames = Arc::new(FrameStatus::default());
    let limits = Limits {
        heartbeat_timeout: options.heartbeat_timeout,
        max_frame_age: options.max_frame_age,
    };
    let (control, outbox, sides) = start_input(input, limits, &frames, &launch.role)?;
    // 每次启动先释放全部输入，再接受任何产出者与动作。操作员控制台随即收到 `core_start`。
    let started = control.release_on_start();
    log!("released all inputs on start");
    outbox.mirror(&started);

    let credentials = &launch.credentials;
    let connection = match connect(
        &credentials.path,
        &launch.role,
        &credentials.key,
        &ConnectionOptions::default(),
    ) {
        Ok(connection) => connection,
        Err(error) => {
            sides.finish();
            return Err(StartError::Connect(error));
        }
    };
    let greeted = hello(&connection).and_then(|()| {
        connection
            .send(&notification::<InputReleased>(&started))
            .map_err(StartError::Connect)
    });
    if let Err(error) = greeted {
        sides.finish();
        return Err(error);
    }

    let (media, media_threads) = match Media::spawn(frames) {
        Ok(spawned) => spawned,
        Err(error) => {
            sides.finish();
            return Err(StartError::Thread(error));
        }
    };
    let (ended, end) = mpsc::channel();
    let peer = RpcPeer::spawn(
        connection,
        handlers(&control, &outbox, &media, ended.clone()),
        &RpcOptions {
            timeout: RPC_TIMEOUT,
        },
    );
    let peer = match peer {
        Ok(peer) => Arc::new(peer),
        Err(error) => {
            media_threads.stop();
            sides.finish();
            return Err(StartError::Thread(error));
        }
    };
    outbox.attach(&peer);
    log!(
        "connected as {}; heartbeat timeout {}ms, health every {}ms, max frame age {}ms",
        launch.role,
        options.heartbeat_timeout.as_millis(),
        options.health_interval.as_millis(),
        options.max_frame_age.as_millis()
    );

    let workers = start_workers(&control, &media, &outbox, &peer, options, &ended, stdin);
    drop(ended);
    let workers = match workers {
        Ok(workers) => workers,
        Err(error) => {
            // 起不了线程就断开核连接：按核连接断开释放。
            outbox.log(&control.release_on_exit(ReleaseReason::CoreConnectionLost));
            outbox.close(OUTBOX_FLUSH_WAIT);
            sides.finish();
            media_threads.stop();
            peer.close();
            return Err(StartError::Thread(error));
        }
    };

    // 所有发送端都在线程里；全部退出（不应发生）时按连接断开处理。
    let reason = end.recv().unwrap_or(Ended::ConnectionClosed(None));
    match reason {
        // `core.stop` 的处理函数已释放并在回应之前发出了 `input.released(stop)`。
        Ended::Stopped => {}
        // 生命线：先释放，再记录；连接已断开时无法通知主进程。
        Ended::ConnectionClosed(_) => {
            outbox.log(&control.release_on_exit(ReleaseReason::CoreConnectionLost));
        }
        // 连接可能仍在：尽量告诉主进程。
        Ended::StdinClosed => outbox.post(control.release_on_exit(ReleaseReason::StdinClosed)),
    }
    log!("{reason}; released all inputs, exiting");
    for worker in workers {
        let _ = worker.join();
    }
    outbox.close(OUTBOX_FLUSH_WAIT);
    // 释放全部输入的 `input.released` 写给操作员控制台、抬起写给测试一方之后才断开它们。
    sides.finish();
    media_threads.stop();
    peer.close();
    Ok(reason)
}

/// 旁路连接：操作员许可连接与测试设备连接，各自可能没有。
#[derive(Default)]
struct Sides {
    operator: Option<SideConnection>,
    device: Option<SideConnection>,
}

impl Sides {
    /// 等队列里的通知与事件写出（各有时限），再断开。
    fn finish(self) {
        if let Some(operator) = self.operator {
            operator.finish(OPERATOR_FLUSH_WAIT);
        }
        if let Some(device) = self.device {
            device.finish(DEVICE_FLUSH_WAIT);
        }
    }
}

/// 建立控制状态、输入后端与许可源。有操作员许可连接时先连上控制台、开始处理它的请求；以假后端启动时
/// 接着连上测试设备。测试许可源只在这里与假后端一起、且只在没有操作员许可连接时出现。
///
/// 交出的 [`Outbox`] 是发给主进程的通知的出口；有操作员许可连接时，它把每条 `input.released` 也放进那条
/// 连接的发送队列。
fn start_input(
    input: InputSetup,
    limits: Limits,
    frames: &Arc<FrameStatus>,
    role: &str,
) -> Result<(Arc<Control>, Arc<Outbox>, Sides), StartError> {
    let operator_queue = input.operator().map(|_| Arc::new(SendQueue::default()));
    let outbox = Arc::new(Outbox::new(operator_queue.clone()));
    let InputSetup::Fake {
        test_device,
        permit,
    } = input
    else {
        return Ok((
            Arc::new(Control::new(limits, Arc::clone(frames), None)),
            outbox,
            Sides::default(),
        ));
    };
    let (backend, events) = FakeBackend::new();
    let backend: Box<dyn InputBackend> = Box::new(backend);
    let control = Arc::new(Control::new(limits, Arc::clone(frames), Some(backend)));
    let post: Post = {
        let outbox = Arc::clone(&outbox);
        Arc::new(move |notices| outbox.post(notices))
    };
    let mut sides = Sides::default();
    let device_role = match permit {
        FakePermit::TestLease => DeviceRole::LeaseSource,
        FakePermit::Source(PermitSource::Operator(operator)) => {
            let connection = connect_operator(&operator, role).map_err(StartError::Operator)?;
            let queue = operator_queue.unwrap_or_default();
            sides.operator = Some(
                connection
                    .start(queue, &control, &post)
                    .map_err(StartError::Thread)?,
            );
            log!("lease source: the operator console");
            DeviceRole::EventsOnly
        }
    };
    let connection = connect_test_device(&test_device, role).map_err(StartError::TestDevice)?;
    sides.device = Some(
        connection
            .start(device_role, events, &control, &post)
            .map_err(StartError::Thread)?,
    );
    match device_role {
        DeviceRole::LeaseSource => {
            log!("input backend: fake, lease from the test device (tests only)");
        }
        DeviceRole::EventsOnly => {
            log!("input backend: fake, events to the test device (tests only)");
        }
    }
    Ok((control, outbox, sides))
}

/// 处理函数经它把命令交给采集线程与音频线程；健康线程从这里读音频的状态。
struct Media {
    capture: Capture,
    audio: Audio,
    audio_status: Arc<AudioStatus>,
}

/// 采集线程与音频线程本身，会话结束时停掉。
struct MediaThreads {
    capture: CaptureThread,
    audio: AudioThread,
}

impl Media {
    /// 启动采集线程与音频线程；两者互不依赖，也都不碰控制状态。
    fn spawn(frames: Arc<FrameStatus>) -> io::Result<(Arc<Self>, MediaThreads)> {
        let audio_status = Arc::new(AudioStatus::default());
        let (capture, capture_thread) = Capture::spawn(frames)?;
        let (audio, audio_thread) = match Audio::spawn(Arc::clone(&audio_status)) {
            Ok(spawned) => spawned,
            Err(error) => {
                stop_thread("capture", capture_thread.stop(CAPTURE_STOP_WAIT));
                return Err(error);
            }
        };
        Ok((
            Arc::new(Self {
                capture,
                audio,
                audio_status,
            }),
            MediaThreads {
                capture: capture_thread,
                audio: audio_thread,
            },
        ))
    }
}

impl MediaThreads {
    /// 停掉采集线程与音频线程；释放已在此前完成，等不到它们结束也照常退出。
    fn stop(self) {
        stop_thread("capture", self.capture.stop(CAPTURE_STOP_WAIT));
        stop_thread("audio", self.audio.stop(AUDIO_STOP_WAIT));
    }
}

fn stop_thread(name: &str, stopped: bool) {
    if !stopped {
        log!("the {name} thread did not stop in time");
    }
}

/// 发 `core.hello` 并等主进程接受。握手完成前双方不发其它消息，所以第一条收到的就应是回应。
fn hello(connection: &Connection) -> Result<(), StartError> {
    const HELLO_ID: i64 = 0;
    let params = CoreHelloParams {
        protocol_version: PROTOCOL_VERSION,
        core_version: CORE_VERSION.to_owned(),
    };
    connection
        .send(
            &json!({"jsonrpc": "2.0", "id": HELLO_ID, "method": CoreHello::NAME, "params": params}),
        )
        .map_err(StartError::Connect)?;
    let message = match connection.recv(Some(HELLO_TIMEOUT)) {
        Ok(Message::Json(message)) => message,
        Ok(Message::Binary(_)) => {
            return Err(StartError::Hello(
                "received binary data before the reply".into(),
            ));
        }
        Err(error) => return Err(StartError::Connect(error)),
    };
    if message.get("id") != Some(&Value::from(HELLO_ID)) {
        return Err(StartError::Hello(format!("unexpected message {message}")));
    }
    match classify(&message) {
        RpcKind::Success => {
            let result: CoreHelloResult = serde_json::from_value(message["result"].clone())
                .map_err(|error| StartError::Hello(format!("invalid result: {error}")))?;
            if result.protocol_version == PROTOCOL_VERSION {
                Ok(())
            } else {
                Err(StartError::Hello(format!(
                    "main process speaks protocol {}, core speaks {PROTOCOL_VERSION}",
                    result.protocol_version
                )))
            }
        }
        RpcKind::Failure => Err(StartError::Hello(format!(
            "rejected by the main process: {}",
            message["error"]
        ))),
        _ => Err(StartError::Hello(format!("invalid reply {message}"))),
    }
}

fn notification<M: Notification>(params: &M::Params) -> Value {
    json!({"jsonrpc": "2.0", "method": M::NAME, "params": params})
}

/// 发给主进程的通知的出口，有两条路：
///
/// - `send`：在调用线程上直接写。读线程上的处理函数用它，`producer.revoke` 与 `core.stop` 由此在回应之前
///   发出 `input.released`；
/// - `post`：放进发送队列，由发送线程写。调度线程与旁路连接的线程用它，从不等网络写入。
///
/// 有操作员许可连接时，经过这里的每条 `input.released`（含只记日志、主进程收不到的）也放进操作员许可连接
/// 的发送队列（[`Self::mirror`]），由它的发送线程写给控制台。
///
/// 读线程启动后才拿到对端；在那之前到达的处理函数等它就位，最多几微秒。发送线程在拿到对端之后才启动。
struct Outbox {
    peer: OnceLock<Weak<RpcPeer>>,
    queue: SendQueue,
    operator: Option<Arc<SendQueue>>,
}

fn to_params(method: &str, params: &impl serde::Serialize) -> Option<Map<String, Value>> {
    if let Ok(Value::Object(params)) = serde_json::to_value(params) {
        Some(params)
    } else {
        log!("{method} not sent: params are not an object");
        None
    }
}

impl Outbox {
    fn new(operator: Option<Arc<SendQueue>>) -> Self {
        Self {
            peer: OnceLock::new(),
            queue: SendQueue::default(),
            operator,
        }
    }

    fn attach(&self, peer: &Arc<RpcPeer>) {
        let _ = self.peer.set(Arc::downgrade(peer));
    }

    /// 写一条通知；连接已断开或写失败只记录。调用方须已完成释放。
    fn write(&self, method: &str, params: &Map<String, Value>) {
        let Some(peer) = self.peer.wait().upgrade() else {
            return;
        };
        if let Err(error) = peer.notify(method, Some(params)) {
            log!("{method} not sent: {error}");
        }
    }

    /// 在调用线程上直接发一条通知。
    fn notify<M: Notification>(&self, params: &M::Params) {
        if let Some(params) = to_params(M::NAME, params) {
            self.write(M::NAME, &params);
        }
    }

    /// 把一次释放的 `input.released` 放进操作员许可连接的发送队列；没有这条连接时什么也不做。
    fn mirror(&self, released: &InputReleasedParams) {
        if let Some(queue) = &self.operator
            && let Some(params) = to_params(OperatorInputReleased::NAME, released)
        {
            queue.push(OperatorInputReleased::NAME, params);
        }
    }

    /// 记下通知中的释放：主进程收不到时，日志是唯一的记录。操作员控制台照常收到。
    fn log(&self, notices: &Notices) {
        for notice in &notices.0 {
            if let Notice::Released(released) = notice {
                log_released(released);
                self.mirror(released);
            }
        }
    }

    fn each(&self, notices: Notices, mut deliver: impl FnMut(&'static str, Map<String, Value>)) {
        for notice in notices.0 {
            let (method, params) = match &notice {
                Notice::Released(released) => {
                    log_released(released);
                    self.mirror(released);
                    (
                        InputReleased::NAME,
                        to_params(InputReleased::NAME, released),
                    )
                }
                Notice::Receipt(receipt) => {
                    (ActionReceipt::NAME, to_params(ActionReceipt::NAME, receipt))
                }
            };
            if let Some(params) = params {
                deliver(method, params);
            }
        }
    }

    /// 在调用线程上直接发出这些通知。
    fn send(&self, notices: Notices) {
        self.each(notices, |method, params| self.write(method, &params));
    }

    /// 把这些通知放进发送队列，立即返回。
    fn post(&self, notices: Notices) {
        self.each(notices, |method, params| self.queue.push(method, params));
    }

    /// 发送线程的循环，直到 [`Self::close`]。
    fn run_sender(&self) {
        self.queue.run(|method, params| self.write(method, params));
    }

    /// 等发送队列写完（至多 `timeout`），然后让发送线程结束。
    fn close(&self, timeout: Duration) {
        if !self.queue.flush(timeout) {
            log!("notifications not flushed within {}ms", timeout.as_millis());
        }
        self.queue.abandon();
    }
}

fn log_released(released: &InputReleasedParams) {
    log!(
        "released all inputs: {:?} (producer {:?})",
        released.reason,
        released.producer
    );
}

fn parse<T: serde::de::DeserializeOwned>(params: &Map<String, Value>) -> Result<T, String> {
    serde_json::from_value(Value::Object(params.clone())).map_err(|error| error.to_string())
}

fn invalid_params(method: &str, error: &str) -> RpcError {
    RpcError::new(
        RpcErrorCode::INVALID_PARAMS,
        format!("invalid params for {method}: {error}"),
    )
}

fn empty() -> Value {
    serde_json::to_value(Empty {}).unwrap_or_else(|_| json!({}))
}

fn capture_stopped() -> RpcError {
    RpcError::new(
        RpcErrorCode::INTERNAL_ERROR,
        "the capture thread has stopped",
    )
}

/// `frame_ring.attach`：打开并核对帧环，把写入方交给采集线程。帧号从帧环头部的 `write_seq` 续写。
fn attach_handler(
    media: &Arc<Media>,
) -> impl Fn(&Map<String, Value>) -> Result<Value, RpcError> + Send + Sync + 'static {
    let media = Arc::clone(media);
    move |params| {
        let params: FrameRingAttachParams =
            parse(params).map_err(|error| invalid_params(FrameRingAttach::NAME, &error))?;
        let writer = FrameRingWriter::attach(&params).map_err(|error| {
            log!(
                "{} {:?} refused: {error}",
                FrameRingAttach::NAME,
                params.name
            );
            RpcError::new(
                i64::from(error_code::FRAME_RING_MISMATCH),
                error.to_string(),
            )
        })?;
        log!(
            "attached frame ring {:?}; frame numbers continue after {}",
            params.name,
            writer.last_frame_number()
        );
        media
            .capture
            .attach(writer)
            .map_err(|_| capture_stopped())?;
        Ok(empty())
    }
}

/// `capture.set_target`：核对目标，交给采集线程；旧的采集源先停掉。
fn set_target_handler(
    media: &Arc<Media>,
) -> impl Fn(&Map<String, Value>) -> Result<Value, RpcError> + Send + Sync + 'static {
    let media = Arc::clone(media);
    move |params| {
        let params: CaptureSetTargetParams =
            parse(params).map_err(|error| invalid_params(CaptureSetTarget::NAME, &error))?;
        let opener = opener_for(params.target).map_err(|error| {
            log!("{} refused: {error}", CaptureSetTarget::NAME);
            let code = match &error {
                TargetError::Invalid(_) => RpcErrorCode::INVALID_PARAMS,
                TargetError::Unsupported(_) => i64::from(error_code::CAPTURE_UNSUPPORTED),
                TargetError::Surface(_) => i64::from(error_code::FRAME_RING_MISMATCH),
            };
            RpcError::new(code, error.to_string())
        })?;
        log!("capture target: {opener}");
        media
            .capture
            .set_target(opener)
            .map_err(|_| capture_stopped())?;
        Ok(empty())
    }
}

fn audio_stopped() -> RpcError {
    RpcError::new(RpcErrorCode::INTERNAL_ERROR, "the audio thread has stopped")
}

/// `audio_ring.attach`：打开并核对音频环，把写入方交给音频线程。块号从音频环头部的 `write_seq` 续写。
fn audio_attach_handler(
    media: &Arc<Media>,
) -> impl Fn(&Map<String, Value>) -> Result<Value, RpcError> + Send + Sync + 'static {
    let media = Arc::clone(media);
    move |params| {
        let params: AudioRingAttachParams =
            parse(params).map_err(|error| invalid_params(AudioRingAttach::NAME, &error))?;
        let writer = AudioRingWriter::attach(&params).map_err(|error| {
            log!(
                "{} {:?} refused: {error}",
                AudioRingAttach::NAME,
                params.name
            );
            RpcError::new(
                i64::from(error_code::AUDIO_RING_MISMATCH),
                error.to_string(),
            )
        })?;
        log!(
            "attached audio ring {:?} ({}); block numbers continue after {}",
            params.name,
            writer.format(),
            writer.last_block_number()
        );
        media.audio.attach(writer).map_err(|_| audio_stopped())?;
        Ok(empty())
    }
}

/// `audio.set_target`：核对目标，交给音频线程；旧的音频源先停掉。
fn audio_target_handler(
    media: &Arc<Media>,
) -> impl Fn(&Map<String, Value>) -> Result<Value, RpcError> + Send + Sync + 'static {
    let media = Arc::clone(media);
    move |params| {
        let params: AudioSetTargetParams =
            parse(params).map_err(|error| invalid_params(AudioSetTarget::NAME, &error))?;
        let opener = audio_opener_for(params.target).map_err(|error| {
            log!("{} refused: {error}", AudioSetTarget::NAME);
            let code = match &error {
                audio::TargetError::Invalid(_) => RpcErrorCode::INVALID_PARAMS,
                audio::TargetError::Unsupported(_) => i64::from(error_code::CAPTURE_UNSUPPORTED),
                audio::TargetError::Output(_) => i64::from(error_code::AUDIO_RING_MISMATCH),
            };
            RpcError::new(code, error.to_string())
        })?;
        log!("audio target: {opener}");
        media
            .audio
            .set_target(opener)
            .map_err(|_| audio_stopped())?;
        Ok(empty())
    }
}

fn handlers(
    control: &Arc<Control>,
    outbox: &Arc<Outbox>,
    media: &Arc<Media>,
    ended: Sender<Ended>,
) -> Handlers {
    let heartbeat = {
        let (control, outbox) = (Arc::clone(control), Arc::clone(outbox));
        move |params: &Map<String, Value>| {
            let heartbeat: ProducerHeartbeatParams = match parse(params) {
                Ok(heartbeat) => heartbeat,
                Err(error) => {
                    log!("ignored {}: {error}", ProducerHeartbeat::NAME);
                    return;
                }
            };
            match control.heartbeat(&heartbeat, mono_clock::now_ns()) {
                Ok(notices) => outbox.send(notices),
                Err(rejection) => log!(
                    "ignored {} from {:?} seq {}: {rejection:?}",
                    ProducerHeartbeat::NAME,
                    heartbeat.producer,
                    heartbeat.seq
                ),
            }
        }
    };
    let revoke = {
        let (control, outbox) = (Arc::clone(control), Arc::clone(outbox));
        move |params: &Map<String, Value>| {
            let revoke: ProducerRevokeParams =
                parse(params).map_err(|error| invalid_params(ProducerRevoke::NAME, &error))?;
            // 释放完成、`input.released` 发出之后才回应：回应即这些代次的输入已释放的确认。
            outbox.send(control.revoke(revoke.generation));
            Ok(empty())
        }
    };
    let stop = {
        let (control, outbox) = (Arc::clone(control), Arc::clone(outbox));
        move |params: &Map<String, Value>| {
            let _: Empty = parse(params).map_err(|error| invalid_params(CoreStop::NAME, &error))?;
            outbox.send(control.stop());
            Ok(empty())
        }
    };
    // 回应写出之后才结束会话，否则退出会丢掉回应。
    let stopped = move || {
        let _ = ended.send(Ended::Stopped);
    };
    Handlers::new()
        .notification(ProducerHeartbeat::NAME, heartbeat)
        .request(ProducerRevoke::NAME, revoke)
        .request_then(CoreStop::NAME, stop, stopped)
        .request(ActionSubmit::NAME, submit_handler(control, outbox))
        .request(FrameRingAttach::NAME, attach_handler(media))
        .request(CaptureSetTarget::NAME, set_target_handler(media))
        .request(AudioRingAttach::NAME, audio_attach_handler(media))
        .request(AudioSetTarget::NAME, audio_target_handler(media))
}

/// `action.submit`：校验并交给调度线程，不在读线程上执行任何一步。参数解析不了（未知字段、类型不对）
/// 回参数无效；不合动作块规则的回 `rejected`。
fn submit_handler(
    control: &Arc<Control>,
    outbox: &Arc<Outbox>,
) -> impl Fn(&Map<String, Value>) -> Result<Value, RpcError> + Send + Sync + 'static {
    let (control, outbox) = (Arc::clone(control), Arc::clone(outbox));
    move |params| {
        let block: ActionBlock =
            parse(params).map_err(|error| invalid_params(ActionSubmit::NAME, &error))?;
        let (producer, seq) = (block.producer.clone(), block.seq);
        let (result, notices) = control.submit(block, mono_clock::now_ns());
        outbox.post(notices);
        if let ActionSubmitResult::Rejected(rejected) = &result {
            log!(
                "{} from {producer:?} seq {seq} rejected: {:?} ({})",
                ActionSubmit::NAME,
                rejected.reason,
                rejected.message
            );
        }
        serde_json::to_value(result).map_err(|error| {
            RpcError::new(
                RpcErrorCode::INTERNAL_ERROR,
                format!("cannot encode: {error}"),
            )
        })
    }
}

/// `core.health` 的内容：控制状态与画面采集由 [`Control::health`] 给出（它先读时刻），音频的状态随后从
/// 音频线程的状态读出，不持控制状态的锁。
fn health(control: &Control, audio: &AudioStatus) -> CoreHealthParams {
    let mut health = control.health();
    (health.audio, health.latest_audio) = audio.snapshot();
    health
}

/// 启动调度、发送、健康与两条生命线的线程，返回会在进入停止后结束、需要等待的线程。
fn start_workers(
    control: &Arc<Control>,
    media: &Arc<Media>,
    outbox: &Arc<Outbox>,
    peer: &Arc<RpcPeer>,
    options: &Options,
    ended: &Sender<Ended>,
    stdin: impl Read + Send + 'static,
) -> io::Result<Vec<JoinHandle<()>>> {
    // 发送线程在 `Outbox::close` 时结束；它阻塞在写上时随进程退出，不等待。
    thread::Builder::new().name("core-sender".into()).spawn({
        let outbox = Arc::clone(outbox);
        move || outbox.run_sender()
    })?;
    let scheduler = thread::Builder::new()
        .name("action-scheduler".into())
        .spawn({
            let (control, outbox) = (Arc::clone(control), Arc::clone(outbox));
            move || control.run_scheduler(|notices| outbox.post(notices))
        })?;
    let health = thread::Builder::new().name("core-health".into()).spawn({
        let (control, media, outbox) = (Arc::clone(control), Arc::clone(media), Arc::clone(outbox));
        let interval = options.health_interval;
        move || loop {
            outbox.notify::<CoreHealth>(&health(&control, &media.audio_status));
            if control.wait_stopping(interval) {
                return;
            }
        }
    })?;
    // 以下两个线程阻塞在读上，进程退出时随之结束，不等待。
    thread::Builder::new()
        .name("connection-lifeline".into())
        .spawn({
            let (peer, ended) = (Arc::clone(peer), ended.clone());
            move || {
                let reason = peer.wait_closed(None).flatten();
                let _ = ended.send(Ended::ConnectionClosed(reason));
            }
        })?;
    thread::Builder::new()
        .name("stdin-lifeline".into())
        .spawn({
            let ended = ended.clone();
            move || {
                read_to_eof(stdin);
                let _ = ended.send(Ended::StdinClosed);
            }
        })?;
    Ok(vec![scheduler, health])
}

/// 读到 EOF 或读取失败为止，丢弃读到的内容：stdin 只作生命线，不传数据。
fn read_to_eof(mut input: impl Read) {
    let mut buffer = [0u8; 256];
    loop {
        match input.read(&mut buffer) {
            Ok(0) => return,
            Ok(_) => {}
            Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
            Err(_) => return,
        }
    }
}
