"""Windows 命名管道的服务端。

asyncio 自带的 `ProactorEventLoop.start_serving_pipe` 以消息模式、缺省安全描述符建管道，
也不拒绝远程客户端，所以这里自己用 CreateNamedPipeW 建管道实例，
接受连接仍交给 asyncio 的 IOCP proactor：

- `FILE_FLAG_FIRST_PIPE_INSTANCE`：同名管道已存在时失败（报 `address-in-use`），防止抢注；
- `PIPE_REJECT_REMOTE_CLIENTS`：拒绝经 SMB 的远程客户端；
- 字节模式，与 Node（libuv）建的管道一致；
- 安全描述符只允许当前用户（DACL `D:P(A;;GA;;;<当前用户 SID>)`）。
  提权的实时核以同一用户运行时能连上，跨完整性级别的可达性由 GS-03 真机验证。

始终保留一个尚未连接的实例，客户端在两次接受之间连接也不会找不到管道。
接受连接与建立传输用到 asyncio 的内部接口
（`_proactor.accept_pipe`、`_make_duplex_pipe_transport`），与 `start_serving_pipe`
自身的做法相同；Python 版本由 `requires-python` 锁定在 3.12。
"""

import sys

if sys.platform != "win32":
    raise ImportError("_winpipe 只用于 Windows")

import asyncio
import ctypes
from asyncio import windows_utils
from ctypes import wintypes
from typing import Any, Protocol, cast

from .errors import ChannelError
from .transport import STREAM_LIMIT, StreamCallback

_PIPE_ACCESS_DUPLEX = 0x00000003
_FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
_FILE_FLAG_OVERLAPPED = 0x40000000
_PIPE_TYPE_BYTE = 0x00000000
_PIPE_READMODE_BYTE = 0x00000000
_PIPE_WAIT = 0x00000000
_PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
_PIPE_UNLIMITED_INSTANCES = 255
_PIPE_BUFFER_BYTES = 64 * 1024
_ERROR_ACCESS_DENIED = 5
_ERROR_INSUFFICIENT_BUFFER = 122
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_SDDL_REVISION_1 = 1
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)


def _function(library: ctypes.WinDLL, name: str, restype: Any, *argtypes: Any) -> Any:
    function = getattr(library, name)
    function.restype = restype
    function.argtypes = argtypes
    return function


_GetCurrentProcess = _function(_kernel32, "GetCurrentProcess", wintypes.HANDLE)
_CloseHandle = _function(_kernel32, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
_LocalFree = _function(_kernel32, "LocalFree", wintypes.HLOCAL, wintypes.HLOCAL)
_CreateNamedPipeW = _function(
    _kernel32,
    "CreateNamedPipeW",
    wintypes.HANDLE,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
)
_OpenProcessToken = _function(
    _advapi32,
    "OpenProcessToken",
    wintypes.BOOL,
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
)
_GetTokenInformation = _function(
    _advapi32,
    "GetTokenInformation",
    wintypes.BOOL,
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
)
_ConvertSidToStringSidW = _function(
    _advapi32,
    "ConvertSidToStringSidW",
    wintypes.BOOL,
    wintypes.LPVOID,
    ctypes.POINTER(wintypes.LPWSTR),
)
_ConvertStringSecurityDescriptorToSecurityDescriptorW = _function(
    _advapi32,
    "ConvertStringSecurityDescriptorToSecurityDescriptorW",
    wintypes.BOOL,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.LPVOID),
    ctypes.POINTER(wintypes.ULONG),
)


class _SecurityAttributes(ctypes.Structure):
    _fields_ = (
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    )


def _check(ok: object) -> None:
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())


def current_user_sid() -> str:
    """当前进程令牌的用户 SID，形如 `S-1-5-21-…`。"""
    token = wintypes.HANDLE()
    _check(_OpenProcessToken(_GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)))
    try:
        size = wintypes.DWORD()
        if _GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size)):
            raise OSError("GetTokenInformation 未要求缓冲区")
        if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(size.value)
        _check(_GetTokenInformation(token, _TOKEN_USER, buffer, size, ctypes.byref(size)))
        # TOKEN_USER 的第一个字段是 SID_AND_ATTRIBUTES.Sid 指针。
        sid = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0]
        text = wintypes.LPWSTR()
        _check(_ConvertSidToStringSidW(sid, ctypes.byref(text)))
        try:
            value = text.value
        finally:
            _LocalFree(ctypes.cast(text, wintypes.HLOCAL))
    finally:
        _CloseHandle(token)
    if value is None:
        raise OSError("ConvertSidToStringSidW 返回空")
    return value


def current_user_only_sddl() -> str:
    """只允许当前用户访问的安全描述符；`P` 表示不继承上级 ACE。"""
    return f"D:P(A;;GA;;;{current_user_sid()})"


class _SecurityDescriptor:
    """自相对安全描述符，由 LocalAlloc 分配；`free()` 之前不能再建管道实例。"""

    def __init__(self, sddl: str) -> None:
        descriptor = wintypes.LPVOID()
        _check(
            _ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None
            )
        )
        self._descriptor = descriptor
        self.attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)

    def free(self) -> None:
        if self._descriptor.value is not None:
            _LocalFree(ctypes.cast(self._descriptor, wintypes.HLOCAL))
            self._descriptor = wintypes.LPVOID()


def create_pipe_instance(
    address: str, security: _SecurityAttributes, *, first: bool
) -> windows_utils.PipeHandle:
    flags = _PIPE_ACCESS_DUPLEX | _FILE_FLAG_OVERLAPPED
    if first:
        flags |= _FILE_FLAG_FIRST_PIPE_INSTANCE
    handle = _CreateNamedPipeW(
        address,
        flags,
        _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_WAIT | _PIPE_REJECT_REMOTE_CLIENTS,
        _PIPE_UNLIMITED_INSTANCES,
        _PIPE_BUFFER_BYTES,
        _PIPE_BUFFER_BYTES,
        0,
        ctypes.byref(security),
    )
    if handle is None or handle == _INVALID_HANDLE_VALUE:
        code = ctypes.get_last_error()
        if first and code == _ERROR_ACCESS_DENIED:
            raise ChannelError("address-in-use", f"通道地址已被占用，可能被抢注：{address}")
        raise ctypes.WinError(code)
    return windows_utils.PipeHandle(handle)


class _Proactor(Protocol):
    def accept_pipe(self, pipe: windows_utils.PipeHandle) -> asyncio.Future[Any]: ...


class _InternalLoop:
    """ProactorEventLoop 的两个内部接口，`start_serving_pipe` 用的也是它们。"""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        if not isinstance(loop, asyncio.ProactorEventLoop):
            raise ChannelError("address-invalid", "命名管道需要 ProactorEventLoop")
        self._loop: Any = loop

    def accept_pipe(self, pipe: windows_utils.PipeHandle) -> asyncio.Future[Any]:
        proactor = cast(_Proactor, self._loop._proactor)
        return proactor.accept_pipe(pipe)

    def make_duplex_pipe_transport(
        self,
        pipe: windows_utils.PipeHandle,
        protocol: asyncio.BaseProtocol,
        waiter: asyncio.Future[None],
        extra: dict[str, object],
    ) -> asyncio.Transport:
        transport = self._loop._make_duplex_pipe_transport(pipe, protocol, waiter, extra)
        return cast(asyncio.Transport, transport)


# 建管道实例失败（例如句柄耗尽）时，隔多久再试。
_RETRY_DELAY = 1.0


class NamedPipeListener:
    """在命名管道地址上接受连接，每个连接以（StreamReader，StreamWriter）回调 `on_stream`。"""

    def __init__(
        self, loop: asyncio.AbstractEventLoop, address: str, on_stream: StreamCallback
    ) -> None:
        self._internal = _InternalLoop(loop)
        self._loop = loop
        self._address = address
        self._on_stream = on_stream
        self._closed = False
        self._security = _SecurityDescriptor(current_user_only_sddl())
        # 全部尚未交出的实例（含正在等待连接的），关闭时一并关掉。
        self._free: set[windows_utils.PipeHandle] = set()
        self._accepting: tuple[asyncio.Future[Any], windows_utils.PipeHandle] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        try:
            self._spare = self._create(first=True)
        except BaseException:
            self._security.free()
            raise
        loop.call_soon(self._accept_next)

    def _create(self, *, first: bool) -> windows_utils.PipeHandle:
        pipe = create_pipe_instance(self._address, self._security.attributes, first=first)
        self._free.add(pipe)
        return pipe

    def _accept_next(self) -> None:
        """在备用实例上等待下一个连接，并先建好新的备用实例：地址上始终有可连接的实例。"""
        if self._closed:
            return
        try:
            pipe, self._spare = self._spare, self._create(first=False)
        except OSError as error:
            self._report(error)
            self._loop.call_later(_RETRY_DELAY, self._accept_next)
            return
        try:
            accepting = self._internal.accept_pipe(pipe)
        except OSError:
            # 客户端在 ConnectNamedPipe 之前连上又断开了。
            self._discard(pipe)
            self._loop.call_soon(self._accept_next)
            return
        self._accepting = (accepting, pipe)
        accepting.add_done_callback(self._on_accepted)

    def _on_accepted(self, done: asyncio.Future[Any]) -> None:
        if self._accepting is None:
            return
        _, pipe = self._accepting
        self._accepting = None
        try:
            done.result()
        except asyncio.CancelledError:
            self._discard(pipe)
            return
        except OSError as error:
            self._discard(pipe)
            if not isinstance(error, BrokenPipeError):
                self._report(error)
        else:
            self._free.discard(pipe)
            if self._closed:
                pipe.close()
                return
            task = self._loop.create_task(self._connected(pipe))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        self._accept_next()

    def _report(self, error: OSError) -> None:
        if not self._closed:
            self._loop.call_exception_handler(
                {"message": f"命名管道接受连接失败：{self._address}", "exception": error}
            )

    def _discard(self, pipe: windows_utils.PipeHandle) -> None:
        self._free.discard(pipe)
        pipe.close()  # 可以重复调用

    async def _connected(self, pipe: windows_utils.PipeHandle) -> None:
        reader = asyncio.StreamReader(limit=STREAM_LIMIT, loop=self._loop)
        protocol = asyncio.StreamReaderProtocol(reader, loop=self._loop)
        waiter: asyncio.Future[None] = self._loop.create_future()
        transport = self._internal.make_duplex_pipe_transport(
            pipe, protocol, waiter, {"addr": self._address}
        )
        try:
            await waiter
        except BaseException:
            transport.abort()
            raise
        self._on_stream(reader, asyncio.StreamWriter(transport, protocol, reader, self._loop))

    async def close(self) -> None:
        """停止接受连接并关闭全部尚未交出的实例；已交出的连接由上层断开。"""
        if self._closed:
            return
        self._closed = True
        if self._accepting is not None:
            self._accepting[0].cancel()
        for pipe in list(self._free):
            self._discard(pipe)
        for task in list(self._tasks):
            task.cancel()
        self._security.free()
