//! GS-03 探针的提权一侧（`docs/plan/spikes.md` 的 GS-03）。
//!
//! 由维护者在**管理员终端**里运行（或由“以最高权限运行”的计划任务经 `schtasks /Run` 拉起）。它：
//! 1. 取得连接信息——优先从凭据管理器读（`--from-cred`，验证 GS-03 第 3 项），也可从环境变量或命令行取，
//!    便于分步排查；
//! 2. 用 `bellis-channel` 客户端连接非提权一侧并完成 HMAC 握手（认证先于数据），做一次 RPC；
//! 3. 用 `frame-ring` 的写入方挂接主进程创建的帧环并写几帧（验证第 1 项的“写入”）；
//! 4. 报告自己的完整性级别与用户 SID（第 2、3 项）。
//!
//! 结果逐行写到标准输出，最后一行成功为 `ok`、失败为 `error <说明>`（退出码 1）。非提权一侧据此核对。
//! 探针不做任何需要管理员权限的动作；提权由维护者给予运行环境。

mod conn;
mod cred;
mod identity;
mod pattern;

use std::process::ExitCode;
use std::time::Duration;

use bellis_channel::{
    ChannelCredentials, ConnectionOptions, Handlers, RpcOptions, RpcPeer, connect,
};
use frame_ring::FrameRingWriter;
use serde_json::{Map, Value, json};

use crate::conn::ConnInfo;
use crate::pattern::pattern_frame;

/// 本探针与非提权一侧约定的凭据服务名（同 Bellis `SecretStore` 的 `bellis`）。
const DEFAULT_SERVICE: &str = "bellis";
/// 本探针的凭据条目名。
const DEFAULT_NAME: &str = "gs03-probe";
/// 没有指定来源时，从这个环境变量读连接信息。
const DEFAULT_ENV: &str = "GS03_CONN_INFO";

/// 连接信息的来源。
enum Source {
    Cred { service: String, name: String },
    Inline(String),
    Env(String),
}

fn usage() -> String {
    format!(
        "用法：gs03-core-probe [--from-cred [--service <服务>] [--name <条目>]] \
         [--conn-info <json>] [--conn-info-env <环境变量>]\n\
         缺省从环境变量 {DEFAULT_ENV} 读连接信息；--from-cred 从凭据管理器读 \
         <条目>.<服务>（缺省 {DEFAULT_NAME}.{DEFAULT_SERVICE}）。"
    )
}

fn parse_source(mut args: impl Iterator<Item = String>) -> Result<Source, String> {
    let mut from_cred = false;
    let mut service = DEFAULT_SERVICE.to_owned();
    let mut name = DEFAULT_NAME.to_owned();
    let mut inline: Option<String> = None;
    let mut env: Option<String> = None;
    while let Some(arg) = args.next() {
        let mut value = || args.next().ok_or_else(|| format!("{arg} 缺少取值"));
        match arg.as_str() {
            "--from-cred" => from_cred = true,
            "--service" => service = value()?,
            "--name" => name = value()?,
            "--conn-info" => inline = Some(value()?),
            "--conn-info-env" => env = Some(value()?),
            "--help" | "-h" => return Err(usage()),
            other => return Err(format!("未知参数 {other:?}\n{}", usage())),
        }
    }
    if let Some(text) = inline {
        return Ok(Source::Inline(text));
    }
    if from_cred {
        return Ok(Source::Cred { service, name });
    }
    Ok(Source::Env(env.unwrap_or_else(|| DEFAULT_ENV.to_owned())))
}

fn load(source: &Source) -> Result<ConnInfo, String> {
    let text = match source {
        Source::Inline(text) => text.clone(),
        Source::Env(var) => std::env::var(var).map_err(|_| {
            format!("环境变量 {var} 没有连接信息；可改用 --from-cred 或 --conn-info")
        })?,
        Source::Cred { service, name } => cred::read(service, name)
            .map_err(|error| format!("读不到凭据 {}：{error}", cred::target_name(service, name)))?,
    };
    ConnInfo::parse(&text)
}

fn object(value: &Value) -> Map<String, Value> {
    value.as_object().cloned().unwrap_or_default()
}

fn run(source: &Source) -> Result<(), String> {
    let info = load(source)?;
    let me = identity::current();
    println!(
        "integrity {} rid={:?}",
        me.integrity_level, me.integrity_rid
    );
    println!("user-sid {}", me.user_sid);

    let credentials = ChannelCredentials::decode(info.channel_path.clone(), &info.channel_key)
        .map_err(|error| format!("连接信息里的地址或密钥不合法：{error}"))?;
    let connection = connect(
        &credentials.path,
        &info.role,
        &credentials.key,
        &ConnectionOptions::default(),
    )
    .map_err(|error| format!("连不上非提权一侧（{}）：{error}", error.code().as_str()))?;
    println!("open {}", connection.role());

    let rpc = RpcPeer::spawn(
        connection,
        Handlers::new(),
        &RpcOptions {
            timeout: Duration::from_secs(10),
        },
    )
    .map_err(|error| format!("无法启动 RPC：{error}"))?;

    // 一次 RPC：把自己的身份报给非提权一侧，取回它的身份。
    let hello = rpc
        .request(
            "gs03.hello",
            Some(&object(&json!({
                "pid": std::process::id(),
                "role": info.role,
                "identity": me,
            }))),
        )
        .map_err(|error| format!("gs03.hello 失败：{error}"))?;
    println!("hello {hello}");

    // 挂接帧环并写几帧。
    let mut writer = FrameRingWriter::attach(&info.frame_ring)
        .map_err(|error| format!("挂接帧环失败：{error}"))?;
    let (max_width, max_height) = (info.frame_ring.max_width, info.frame_ring.max_height);
    let mut last = writer.last_frame_number();
    for number in 1..=info.frames {
        let frame = pattern_frame(number, max_width, max_height);
        last = writer
            .write(&frame)
            .map_err(|error| format!("写第 {number} 帧失败：{error}"))?;
    }
    println!("frames {last}");

    // 让非提权一侧核对它从帧环读到的帧。
    let done = rpc
        .request(
            "gs03.frames",
            Some(&object(&json!({
                "last_frame_number": last,
                "count": info.frames,
            }))),
        )
        .map_err(|error| format!("gs03.frames 失败：{error}"))?;
    println!("verified {done}");

    rpc.close();
    Ok(())
}

fn main() -> ExitCode {
    let source = match parse_source(std::env::args().skip(1)) {
        Ok(source) => source,
        Err(message) => {
            eprintln!("{message}");
            return ExitCode::from(2);
        }
    };
    match run(&source) {
        Ok(()) => {
            println!("ok");
            ExitCode::SUCCESS
        }
        Err(message) => {
            println!("error {message}");
            eprintln!("gs03-core-probe: {message}");
            ExitCode::FAILURE
        }
    }
}
