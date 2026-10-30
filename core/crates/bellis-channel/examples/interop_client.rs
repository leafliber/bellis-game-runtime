//! Python 与 Rust 互通测试用的客户端，由 pytest 拉起（`runtime/tests/channel/test_interop.py`）。
//!
//! 环境变量 `BELLIS_CHANNEL_PATH` 与 `BELLIS_CHANNEL_KEY` 给出地址与 base64url 编码的会话密钥；
//! 第一个参数是脚本名：
//! - `session`：请求 `interop.add`；通知 `interop.hello`；应答服务端的 `interop.echo` 请求；
//!   收到服务端的 `interop.note` 通知后，把它带进 `interop.done` 请求，然后关闭；
//! - `handshake`：只握手，然后关闭。
//!
//! 每一步在标准输出打印一行；成功时最后一行为 `ok`，失败时打印 `error <错误码>` 并以 1 退出。

use std::process::ExitCode;
use std::sync::mpsc;
use std::time::Duration;

use bellis_channel::{
    CallError, ChannelCredentials, ConnectionOptions, Handlers, RpcOptions, RpcPeer, connect,
};
use serde_json::{Map, Value, json};

const ROLE: &str = "game-runtime.core";

fn object(value: &Value) -> Map<String, Value> {
    value.as_object().cloned().unwrap_or_default()
}

fn code(error: &CallError) -> &'static str {
    match error {
        CallError::Channel(error) => error.code().as_str(),
        CallError::Rpc(_) => "rpc-error",
    }
}

fn run(script: &str) -> Result<(), CallError> {
    let path = std::env::var("BELLIS_CHANNEL_PATH").unwrap_or_default();
    let key = std::env::var("BELLIS_CHANNEL_KEY").unwrap_or_default();
    let credentials = ChannelCredentials::decode(path, &key)?;
    let connection = connect(
        &credentials.path,
        ROLE,
        &credentials.key,
        &ConnectionOptions::default(),
    )?;
    println!("open {}", connection.role());
    if script == "handshake" {
        connection.close();
        return Ok(());
    }

    let (notes, received) = mpsc::channel();
    let handlers = Handlers::new()
        .request("interop.echo", |params| Ok(Value::Object(params.clone())))
        .notification("interop.note", move |params| {
            let _ = notes.send(Value::Object(params.clone()));
        });
    let rpc = RpcPeer::spawn(
        connection,
        handlers,
        &RpcOptions {
            timeout: Duration::from_secs(5),
        },
    )
    .map_err(|error| {
        CallError::Channel(bellis_channel::ChannelError::new(
            bellis_channel::ErrorCode::Closed,
            error.to_string(),
        ))
    })?;

    let sum = rpc.request("interop.add", Some(&object(&json!({"a": 2, "b": 3}))))?;
    println!("add {sum}");
    rpc.notify("interop.hello", Some(&object(&json!({"from": "rust"}))))?;
    let note = received
        .recv_timeout(Duration::from_secs(5))
        .unwrap_or(Value::Null);
    println!("note {note}");
    let done = rpc.request("interop.done", Some(&object(&json!({"note": note}))))?;
    println!("done {done}");
    rpc.close();
    Ok(())
}

fn main() -> ExitCode {
    let script = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "session".to_owned());
    match run(&script) {
        Ok(()) => {
            println!("ok");
            ExitCode::SUCCESS
        }
        Err(error) => {
            println!("error {}", code(&error));
            eprintln!("{error}");
            ExitCode::FAILURE
        }
    }
}
