"""本机通道的错误。错误码与 Bellis `packages/channel/src/errors.ts` 一致。"""

from typing import Literal

type ChannelErrorCode = Literal[
    # 监听地址已被占用：会话地址每次随机生成，出现即视为可能被抢注，不得用同名重试。
    "address-in-use",
    # 地址或会话密钥的文本不合法（会话编号、UDS 路径长度、密钥编码）。
    "address-invalid",
    # 会话密钥长度不对。
    "invalid-key",
    # 同时进行的未认证握手已达上限。
    "busy",
    # 连接不到对端。
    "connect-failed",
    # 对端未能证明持有会话密钥。
    "auth-failed",
    "handshake-timeout",
    "frame-too-large",
    # 帧或消息不符合协议。
    "protocol-error",
    # 读取或请求超过截止时间。
    "timeout",
    "closed",
]


class ChannelError(Exception):
    """本机通道的错误，`code` 取 `ChannelErrorCode` 之一。"""

    def __init__(self, code: ChannelErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: ChannelErrorCode = code

    def __repr__(self) -> str:
        return f"ChannelError({self.code!r}, {str(self)!r})"
