"""模板文件：按 SHA-256 存放、读取时核对；本地数据目录的位置。

规则见 `docs/modules/game-adapter.md` 的“模板文件”与 `docs/architecture.md` 的“术语”。
模板都从合成游戏的渲染裁出或在测试里合成。
"""

import hashlib
import sys
from pathlib import Path

import numpy as np
import pytest

from game_runtime.local_data import DATA_DIR_ENV, LocalDataError, local_data_dir
from game_runtime.scene import TemplateError, load_template, store_template, template_path
from synthetic_game.anchors import anchor_templates, encode_png

GAME = "synthetic-game"


def png(width: int, height: int, value: int) -> bytes:
    rows = [bytes([(value + x) % 256 for x in range(width * 3)]) for _ in range(height)]
    return encode_png(width, height, rows)


def test_templates_are_stored_by_digest_and_read_back(root: Path) -> None:
    [template] = anchor_templates()

    sha256 = store_template(root, GAME, template.png)
    path = template_path(root, GAME, sha256)
    assert sha256 == template.sha256 == hashlib.sha256(template.png).hexdigest()
    assert path == root / "templates" / GAME / f"{sha256}.png"
    assert path.read_bytes() == template.png
    # 已在且内容相符时不再写；目录里没有留下临时文件。
    assert store_template(root, GAME, template.png) == sha256
    assert [p.name for p in path.parent.iterdir()] == [path.name]

    image = load_template(root, GAME, sha256)
    assert image.shape == (template.rect.height, template.rect.width, 3)
    assert image.dtype == np.uint8
    assert tuple(image[0, 0]) == (38, 26, 24)  # BGR：钟楼的面板色


def test_a_missing_template_is_reported(root: Path) -> None:
    with pytest.raises(TemplateError, match="没有模板文件"):
        load_template(root, GAME, "0" * 64)


def test_a_template_whose_content_does_not_match_its_name_is_rejected(root: Path) -> None:
    sha256 = store_template(root, GAME, png(4, 4, 10))
    path = template_path(root, GAME, sha256)
    path.write_bytes(png(4, 4, 11))

    with pytest.raises(TemplateError, match="摘要不符"):
        load_template(root, GAME, sha256)


def test_only_decodable_png_with_variation_is_accepted(root: Path) -> None:
    def put(data: bytes) -> str:
        sha256 = hashlib.sha256(data).hexdigest()
        path = template_path(root, GAME, sha256)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return sha256

    with pytest.raises(TemplateError, match="不是 PNG"):
        load_template(root, GAME, put(b"GIF89a not a png"))
    with pytest.raises(TemplateError, match="解不开"):
        load_template(root, GAME, put(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20))
    flat = encode_png(3, 2, [bytes([7, 8, 9]) * 3] * 2)
    with pytest.raises(TemplateError, match="全部像素相同"):
        load_template(root, GAME, put(flat))
    with pytest.raises(ValueError, match="须为 PNG"):
        store_template(root, GAME, b"not a png")


def test_paths_are_checked() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        template_path(Path("/data"), GAME, "ABC")
    with pytest.raises(ValueError, match="适配包 id"):
        template_path(Path("/data"), "../escape", "0" * 64)


def test_the_local_data_directory_follows_the_environment(root: Path) -> None:
    assert local_data_dir({DATA_DIR_ENV: str(root)}) == root
    with pytest.raises(LocalDataError, match="绝对路径"):
        local_data_dir({DATA_DIR_ENV: "relative/dir"})
    if sys.platform == "win32":
        base = str(root)
        assert local_data_dir({"LOCALAPPDATA": base}) == root / "BellisGameRuntime"
        with pytest.raises(LocalDataError, match="LOCALAPPDATA"):
            local_data_dir({})
    else:
        xdg = {"XDG_DATA_HOME": str(root)}
        assert local_data_dir(xdg) == root / "bellis-game-runtime"
        default = Path.home() / ".local" / "share" / "bellis-game-runtime"
        assert local_data_dir({}) == default
        assert local_data_dir({"XDG_DATA_HOME": "relative"}) == default
