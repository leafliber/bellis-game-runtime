//! JSON 负载的解析，按 Node 的 `JSON.parse(payload.toString('utf8'))` 的语义。
//!
//! 非法的 UTF-8 替换为 U+FFFD 而不是报错；不跳过 BOM；不接受 NaN 与 Infinity；同名键取最后一个。
//! 与 Node 的已知出入：`serde_json` 不接受孤立代理项的 `\uD800` 转义，嵌套超过 128 层也拒绝
//! （实时核宁可断开，也不在栈上无限递归）。

use serde_json::Value;

use crate::error::{ChannelError, ErrorCode};

/// 解析一帧 JSON 负载；`what` 用于错误说明（“消息”或“握手消息”）。
pub(crate) fn loads(payload: &[u8], what: &str) -> Result<Value, ChannelError> {
    let text = String::from_utf8_lossy(payload);
    serde_json::from_str(&text).map_err(|error| {
        ChannelError::new(
            ErrorCode::ProtocolError,
            format!("{what}不是合法 JSON：{error}"),
        )
    })
}

/// 把一个 JSON 值编码为紧凑的 UTF-8。
pub(crate) fn dumps(value: &Value) -> Vec<u8> {
    // serde_json::Value 的序列化不会失败。
    serde_json::to_vec(value).unwrap_or_default()
}

/// 把字符串编码为 JSON 字符串字面量。
pub(crate) fn string_literal(text: &str) -> String {
    Value::from(text).to_string()
}
