"""主进程创建的命名共享内存（非提权一方创建全部进程间对象，GD-02）。

- Windows：命名文件映射，以页面文件为后备，名字在本会话的命名空间（`Local\\`）里。
  Python 标准库的 `mmap` 与 `multiprocessing.shared_memory` 都设不了安全属性，所以用 ctypes
  以只允许当前用户的 DACL（`game_runtime._winsecurity`）调 `CreateFileMappingW` 创建，
  同名对象已存在即失败；视图仍由 `mmap` 按名字打开同一个对象，借它管理映射的生命期
  （还有导出的缓冲时 `close` 报错，不会解除映射后再访问）。最后一个句柄与视图关闭后对象消失。
- 其它平台：`shm_open(O_CREAT | O_EXCL)`，权限位只是缺省的 0600，不靠它做安全判断；`close` 时
  `shm_unlink`。不用 `multiprocessing.shared_memory`：它在 Python 3.12 里总会登记到另起的
  resource tracker 进程。

名字形如 `bellis-<用途>-<128 位随机数的 base32>`，与本机通道的地址同样取随机数；Windows 上加
`Local\\` 前缀，POSIX 上加 `/` 前缀。名字就是交给对方 `OpenFileMappingW` 或 `shm_open` 的字符串。
"""

import base64
import mmap
import os
import secrets
import sys
from collections.abc import Callable
from typing import Final

NAME_RANDOM_BYTES: Final = 16
"""名字里随机数的字节数。"""


def shared_memory_name(kind: str) -> str:
    """新的随机共享内存名：Windows 上为 `Local\\bellis-<kind>-<随机数>`，POSIX 上为 `/bellis-…`。"""
    random = base64.b32encode(secrets.token_bytes(NAME_RANDOM_BYTES)).decode("ascii")
    name = f"bellis-{kind}-{random.rstrip('=').lower()}"
    return f"Local\\{name}" if sys.platform == "win32" else f"/{name}"


class SharedMemory:
    """一块本进程创建的命名共享内存及其映射。"""

    def __init__(self, name: str, size: int, buffer: mmap.mmap, destroy: Callable[[], None]):
        self._name = name
        self._size = size
        self._buffer: mmap.mmap | None = buffer
        self._destroy = destroy

    @property
    def name(self) -> str:
        """交给对方打开的名字。"""
        return self._name

    @property
    def size(self) -> int:
        return self._size

    @property
    def buffer(self) -> mmap.mmap:
        """映射；关闭后访问报 `ValueError`。"""
        if self._buffer is None:
            raise ValueError(f"共享内存已关闭：{self._name}")
        return self._buffer

    @property
    def closed(self) -> bool:
        return self._buffer is None

    def close(self) -> None:
        """解除映射并销毁共享内存。可以重复调用。

        还有从映射导出的缓冲（ctypes 视图、memoryview）时报 `BufferError`，共享内存保持原样。
        """
        if self._buffer is None:
            return
        self._buffer.close()
        self._buffer = None
        self._destroy()


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    from typing import Any

    from game_runtime._winsecurity import (
        SecurityAttributes,
        SecurityDescriptor,
        current_user_only_sddl,
    )

    _PAGE_READWRITE = 0x04
    _ERROR_ALREADY_EXISTS = 183
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def _function(library: ctypes.WinDLL, name: str, restype: Any, *argtypes: Any) -> Any:
        function = getattr(library, name)
        function.restype = restype
        function.argtypes = argtypes
        return function

    _CreateFileMappingW = _function(
        _kernel32,
        "CreateFileMappingW",
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(SecurityAttributes),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPCWSTR,
    )
    _CloseHandle = _function(_kernel32, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)

    def create_shared_memory(name: str, size: int) -> SharedMemory:
        """以只允许当前用户的 DACL 创建名为 `name` 的文件映射。

        同名对象已存在时报 `FileExistsError`。
        """
        if size <= 0:
            raise ValueError(f"共享内存的大小须为正数：{size}")
        security = SecurityDescriptor(current_user_only_sddl())
        try:
            # 新建对象时 CreateFileMappingW 不一定清除最后错误码，
            # 先清零再判断 ERROR_ALREADY_EXISTS。
            ctypes.set_last_error(0)
            handle = _CreateFileMappingW(
                _INVALID_HANDLE_VALUE,
                ctypes.byref(security.attributes),
                _PAGE_READWRITE,
                size >> 32,
                size & 0xFFFFFFFF,
                name,
            )
            error = ctypes.get_last_error()
        finally:
            security.free()
        if not handle:
            raise ctypes.WinError(error)
        if error == _ERROR_ALREADY_EXISTS:
            _CloseHandle(handle)
            raise FileExistsError(f"共享内存名已存在，可能被抢注：{name}")
        try:
            # 按名字打开刚建的对象（对象已存在时 mmap 打开它，不新建）。
            buffer = mmap.mmap(-1, size, tagname=name)
        except BaseException:
            _CloseHandle(handle)
            raise
        return SharedMemory(name, size, buffer, lambda: _CloseHandle(handle))

else:
    import ctypes
    import ctypes.util
    from typing import Any

    def _load_libc() -> Any:
        # glibc 2.34 起 shm_open 在 libc 里，更早的在 librt 里。
        library = ctypes.CDLL(None, use_errno=True)
        if not hasattr(library, "shm_open"):
            library = ctypes.CDLL(ctypes.util.find_library("rt"), use_errno=True)
        library.shm_open.argtypes = (ctypes.c_char_p, ctypes.c_int, ctypes.c_uint)
        library.shm_open.restype = ctypes.c_int
        library.shm_unlink.argtypes = (ctypes.c_char_p,)
        library.shm_unlink.restype = ctypes.c_int
        return library

    _libc = _load_libc()

    def posix_shm_open(name: str, flags: int, mode: int = 0) -> int:
        """`shm_open`，失败时报对应的 `OSError`（如 `FileExistsError`、`FileNotFoundError`）。"""
        fd: int = _libc.shm_open(os.fsencode(name), flags, mode)
        if fd < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), name)
        return fd

    def posix_shm_unlink(name: str) -> None:
        """`shm_unlink`，失败时报对应的 `OSError`。"""
        if _libc.shm_unlink(os.fsencode(name)) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), name)

    def create_shared_memory(name: str, size: int) -> SharedMemory:
        """以 `O_CREAT | O_EXCL` 创建名为 `name` 的共享内存；同名的已存在时报 `FileExistsError`。"""
        if size <= 0:
            raise ValueError(f"共享内存的大小须为正数：{size}")
        fd = posix_shm_open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        try:
            os.ftruncate(fd, size)
            buffer = mmap.mmap(fd, size)
        except BaseException:
            posix_shm_unlink(name)
            raise
        finally:
            os.close(fd)
        return SharedMemory(name, size, buffer, lambda: posix_shm_unlink(name))
