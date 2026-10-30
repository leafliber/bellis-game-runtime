"""凭据管理器的写入与读取在非提权下往返（GS-03 第 3 项的非提权侧）。

是当前用户自己的凭据，用一次性的服务名，测完即删，不碰真实的 `bellis` 条目。只在 Windows 上运行。
"""

import secrets
import sys

import pytest

from gs03_probe import secret

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="凭据管理器只在 Windows 上可用")


def test_target_name_follows_the_convention() -> None:
    assert secret.target_name("bellis", "gs03-probe") == "gs03-probe.bellis"


def test_round_trip_write_read_delete() -> None:
    service = f"bellis-gs03-test-{secrets.token_hex(4)}"
    name = "probe"
    value = '{"v":1,"channel_key":"abc","中文":"✓"}'
    assert secret.read_secret(service, name) is None
    secret.write_secret(service, name, value)
    try:
        assert secret.read_secret(service, name) == value
        # 覆盖写。
        secret.write_secret(service, name, "v2")
        assert secret.read_secret(service, name) == "v2"
    finally:
        assert secret.delete_secret(service, name) is True
    assert secret.delete_secret(service, name) is False
    assert secret.read_secret(service, name) is None


def test_rejects_bad_name() -> None:
    with pytest.raises(ValueError, match="凭据名不合法"):
        secret.target_name("bellis", "../escape")
