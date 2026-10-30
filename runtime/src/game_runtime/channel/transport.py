"""字节流层：Windows 命名管道与 POSIX UDS 放在同一抽象后面（Bellis D-12）。

客户端得到一对 asyncio 的 StreamReader / StreamWriter；服务端每接受一个连接就以这一对回调
`on_stream`。上层的握手与帧处理与传输无关。
"""

import asyncio
import errno
import socket
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .errors import ChannelError

# StreamReader 的缓冲上限只影响流控（超过两倍即暂停读取），不限制单帧长度。
STREAM_LIMIT = 1024 * 1024

type StreamCallback = Callable[[asyncio.StreamReader, asyncio.StreamWriter], None]


class Listener(Protocol):
    """监听中的地址。`close()` 停止接受新连接并释放地址，不断开已接受的连接。"""

    async def close(self) -> None: ...


async def open_stream(path: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """连接通道地址。连不上时为 `connect-failed`。"""
    try:
        if sys.platform == "win32":
            loop = asyncio.get_running_loop()
            if not isinstance(loop, asyncio.ProactorEventLoop):
                raise ChannelError("connect-failed", "命名管道需要 ProactorEventLoop")
            reader = asyncio.StreamReader(limit=STREAM_LIMIT, loop=loop)
            protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
            transport, _ = await loop.create_pipe_connection(lambda: protocol, path)
            return reader, asyncio.StreamWriter(transport, protocol, reader, loop)
        return await asyncio.open_unix_connection(path, limit=STREAM_LIMIT)
    except OSError as error:
        raise ChannelError("connect-failed", f"无法连接通道：{error}") from error


async def start_listener(path: str, on_stream: StreamCallback) -> Listener:
    """在通道地址上监听。

    地址已被占用时为 `address-in-use`：会话地址每次随机生成，不得用同名重试。
    """
    if sys.platform == "win32":
        from ._winpipe import NamedPipeListener

        return NamedPipeListener(asyncio.get_running_loop(), path, on_stream)
    return await _UnixListener.start(path, on_stream)


class _UnixListener:
    """UDS 监听。

    自己 bind，以便地址已存在时报 `address-in-use`，而不是像 asyncio 那样删掉旧 socket。
    """

    def __init__(self, server: asyncio.Server, path: str) -> None:
        self._server = server
        self._path = path

    @classmethod
    async def start(cls, path: str, on_stream: StreamCallback) -> "_UnixListener":
        if sys.platform == "win32":
            raise RuntimeError("Windows 上用命名管道")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(path)
            sock.listen()
            server = await asyncio.start_unix_server(on_stream, sock=sock, limit=STREAM_LIMIT)
        except OSError as error:
            sock.close()
            if error.errno == errno.EADDRINUSE:
                raise ChannelError(
                    "address-in-use", f"通道地址已被占用，可能被抢注：{path}"
                ) from error
            raise
        return cls(server, path)

    async def close(self) -> None:
        self._server.close()
        Path(self._path).unlink(missing_ok=True)
