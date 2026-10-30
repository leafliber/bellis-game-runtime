"""监听与连接，对应 Bellis `packages/channel/src/endpoint.ts`。非提权一方（主进程）永远做服务端。"""

import asyncio
import weakref
from collections.abc import Callable

from .address import check_session_key
from .connection import Connection
from .errors import ChannelError
from .handshake import DEFAULT_HANDSHAKE_TIMEOUT, DEFAULT_MAX_FRAME_BYTES, is_valid_role
from .transport import Listener, open_stream, start_listener

DEFAULT_MAX_PENDING_HANDSHAKES = 16


class ChannelServer:
    """监听中的通道。由 `listen` 交出。"""

    def __init__(
        self,
        path: str,
        key: bytes,
        *,
        on_connection: Callable[[Connection], None],
        on_rejected: Callable[[ChannelError], None],
        on_error: Callable[[BaseException], None],
        handshake_timeout: float,
        max_frame_bytes: int,
        max_pending_handshakes: int,
    ) -> None:
        check_session_key(key)
        self.path = path
        self._key = key
        self._on_connection = on_connection
        self._on_rejected = on_rejected
        self._on_error = on_error
        self._handshake_timeout = handshake_timeout
        self._max_frame_bytes = max_frame_bytes
        self._max_pending = max_pending_handshakes
        self._pending = 0
        self._listener: Listener | None = None
        # 已接受的字节流（含握手中的），关闭时一并断开；连接被丢弃后自动移除。
        self._writers: weakref.WeakSet[asyncio.StreamWriter] = weakref.WeakSet()
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    async def start(self) -> None:
        """开始监听；由 `listen` 调用。"""
        if self._listener is not None or self._closed:
            raise RuntimeError("ChannelServer 只能启动一次")
        self._listener = await start_listener(self.path, self._on_stream)

    async def close(self) -> None:
        """停止监听，并断开全部连接（含握手中的）。"""
        if self._closed:
            return
        self._closed = True
        if self._listener is not None:
            await self._listener.close()
        for task in list(self._tasks):
            task.cancel()
        for writer in list(self._writers):
            writer.transport.abort()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def _on_stream(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._closed:
            writer.transport.abort()
            return
        if self._pending >= self._max_pending:
            writer.transport.abort()
            self._on_rejected(ChannelError("busy", f"未认证的握手已达上限 {self._max_pending}"))
            return
        self._pending += 1
        self._writers.add(writer)
        task = asyncio.get_running_loop().create_task(self._handshake(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handshake(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            connection = await Connection.accept(
                reader,
                writer,
                self._key,
                handshake_timeout=self._handshake_timeout,
                max_frame_bytes=self._max_frame_bytes,
            )
        except ChannelError as error:
            self._on_rejected(error)
            return
        finally:
            self._pending -= 1
        try:
            self._on_connection(connection)
        except Exception as error:
            # 交给 on_error，不让一个连接的装配错误拖垮监听。
            connection.abort()
            self._on_error(error)


async def listen(
    path: str,
    key: bytes,
    *,
    on_connection: Callable[[Connection], None],
    on_rejected: Callable[[ChannelError], None],
    on_error: Callable[[BaseException], None],
    handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT,
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
    max_pending_handshakes: int = DEFAULT_MAX_PENDING_HANDSHAKES,
) -> ChannelServer:
    """在本机通道地址上监听。

    握手成功的连接交给 `on_connection`，它抛错时断开该连接并交给 `on_error`；
    握手失败（认证失败、超时、协议错误、提前断开、未认证的握手超过 `max_pending_handshakes`）
    时调用 `on_rejected`，用于告警。地址已被占用时以 `address-in-use` 失败：会话地址每次随机生成，
    出现即按可能被抢注处理，不得用同名重试。
    """
    server = ChannelServer(
        path,
        key,
        on_connection=on_connection,
        on_rejected=on_rejected,
        on_error=on_error,
        handshake_timeout=handshake_timeout,
        max_frame_bytes=max_frame_bytes,
        max_pending_handshakes=max_pending_handshakes,
    )
    await server.start()
    return server


async def connect(
    path: str,
    role: str,
    key: bytes,
    *,
    handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT,
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
) -> Connection:
    """以给定角色连接本机通道，握手成功后返回连接。服务端未能证明持有密钥时为 `auth-failed`。

    角色名不合法时在连接之前就以 `protocol-error` 失败。管道的所有实例都忙时，asyncio 会一直重试，
    这里以 `handshake_timeout` 为限，超时为 `connect-failed`。
    """
    check_session_key(key)
    if not is_valid_role(role):
        raise ChannelError("protocol-error", f"角色名不合法：{role}")
    try:
        async with asyncio.timeout(handshake_timeout):
            reader, writer = await open_stream(path)
    except TimeoutError:
        raise ChannelError("connect-failed", f"未在 {handshake_timeout}s 内连上通道") from None
    return await Connection.open(
        reader,
        writer,
        role,
        key,
        handshake_timeout=handshake_timeout,
        max_frame_bytes=max_frame_bytes,
    )
