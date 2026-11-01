"""SecretStore 与 `SessionInfo`：条目名的规则、内存实现、与 Bellis 相同的字段约束与编码，读到不合法
的内容时报错而不泄露值；Windows 凭据管理器的往返（只在 Windows 上，用一次性的服务名，不碰真实的
`bellis-game-runtime` 条目）。"""

import json
import secrets
import sys
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from game_runtime.channel import ChannelCredentials, create_session_key
from game_runtime.secret_store import (
    OPERATOR_SESSION_ENTRY,
    SERVICE_NAME,
    CredentialManagerStore,
    MemorySecretStore,
    SecretStoreError,
    SecretStoreUnsupportedError,
    SessionInfo,
    check_name,
    delete_session_info,
    read_session_info,
    target_name,
    write_session_info,
)

CREDENTIALS = ChannelCredentials(
    "\\\\.\\pipe\\bellis-op1234-abcdefghijklmnopqrstuvwxyz", create_session_key()
)
STARTED = datetime(2026, 11, 1, 8, 30, 15, 250000, tzinfo=UTC)


def info() -> SessionInfo:
    return SessionInfo.create(CREDENTIALS, "op1234", launcher_pid=4242, started_at=STARTED)


def test_names_follow_bellis() -> None:
    assert check_name("operator-session") == "operator-session"
    assert target_name(SERVICE_NAME, OPERATOR_SESSION_ENTRY) == (
        "operator-session.bellis-game-runtime"
    )
    for bad in ("", "Upper", "-lead", "a" * 65, "../escape", "空格"):
        with pytest.raises(ValueError, match="凭据名不合法"):
            check_name(bad)


def test_the_memory_store_gets_sets_and_deletes() -> None:
    store = MemorySecretStore()
    assert store.get("x") is None
    store.set("x", "1")
    store.set("x", "2")
    assert store.get("x") == "2"
    assert store.delete("x") is True
    assert store.delete("x") is False
    with pytest.raises(ValueError, match="凭据名不合法"):
        store.set("Bad", "1")


def test_session_info_is_encoded_like_bellis() -> None:
    value = info()
    text = value.to_json()
    # 紧凑的 JSON，键序同 Bellis 的 SessionInfo，时刻同 JS 的 toISOString。
    assert list(json.loads(text)) == [
        "v",
        "sessionId",
        "channelPath",
        "channelKey",
        "launcherPid",
        "startedAt",
    ]
    assert json.loads(text)["startedAt"] == "2026-11-01T08:30:15.250Z"
    assert ", " not in text
    assert value.credentials() == CREDENTIALS
    # repr 不含密钥。
    assert CREDENTIALS.encoded_key not in repr(value)
    plus8 = SessionInfo.create(
        CREDENTIALS,
        "op1234",
        launcher_pid=1,
        started_at=STARTED.astimezone(timezone(timedelta(hours=8))),
    )
    assert plus8.started_at == "2026-11-01T08:30:15.250Z"
    with pytest.raises(ValueError, match="时区"):
        SessionInfo.create(CREDENTIALS, "op1", launcher_pid=1, started_at=datetime(2026, 1, 1))


def test_the_store_round_trips_and_deletes_the_session_info() -> None:
    store = MemorySecretStore()
    assert read_session_info(store) is None
    write_session_info(store, info())
    assert store.get(OPERATOR_SESSION_ENTRY) == info().to_json()
    assert read_session_info(store) == info()
    assert delete_session_info(store) is True
    assert read_session_info(store) is None
    assert delete_session_info(store) is False


def bad_variants() -> list[dict[str, Any]]:
    good: dict[str, Any] = json.loads(info().to_json())
    variants: list[dict[str, Any]] = []
    for key, value in (
        ("v", 2),
        ("v", "1"),
        ("sessionId", "OP1"),
        ("sessionId", "a" * 17),
        ("channelPath", ""),
        ("channelPath", "x" * 261),
        ("channelKey", "short"),
        ("launcherPid", 0),
        ("launcherPid", "42"),
        ("startedAt", "yesterday"),
        ("startedAt", "2026-11-01T08:30:15"),
        ("startedAt", "2026-13-01T08:30:15Z"),
    ):
        variants.append({**good, key: value})
    variants.append({**good, "extra": True})
    missing = dict(good)
    del missing["launcherPid"]
    variants.append(missing)
    return variants


@pytest.mark.parametrize("value", bad_variants())
def test_invalid_session_info_is_refused_without_its_values(value: dict[str, Any]) -> None:
    store = MemorySecretStore()
    store.set(OPERATOR_SESSION_ENTRY, json.dumps(value))
    with pytest.raises(SecretStoreError, match="不合法") as error:
        read_session_info(store)
    assert CREDENTIALS.encoded_key not in str(error.value)


def test_text_that_is_not_json_is_refused() -> None:
    store = MemorySecretStore()
    store.set(OPERATOR_SESSION_ENTRY, "{not json")
    with pytest.raises(SecretStoreError, match="不是 JSON"):
        read_session_info(store)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 上有凭据管理器")
def test_there_is_no_real_store_off_windows() -> None:
    with pytest.raises(SecretStoreUnsupportedError, match="unsupported"):
        CredentialManagerStore()


@pytest.mark.skipif(sys.platform != "win32", reason="凭据管理器只在 Windows 上可用")
def test_the_credential_manager_round_trips_under_a_throwaway_service() -> None:
    service = f"bellis-game-runtime-test-{secrets.token_hex(4)}"
    store = CredentialManagerStore(service)
    assert store.get(OPERATOR_SESSION_ENTRY) is None
    try:
        write_session_info(store, info())
        assert read_session_info(store) == info()
        store.set("probe", "中文 ✓")
        assert store.get("probe") == "中文 ✓"
        store.set("probe", "")
        assert store.get("probe") == ""
    finally:
        delete_session_info(store)
        store.delete("probe")
    assert read_session_info(store) is None
    assert store.delete("probe") is False
