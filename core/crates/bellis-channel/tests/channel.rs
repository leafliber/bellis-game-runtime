//! 真实传输上的客户端测试：Windows 上是命名管道，其它平台是 UDS。
//! 服务端按脚本收发原始帧，覆盖正常往来与各种拒绝。

mod common;

use std::sync::mpsc;
use std::time::{Duration, Instant};

use bellis_channel::{
    CallError, ConnectionOptions, ErrorCode, FrameKind, Handlers, Message, PROOF_DOMAIN,
    RPC_CANCEL, RpcError, RpcOptions, RpcPeer, connect, create_session_key, encode_session_key,
    mac,
};
use common::{ROLE, ServerStream, serve, unused_path};
use serde_json::{Map, Value, json};

fn options() -> ConnectionOptions {
    ConnectionOptions {
        handshake_timeout: Duration::from_secs(2),
        ..ConnectionOptions::default()
    }
}

fn rpc_options() -> RpcOptions {
    RpcOptions {
        timeout: Duration::from_secs(2),
    }
}

fn params(value: &Value) -> Map<String, Value> {
    value.as_object().expect("对象").clone()
}

fn code_of<T>(result: Result<T, bellis_channel::ChannelError>) -> ErrorCode {
    match result {
        Ok(_) => panic!("期望失败"),
        Err(error) => error.code(),
    }
}

#[cfg(windows)]
#[test]
fn windows_uses_named_pipes() {
    assert!(unused_path().starts_with(r"\\.\pipe\bellis-"));
}

#[test]
fn authenticated_rpc_in_both_directions() {
    let key = create_session_key();
    let (path, server) = serve(move |mut s: ServerStream| {
        assert_eq!(s.handshake(&key), ROLE);
        // 客户端的请求
        let request = s.read_json();
        assert_eq!(request["method"], "math.add");
        let sum =
            request["params"]["a"].as_i64().unwrap() + request["params"]["b"].as_i64().unwrap();
        s.write_json(
            FrameKind::Json,
            &json!({"jsonrpc": "2.0", "id": request["id"], "result": sum}),
        );
        // 服务端的请求与通知
        s.write_json(FrameKind::Json, &json!({"jsonrpc": "2.0", "id": "srv-1", "method": "core.echo", "params": {"x": [1, 2]}}));
        let reply = s.read_json();
        s.write_json(
            FrameKind::Json,
            &json!({"jsonrpc": "2.0", "method": "core.note", "params": {"seq": 7}}),
        );
        s.write_frame(FrameKind::Binary, &[1, 2, 3]);
        let notification = s.read_json();
        (reply, notification)
    });
    let connection = connect(&path, ROLE, &key, &options()).expect("握手成功");
    assert_eq!(connection.role(), ROLE);
    let (notes, received) = mpsc::channel();
    let handlers = Handlers::new()
        .request("core.echo", |params| Ok(Value::Object(params.clone())))
        .notification("core.note", move |params| {
            let _ = notes.send(Value::Object(params.clone()));
        });
    let rpc = RpcPeer::spawn(connection, handlers, &rpc_options()).expect("读线程");
    let sum = rpc
        .request("math.add", Some(&params(&json!({"a": 2, "b": 3}))))
        .expect("应答");
    assert_eq!(sum, json!(5));
    assert_eq!(
        received.recv_timeout(Duration::from_secs(2)).unwrap(),
        json!({"seq": 7})
    );
    rpc.notify("core.done", Some(&params(&json!({"ok": true}))))
        .expect("通知");
    let (reply, notification) = server.join().unwrap();
    assert_eq!(
        reply,
        json!({"jsonrpc": "2.0", "id": "srv-1", "result": {"x": [1, 2]}})
    );
    assert_eq!(
        notification,
        json!({"jsonrpc": "2.0", "method": "core.done", "params": {"ok": true}})
    );
}

#[test]
fn server_without_the_key_is_rejected() {
    let key = create_session_key();
    let other = create_session_key();
    let (path, server) = serve(move |mut s: ServerStream| {
        s.challenge(&other, |_| {});
        s.frames_until_closed()
    });
    assert_eq!(
        code_of(connect(&path, ROLE, &key, &options())),
        ErrorCode::AuthFailed
    );
    // 核验失败后客户端不再发送任何东西（不回送证明）。
    assert_eq!(server.join().unwrap(), 0);
}

#[test]
fn tampered_server_proof_is_rejected() {
    let key = create_session_key();
    let (path, server) = serve(move |mut s: ServerStream| {
        s.challenge(&key, |challenge| {
            let proof = challenge["proof"].as_str().unwrap();
            let flipped = if proof.starts_with('A') { "B" } else { "A" };
            challenge["proof"] = Value::from(format!("{flipped}{}", &proof[1..]));
        });
        s.frames_until_closed()
    });
    assert_eq!(
        code_of(connect(&path, ROLE, &key, &options())),
        ErrorCode::AuthFailed
    );
    assert_eq!(server.join().unwrap(), 0);
}

#[test]
fn replayed_challenge_is_rejected() {
    let key = create_session_key();
    // 第一次连接正常完成，录下服务端的 challenge。
    let (path, server) = serve(move |mut s: ServerStream| {
        let hello = s.read_json();
        let client_nonce = hello["nonce"].as_str().unwrap().to_owned();
        let server_nonce = encode_session_key(&create_session_key());
        let proof = mac(
            &key,
            PROOF_DOMAIN,
            "server",
            ROLE,
            &client_nonce,
            &server_nonce,
        );
        let challenge = json!({"v": 1, "type": "challenge", "nonce": server_nonce, "proof": encode_session_key(&proof)});
        s.write_json(FrameKind::Handshake, &challenge);
        let _ = s.read_json();
        s.write_json(FrameKind::Handshake, &json!({"v": 1, "type": "accept"}));
        s.frames_until_closed();
        challenge
    });
    let first = connect(&path, ROLE, &key, &options()).expect("第一次握手成功");
    first.close();
    let challenge = server.join().unwrap();

    // 第二次连接：把录下的 challenge 原样发回。客户端的随机数已变，证明对不上。
    let (path, server) = serve(move |mut s: ServerStream| {
        let _ = s.read_json();
        s.write_json(FrameKind::Handshake, &challenge);
        s.frames_until_closed()
    });
    assert_eq!(
        code_of(connect(&path, ROLE, &key, &options())),
        ErrorCode::AuthFailed
    );
    assert_eq!(server.join().unwrap(), 0);
}

#[test]
fn data_before_authentication_is_rejected() {
    let key = create_session_key();
    let (path, server) = serve(|mut s: ServerStream| {
        let _ = s.read_json();
        s.write_json(FrameKind::Json, &json!({"jsonrpc": "2.0", "method": "a.b"}));
        s.frames_until_closed()
    });
    assert_eq!(
        code_of(connect(&path, ROLE, &key, &options())),
        ErrorCode::ProtocolError
    );
    assert_eq!(server.join().unwrap(), 0);
}

#[test]
fn oversize_frame_before_authentication_is_rejected() {
    let key = create_session_key();
    let (path, server) = serve(|mut s: ServerStream| {
        let _ = s.read_json();
        // 只发帧头：只凭帧头就能判定超长。
        let mut header = 1025u32.to_be_bytes().to_vec();
        header.push(0);
        s.write_raw(&header);
        s.frames_until_closed()
    });
    assert_eq!(
        code_of(connect(&path, ROLE, &key, &options())),
        ErrorCode::FrameTooLarge
    );
    assert_eq!(server.join().unwrap(), 0);
}

#[test]
fn oversize_frame_after_authentication_is_rejected() {
    let key = create_session_key();
    let (path, server) = serve(move |mut s: ServerStream| {
        s.handshake(&key);
        let at_limit = format!(r#"{{"pad":"{}"}}"#, "x".repeat(64 - 10));
        s.write_frame(FrameKind::Json, at_limit.as_bytes());
        s.write_frame(FrameKind::Json, format!("{at_limit} ").as_bytes());
        s.frames_until_closed()
    });
    let small = ConnectionOptions {
        max_frame_bytes: 64,
        ..options()
    };
    let connection = connect(&path, ROLE, &key, &small).expect("握手成功");
    // 发送方超长：直接报错，连接不受影响。
    let too_long = json!({"text": "x".repeat(100)});
    assert_eq!(
        code_of(connection.send(&too_long)),
        ErrorCode::FrameTooLarge
    );
    assert!(connection.is_open());
    // 接收方：等于上限的送达，超过上限的断开。
    match connection
        .recv(Some(Duration::from_secs(2)))
        .expect("等于上限")
    {
        Message::Json(value) => assert_eq!(value["pad"].as_str().unwrap().len(), 54),
        Message::Binary(_) => panic!("意外的二进制帧"),
    }
    assert_eq!(
        code_of(connection.recv(Some(Duration::from_secs(2)))),
        ErrorCode::FrameTooLarge
    );
    assert!(!connection.is_open());
    assert_eq!(server.join().unwrap(), 0);
}

#[test]
fn handshake_timeout() {
    let key = create_session_key();
    let (path, server) = serve(|mut s: ServerStream| {
        let _ = s.read_json(); // 收到 hello 后一言不发
        s.frames_until_closed()
    });
    let quick = ConnectionOptions {
        handshake_timeout: Duration::from_millis(200),
        ..options()
    };
    let started = Instant::now();
    assert_eq!(
        code_of(connect(&path, ROLE, &key, &quick)),
        ErrorCode::HandshakeTimeout
    );
    let elapsed = started.elapsed();
    assert!(elapsed >= Duration::from_millis(200), "{elapsed:?}");
    assert!(elapsed < Duration::from_secs(2), "{elapsed:?}");
    assert_eq!(server.join().unwrap(), 0);
}

#[test]
fn read_timeout_keeps_the_connection_usable() {
    let key = create_session_key();
    let (go, wait) = mpsc::channel::<()>();
    let (path, server) = serve(move |mut s: ServerStream| {
        s.handshake(&key);
        wait.recv().unwrap();
        let frame = bellis_channel::encode_frame(FrameKind::Json, br#"{"n":1}"#);
        s.write_raw(&frame[..7]); // 半帧
        wait.recv().unwrap();
        s.write_raw(&frame[7..]);
        s.frames_until_closed()
    });
    let connection = connect(&path, ROLE, &key, &options()).expect("握手成功");
    let started = Instant::now();
    assert_eq!(
        code_of(connection.recv(Some(Duration::from_millis(100)))),
        ErrorCode::Timeout
    );
    assert!(started.elapsed() >= Duration::from_millis(100));
    go.send(()).unwrap();
    assert_eq!(
        code_of(connection.recv(Some(Duration::from_millis(100)))),
        ErrorCode::Timeout
    );
    go.send(()).unwrap();
    assert_eq!(
        connection
            .recv(Some(Duration::from_secs(2)))
            .expect("读到完整的帧"),
        Message::Json(json!({"n": 1}))
    );
    connection.close();
    assert_eq!(server.join().unwrap(), 0);
}

#[test]
fn version_mismatch_is_rejected() {
    let key = create_session_key();
    let (path, server) = serve(move |mut s: ServerStream| {
        s.challenge(&key, |challenge| challenge["v"] = json!(2));
        s.frames_until_closed()
    });
    assert_eq!(
        code_of(connect(&path, ROLE, &key, &options())),
        ErrorCode::ProtocolError
    );
    assert_eq!(server.join().unwrap(), 0);
}

#[test]
fn wrong_protocol_domain_is_rejected() {
    let key = create_session_key();
    let (path, server) = serve(move |mut s: ServerStream| {
        let hello = s.read_json();
        let client_nonce = hello["nonce"].as_str().unwrap().to_owned();
        let server_nonce = encode_session_key(&create_session_key());
        let proof = mac(
            &key,
            "bellis-channel/2",
            "server",
            ROLE,
            &client_nonce,
            &server_nonce,
        );
        s.write_json(
            FrameKind::Handshake,
            &json!({"v": 1, "type": "challenge", "nonce": server_nonce, "proof": encode_session_key(&proof)}),
        );
        s.frames_until_closed()
    });
    assert_eq!(
        code_of(connect(&path, ROLE, &key, &options())),
        ErrorCode::AuthFailed
    );
    assert_eq!(server.join().unwrap(), 0);
}

#[test]
fn connect_failed_without_server() {
    let key = create_session_key();
    let quick = ConnectionOptions {
        handshake_timeout: Duration::from_millis(200),
        ..options()
    };
    assert_eq!(
        code_of(connect(&unused_path(), ROLE, &key, &quick)),
        ErrorCode::ConnectFailed
    );
}

#[test]
fn invalid_role_is_rejected_before_connecting() {
    // 地址上没有服务端：报 protocol-error 而不是 connect-failed，说明根本没有去连。
    let key = create_session_key();
    assert_eq!(
        code_of(connect(&unused_path(), "Bad Role", &key, &options())),
        ErrorCode::ProtocolError
    );
}

#[test]
fn request_timeout_sends_cancel() {
    let key = create_session_key();
    let (path, server) = serve(move |mut s: ServerStream| {
        s.handshake(&key);
        let request = s.read_json();
        let cancel = s.read_json();
        s.frames_until_closed();
        (request, cancel)
    });
    let connection = connect(&path, ROLE, &key, &options()).expect("握手成功");
    let rpc = RpcPeer::spawn(connection, Handlers::new(), &rpc_options()).expect("读线程");
    let result = rpc.request_with_timeout("a.hang", None, Duration::from_millis(50));
    match result {
        Err(CallError::Channel(error)) => assert_eq!(error.code(), ErrorCode::Timeout),
        other => panic!("期望超时，得到 {other:?}"),
    }
    rpc.close();
    let (request, cancel) = server.join().unwrap();
    assert_eq!(cancel["method"], RPC_CANCEL);
    assert_eq!(cancel["params"]["id"], request["id"]);
}

#[test]
fn peer_errors_and_unknown_methods() {
    let key = create_session_key();
    let (done, finished) = mpsc::channel::<()>();
    let (path, server) = serve(move |mut s: ServerStream| {
        s.handshake(&key);
        let request = s.read_json();
        s.write_json(
            FrameKind::Json,
            &json!({"jsonrpc": "2.0", "id": request["id"], "error": {"code": 1001, "message": "不允许", "data": {"reason": "x"}}}),
        );
        s.write_json(
            FrameKind::Json,
            &json!({"jsonrpc": "2.0", "id": 9, "method": "no.such"}),
        );
        let not_found = s.read_json();
        s.write_json(
            FrameKind::Json,
            &json!({"jsonrpc": "2.0", "method": "Bad Name", "id": 1}),
        );
        let invalid = s.read_json();
        s.write_json(
            FrameKind::Json,
            &json!({"jsonrpc": "2.0", "id": 10, "method": "core.fail"}),
        );
        let failed = s.read_json();
        done.send(()).unwrap();
        s.frames_until_closed();
        (not_found, invalid, failed)
    });
    let connection = connect(&path, ROLE, &key, &options()).expect("握手成功");
    let handlers = Handlers::new().request("core.fail", |_| {
        Err(RpcError::new(7, "不行").with_data(json!([1])))
    });
    let rpc = RpcPeer::spawn(connection, handlers, &rpc_options()).expect("读线程");
    match rpc.request("a.fail", None) {
        Err(CallError::Rpc(error)) => {
            assert_eq!((error.code, error.message.as_str()), (1001, "不允许"));
            assert_eq!(error.data, Some(json!({"reason": "x"})));
        }
        other => panic!("期望 RPC 错误，得到 {other:?}"),
    }
    // 等服务端把后面三条应答都收完再关闭。
    finished
        .recv_timeout(Duration::from_secs(2))
        .expect("服务端收齐应答");
    rpc.close();
    let (not_found, invalid, failed) = server.join().unwrap();
    assert_eq!(
        not_found,
        json!({"jsonrpc": "2.0", "id": 9, "error": {"code": -32601, "message": "Method not found: no.such"}})
    );
    assert_eq!(
        invalid,
        json!({"jsonrpc": "2.0", "id": null, "error": {"code": -32600, "message": "Invalid Request"}})
    );
    assert_eq!(
        failed,
        json!({"jsonrpc": "2.0", "id": 10, "error": {"code": 7, "message": "不行", "data": [1]}})
    );
}

#[test]
fn server_close_fails_pending_requests_and_wakes_the_reader() {
    let key = create_session_key();
    let (path, server) = serve(move |mut s: ServerStream| {
        s.handshake(&key);
        let _ = s.read_json();
        // 不应答，直接断开。
    });
    let connection = connect(&path, ROLE, &key, &options()).expect("握手成功");
    let rpc = RpcPeer::spawn(connection, Handlers::new(), &rpc_options()).expect("读线程");
    let started = Instant::now();
    match rpc.request("a.hang", None) {
        Err(CallError::Channel(error)) => assert_eq!(error.code(), ErrorCode::Closed),
        other => panic!("期望 closed，得到 {other:?}"),
    }
    assert!(started.elapsed() < Duration::from_secs(1));
    assert_eq!(rpc.wait_closed(Some(Duration::from_secs(2))), Some(None));
    server.join().unwrap();
}

#[test]
fn local_close_wakes_the_blocked_reader() {
    let key = create_session_key();
    let (path, server) = serve(move |mut s: ServerStream| {
        s.handshake(&key);
        s.frames_until_closed()
    });
    let connection = connect(&path, ROLE, &key, &options()).expect("握手成功");
    let rpc = RpcPeer::spawn(connection, Handlers::new(), &rpc_options()).expect("读线程");
    std::thread::sleep(Duration::from_millis(50));
    let started = Instant::now();
    rpc.close();
    assert!(started.elapsed() < Duration::from_secs(1));
    assert_eq!(rpc.wait_closed(Some(Duration::from_secs(1))), Some(None));
    assert_eq!(code_of(rpc.notify("a.b", None)), ErrorCode::Closed);
    // 服务端看到连接断开。
    assert_eq!(server.join().unwrap(), 0);
}

#[test]
fn after_reply_runs_once_the_reply_is_written() {
    let key = create_session_key();
    let (hooked, hook_fired) = mpsc::channel();
    let (closed, client_closed) = mpsc::channel::<()>();
    let (path, server) = serve(move |mut s: ServerStream| {
        s.handshake(&key);
        s.write_json(
            FrameKind::Json,
            &json!({"jsonrpc": "2.0", "id": 1, "method": "core.stop", "params": {}}),
        );
        s.write_json(
            FrameKind::Json,
            &json!({"jsonrpc": "2.0", "id": 2, "method": "core.refuse"}),
        );
        // 客户端在钩子触发后立即关闭连接；应答若在钩子之后才写，这里就读不到。
        client_closed.recv().unwrap();
        (s.read_json(), s.read_json(), s.frames_until_closed())
    });
    let connection = connect(&path, ROLE, &key, &options()).expect("握手成功");
    let stop_hooked = hooked.clone();
    let handlers = Handlers::new()
        .request_then(
            "core.stop",
            |_| Ok(json!({})),
            move || stop_hooked.send("stop").unwrap(),
        )
        .request_then(
            "core.refuse",
            |_| Err(RpcError::new(7, "不行")),
            move || hooked.send("refuse").unwrap(),
        );
    let rpc = RpcPeer::spawn(connection, handlers, &rpc_options()).expect("读线程");
    let timeout = Duration::from_secs(2);
    assert_eq!(hook_fired.recv_timeout(timeout), Ok("stop"));
    assert_eq!(hook_fired.recv_timeout(timeout), Ok("refuse"));
    rpc.close();
    closed.send(()).unwrap();
    let (stopped, refused, rest) = server.join().unwrap();
    assert_eq!(stopped, json!({"jsonrpc": "2.0", "id": 1, "result": {}}));
    assert_eq!(
        refused,
        json!({"jsonrpc": "2.0", "id": 2, "error": {"code": 7, "message": "不行"}})
    );
    assert_eq!(rest, 0);
}
