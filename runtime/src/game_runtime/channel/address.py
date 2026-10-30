"""本次会话的通道地址与会话密钥（Bellis R-05）。

命名与 Bellis `packages/channel/src/address.ts` 一致。

地址形如 `bellis-<会话编号>-<128 位随机数的 base32>`：Windows 上是命名管道 `\\\\.\\pipe\\…`，
其它平台是 UDS。与 Bellis 不同的是，UDS 放在本进程新建的私有临时目录里，
而不直接放在系统临时目录下。
"""

import base64
import os
import re
import secrets
import shutil
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .errors import ChannelError

SESSION_KEY_BYTES = 32
# macOS 的 sun_path 为 104 字节（含结尾 NUL），取各平台中最小者。
MAX_UDS_PATH_BYTES = 103
PIPE_PREFIX = "\\\\.\\pipe\\"
_SESSION_ID = re.compile(r"[a-z0-9]{1,16}")
_KEY_TEXT = re.compile(r"[A-Za-z0-9_-]{43}")
_PATH_RANDOM_BYTES = 16


def _base32(data: bytes) -> str:
    """RFC 4648 base32，小写、无填充。

    Windows 管道名与 macOS 文件名不区分大小写，所以不用 base64。
    """
    return base64.b32encode(data).decode("ascii").rstrip("=").lower()


def channel_name(session_id: str, random: bytes) -> str:
    """`bellis-<会话编号>-<random 的 base32>`。会话编号不合法时为 `address-invalid`。"""
    if _SESSION_ID.fullmatch(session_id) is None:
        raise ChannelError("address-invalid", f"会话编号须匹配 {_SESSION_ID.pattern}")
    return f"bellis-{session_id}-{_base32(random)}"


def create_channel_path(session_id: str, directory: str | os.PathLike[str] | None = None) -> str:
    """生成本次会话的通道地址。

    Windows 上为命名管道，忽略 `directory`；其它平台为 `directory` 下的 `<名字>.sock`，
    `directory` 缺省为系统临时目录。UDS 路径超过 103 字节时为 `address-invalid`。
    """
    name = channel_name(session_id, secrets.token_bytes(_PATH_RANDOM_BYTES))
    if sys.platform == "win32":
        return PIPE_PREFIX + name
    base = tempfile.gettempdir() if directory is None else os.fspath(directory)
    path = str(Path(base) / f"{name}.sock")
    if len(os.fsencode(path)) > MAX_UDS_PATH_BYTES:
        raise ChannelError("address-invalid", f"UDS 路径超过 {MAX_UDS_PATH_BYTES} 字节：{path}")
    return path


def create_session_key() -> bytes:
    return secrets.token_bytes(SESSION_KEY_BYTES)


def check_session_key(key: bytes) -> None:
    """会话密钥必须是完整的 32 字节随机数（R-05）。"""
    if len(key) != SESSION_KEY_BYTES:
        raise ChannelError("invalid-key", f"会话密钥须为 {SESSION_KEY_BYTES} 字节")


def encode_session_key(key: bytes) -> str:
    """base64url、无填充。"""
    return base64.urlsafe_b64encode(key).decode("ascii").rstrip("=")


def decode_session_key(text: str) -> bytes:
    """只接受 32 字节密钥的规范 base64url 编码。

    否则为 `address-invalid`，与 Bellis 的 TS 实现一致。
    """
    if _KEY_TEXT.fullmatch(text) is not None:
        key = base64.urlsafe_b64decode(text + "=")
        if encode_session_key(key) == text:
            return key
    raise ChannelError("address-invalid", "会话密钥格式不正确")


@dataclass(frozen=True, slots=True)
class ChannelCredentials:
    """交给客户端的连接信息：通道地址与会话密钥。"""

    path: str
    key: bytes

    def __post_init__(self) -> None:
        check_session_key(self.key)

    @property
    def encoded_key(self) -> str:
        return encode_session_key(self.key)

    @classmethod
    def decode(cls, path: str, encoded_key: str) -> "ChannelCredentials":
        return cls(path, decode_session_key(encoded_key))

    def __repr__(self) -> str:
        return f"ChannelCredentials(path={self.path!r}, key=<redacted>)"


@contextmanager
def session_credentials(session_id: str) -> Generator[ChannelCredentials]:
    """生成本次会话的地址与密钥。

    其它平台上 UDS 放在 `tempfile.mkdtemp` 新建的私有目录（仅当前用户可访问）里，退出时删除该目录；
    访问控制仍靠握手认证（Bellis D-12），私有目录只是减少暴露。Windows 上不建目录。
    """
    key = create_session_key()
    if sys.platform == "win32":
        yield ChannelCredentials(create_channel_path(session_id), key)
        return
    directory = tempfile.mkdtemp(prefix="bgr-")
    try:
        yield ChannelCredentials(create_channel_path(session_id, directory), key)
    finally:
        shutil.rmtree(Path(directory), ignore_errors=True)
