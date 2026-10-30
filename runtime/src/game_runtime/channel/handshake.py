"""握手：双向 HMAC 认证，与 Bellis `packages/channel/src/connection.ts` 逐字节兼容。不做 IO。

客户端发 hello（角色 + 随机数）；服务端回 challenge（随机数 + 证明）；客户端核验服务端证明后才回送
自己的证明；服务端核验后回 accept。证明为
HMAC-SHA256(会话密钥, JSON.stringify([域, 方向, 角色, 客户端随机数, 服务端随机数]))，
base64url 无填充，域为 `bellis-channel/1`，方向为 `server` 或 `client`。
消息的校验与 Bellis 用的 TypeBox 模式等价（`packages/protocol/src/channel.ts`）。
"""

import base64
import hashlib
import hmac
import re
from typing import cast

from .address import check_session_key
from .errors import ChannelError
from .frame import Frame, FrameKind, dumps, encode_frame, loads

PROOF_DOMAIN = "bellis-channel/1"
NONCE_BYTES = 32
# 认证前单帧上限：握手消息都很小，避免未认证的对端占用内存。
HANDSHAKE_MAX_FRAME_BYTES = 1024
DEFAULT_HANDSHAKE_TIMEOUT = 2.0
DEFAULT_MAX_FRAME_BYTES = 1024 * 1024

_BYTES32 = re.compile(r"[A-Za-z0-9_-]{43}")
_ROLE = re.compile(r"[a-z][a-z0-9_.:-]{0,63}")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _decode_bytes32(text: str) -> bytes:
    """解码已通过格式校验的 43 个字符。与 Node 一样忽略最后一个字符未用到的低 2 位。"""
    return base64.urlsafe_b64decode(text + "=")


def mac(
    key: bytes,
    direction: str,
    role: str,
    client_nonce: str,
    server_nonce: str,
    *,
    domain: str = PROOF_DOMAIN,
) -> bytes:
    """握手证明。记录的序列化与 Node 的 `JSON.stringify` 完全一致。"""
    record = dumps([domain, direction, role, client_nonce, server_nonce])
    return hmac.new(key, record, hashlib.sha256).digest()


def is_valid_role(role: object) -> bool:
    return isinstance(role, str) and _ROLE.fullmatch(role) is not None


def _is_version(value: object) -> bool:
    # TypeBox 的 Literal(1)：JSON 的 1、1.0、1e0 都解析为数 1，布尔值不算。
    return isinstance(value, int | float) and not isinstance(value, bool) and value == 1


def _bytes32(value: object) -> str | None:
    if isinstance(value, str) and _BYTES32.fullmatch(value) is not None:
        return value
    return None


def _closed_object(message: object, kind: str, keys: frozenset[str]) -> dict[str, object] | None:
    """键集合正好是 `keys`、版本为 1、类型为 `kind` 的对象。

    即 TypeBox 的 `additionalProperties: false`。
    """
    if not isinstance(message, dict):
        return None
    fields = cast("dict[str, object]", message)
    if fields.keys() != keys or not _is_version(fields["v"]) or fields["type"] != kind:
        return None
    return fields


def parse_hello(message: object) -> tuple[str, str] | None:
    """合法的 hello 返回（角色，客户端随机数），否则返回 None。"""
    fields = _closed_object(message, "hello", frozenset({"v", "type", "role", "nonce"}))
    if fields is None:
        return None
    role = fields["role"]
    nonce = _bytes32(fields["nonce"])
    if not isinstance(role, str) or not is_valid_role(role) or nonce is None:
        return None
    return role, nonce


def parse_challenge(message: object) -> tuple[str, str] | None:
    """合法的 challenge 返回（服务端随机数，证明），否则返回 None。"""
    fields = _closed_object(message, "challenge", frozenset({"v", "type", "nonce", "proof"}))
    if fields is None:
        return None
    nonce = _bytes32(fields["nonce"])
    proof = _bytes32(fields["proof"])
    if nonce is None or proof is None:
        return None
    return nonce, proof


def parse_proof(message: object) -> str | None:
    """合法的 proof 返回证明，否则返回 None。"""
    fields = _closed_object(message, "proof", frozenset({"v", "type", "proof"}))
    return None if fields is None else _bytes32(fields["proof"])


def is_accept(message: object) -> bool:
    return _closed_object(message, "accept", frozenset({"v", "type"})) is not None


def _handshake_message(frame: Frame) -> object:
    if frame.kind != FrameKind.HANDSHAKE:
        raise ChannelError("protocol-error", "认证完成前收到业务数据")
    return loads(frame.payload, what="握手消息")


def _invalid(expected: str) -> ChannelError:
    return ChannelError("protocol-error", f"握手消息不合法，期望 {expected}")


def _handshake_frame(message: dict[str, object]) -> bytes:
    return encode_frame(FrameKind.HANDSHAKE, dumps(message))


def _check_nonce(nonce: bytes) -> str:
    if len(nonce) != NONCE_BYTES:
        raise ValueError(f"随机数须为 {NONCE_BYTES} 字节")
    return b64url(nonce)


class ClientHandshake:
    """客户端一侧：`hello()` → `on_challenge()` → `on_accept()`，任一步失败即抛出 ChannelError。"""

    def __init__(self, key: bytes, role: str, nonce: bytes) -> None:
        check_session_key(key)
        if not is_valid_role(role):
            raise ChannelError("protocol-error", f"角色名不合法：{role}")
        self._key = key
        self.role = role
        self._nonce = _check_nonce(nonce)

    def hello(self) -> bytes:
        return _handshake_frame({"v": 1, "type": "hello", "role": self.role, "nonce": self._nonce})

    def on_challenge(self, frame: Frame) -> bytes:
        """核验服务端的证明，返回要发出的 proof 帧。服务端未能证明时为 `auth-failed`。"""
        challenge = parse_challenge(_handshake_message(frame))
        if challenge is None:
            raise _invalid("challenge")
        server_nonce, proof = challenge
        expected = mac(self._key, "server", self.role, self._nonce, server_nonce)
        if not hmac.compare_digest(_decode_bytes32(proof), expected):
            raise ChannelError("auth-failed", "服务端未能证明持有会话密钥，地址可能被抢注")
        own = b64url(mac(self._key, "client", self.role, self._nonce, server_nonce))
        return _handshake_frame({"v": 1, "type": "proof", "proof": own})

    def on_accept(self, frame: Frame) -> None:
        if not is_accept(_handshake_message(frame)):
            raise _invalid("accept")


class ServerHandshake:
    """服务端一侧：`on_hello()` → `on_proof()`。

    返回值为要发出的帧；`on_proof()` 返回前核验客户端。
    """

    def __init__(self, key: bytes, nonce: bytes) -> None:
        check_session_key(key)
        self._key = key
        self._nonce = _check_nonce(nonce)
        self._hello: tuple[str, str] | None = None

    def on_hello(self, frame: Frame) -> bytes:
        """返回要发出的 challenge 帧。"""
        hello = parse_hello(_handshake_message(frame))
        if hello is None:
            raise _invalid("hello")
        self._hello = hello
        role, client_nonce = hello
        proof = b64url(mac(self._key, "server", role, client_nonce, self._nonce))
        return _handshake_frame({"v": 1, "type": "challenge", "nonce": self._nonce, "proof": proof})

    def on_proof(self, frame: Frame) -> tuple[str, bytes]:
        """核验客户端的证明，返回（经认证的角色，accept 帧）。客户端未能证明时为 `auth-failed`。"""
        if self._hello is None:
            raise RuntimeError("on_hello 尚未成功")
        role, client_nonce = self._hello
        proof = parse_proof(_handshake_message(frame))
        if proof is None:
            raise _invalid("proof")
        expected = mac(self._key, "client", role, client_nonce, self._nonce)
        if not hmac.compare_digest(_decode_bytes32(proof), expected):
            raise ChannelError("auth-failed", f"客户端（{role}）未能证明持有会话密钥")
        return role, _handshake_frame({"v": 1, "type": "accept"})
