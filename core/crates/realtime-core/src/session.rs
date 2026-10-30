//! 核连接上的一次会话：启动、握手、控制产出者的消息与生命线（`docs/modules/realtime-core.md` 的“启动与生命线”）。
//!
//! 线程：
//! - 调用 [`run`] 的线程：先释放全部输入，连接并握手，发出 `input.released(core_start)`，再启动其余线程，
//!   然后等会话结束；
//! - `RpcPeer` 的读线程：处理 `producer.heartbeat`、`producer.revoke` 与 `core.stop`，处理函数在读线程上同步
//!   执行，只动控制状态并发通知，很快返回，也不向主进程发请求；
//! - 心跳看门狗：到截止时刻即释放；
//! - 健康：启动时立即发一次 `core.health`，之后按周期发；
//! - 两条生命线：一个线程等核连接关闭，一个线程读 stdin 直到 EOF。
//!
//! 握手与 `core_start` 在启动读线程之前完成，所以读线程处理任何产出者消息之前，`core_start` 已经发出。

use std::fmt;
use std::io::{self, Read};
use std::sync::mpsc::{self, Sender};
use std::sync::{Arc, OnceLock, Weak};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use bellis_channel::{
    ChannelError, Connection, ConnectionOptions, Handlers, Message, RpcError, RpcErrorCode,
    RpcKind, RpcOptions, RpcPeer, classify, connect,
};
use core_protocol::PROTOCOL_VERSION;
use core_protocol::messages::{
    CoreHelloParams, CoreHelloResult, Empty, InputReleasedParams, ProducerHeartbeatParams,
    ProducerRevokeParams,
};
use core_protocol::methods::{
    CoreHealth, CoreHello, CoreStop, InputReleased, Method, Notification, ProducerHeartbeat,
    ProducerRevoke,
};
use serde_json::{Map, Value, json};

use crate::config::{HELLO_TIMEOUT, Launch, Options};
use crate::control::Control;

/// 实时核的程序版本，经 `core.hello` 报给主进程。
pub const CORE_VERSION: &str = env!("CARGO_PKG_VERSION");

/// 本端发出的请求的截止时间。实时核目前只在握手时发请求（`core.hello`，另有 [`HELLO_TIMEOUT`]）。
const RPC_TIMEOUT: Duration = Duration::from_secs(2);

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
    /// 连不上通道或握手失败。
    Connect(ChannelError),
    /// `core.hello` 没有得到合法的接受。
    Hello(String),
    /// 无法创建线程。
    Thread(io::Error),
}

impl fmt::Display for StartError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Connect(error) => write!(f, "cannot connect to the main process: {error}"),
            Self::Hello(reason) => write!(f, "core.hello failed: {reason}"),
            Self::Thread(error) => write!(f, "cannot start a thread: {error}"),
        }
    }
}

impl std::error::Error for StartError {}

/// 运行一次会话直到结束；返回前已释放全部输入。
///
/// `stdin` 是生命线：读到 EOF 即结束。
///
/// # Errors
///
/// 连不上主进程、握手失败或无法创建线程时返回 [`StartError`]；此时已释放全部输入。
pub fn run(
    options: &Options,
    launch: &Launch,
    stdin: impl Read + Send + 'static,
) -> Result<Ended, StartError> {
    let control = Arc::new(Control::new(options.heartbeat_timeout));
    // 每次启动先释放全部输入，再接受任何产出者与动作。
    let started = control.release_on_start();
    log!("released all inputs on start");

    let credentials = &launch.credentials;
    let connection = connect(
        &credentials.path,
        &launch.role,
        &credentials.key,
        &ConnectionOptions::default(),
    )
    .map_err(StartError::Connect)?;
    hello(&connection)?;
    connection
        .send(&notification::<InputReleased>(&started))
        .map_err(StartError::Connect)?;

    let outbox = Arc::new(Outbox::default());
    let (ended, end) = mpsc::channel();
    let peer = RpcPeer::spawn(
        connection,
        handlers(&control, &outbox, ended.clone()),
        &RpcOptions {
            timeout: RPC_TIMEOUT,
        },
    )
    .map_err(StartError::Thread)?;
    let peer = Arc::new(peer);
    outbox.attach(&peer);
    log!(
        "connected as {}; heartbeat timeout {}ms, health every {}ms",
        launch.role,
        options.heartbeat_timeout.as_millis(),
        options.health_interval.as_millis()
    );

    let workers = start_workers(&control, &outbox, &peer, options, &ended, stdin);
    drop(ended);
    let workers = match workers {
        Ok(workers) => workers,
        Err(error) => {
            control.release_on_exit();
            peer.close();
            return Err(StartError::Thread(error));
        }
    };

    // 所有发送端都在线程里；全部退出（不应发生）时按连接断开处理。
    let reason = end.recv().unwrap_or(Ended::ConnectionClosed(None));
    if !matches!(reason, Ended::Stopped) {
        // 生命线：先释放，再记录；连接已断开时无法通知主进程。
        control.release_on_exit();
    }
    log!("{reason}; released all inputs, exiting");
    for worker in workers {
        let _ = worker.join();
    }
    peer.close();
    Ok(reason)
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

/// 处理函数之外发通知的入口。读线程启动后才拿到对端；在那之前到达的处理函数等它就位，最多几微秒。
#[derive(Default)]
struct Outbox {
    peer: OnceLock<Weak<RpcPeer>>,
}

impl Outbox {
    fn attach(&self, peer: &Arc<RpcPeer>) {
        let _ = self.peer.set(Arc::downgrade(peer));
    }

    /// 发一条通知；连接已断开或写失败只记录。调用方须已完成释放。
    fn notify<M: Notification>(&self, params: &M::Params) {
        let Some(peer) = self.peer.wait().upgrade() else {
            return;
        };
        let Ok(Value::Object(params)) = serde_json::to_value(params) else {
            log!("{} not sent: params are not an object", M::NAME);
            return;
        };
        if let Err(error) = peer.notify(M::NAME, Some(&params)) {
            log!("{} not sent: {error}", M::NAME);
        }
    }

    fn released(&self, released: &InputReleasedParams) {
        log!(
            "released all inputs: {:?} (producer {:?})",
            released.reason,
            released.producer
        );
        self.notify::<InputReleased>(released);
    }
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

fn handlers(control: &Arc<Control>, outbox: &Arc<Outbox>, ended: Sender<Ended>) -> Handlers {
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
            match control.heartbeat(&heartbeat, Instant::now()) {
                Ok(Some(released)) => outbox.released(&released),
                Ok(None) => {}
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
            // 释放完成后才回应：回应即这些代次的输入已释放的确认。
            if let Some(released) = control.revoke(revoke.generation) {
                outbox.released(&released);
            }
            Ok(empty())
        }
    };
    let stop = {
        let (control, outbox) = (Arc::clone(control), Arc::clone(outbox));
        move |params: &Map<String, Value>| {
            let _: Empty = parse(params).map_err(|error| invalid_params(CoreStop::NAME, &error))?;
            outbox.released(&control.stop());
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
}

/// 启动看门狗、健康与两条生命线的线程，返回会在进入停止后结束、需要等待的线程。
fn start_workers(
    control: &Arc<Control>,
    outbox: &Arc<Outbox>,
    peer: &Arc<RpcPeer>,
    options: &Options,
    ended: &Sender<Ended>,
    stdin: impl Read + Send + 'static,
) -> io::Result<Vec<JoinHandle<()>>> {
    let watchdog = thread::Builder::new()
        .name("heartbeat-watchdog".into())
        .spawn({
            let (control, outbox) = (Arc::clone(control), Arc::clone(outbox));
            move || control.run_watchdog(|released| outbox.released(&released))
        })?;
    let health = thread::Builder::new().name("core-health".into()).spawn({
        let (control, outbox) = (Arc::clone(control), Arc::clone(outbox));
        let interval = options.health_interval;
        move || loop {
            outbox.notify::<CoreHealth>(&control.health());
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
    Ok(vec![watchdog, health])
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
