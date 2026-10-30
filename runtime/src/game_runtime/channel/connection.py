"""已认证的本机连接，对应 Bellis `packages/channel/src/connection.ts` 的 Connection。

`Connection.accept` / `Connection.open` 在给定的字节流上完成握手后交出连接；
握手失败时断开字节流并抛出 ChannelError。认证前只收发握手帧，帧长上限为 1024 字节；
认证后上限为 `max_frame_bytes`。
"""

import asyncio
import secrets
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

from .address import check_session_key
from .errors import ChannelError
from .frame import FRAME_HEADER_BYTES, Frame, FrameKind, dumps, encode_frame, loads, parse_header
from .handshake import (
    DEFAULT_HANDSHAKE_TIMEOUT,
    DEFAULT_MAX_FRAME_BYTES,
    HANDSHAKE_MAX_FRAME_BYTES,
    NONCE_BYTES,
    ClientHandshake,
    ServerHandshake,
)

# 正常关闭时等待对端收完已写出数据的宽限期，超过即强制断开（与 Bellis 的 CLOSE_GRACE_MS 一致）。
CLOSE_GRACE = 1.0


@dataclass(frozen=True, slots=True)
class JsonMessage:
    value: Any


@dataclass(frozen=True, slots=True)
class BinaryMessage:
    data: bytes


type Message = JsonMessage | BinaryMessage


class _FrameReader:
    """从 StreamReader 逐帧读取。读到一半被取消（超时）时保留已读的帧头，下次接着读负载，不失步。"""

    def __init__(self, reader: asyncio.StreamReader) -> None:
        self._reader = reader
        self._header: tuple[FrameKind, int] | None = None

    async def read(self, max_payload_bytes: int) -> Frame:
        try:
            if self._header is None:
                head = await self._reader.readexactly(FRAME_HEADER_BYTES)
                self._header = parse_header(head, max_payload_bytes)
            kind, length = self._header
            payload = await self._reader.readexactly(length)
        except asyncio.IncompleteReadError as error:
            raise ChannelError("closed", "连接已关闭") from error
        except OSError as error:
            raise ChannelError("closed", f"连接已断开：{error}") from error
        self._header = None
        return Frame(kind, payload)


def _abort(writer: asyncio.StreamWriter) -> None:
    writer.transport.abort()


class Connection:
    """一条已认证的本机连接。只由 `accept` / `open` 在握手成功后交出。"""

    def __init__(
        self,
        frames: _FrameReader,
        writer: asyncio.StreamWriter,
        role: str,
        max_frame_bytes: int,
    ) -> None:
        self._frames = frames
        self._writer = writer
        self._max_frame_bytes = max_frame_bytes
        self._failed: ChannelError | None = None
        self.role = role

    @classmethod
    async def accept(
        cls,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        key: bytes,
        *,
        handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        nonce: bytes | None = None,
    ) -> "Connection":
        """服务端：等待客户端完成握手。`nonce` 只供已知答案测试固定随机数。"""
        check_session_key(key)
        handshake = ServerHandshake(
            key, secrets.token_bytes(NONCE_BYTES) if nonce is None else nonce
        )
        frames = _FrameReader(reader)

        async def run() -> str:
            writer.write(handshake.on_hello(await frames.read(HANDSHAKE_MAX_FRAME_BYTES)))
            role, accept = handshake.on_proof(await frames.read(HANDSHAKE_MAX_FRAME_BYTES))
            writer.write(accept)
            return role

        role = await _handshake(run(), writer, handshake_timeout)
        return cls(frames, writer, role, max_frame_bytes)

    @classmethod
    async def open(
        cls,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        role: str,
        key: bytes,
        *,
        handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        nonce: bytes | None = None,
    ) -> "Connection":
        """客户端：以给定角色发起握手。`nonce` 只供已知答案测试固定随机数。"""
        check_session_key(key)
        try:
            handshake = ClientHandshake(
                key, role, secrets.token_bytes(NONCE_BYTES) if nonce is None else nonce
            )
        except ChannelError:
            _abort(writer)
            raise
        frames = _FrameReader(reader)

        async def run() -> str:
            writer.write(handshake.hello())
            writer.write(handshake.on_challenge(await frames.read(HANDSHAKE_MAX_FRAME_BYTES)))
            handshake.on_accept(await frames.read(HANDSHAKE_MAX_FRAME_BYTES))
            return role

        await _handshake(run(), writer, handshake_timeout)
        return cls(frames, writer, role, max_frame_bytes)

    @property
    def is_open(self) -> bool:
        return self._failed is None and not self._writer.is_closing()

    def send(self, message: object) -> None:
        """发送一条 JSON 消息。连接未打开或超出帧长上限时抛出 ChannelError，超长不影响连接。"""
        self._write(FrameKind.JSON, dumps(message))

    def send_binary(self, data: bytes) -> None:
        """发送一帧二进制数据；调用方随后 `await drain()` 施加背压。"""
        self._write(FrameKind.BINARY, data)

    async def drain(self) -> None:
        try:
            await self._writer.drain()
        except OSError as error:
            raise ChannelError("closed", f"连接已断开：{error}") from error

    async def receive(self, timeout: float | None = None) -> Message:
        """下一条消息。

        超过 `timeout` 秒没有收到完整的帧时为 `timeout`，连接仍可用；对端关闭时为 `closed`；
        帧超长、帧类型未知、认证后收到握手帧或 JSON 不合法时断开连接并抛出对应的错误。
        """
        if self._failed is not None:
            raise self._failed
        try:
            async with asyncio.timeout(timeout):
                frame = await self._frames.read(self._max_frame_bytes)
        except TimeoutError:
            raise ChannelError("timeout", f"未在 {timeout}s 内收到消息") from None
        except ChannelError as error:
            raise self._fail(error) from None
        match frame.kind:
            case FrameKind.BINARY:
                return BinaryMessage(frame.payload)
            case FrameKind.HANDSHAKE:
                raise self._fail(ChannelError("protocol-error", "认证后收到握手帧"))
            case FrameKind.JSON:
                try:
                    return JsonMessage(loads(frame.payload))
                except ChannelError as error:
                    raise self._fail(error) from None

    async def close(self) -> None:
        """正常关闭：把已写出的数据送出后关闭，超过宽限期强制断开。"""
        if self._writer.is_closing():
            return
        self._writer.close()
        try:
            async with asyncio.timeout(CLOSE_GRACE):
                await self._writer.wait_closed()
        except (TimeoutError, OSError):
            _abort(self._writer)

    def abort(self) -> None:
        """立即断开。"""
        if self._failed is None:
            self._failed = ChannelError("closed", "连接已关闭")
        _abort(self._writer)

    def _fail(self, error: ChannelError) -> ChannelError:
        if self._failed is None:
            self._failed = error
        _abort(self._writer)
        return error

    def _write(self, kind: FrameKind, payload: bytes) -> None:
        if not self.is_open:
            raise ChannelError("closed", "连接未打开")
        if len(payload) > self._max_frame_bytes:
            raise ChannelError(
                "frame-too-large", f"负载 {len(payload)} 字节超过上限 {self._max_frame_bytes}"
            )
        self._writer.write(encode_frame(kind, payload))


async def _handshake[T](
    run: Coroutine[Any, Any, T],
    writer: asyncio.StreamWriter,
    timeout: float,
) -> T:
    """在时限内跑完握手；任何失败都断开字节流并抛出 ChannelError。"""
    try:
        async with asyncio.timeout(timeout):
            return await run
    except TimeoutError:
        _abort(writer)
        raise ChannelError(
            "handshake-timeout", f"握手未在 {round(timeout * 1000)}ms 内完成"
        ) from None
    except ChannelError as error:
        _abort(writer)
        if error.code == "closed":
            raise ChannelError("closed", "握手完成前连接已关闭") from error
        raise
    except BaseException:
        _abort(writer)
        raise
