"""凭据管理器的写入、读取与删除（GS-03 的第 3 项的非提权一侧）。

命名与编码同 Bellis 的 SecretStore（`@napi-rs/keyring` + `windows-native-keyring-store`）：探针
据此写入，提权一侧（`gs03-core-probe` 用 `CredReadW`）读取，验证“提权进程能读到同一用户的
SecretStore 条目”。

- 服务 `bellis`、条目名 `<name>`，目标名为 `<name>.<service>`（分隔符 `.`，无前后缀）；
- 通用凭据（`CRED_TYPE_GENERIC`），值以 UTF-16LE 存在 `CredentialBlob` 里，企业级持久化
  （`CRED_PERSIST_ENTERPRISE`，同 keyring-rs 的默认）。

这些是当前用户自己的凭据，探针结束即删除。只在 Windows 上可用。
"""

import re
import sys

#: 凭据服务名，同 Bellis SecretStore。
DEFAULT_SERVICE = "bellis"
#: 本探针的凭据条目名。
DEFAULT_NAME = "gs03-probe"

# 凭据名的规则同 Bellis SecretStore：小写字母数字起头，其后可含 `._-`。
_SECRET_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


def check_name(name: str) -> str:
    """校验条目名；不合法时报错。"""
    if _SECRET_NAME.fullmatch(name) is None:
        raise ValueError(f"凭据名不合法：{name}")
    return name


def target_name(service: str, name: str) -> str:
    """凭据目标名，同 keyring-rs 默认的 `<user>.<service>`（这里 user 即条目名）。"""
    return f"{check_name(name)}.{service}"


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    from typing import Any

    _CRED_TYPE_GENERIC = 1
    _CRED_PERSIST_ENTERPRISE = 3
    _ERROR_NOT_FOUND = 1168

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    class _FileTime(ctypes.Structure):
        _fields_ = (
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        )

    class _CredentialW(ctypes.Structure):
        _fields_ = (
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", _FileTime),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        )

    def _function(name: str, restype: Any, *argtypes: Any) -> Any:
        function = getattr(_advapi32, name)
        function.restype = restype
        function.argtypes = argtypes
        return function

    _CredWriteW = _function(
        "CredWriteW", wintypes.BOOL, ctypes.POINTER(_CredentialW), wintypes.DWORD
    )
    _CredReadW = _function(
        "CredReadW",
        wintypes.BOOL,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CredentialW)),
    )
    _CredDeleteW = _function(
        "CredDeleteW", wintypes.BOOL, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD
    )
    _CredFree = _function("CredFree", None, ctypes.c_void_p)

    def write_secret(service: str, name: str, value: str) -> None:
        """写入（或覆盖）条目，值以 UTF-16LE 存储。"""
        target = target_name(service, name)
        blob = value.encode("utf-16-le")
        buffer = (ctypes.c_byte * len(blob)).from_buffer_copy(blob)
        credential = _CredentialW(
            Flags=0,
            Type=_CRED_TYPE_GENERIC,
            TargetName=target,
            Comment=None,
            CredentialBlobSize=len(blob),
            CredentialBlob=ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
            Persist=_CRED_PERSIST_ENTERPRISE,
            AttributeCount=0,
            Attributes=None,
            TargetAlias=None,
            UserName=name,
        )
        if not _CredWriteW(ctypes.byref(credential), 0):
            raise ctypes.WinError(ctypes.get_last_error())

    def read_secret(service: str, name: str) -> str | None:
        """读取条目的值；不存在时为 `None`。"""
        target = target_name(service, name)
        pointer = ctypes.POINTER(_CredentialW)()
        if not _CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == _ERROR_NOT_FOUND:
                return None
            raise ctypes.WinError(error)
        try:
            credential = pointer.contents
            size = credential.CredentialBlobSize
            if size == 0:
                return ""
            raw = ctypes.string_at(credential.CredentialBlob, size)
            return raw.decode("utf-16-le")
        finally:
            _CredFree(pointer)

    def delete_secret(service: str, name: str) -> bool:
        """删除条目；本就不存在时返回 `False`。"""
        target = target_name(service, name)
        if not _CredDeleteW(target, _CRED_TYPE_GENERIC, 0):
            error = ctypes.get_last_error()
            if error == _ERROR_NOT_FOUND:
                return False
            raise ctypes.WinError(error)
        return True

else:

    def write_secret(service: str, name: str, value: str) -> None:
        raise OSError("凭据管理器只在 Windows 上可用")

    def read_secret(service: str, name: str) -> str | None:
        raise OSError("凭据管理器只在 Windows 上可用")

    def delete_secret(service: str, name: str) -> bool:
        raise OSError("凭据管理器只在 Windows 上可用")
