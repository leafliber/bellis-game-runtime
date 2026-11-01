"""操作员控制台测试的共用夹具。"""

import sys
from pathlib import Path

import pytest


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """临时的本地数据目录。Windows 上加扩展长度前缀：会话记录在本地数据目录下几层，临时目录较深而
    系统没有开长路径支持时会超过 MAX_PATH。"""
    if sys.platform == "win32":
        return Path("\\\\?\\" + str(tmp_path.resolve())) / "data"
    return tmp_path / "data"
