"""跨插件共用的未完整标定适配包，仅用于离线测试。"""

from pathlib import Path

import pytest

from game_runtime.adapter import Adapter, load_adapter


@pytest.fixture(scope="session")
def fixture_games() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def partial_adapter(fixture_games: Path) -> Adapter:
    return load_adapter(fixture_games / "partial-sample")
