"""帧编解码与 JSON 负载，与 Bellis `packages/channel/src/frame.ts` 逐字节兼容。

帧：4 字节大端负载长度 + 1 字节类型（0 握手、1 JSON、2 二进制）+ 负载。
先查类型再查长度；只收到帧头就能判定超长。
"""

import json
import struct
from dataclasses import dataclass
from enum import IntEnum

from .errors import ChannelError

FRAME_HEADER_BYTES = 5
_HEADER = struct.Struct(">IB")


class FrameKind(IntEnum):
    HANDSHAKE = 0
    JSON = 1
    BINARY = 2


_KINDS = frozenset(int(kind) for kind in FrameKind)


@dataclass(frozen=True, slots=True)
class Frame:
    kind: FrameKind
    payload: bytes


def encode_frame(kind: FrameKind, payload: bytes) -> bytes:
    return _HEADER.pack(len(payload), kind) + payload


def parse_header(header: bytes, max_payload_bytes: int) -> tuple[FrameKind, int]:
    """解析帧头，返回类型与负载长度。类型未知时为 `protocol-error`，超长时为 `frame-too-large`。"""
    length, kind = _HEADER.unpack(header)
    if kind not in _KINDS:
        raise ChannelError("protocol-error", f"未知的帧类型 {kind}")
    if length > max_payload_bytes:
        raise ChannelError("frame-too-large", f"帧长 {length} 超过上限 {max_payload_bytes}")
    return FrameKind(kind), length


class FrameDecoder:
    """增量解帧。每次 `read()` 只取一帧，调用方可以在两帧之间调整长度上限。"""

    def __init__(self, max_payload_bytes: int) -> None:
        self.max_payload_bytes = max_payload_bytes
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def push(self, chunk: bytes) -> None:
        self._buffer += chunk

    def read(self) -> Frame | None:
        """取出下一帧；数据不足时返回 None，帧头非法时抛出 ChannelError。"""
        if len(self._buffer) < FRAME_HEADER_BYTES:
            return None
        kind, length = parse_header(
            bytes(self._buffer[:FRAME_HEADER_BYTES]), self.max_payload_bytes
        )
        end = FRAME_HEADER_BYTES + length
        if len(self._buffer) < end:
            return None
        payload = bytes(self._buffer[FRAME_HEADER_BYTES:end])
        del self._buffer[:end]
        return Frame(kind, payload)


def dumps(message: object) -> bytes:
    """紧凑的 UTF-8 JSON，与 Node 的 `JSON.stringify` 输出一致（不转义非 ASCII，不接受 NaN）。"""
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _reject_constant(name: str) -> object:
    raise ValueError(f"JSON 不允许 {name}")


def loads(payload: bytes, *, what: str = "消息") -> object:
    """按 Node 的 `JSON.parse(payload.toString('utf8'))` 解析负载。

    非法的 UTF-8 替换为 U+FFFD 而不是报错；不跳过 BOM，也不按 UTF-16 猜编码；
    不接受 NaN 与 Infinity；同名键取最后一个。解析失败时为 `protocol-error`。
    """
    text = payload.decode("utf-8", errors="replace")
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as error:
        raise ChannelError("protocol-error", f"{what}不是合法 JSON") from error
