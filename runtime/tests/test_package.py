"""冒烟测试：工作区以可编辑方式安装了本仓库的 `game_runtime`。"""

from pathlib import Path

import game_runtime

RUNTIME_SRC = Path(__file__).resolve().parents[1] / "src"


def test_game_runtime_is_imported_from_workspace_source() -> None:
    module_file = Path(game_runtime.__file__).resolve()
    assert module_file.is_relative_to(RUNTIME_SRC), module_file
