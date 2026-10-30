"""字体子集：台词用到的每个字都在子集的 cmap 里，改了台词而没重新生成子集即失败。

另核对来源说明与文件一致、许可是未改动的 OFL 全文。
"""

# fontTools 不带类型标注。
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false

import tomllib
from collections.abc import Iterator

import pytest
from fontTools.ttLib import TTFont
from synthetic_game.font_subset import (
    FAMILY_NAME,
    FONT_SHA256,
    FONT_URL,
    LICENSE_FILE,
    LICENSE_SHA256,
    SOURCE_FILE,
    cmap_characters,
    required_characters,
    sha256,
)
from synthetic_game.render import FONT_FILE
from synthetic_game.story import all_text

REGENERATE = "uv run python -m synthetic_game.font_subset"


@pytest.fixture(scope="module")
def font() -> Iterator[TTFont]:
    with TTFont(FONT_FILE) as font:
        yield font


def test_every_character_of_the_text_is_in_the_subset_cmap(font: TTFont) -> None:
    covered = cmap_characters(font)

    for text in all_text():
        missing = sorted(set(text) - covered)
        assert not missing, f"{text!r} 用到的 {missing} 不在字体子集里，运行 {REGENERATE}"


def test_subset_is_regenerated_whenever_the_text_changes(font: TTFont) -> None:
    covered = cmap_characters(font)
    required = required_characters()

    missing, extra = sorted(required - covered), sorted(covered - required)
    assert covered == required, (
        f"字体子集与台词不一致（缺 {missing}，多 {extra}），运行 {REGENERATE}"
    )


def test_subset_is_renamed_and_keeps_the_notices(font: TTFont) -> None:
    names = font["name"]

    def name(name_id: int) -> str:
        record = names.getName(name_id, 3, 1, 0x409)
        assert record is not None, name_id
        return record.toUnicode()

    assert name(1) == FAMILY_NAME
    assert "Noto" not in name(4)
    assert name(0).startswith("© 2014-2021 Adobe")
    assert "SIL Open Font License" in name(13)


def test_source_note_matches_the_files() -> None:
    note = tomllib.loads(SOURCE_FILE.read_text(encoding="utf-8"))

    assert note["source"]["url"] == FONT_URL
    assert note["source"]["sha256"] == FONT_SHA256
    assert note["source"]["license_sha256"] == LICENSE_SHA256
    assert note["subset"]["file"] == FONT_FILE.name
    assert note["subset"]["sha256"] == sha256(FONT_FILE)
    assert note["subset"]["characters"] == len(required_characters())


def test_license_is_the_unmodified_ofl_text() -> None:
    assert sha256(LICENSE_FILE) == LICENSE_SHA256
    assert "SIL OPEN FONT LICENSE Version 1.1" in LICENSE_FILE.read_text(encoding="ascii")
