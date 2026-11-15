"""直连方舟的单次 HTTPS POST，读取有界的 HTTP/1.1 事件流。"""

import asyncio
import re
import socket
import ssl
from collections.abc import AsyncIterator
from typing import cast

from game_runtime.models.config import ARK_HOST
from game_runtime.models.errors import ModelCallError, ModelFailure
from game_runtime.models.stream import StreamStats, read_completion

MAX_REQUEST_BYTES = 16 * 1024 * 1024
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_HEADER_BYTES = 32 * 1024
_READ_SIZE = 16 * 1024
_CRLF = b"\r\n"
_HTTP_FIELD = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_RELEVANT_HEADERS = frozenset(
    {b"content-type", b"content-length", b"content-encoding", b"transfer-encoding"}
)

type InetAddress = tuple[str, int] | tuple[str, int, int, int]
type ArkAddress = tuple[int, InetAddress]


def resolve_ark() -> tuple[ArkAddress, ...]:
    """解析批准的主机。调用方负责在线程里运行并跟踪此操作。"""
    try:
        records = socket.getaddrinfo(
            ARK_HOST, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
        )
    except OSError:
        raise ModelCallError(ModelFailure.NETWORK) from None
    addresses = tuple(
        (family, cast(InetAddress, address))
        for family, _, _, _, address in records
        if family in (socket.AF_INET, socket.AF_INET6)
    )
    if not addresses:
        raise ModelCallError(ModelFailure.NETWORK)
    return addresses


async def connect_socket(family: int, address: InetAddress) -> socket.socket:
    # 核对数字地址，避免 sock_connect 另开一个解析线程。
    socket.inet_pton(family, address[0])
    connection = socket.socket(family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    try:
        connection.setblocking(False)
        await asyncio.get_running_loop().sock_connect(connection, address)
    except BaseException:
        connection.close()
        raise
    return connection


class ChatTransport:
    def __init__(self) -> None:
        # create_default_context 会打开 SSLKEYLOGFILE 指定的文件。
        self._tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._tls.load_default_certs()
        # Python 接受 None 来禁用回调，类型存根的 setter 只声明了 str。
        self._tls.keylog_filename = None  # pyright: ignore[reportAttributeAccessIssue]
        self._tls.set_alpn_protocols(["http/1.1"])

    async def post(
        self,
        path: str,
        body: bytes,
        key: str,
        *,
        addresses: tuple[ArkAddress, ...],
        stats: StreamStats,
        deadline: float,
    ) -> str:
        loop = asyncio.get_running_loop()
        if loop.time() >= deadline:
            raise ModelCallError(ModelFailure.TIMEOUT)
        head = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: {ARK_HOST}\r\n"
            f"Authorization: Bearer {key}\r\n"
            "Accept: text/event-stream\r\n"
            "Accept-Encoding: identity\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        try:
            reader, writer = await self._open(addresses)
            try:
                if loop.time() >= deadline:
                    raise ModelCallError(ModelFailure.TIMEOUT)
                writer.write(head)
                writer.write(body)
                await writer.drain()
                headers = await _response_headers(reader)
                return await read_completion(_body(reader, headers), stats)
            finally:
                writer.transport.abort()
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            raise ModelCallError(ModelFailure.PROTOCOL) from None
        except OSError:
            raise ModelCallError(ModelFailure.NETWORK) from None

    async def _open(
        self, addresses: tuple[ArkAddress, ...]
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        for family, address in addresses:
            try:
                connection = await connect_socket(family, address)
            except OSError:
                continue
            try:
                return await asyncio.open_connection(
                    sock=connection,
                    ssl=self._tls,
                    server_hostname=ARK_HOST,
                    limit=_MAX_HEADER_BYTES,
                )
            except BaseException:
                connection.close()
                raise
        raise ModelCallError(ModelFailure.NETWORK)


def _field(line: bytes) -> tuple[bytes, bytes]:
    name, colon, value = line.partition(b":")
    if not colon or _HTTP_FIELD.fullmatch(name) is None:
        raise ModelCallError(ModelFailure.PROTOCOL)
    return name.lower(), value.strip().lower()


async def _response_headers(reader: asyncio.StreamReader) -> dict[bytes, bytes]:
    raw = await reader.readuntil(b"\r\n\r\n")
    status_line, _, fields = raw[:-4].partition(_CRLF)
    match = re.fullmatch(rb"HTTP/1\.[01] ([1-5][0-9]{2})(?: [^\r\n]*)?", status_line)
    if match is None:
        raise ModelCallError(ModelFailure.PROTOCOL)
    status = int(match[1])
    if status != 200:
        raise ModelCallError(ModelFailure.HTTP, http_status=status)

    headers: dict[bytes, bytes] = {}
    for line in fields.split(_CRLF):
        name, value = _field(line)
        if name in _RELEVANT_HEADERS:
            if name in headers:
                raise ModelCallError(ModelFailure.PROTOCOL)
            headers[name] = value
    media_type = headers.get(b"content-type", b"").partition(b";")[0].strip()
    if (
        media_type != b"text/event-stream"
        or headers.get(b"content-encoding", b"identity") != b"identity"
    ):
        raise ModelCallError(ModelFailure.PROTOCOL)
    return headers


async def _body(reader: asyncio.StreamReader, headers: dict[bytes, bytes]) -> AsyncIterator[bytes]:
    transfer = headers.get(b"transfer-encoding")
    length = headers.get(b"content-length")
    if transfer is not None:
        if transfer != b"chunked" or length is not None:
            raise ModelCallError(ModelFailure.PROTOCOL)
        async for piece in _chunks(reader):
            yield piece
    elif length is not None:
        if not re.fullmatch(rb"[0-9]{1,10}", length):
            raise ModelCallError(ModelFailure.PROTOCOL)
        size = int(length)
        _check_size(size)
        async for piece in _read_bytes(reader, size):
            yield piece
    else:
        consumed = 0
        while piece := await reader.read(_READ_SIZE):
            consumed += len(piece)
            _check_size(consumed)
            yield piece


async def _chunks(reader: asyncio.StreamReader) -> AsyncIterator[bytes]:
    consumed = 0
    while True:
        line = await reader.readuntil(_CRLF)
        size_text = line[:-2].partition(b";")[0]
        if not re.fullmatch(rb"[0-9a-fA-F]{1,8}", size_text):
            raise ModelCallError(ModelFailure.PROTOCOL)
        size = int(size_text, 16)
        consumed += len(line)
        if size == 0:
            _check_size(consumed)
            await _read_trailers(reader, consumed)
            return
        consumed += size + len(_CRLF)
        _check_size(consumed)
        async for piece in _read_bytes(reader, size):
            yield piece
        if await reader.readexactly(2) != _CRLF:
            raise ModelCallError(ModelFailure.PROTOCOL)


async def _read_trailers(reader: asyncio.StreamReader, size: int) -> None:
    while True:
        line = await reader.readuntil(_CRLF)
        size += len(line)
        _check_size(size)
        if line == _CRLF:
            return
        name, _ = _field(line[:-2])
        if name in _RELEVANT_HEADERS:
            raise ModelCallError(ModelFailure.PROTOCOL)


async def _read_bytes(reader: asyncio.StreamReader, count: int) -> AsyncIterator[bytes]:
    while count:
        piece = await reader.read(min(count, _READ_SIZE))
        if not piece:
            raise ModelCallError(ModelFailure.PROTOCOL)
        count -= len(piece)
        yield piece


def _check_size(size: int) -> None:
    if size > _MAX_RESPONSE_BYTES:
        raise ModelCallError(ModelFailure.SIZE_LIMIT)
