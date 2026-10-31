"""本地数据目录：版本库外、只在本机，存放从游戏派生的数据、模型制品与运行数据。

缺省位置与环境变量见 `docs/architecture.md` 的“术语”。
"""

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final

DATA_DIR_ENV: Final = "BELLIS_GAME_RUNTIME_DATA"
"""改变本地数据目录位置的环境变量，值须为绝对路径。"""


class LocalDataError(Exception):
    """本地数据目录的位置定不下来：环境变量不是绝对路径，或 Windows 上没有 LOCALAPPDATA。"""


def local_data_dir(environ: Mapping[str, str] | None = None) -> Path:
    """本地数据目录的路径；不创建目录。

    `BELLIS_GAME_RUNTIME_DATA` 非空时取它；否则 Windows 上为 `%LOCALAPPDATA%\\BellisGameRuntime`，
    其它平台为 `$XDG_DATA_HOME/bellis-game-runtime`，`XDG_DATA_HOME` 未设或不是绝对路径时
    （XDG 规范要求忽略相对路径）为 `~/.local/share/bellis-game-runtime`。
    """
    env = os.environ if environ is None else environ
    override = env.get(DATA_DIR_ENV)
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise LocalDataError(f"{DATA_DIR_ENV} 须为绝对路径：{override}")
        return path
    if sys.platform == "win32":
        base = env.get("LOCALAPPDATA")
        if not base or not Path(base).is_absolute():
            raise LocalDataError(f"没有 LOCALAPPDATA，也没有设 {DATA_DIR_ENV}")
        return Path(base) / "BellisGameRuntime"
    xdg = env.get("XDG_DATA_HOME")
    base_dir = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".local" / "share"
    return base_dir / "bellis-game-runtime"
