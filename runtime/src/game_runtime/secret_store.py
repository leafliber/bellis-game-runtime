"""SecretStore：当前用户的凭据存储，命名与编码同 Bellis 的 `SecretStore`
（`packages/secret-store`），以及操作员控制台本次会话的连接信息（`SessionInfo`）。

设计见 `docs/modules/game-bridge.md` 的“操作员控制台”：控制台启动时把新生成的通道地址与
密钥写进 `operator-session` 条目（服务名 `bellis-game-runtime`），格式同 Bellis 的
`SessionInfo`，退出时删除；主进程读取它连上控制台。

- Windows 上是凭据管理器（`CredentialManagerStore`），与 Bellis 用的 `@napi-rs/keyring`
  相同：通用凭据，目标名 `<条目名>.<服务名>`，值以 UTF-16LE 存在 `CredentialBlob` 里，
  企业级持久化。
- 其它平台只有测试用的内存实现（`MemorySecretStore`）；真实存储 unsupported，构造时报错。

只防其他用户，不防同一用户下的其他程序，与威胁模型一致（Bellis D-12）。
"""

import json
import re
import sys
from datetime import UTC, datetime
from typing import Annotated, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from game_runtime.channel import ChannelCredentials

SERVICE_NAME: Final = "bellis-game-runtime"
"""Game Runtime 的凭据服务名。"""
OPERATOR_SESSION_ENTRY: Final = "operator-session"
"""操作员控制台本次会话的连接信息的条目名。"""

_SECRET_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
"""条目名的规则，同 Bellis SecretStore。"""


class SecretStoreError(Exception):
    """凭据存储读写失败，或存的内容不合法。说明里不含存的值。"""


class SecretStoreUnsupportedError(SecretStoreError):
    """这个平台上没有真实的凭据存储（只有 Windows 的凭据管理器）。"""


def check_name(name: str) -> str:
    """校验条目名；不合法时抛 `ValueError`。"""
    if _SECRET_NAME.fullmatch(name) is None:
        raise ValueError(f"凭据名不合法：{name}")
    return name


class SecretStore(Protocol):
    """凭据存储的接口，同 Bellis `SecretStore` 的 get、set、delete。"""

    def get(self, name: str) -> str | None:
        """条目的值；不存在时为 None。"""
        ...

    def set(self, name: str, value: str) -> None:
        """写入或覆盖条目。"""
        ...

    def delete(self, name: str) -> bool:
        """删除条目，返回是否确实删除了一项。"""
        ...


class MemorySecretStore:
    """进程内存储，只用于测试。"""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self._values.get(check_name(name))

    def set(self, name: str, value: str) -> None:
        self._values[check_name(name)] = value

    def delete(self, name: str) -> bool:
        return self._values.pop(check_name(name), None) is not None


def target_name(service: str, name: str) -> str:
    """凭据管理器里的目标名，同 keyring-rs 缺省的 `<user>.<service>`（user 即条目名）。"""
    return f"{check_name(name)}.{service}"


class CredentialManagerStore:
    """Windows 凭据管理器。其它平台上构造即抛 `SecretStoreUnsupportedError`。"""

    def __init__(self, service: str = SERVICE_NAME) -> None:
        self.service = service
        if sys.platform != "win32":
            raise SecretStoreUnsupportedError(
                "凭据管理器只在 Windows 上可用；这个平台的真实存储 unsupported"
            )

    def get(self, name: str) -> str | None:
        return _read(target_name(self.service, name))

    def set(self, name: str, value: str) -> None:
        _write(target_name(self.service, name), check_name(name), value)

    def delete(self, name: str) -> bool:
        return _delete(target_name(self.service, name))


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

    _cred_write = _function(
        "CredWriteW", wintypes.BOOL, ctypes.POINTER(_CredentialW), wintypes.DWORD
    )
    _cred_read = _function(
        "CredReadW",
        wintypes.BOOL,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CredentialW)),
    )
    _cred_delete = _function(
        "CredDeleteW", wintypes.BOOL, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD
    )
    _cred_free = _function("CredFree", None, ctypes.c_void_p)

    def _failure(operation: str) -> SecretStoreError:
        error = ctypes.WinError(ctypes.get_last_error())
        return SecretStoreError(f"凭据管理器的 {operation} 失败：{error}")

    def _write(target: str, user: str, value: str) -> None:
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
            UserName=user,
        )
        if not _cred_write(ctypes.byref(credential), 0):
            raise _failure("CredWriteW")

    def _read(target: str) -> str | None:
        pointer = ctypes.POINTER(_CredentialW)()
        if not _cred_read(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            if ctypes.get_last_error() == _ERROR_NOT_FOUND:
                return None
            raise _failure("CredReadW")
        try:
            credential = pointer.contents
            size = credential.CredentialBlobSize
            if size == 0:
                return ""
            raw = ctypes.string_at(credential.CredentialBlob, size)
        finally:
            _cred_free(pointer)
        try:
            return raw.decode("utf-16-le")
        except UnicodeDecodeError:
            raise SecretStoreError(f"凭据 {target} 不是 UTF-16LE 文本") from None

    def _delete(target: str) -> bool:
        if not _cred_delete(target, _CRED_TYPE_GENERIC, 0):
            if ctypes.get_last_error() == _ERROR_NOT_FOUND:
                return False
            raise _failure("CredDeleteW")
        return True

else:

    def _unsupported() -> SecretStoreUnsupportedError:
        return SecretStoreUnsupportedError("凭据管理器只在 Windows 上可用")

    def _write(target: str, user: str, value: str) -> None:
        raise _unsupported()

    def _read(target: str) -> str | None:
        raise _unsupported()

    def _delete(target: str) -> bool:
        raise _unsupported()


# ---------------------------------------------------------------- SessionInfo

_DATE_TIME = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})"
)
"""RFC 3339 的 date-time，Bellis 以 TypeBox 的 `format: 'date-time'` 校验。"""


class SessionInfo(BaseModel):
    """本次会话的连接信息，字段与约束同 Bellis `packages/protocol/src/process.ts` 的 `SessionInfo`：
    封闭对象，JSON 的键为驼峰。`repr` 不含密钥。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    v: Literal[1] = 1
    session_id: Annotated[str, Field(alias="sessionId", pattern=r"^[a-z0-9]{1,16}$")]
    channel_path: Annotated[str, Field(alias="channelPath", min_length=1, max_length=260)]
    channel_key: Annotated[
        str, Field(alias="channelKey", pattern=r"^[A-Za-z0-9_-]{43}$", repr=False)
    ]
    """会话密钥的 base64url 编码。"""
    launcher_pid: Annotated[int, Field(alias="launcherPid", ge=1)]
    """写入它的进程（操作员控制台）的进程号。"""
    started_at: Annotated[str, Field(alias="startedAt")]
    """开始时刻，RFC 3339 的 date-time，如 `2026-11-01T08:00:00.000Z`。"""

    @field_validator("started_at")
    @classmethod
    def _date_time(cls, value: str) -> str:
        if _DATE_TIME.fullmatch(value) is None:
            raise ValueError("须为 RFC 3339 的 date-time")
        try:
            datetime.fromisoformat(value.replace("z", "Z").replace("t", "T"))
        except ValueError:
            raise ValueError("须为 RFC 3339 的 date-time") from None
        return value

    @classmethod
    def create(
        cls,
        credentials: ChannelCredentials,
        session_id: str,
        *,
        launcher_pid: int,
        started_at: datetime,
    ) -> "SessionInfo":
        """由通道的地址与密钥生成；`started_at` 须带时区，写成 UTC、毫秒
        （同 JS 的 `toISOString`）。"""
        if started_at.tzinfo is None:
            raise ValueError("started_at 须带时区")
        text = started_at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return cls.model_validate(
            {
                "v": 1,
                "sessionId": session_id,
                "channelPath": credentials.path,
                "channelKey": credentials.encoded_key,
                "launcherPid": launcher_pid,
                "startedAt": text,
            }
        )

    def credentials(self) -> ChannelCredentials:
        """通道的地址与密钥。"""
        return ChannelCredentials.decode(self.channel_path, self.channel_key)

    def to_json(self) -> str:
        """存进 SecretStore 的文本：紧凑的 JSON，键序同 Bellis 的 `JSON.stringify`。"""
        return json.dumps(self.model_dump(by_alias=True), ensure_ascii=False, separators=(",", ":"))


def write_session_info(store: SecretStore, info: SessionInfo) -> None:
    """把操作员控制台本次会话的连接信息写进 `operator-session` 条目，已有的覆盖。"""
    store.set(OPERATOR_SESSION_ENTRY, info.to_json())


def read_session_info(store: SecretStore) -> SessionInfo | None:
    """读取操作员控制台本次会话的连接信息；没有时为 None，内容不合法时抛 `SecretStoreError`
    （说明里只有出错的字段名，不含存的值）。"""
    text = store.get(OPERATOR_SESSION_ENTRY)
    if text is None:
        return None
    try:
        return SessionInfo.model_validate(json.loads(text))
    except json.JSONDecodeError:
        raise SecretStoreError("SecretStore 中的会话信息不是 JSON") from None
    except ValidationError as error:
        fields = sorted(
            {".".join(str(part) for part in e["loc"]) or "(整体)" for e in error.errors()}
        )
        raise SecretStoreError(f"SecretStore 中的会话信息不合法：{'、'.join(fields)}") from None


def delete_session_info(store: SecretStore) -> bool:
    """删除操作员控制台本次会话的连接信息，返回是否确实删除了一项。"""
    return store.delete(OPERATOR_SESSION_ENTRY)
