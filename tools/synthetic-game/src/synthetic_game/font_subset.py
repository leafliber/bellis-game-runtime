"""从 Noto Sans SC 的官方发布生成合成游戏的字体子集。

在仓库根目录运行 `uv run python -m synthetic_game.font_subset`：按写死的 URL 下载原字体与许可，
核对 SHA-256，用 fontTools 按 `required_characters()` 取子集并改名，写出子集、许可全文与来源说明。
同样的原字体与同样的台词总是得到逐字节相同的子集。下载的文件只放在临时目录里，不进仓库。
fontTools 是开发依赖，只有本脚本与字体测试用到它。
"""

# fontTools 不带类型标注。
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false

import argparse
import hashlib
import json
import sys
import tempfile
import urllib.request
from collections.abc import Sequence
from pathlib import Path

from fontTools.subset import Options, Subsetter
from fontTools.ttLib import TTFont

from synthetic_game.render import FONT_DIR, FONT_FILE
from synthetic_game.story import all_text

RELEASE_TAG = "Sans2.004"
RELEASE_URL = f"https://github.com/notofonts/noto-cjk/releases/tag/{RELEASE_TAG}"
_RAW = f"https://raw.githubusercontent.com/notofonts/noto-cjk/{RELEASE_TAG}"
FONT_URL = f"{_RAW}/Sans/SubsetOTF/SC/NotoSansSC-Regular.otf"
FONT_SHA256 = "faa6c9df652116dde789d351359f3d7e5d2285a2b2a1f04a2d7244df706d5ea9"
LICENSE_URL = f"{_RAW}/LICENSE"
LICENSE_SHA256 = "6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2"

ORIGINAL_VERSION = "2.004"
ORIGINAL_POSTSCRIPT_NAME = "NotoSansSC-Regular"
FAMILY_NAME = "Synthetic Game Sans SC"
POSTSCRIPT_NAME = "SyntheticGameSansSC-Regular"

LICENSE_FILE = FONT_DIR / "OFL.txt"
SOURCE_FILE = FONT_DIR / "SOURCE.toml"

ASCII_PRINTABLE = "".join(chr(code) for code in range(0x20, 0x7F))
COMMON_PUNCTUATION = "，。、；：？！“”‘’（）《》〈〉【】「」『』—…·～　"

DOWNLOAD_TIMEOUT_SECONDS = 120
WINDOWS_ENGLISH = (3, 1, 0x409)


def required_characters() -> frozenset[str]:
    """子集包含的字：台词与界面文字用到的字、ASCII 可打印字符与常用标点。"""
    characters = set(ASCII_PRINTABLE) | set(COMMON_PUNCTUATION)
    for text in all_text():
        characters.update(text)
    return frozenset(characters)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def download(url: str, expected_sha256: str, destination: Path) -> None:
    """下载并核对摘要；摘要不符即删除文件并失败。"""
    print(f"下载 {url}", flush=True)
    if not url.startswith("https://"):
        raise ValueError(f"只从 https 下载：{url}")
    # S310：URL 是本模块写死的 https 地址，上面已核对协议。
    with (
        urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response,  # noqa: S310
        destination.open("wb") as file,
    ):
        while chunk := response.read(1 << 20):
            file.write(chunk)
    actual = sha256(destination)
    if actual != expected_sha256:
        destination.unlink()
        raise RuntimeError(f"{url} 的 SHA-256 为 {actual}，应为 {expected_sha256}")


def build_subset(source: Path, output: Path, characters: frozenset[str]) -> None:
    """取子集、改名并写出；子集的 cmap 必须恰好是 `characters`。"""
    with TTFont(source, recalcTimestamp=False) as font:
        _subset(font, characters)
        font.save(output)


def _subset(font: TTFont, characters: frozenset[str]) -> None:
    options = Options()
    options.layout_features = []  # 渲染固定用 Pillow 的基本排版引擎，不用 OpenType 特性
    # 保留全部预定义的英文名称记录：版权、商标、作者与许可等。
    options.name_IDs = list(range(26))
    options.name_languages = [WINDOWS_ENGLISH[2]]
    options.notdef_outline = True  # 缺字时画出方框而不是空白
    subsetter = Subsetter(options)
    subsetter.populate(unicodes=sorted(ord(char) for char in characters))
    subsetter.subset(font)
    _rename(font)
    covered = cmap_characters(font)
    if covered != characters:
        missing = "".join(sorted(characters - covered))
        extra = "".join(sorted(covered - characters))
        raise RuntimeError(f"子集与所需的字不一致：缺 {missing!r}，多 {extra!r}")


def cmap_characters(font: TTFont) -> frozenset[str]:
    cmap = font["cmap"].getBestCmap()
    if cmap is None:
        raise RuntimeError("字体没有 Unicode cmap")
    return frozenset(chr(code) for code in cmap)


def _rename(font: TTFont) -> None:
    """按 OFL 对修改版的要求改名：子集是修改版，主名称不再用原字体的名字。

    原字体的版权声明与许可里没有声明 Reserved Font Name，OFL 并不强制改名；仍然改名，
    是为了不让这个只有几百个字的子集以“Noto Sans SC”的名字出现（Noto 是 Google 的商标）。
    版权（0）、商标（7）、作者与许可（8–14）等记录原样保留。
    """
    name = font["name"]
    records = {
        1: FAMILY_NAME,
        3: f"{ORIGINAL_VERSION};{POSTSCRIPT_NAME}",
        4: FAMILY_NAME,
        6: POSTSCRIPT_NAME,
        10: f"Subset of Noto Sans SC Regular {ORIGINAL_VERSION} for the Bellis synthetic game.",
    }
    for name_id, value in records.items():
        name.setName(value, name_id, *WINDOWS_ENGLISH)
    cff = font["CFF "].cff
    cff.fontNames = [POSTSCRIPT_NAME]
    top = cff.topDictIndex[0]
    top.FullName = FAMILY_NAME
    top.FamilyName = FAMILY_NAME
    for font_dict in top.FDArray:
        font_dict.FontName = font_dict.FontName.replace(ORIGINAL_POSTSCRIPT_NAME, POSTSCRIPT_NAME)


def original_notices(font_path: Path) -> dict[int, str]:
    """原字体的版权（0）、版本（5）与商标（7）声明。"""
    notices: dict[int, str] = {}
    with TTFont(font_path, lazy=True) as font:
        for name_id in (0, 5, 7):
            record = font["name"].getName(name_id, *WINDOWS_ENGLISH)
            if record is None:
                raise RuntimeError(f"原字体缺少名称记录 {name_id}")
            notices[name_id] = record.toUnicode()
    return notices


def write_source_note(notices: dict[int, str], subset_path: Path, count: int) -> None:
    fields: list[tuple[str, str | int] | str] = [
        "[source]",
        ("family", "Noto Sans SC Regular"),
        ("version", notices[5]),
        ("release", RELEASE_URL),
        ("url", FONT_URL),
        ("sha256", FONT_SHA256),
        ("license_url", LICENSE_URL),
        ("license_sha256", LICENSE_SHA256),
        ("copyright", notices[0]),
        ("trademark", notices[7]),
        ("license", "SIL Open Font License 1.1，全文见 OFL.txt"),
        ("reserved_font_names", "无：原字体的版权声明与许可都没有声明 Reserved Font Name"),
        "",
        "[subset]",
        ("file", subset_path.name),
        ("family", FAMILY_NAME),
        ("postscript_name", POSTSCRIPT_NAME),
        ("sha256", sha256(subset_path)),
        ("characters", count),
    ]
    lines = [
        "# 合成游戏字体子集的来源说明。",
        "# 由 `uv run python -m synthetic_game.font_subset` 生成，不要手改。",
        "",
    ]
    for field in fields:
        if isinstance(field, str):
            lines.append(field)
        else:
            key, value = field
            # JSON 的字符串转义是 TOML 基本字符串的子集。
            lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")
    SOURCE_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m synthetic_game.font_subset",
        description="Download Noto Sans SC and regenerate the synthetic game's font subset.",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        help="directory that already holds NotoSansSC-Regular.otf and LICENSE (still verified)",
    )
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="synthetic-game-font-") as temporary:
        directory: Path = args.source_dir or Path(temporary)
        font_path = directory / "NotoSansSC-Regular.otf"
        license_path = directory / "LICENSE"
        for url, digest, path in (
            (FONT_URL, FONT_SHA256, font_path),
            (LICENSE_URL, LICENSE_SHA256, license_path),
        ):
            if args.source_dir is None:
                download(url, digest, path)
            elif sha256(path) != digest:
                raise RuntimeError(f"{path} 的 SHA-256 不是 {digest}")
        notices = original_notices(font_path)
        if not notices[5].startswith(f"Version {ORIGINAL_VERSION};"):
            raise RuntimeError(f"原字体的版本不是 {ORIGINAL_VERSION}：{notices[5]}")
        if "Reserved Font Name" in notices[0]:
            # 改名的依据是“没有声明 Reserved Font Name”；原字体变了就要重新核对命名。
            raise RuntimeError(f"原字体声明了 Reserved Font Name，须重新核对命名：{notices[0]}")
        characters = required_characters()
        FONT_DIR.mkdir(exist_ok=True)
        build_subset(font_path, FONT_FILE, characters)
        LICENSE_FILE.write_bytes(license_path.read_bytes())
        write_source_note(notices, FONT_FILE, len(characters))
    print(f"已写出 {FONT_FILE}（{len(characters)} 个字，{FONT_FILE.stat().st_size} 字节）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
