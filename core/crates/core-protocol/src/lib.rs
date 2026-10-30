//! 核协议：主进程与实时核之间核连接上的消息与帧环布局，是它们的唯一源（GR-03）。
//!
//! 消息集、字段含义与帧环布局见 `docs/modules/core-protocol.md`。JSON Schema、Pydantic 模型、
//! 帧环偏移表与示例消息都由本 crate 导出到仓库的 `protocol/`，重新生成用 `uv run generate`。

pub mod export;
pub mod frame_ring;
pub mod messages;
pub mod methods;

/// 核协议版本。握手时两端都核对，不相等即拒绝；两端同仓库同发布，仍然校验。
pub const PROTOCOL_VERSION: u32 = 1;
