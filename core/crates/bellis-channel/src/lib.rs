//! 本机通道 `bellis-channel/1` 的 Rust 客户端（GR-28）。
//!
//! 实时核用它连接主进程（核连接）与 Bellis 宿主（控制连接）。不用异步运行时：标准线程加阻塞 IO，
//! 读取可设截止时间。帧格式、握手、HMAC 的输入与顺序、长度上限、超时与错误码与 Bellis
//! `packages/channel` 逐字节兼容，以从 Bellis 导出的已知答案向量（`testdata/vectors.json`）测试。
//! 设计见仓库的 `docs/modules/core-protocol.md`“本机通道”。

mod address;
mod base64url;
mod connection;
mod error;
mod frame;
mod handshake;
mod json;
mod rpc;
mod transport;

pub use address::{
    ChannelCredentials, MAX_UDS_PATH_BYTES, SESSION_KEY_BYTES, SessionKey, channel_name,
    create_channel_path, create_session_key, decode_session_key, encode_session_key,
};
pub use connection::{
    Connection, ConnectionOptions, DEFAULT_HANDSHAKE_TIMEOUT, DEFAULT_MAX_FRAME_BYTES,
    HANDSHAKE_MAX_FRAME_BYTES, Message, connect,
};
pub use error::{ChannelError, ErrorCode};
pub use frame::{FRAME_HEADER_BYTES, Frame, FrameDecoder, FrameKind, encode_frame};
pub use handshake::{ClientHandshake, PROOF_DOMAIN, mac};
pub use rpc::{
    CallError, Handlers, RPC_CANCEL, RpcError, RpcErrorCode, RpcId, RpcKind, RpcOptions, RpcPeer,
    classify, is_cancel_params,
};
#[cfg(windows)]
pub use transport::NamedPipeTransport;
#[cfg(unix)]
pub use transport::UnixTransport;
pub use transport::{Transport, open_transport};

#[cfg(test)]
mod tests;
