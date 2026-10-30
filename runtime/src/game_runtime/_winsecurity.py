"""Windows 上只允许当前用户访问的安全描述符。

主进程创建的进程间对象（本机通道的命名管道、帧环的文件映射）都用它：DACL 为
`D:P(A;;GA;;;<当前用户 SID>)`，`P` 表示不继承上级 ACE。提权的实时核以同一用户运行时满足这个 DACL，
跨完整性级别的可达性由 GS-03 真机验证。这只是减少暴露，同一用户下的恶意进程不在威胁模型内
（Bellis D-12）。
"""

import sys

if sys.platform != "win32":
    raise ImportError("_winsecurity 只用于 Windows")

import ctypes
from ctypes import wintypes
from typing import Any

_ERROR_INSUFFICIENT_BUFFER = 122
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_SDDL_REVISION_1 = 1

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


class SecurityAttributes(ctypes.Structure):
    """Win32 的 `SECURITY_ATTRIBUTES`。"""

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


class SecurityDescriptor:
    """自相对安全描述符，由 LocalAlloc 分配；`free()` 之后不能再用 `attributes` 建对象。"""

    def __init__(self, sddl: str) -> None:
        descriptor = wintypes.LPVOID()
        _check(
            _ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None
            )
        )
        self._descriptor = descriptor
        self.attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)

    def free(self) -> None:
        if self._descriptor.value is not None:
            _LocalFree(ctypes.cast(self._descriptor, wintypes.HLOCAL))
            self._descriptor = wintypes.LPVOID()
