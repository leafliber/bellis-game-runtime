"""本机通道测试的共用夹具。"""

import json
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from game_runtime.channel import ChannelCredentials, session_credentials

REPO_ROOT = Path(__file__).resolve().parents[3]
VECTORS_PATH = REPO_ROOT / "core" / "crates" / "bellis-channel" / "testdata" / "vectors.json"


@pytest.fixture(scope="session")
def vectors() -> dict[str, Any]:
    """从 Bellis 的 TypeScript 实现导出的已知答案向量。"""
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def credentials() -> Iterator[ChannelCredentials]:
    """随机的会话地址与密钥；其它平台上 UDS 在私有临时目录里，用完删除。"""
    with session_credentials(f"t{secrets.token_hex(4)}") as created:
        yield created
