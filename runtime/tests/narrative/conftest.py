"""剧情记录测试的共用夹具。"""

import sys
from pathlib import Path

import pytest

from game_runtime.scene import store_template
from synthetic_game.anchors import anchor_templates


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """测试的临时目录。Windows 上加扩展长度前缀：模板路径带 64 位摘要，临时目录较深而系统没有
    开长路径支持时会超过 MAX_PATH。"""
    if sys.platform == "win32":
        return Path("\\\\?\\" + str(tmp_path.resolve()))
    return tmp_path


@pytest.fixture
def data_dir(root: Path) -> Path:
    """临时的本地数据目录，里面是从合成游戏渲染裁出的锚点模板。"""
    directory = root / "data"
    for template in anchor_templates():
        assert store_template(directory, "synthetic-game", template.png) == template.sha256
    return directory
