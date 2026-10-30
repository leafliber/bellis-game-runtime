//! 连接之上的最小 JSON-RPC 2.0 对端，对应 Bellis `packages/channel/src/rpc.ts`。
//!
//! 双方都可以请求和响应；参数只用按名传递的对象。一个读线程负责收消息：应答交给等待中的请求，
//! 请求与通知在读线程上同步调用处理函数。处理函数应当很快返回，且不能在里面调用本对端的
//! `request`（读线程被占着，应答永远收不到）。每个请求都有截止时间，超时后向对端发 `rpc.cancel`。
//! 处理是同步的，收到 `rpc.cancel` 时对应的处理已经结束，所以忽略它。
//!
//! 消息的分类与 Bellis 用的 `TypeBox` 模式等价（`packages/protocol/src/jsonrpc.ts`），顺序为请求、通知、
//! 成功应答、失败应答；都不是时回 InvalidRequest（id 为 null）。二进制帧不属于 RPC，忽略。
//! 外层信封按 Bellis 的键顺序编码；`params` 等内层对象的键序由 `serde_json` 决定（启用 `preserve_order`
//! 时为插入顺序，否则为字典序），对端解析后相同。

use std::collections::HashMap;
use std::fmt::{self, Write as _};
use std::io;
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::sync::{Arc, Condvar, Mutex, PoisonError};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use serde_json::{Map, Value};

use crate::connection::{Connection, Message};
use crate::error::{ChannelError, ErrorCode};
use crate::json;

pub const RPC_CANCEL: &str = "rpc.cancel";

/// JSON-RPC 2.0 预定义错误码，以及 Bellis 在保留区间内定义的错误码。
pub struct RpcErrorCode;

impl RpcErrorCode {
    pub const PARSE_ERROR: i64 = -32700;
    pub const INVALID_REQUEST: i64 = -32600;
    pub const METHOD_NOT_FOUND: i64 = -32601;
    pub const INVALID_PARAMS: i64 = -32602;
    pub const INTERNAL_ERROR: i64 = -32603;
    /// 正在处理的请求已达上限。
    pub const SERVER_BUSY: i64 = -32000;
}

/// 对端返回的 JSON-RPC 错误；处理函数返回它即可指定错误码。
#[derive(Clone, Debug, PartialEq)]
pub struct RpcError {
    pub code: i64,
    pub message: String,
    pub data: Option<Value>,
}

impl RpcError {
    pub fn new(code: i64, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
            data: None,
        }
    }

    #[must_use]
    pub fn with_data(mut self, data: Value) -> Self {
        self.data = Some(data);
        self
    }
}

impl fmt::Display for RpcError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "RPC 错误 {}: {}", self.code, self.message)
    }
}

impl std::error::Error for RpcError {}

/// 请求失败：通道错误（超时、关闭等），或对端返回的 JSON-RPC 错误。
#[derive(Clone, Debug, PartialEq)]
pub enum CallError {
    Channel(ChannelError),
    Rpc(RpcError),
}

impl fmt::Display for CallError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Channel(error) => error.fmt(f),
            Self::Rpc(error) => error.fmt(f),
        }
    }
}

impl std::error::Error for CallError {}

impl From<ChannelError> for CallError {
    fn from(error: ChannelError) -> Self {
        Self::Channel(error)
    }
}

// ---------------------------------------------------------------------------
// 消息校验：与 `TypeBox` 的检查等价。JSON 的整数值浮点（如 1.0）在 JS 中就是整数。
// ---------------------------------------------------------------------------

/// 规范化后的请求 id。
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum RpcId {
    Int(i64),
    Str(String),
}

impl RpcId {
    fn to_json(&self) -> String {
        match self {
            Self::Int(id) => id.to_string(),
            Self::Str(id) => json::string_literal(id),
        }
    }
}

const MAX_SAFE_INTEGER: i64 = (1 << 53) - 1;

fn integer(value: &Value) -> Option<f64> {
    let number = value.as_number()?;
    if let Some(n) = number.as_i64() {
        #[allow(clippy::cast_precision_loss)] // 只用于判断范围
        return Some(n as f64);
    }
    if let Some(n) = number.as_u64() {
        #[allow(clippy::cast_precision_loss)]
        return Some(n as f64);
    }
    number
        .as_f64()
        .filter(|n| n.is_finite() && n.fract() == 0.0)
}

fn rpc_id(value: &Value) -> Option<RpcId> {
    if let Some(text) = value.as_str() {
        let count = text.chars().count();
        return (1..=128)
            .contains(&count)
            .then(|| RpcId::Str(text.to_owned()));
    }
    // 先按 i64 判断，避免安全整数边界附近的浮点舍入。
    if let Some(n) = value.as_i64() {
        return (-MAX_SAFE_INTEGER..=MAX_SAFE_INTEGER)
            .contains(&n)
            .then_some(RpcId::Int(n));
    }
    if value.is_u64() {
        return None; // 大于 i64::MAX，必然超出安全整数范围
    }
    let n = integer(value)?;
    #[allow(clippy::cast_precision_loss)]
    let limit = MAX_SAFE_INTEGER as f64;
    #[allow(clippy::cast_possible_truncation)] // 已检查是范围内的整数
    (-limit..=limit)
        .contains(&n)
        .then_some(RpcId::Int(n as i64))
}

fn is_method(value: &Value) -> bool {
    let Some(method) = value.as_str() else {
        return false;
    };
    let mut segments = 0;
    for segment in method.split('.') {
        let bytes = segment.as_bytes();
        let ok = !bytes.is_empty()
            && bytes[0].is_ascii_lowercase()
            && bytes[1..]
                .iter()
                .all(|&c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'_');
        if !ok {
            return false;
        }
        segments += 1;
    }
    segments >= 2
}

fn fields<'a>(
    message: &'a Value,
    required: &[&str],
    optional: &[&str],
) -> Option<&'a Map<String, Value>> {
    let fields = message.as_object()?;
    let complete = required.iter().all(|key| fields.contains_key(*key));
    let known = fields
        .keys()
        .all(|key| required.contains(&key.as_str()) || optional.contains(&key.as_str()));
    (complete && known && fields["jsonrpc"] == "2.0").then_some(fields)
}

fn params_ok(fields: &Map<String, Value>) -> bool {
    fields.get("params").is_none_or(Value::is_object)
}

fn error_object(value: &Value) -> Option<(i64, &str, Option<&Value>)> {
    let fields = value.as_object()?;
    let known = fields
        .keys()
        .all(|key| matches!(key.as_str(), "code" | "message" | "data"));
    let code = integer(fields.get("code")?)?;
    let message = fields.get("message")?.as_str()?;
    #[allow(clippy::cast_possible_truncation)] // 超出 i64 的错误码饱和截断
    known.then_some((code as i64, message, fields.get("data")))
}

/// 消息的类别。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RpcKind {
    Request,
    Notification,
    Success,
    Failure,
    Invalid,
}

/// 按 Bellis 的 `RpcPeer` 的顺序判断消息类别。
#[must_use]
pub fn classify(message: &Value) -> RpcKind {
    if fields(message, &["jsonrpc", "id", "method"], &["params"])
        .is_some_and(|f| rpc_id(&f["id"]).is_some() && is_method(&f["method"]) && params_ok(f))
    {
        RpcKind::Request
    } else if fields(message, &["jsonrpc", "method"], &["params"])
        .is_some_and(|f| is_method(&f["method"]) && params_ok(f))
    {
        RpcKind::Notification
    } else if fields(message, &["jsonrpc", "id", "result"], &[])
        .is_some_and(|f| rpc_id(&f["id"]).is_some())
    {
        RpcKind::Success
    } else if fields(message, &["jsonrpc", "id", "error"], &[]).is_some_and(|f| {
        (f["id"].is_null() || rpc_id(&f["id"]).is_some()) && error_object(&f["error"]).is_some()
    }) {
        RpcKind::Failure
    } else {
        RpcKind::Invalid
    }
}

/// `rpc.cancel` 的参数是否恰为 `{"id": <合法 id>}`。
#[must_use]
pub fn is_cancel_params(params: &Value) -> bool {
    params
        .as_object()
        .is_some_and(|f| f.len() == 1 && f.get("id").and_then(rpc_id).is_some())
}

// ---------------------------------------------------------------------------
// 信封编码：外层按 Bellis 的键顺序。
// ---------------------------------------------------------------------------

fn with_params(mut head: String, params: Option<&Map<String, Value>>) -> Vec<u8> {
    if let Some(params) = params {
        head.push_str(r#","params":"#);
        head.push_str(&Value::Object(params.clone()).to_string());
    }
    head.push('}');
    head.into_bytes()
}

fn request_envelope(id: i64, method: &str, params: Option<&Map<String, Value>>) -> Vec<u8> {
    let head = format!(
        r#"{{"jsonrpc":"2.0","id":{id},"method":{}"#,
        json::string_literal(method)
    );
    with_params(head, params)
}

fn notification_envelope(method: &str, params: Option<&Map<String, Value>>) -> Vec<u8> {
    let head = format!(
        r#"{{"jsonrpc":"2.0","method":{}"#,
        json::string_literal(method)
    );
    with_params(head, params)
}

fn success_envelope(id: &RpcId, result: &Value) -> Vec<u8> {
    format!(
        r#"{{"jsonrpc":"2.0","id":{},"result":{result}}}"#,
        id.to_json()
    )
    .into_bytes()
}

fn failure_envelope(id: Option<&RpcId>, error: &RpcError) -> Vec<u8> {
    let id = id.map_or_else(|| "null".to_owned(), RpcId::to_json);
    let mut body = format!(
        r#"{{"code":{},"message":{}"#,
        error.code,
        json::string_literal(&error.message)
    );
    if let Some(data) = &error.data {
        let _ = write!(body, r#","data":{data}"#);
    }
    format!(r#"{{"jsonrpc":"2.0","id":{id},"error":{body}}}}}"#).into_bytes()
}

// ---------------------------------------------------------------------------
// 对端
// ---------------------------------------------------------------------------

type RequestHandler = Box<dyn Fn(&Map<String, Value>) -> Result<Value, RpcError> + Send + Sync>;
type NotificationHandler = Box<dyn Fn(&Map<String, Value>) + Send + Sync>;

/// 处理函数表，在启动对端前登记。
#[derive(Default)]
pub struct Handlers {
    requests: HashMap<String, RequestHandler>,
    notifications: HashMap<String, NotificationHandler>,
}

fn check_method(method: &str, taken: bool) {
    assert!(!method.starts_with("rpc."), "rpc. 前缀保留给协议自身");
    assert!(!taken, "方法 {method} 已有处理函数");
}

impl Handlers {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// 登记请求处理函数。
    ///
    /// # Panics
    ///
    /// 方法名以 `rpc.` 开头或已登记时 panic。
    #[must_use]
    pub fn request(
        mut self,
        method: &str,
        handler: impl Fn(&Map<String, Value>) -> Result<Value, RpcError> + Send + Sync + 'static,
    ) -> Self {
        check_method(method, self.requests.contains_key(method));
        self.requests.insert(method.to_owned(), Box::new(handler));
        self
    }

    /// 登记通知处理函数。
    ///
    /// # Panics
    ///
    /// 方法名以 `rpc.` 开头或已登记时 panic。
    #[must_use]
    pub fn notification(
        mut self,
        method: &str,
        handler: impl Fn(&Map<String, Value>) + Send + Sync + 'static,
    ) -> Self {
        check_method(method, self.notifications.contains_key(method));
        self.notifications
            .insert(method.to_owned(), Box::new(handler));
        self
    }
}

#[derive(Clone, Debug)]
pub struct RpcOptions {
    /// 请求的默认截止时间。
    pub timeout: Duration,
}

type Reply = Result<Value, CallError>;

enum State {
    Open,
    /// 连接已关闭；正常关闭时为 None。
    Closed(Option<ChannelError>),
}

struct Shared {
    connection: Connection,
    timeout: Duration,
    next_id: AtomicI64,
    pending: Mutex<HashMap<i64, mpsc::Sender<Reply>>>,
    state: Mutex<State>,
    closed: Condvar,
}

/// 连接之上的 JSON-RPC 2.0 对端。析构时关闭连接并等读线程结束。
pub struct RpcPeer {
    shared: Arc<Shared>,
    reader: Mutex<Option<JoinHandle<()>>>,
}

impl RpcPeer {
    /// 启动读线程。
    ///
    /// # Errors
    ///
    /// 无法创建线程时返回 IO 错误。
    pub fn spawn(
        connection: Connection,
        handlers: Handlers,
        options: &RpcOptions,
    ) -> io::Result<Self> {
        let shared = Arc::new(Shared {
            connection,
            timeout: options.timeout,
            next_id: AtomicI64::new(1),
            pending: Mutex::new(HashMap::new()),
            state: Mutex::new(State::Open),
            closed: Condvar::new(),
        });
        let reader = thread::Builder::new()
            .name("bellis-channel-rpc".to_owned())
            .spawn({
                let shared = Arc::clone(&shared);
                move || shared.read_loop(&handlers)
            })?;
        Ok(Self {
            shared,
            reader: Mutex::new(Some(reader)),
        })
    }

    #[must_use]
    pub fn connection(&self) -> &Connection {
        &self.shared.connection
    }

    /// 发出请求并阻塞等待应答，截止时间为 `RpcOptions::timeout`。
    ///
    /// # Errors
    ///
    /// 超过截止时间为 `timeout`（并向对端发 `rpc.cancel`）；连接关闭为 `closed`；
    /// 对端返回错误时为 `CallError::Rpc`。
    pub fn request(
        &self,
        method: &str,
        params: Option<&Map<String, Value>>,
    ) -> Result<Value, CallError> {
        self.request_with_timeout(method, params, self.shared.timeout)
    }

    /// 同 `request`，但截止时间由调用方给出。
    ///
    /// # Errors
    ///
    /// 同 `request`。
    pub fn request_with_timeout(
        &self,
        method: &str,
        params: Option<&Map<String, Value>>,
        timeout: Duration,
    ) -> Result<Value, CallError> {
        let shared = &self.shared;
        let id = shared.next_id.fetch_add(1, Ordering::Relaxed);
        let (sender, receiver) = mpsc::channel();
        shared.pending().insert(id, sender);
        // 先登记再看状态：关闭时先改状态再清空登记，二者必有其一看到对方。
        if !matches!(*shared.state(), State::Open) {
            shared.pending().remove(&id);
            return Err(closed_before_reply().into());
        }
        if let Err(error) = shared
            .connection
            .send_json_bytes(&request_envelope(id, method, params))
        {
            shared.pending().remove(&id);
            return Err(error.into());
        }
        match receiver.recv_timeout(timeout) {
            Ok(reply) => reply,
            Err(RecvTimeoutError::Timeout) => {
                shared.pending().remove(&id);
                if shared.connection.is_open() {
                    let mut cancel = Map::new();
                    cancel.insert("id".to_owned(), Value::from(id));
                    let _ = shared
                        .connection
                        .send_json_bytes(&notification_envelope(RPC_CANCEL, Some(&cancel)));
                }
                Err(ChannelError::new(
                    ErrorCode::Timeout,
                    format!("{method} 未在 {}ms 内响应", timeout.as_millis()),
                )
                .into())
            }
            Err(RecvTimeoutError::Disconnected) => Err(closed_before_reply().into()),
        }
    }

    /// 发出通知。
    ///
    /// # Errors
    ///
    /// 超出帧长上限或连接已关闭时返回错误。
    pub fn notify(
        &self,
        method: &str,
        params: Option<&Map<String, Value>>,
    ) -> Result<(), ChannelError> {
        self.shared
            .connection
            .send_json_bytes(&notification_envelope(method, params))
    }

    /// 连接是否已关闭（读线程已结束或即将结束）。
    #[must_use]
    pub fn is_closed(&self) -> bool {
        !matches!(*self.shared.state(), State::Open)
    }

    /// 等连接关闭，最多等 `timeout`。已关闭时返回 `Some(原因)`，正常关闭的原因为 None。
    #[must_use]
    pub fn wait_closed(&self, timeout: Option<Duration>) -> Option<Option<ChannelError>> {
        let deadline = timeout.map(|timeout| Instant::now() + timeout);
        let mut state = self.shared.state();
        loop {
            if let State::Closed(reason) = &*state {
                return Some(reason.clone());
            }
            match deadline {
                None => {
                    state = self
                        .shared
                        .closed
                        .wait(state)
                        .unwrap_or_else(PoisonError::into_inner);
                }
                Some(deadline) => {
                    let left = deadline.saturating_duration_since(Instant::now());
                    if left.is_zero() {
                        return None;
                    }
                    state = self
                        .shared
                        .closed
                        .wait_timeout(state, left)
                        .unwrap_or_else(PoisonError::into_inner)
                        .0;
                }
            }
        }
    }

    /// 关闭连接并等读线程结束（在读线程里调用时不等）。
    pub fn close(&self) {
        self.shared.connection.close();
        let reader = self
            .reader
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take();
        if let Some(reader) = reader
            && reader.thread().id() != thread::current().id()
        {
            let _ = reader.join();
        }
    }
}

impl Drop for RpcPeer {
    fn drop(&mut self) {
        self.close();
    }
}

fn closed_before_reply() -> ChannelError {
    ChannelError::new(ErrorCode::Closed, "连接已关闭，请求未完成")
}

/// 读线程退出时（含处理函数 panic）把对端标记为关闭。
struct CloseOnExit<'a> {
    shared: &'a Shared,
    reason: Option<ChannelError>,
}

impl Drop for CloseOnExit<'_> {
    fn drop(&mut self) {
        self.shared.on_close(self.reason.take());
    }
}

impl Shared {
    fn pending(&self) -> std::sync::MutexGuard<'_, HashMap<i64, mpsc::Sender<Reply>>> {
        self.pending.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn state(&self) -> std::sync::MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn read_loop(&self, handlers: &Handlers) {
        let mut exit = CloseOnExit {
            shared: self,
            reason: None,
        };
        loop {
            match self.connection.recv(None) {
                Ok(Message::Json(message)) => self.receive(&message, handlers),
                Ok(Message::Binary(_)) => {}
                Err(error) => {
                    exit.reason = (error.code() != ErrorCode::Closed).then_some(error);
                    return;
                }
            }
        }
    }

    fn receive(&self, message: &Value, handlers: &Handlers) {
        match classify(message) {
            RpcKind::Invalid => self.reply(&failure_envelope(
                None,
                &RpcError::new(RpcErrorCode::INVALID_REQUEST, "Invalid Request"),
            )),
            RpcKind::Request => self.serve(message, handlers),
            RpcKind::Notification => {
                let method = message["method"].as_str().unwrap_or_default();
                if method == RPC_CANCEL {
                    return;
                }
                if let Some(handler) = handlers.notifications.get(method) {
                    handler(&params(message));
                }
            }
            RpcKind::Success => self.settle(&message["id"], Ok(message["result"].clone())),
            RpcKind::Failure => {
                if let Some((code, text, data)) = error_object(&message["error"]) {
                    let error = RpcError {
                        code,
                        message: text.to_owned(),
                        data: data.cloned(),
                    };
                    self.settle(&message["id"], Err(CallError::Rpc(error)));
                }
            }
        }
    }

    fn settle(&self, id: &Value, reply: Reply) {
        // 本端的请求 id 都是整数；字符串 id 与 null 不会对上任何请求。
        if let Some(RpcId::Int(id)) = rpc_id(id)
            && let Some(sender) = self.pending().remove(&id)
        {
            let _ = sender.send(reply);
        }
    }

    fn serve(&self, request: &Value, handlers: &Handlers) {
        let Some(id) = rpc_id(&request["id"]) else {
            return;
        };
        let method = request["method"].as_str().unwrap_or_default();
        let Some(handler) = handlers.requests.get(method) else {
            self.reply(&failure_envelope(
                Some(&id),
                &RpcError::new(
                    RpcErrorCode::METHOD_NOT_FOUND,
                    format!("Method not found: {method}"),
                ),
            ));
            return;
        };
        let internal = || RpcError::new(RpcErrorCode::INTERNAL_ERROR, "Internal error");
        match handler(&params(request)) {
            Ok(result) => {
                // 结果超过帧长上限时与 Bellis 一样改回 InternalError。
                if let Err(error) = self
                    .connection
                    .send_json_bytes(&success_envelope(&id, &result))
                    && error.code() == ErrorCode::FrameTooLarge
                {
                    self.reply(&failure_envelope(Some(&id), &internal()));
                }
            }
            Err(error) => self.reply(&failure_envelope(Some(&id), &error)),
        }
    }

    fn reply(&self, envelope: &[u8]) {
        if self.connection.is_open() {
            let _ = self.connection.send_json_bytes(envelope);
        }
    }

    fn on_close(&self, reason: Option<ChannelError>) {
        self.connection.close();
        {
            let mut state = self.state();
            if matches!(*state, State::Open) {
                *state = State::Closed(reason);
            }
        }
        self.closed.notify_all();
        let pending: Vec<_> = self.pending().drain().map(|(_, sender)| sender).collect();
        for sender in pending {
            let _ = sender.send(Err(closed_before_reply().into()));
        }
    }
}

fn params(message: &Value) -> Map<String, Value> {
    message
        .get("params")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default()
}
