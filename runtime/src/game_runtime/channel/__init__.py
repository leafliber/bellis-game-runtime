"""本机通道 `bellis-channel/1`：按 Bellis R-05 自写的服务端、客户端
与最小 JSON-RPC 2.0 对端（GR-28）。

设计见 `docs/modules/core-protocol.md` 的“本机通道”。帧格式、握手、长度上限、超时与错误码与 Bellis
`packages/channel` 逐字节兼容，以从 Bellis 导出的已知答案向量测试。
"""

from .address import (
    MAX_UDS_PATH_BYTES,
    SESSION_KEY_BYTES,
    ChannelCredentials,
    create_channel_path,
    create_session_key,
    decode_session_key,
    encode_session_key,
    session_credentials,
)
from .connection import BinaryMessage, Connection, JsonMessage, Message
from .endpoint import DEFAULT_MAX_PENDING_HANDSHAKES, ChannelServer, connect, listen
from .errors import ChannelError, ChannelErrorCode
from .frame import Frame, FrameDecoder, FrameKind, encode_frame
from .handshake import (
    DEFAULT_HANDSHAKE_TIMEOUT,
    DEFAULT_MAX_FRAME_BYTES,
    HANDSHAKE_MAX_FRAME_BYTES,
    PROOF_DOMAIN,
)
from .rpc import RPC_CANCEL, RpcError, RpcErrorCode, RpcPeer

__all__ = [
    "DEFAULT_HANDSHAKE_TIMEOUT",
    "DEFAULT_MAX_FRAME_BYTES",
    "DEFAULT_MAX_PENDING_HANDSHAKES",
    "HANDSHAKE_MAX_FRAME_BYTES",
    "MAX_UDS_PATH_BYTES",
    "PROOF_DOMAIN",
    "RPC_CANCEL",
    "SESSION_KEY_BYTES",
    "BinaryMessage",
    "ChannelCredentials",
    "ChannelError",
    "ChannelErrorCode",
    "ChannelServer",
    "Connection",
    "Frame",
    "FrameDecoder",
    "FrameKind",
    "JsonMessage",
    "Message",
    "RpcError",
    "RpcErrorCode",
    "RpcPeer",
    "connect",
    "create_channel_path",
    "create_session_key",
    "decode_session_key",
    "encode_frame",
    "encode_session_key",
    "listen",
    "session_credentials",
]
