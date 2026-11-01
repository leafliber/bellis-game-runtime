"""GS-04 的泛化样本：原创的对白按多种字体、字号、底色与字形（描边、阴影）渲染在对白框的位置。

组成见 `docs/modules/perception.md` 的“GS-04 的评测工具”。字体取本机系统自带的中文字体，就地读取，
不复制、不下载；一种也没有时退回合成游戏的字体子集，只用它覆盖得到的句子。
"""

# fontTools 不带类型标注。
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false

import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from fontTools.ttLib import TTFont
from numpy.typing import NDArray
from PIL import Image, ImageDraw, ImageFont

from game_runtime.adapter import PixelRect
from synthetic_game.render import FONT_FILE
from synthetic_game.story import NODES, Choice, Dialogue, Ending

CATEGORY = "general"
PARTIAL_CATEGORY = "general-partial"
IMAGES_DIR = "general"

LINES = (
    "今天先到这里吧，明天见。",
    "你终于来了！我在这儿等了快一个小时。",
    "「这把钥匙……是从哪里找到的？」",
    "第3章　雾中的车站",
    "如果你愿意的话，我们可以一起去看看那座旧灯塔，听说晚上会有人在那里点灯。",
    "嗯，好吧。",
    "请输入房间号：A7-305",
    "他没有回答，只是把信放回了抽屉里。",
    "等等，这条路我们是不是已经走过两次了？",
    "获得道具：铜色的旧怀表 ×1",
    "雨下得越来越大，窗外的路灯一盏接一盏地熄灭了。",
    "（门的另一边，好像有人在小声说话。）",
    "真的吗？太好了！那我现在就去告诉大家。",
    "已经是第十二次失败了——可是我还不想放弃。",
    "Lv.15 的话，应该能打开北边的大门。",
    "她看着远处的山，轻声说：“春天快要到了。”",
    "这里禁止通行，请从左侧的楼梯绕行。",
    "我记得那天晚上的月亮特别圆，我们在河边一直坐到天亮，谁也没有提起离开的事。",
    "好，就这么办。",
    "所以说，问题根本不在钥匙上，而在那扇门本身。",
)
"""为评测原创的对白，不取任何游戏的台词。"""


@dataclass(frozen=True)
class FontChoice:
    tag: str
    """标签 `font` 的取值。"""
    name: str
    path: Path
    index: int = 0
    """TTC 里的序号。"""


SYSTEM_FONTS = (
    ("msyh", "微软雅黑", "msyh.ttc"),
    ("msyh-bold", "微软雅黑粗体", "msyhbd.ttc"),
    ("simhei", "黑体", "simhei.ttf"),
    ("simsun", "宋体", "simsun.ttc"),
    ("simkai", "楷体", "simkai.ttf"),
    ("dengxian", "等线", "Deng.ttf"),
    ("simfang", "仿宋", "simfang.ttf"),
)
"""Windows 自带的中文字体：标签、名字与 `%WINDIR%\\Fonts` 下的文件名（TTC 取第一个）。"""

FALLBACK_FONT = FontChoice("synthetic-sans", "合成游戏的字体子集", FONT_FILE)

SIZES = (16, 22, 30, 42)
"""字号：客户区高 720 时的像素，其它高度等比缩放。"""
BACKGROUNDS = ("dark", "light", "gradient", "texture", "translucent")
STYLES = ("plain", "outline", "shadow")
PARTIAL_EVERY = 4
"""每这么多个样本另有一个逐字显示到一半的。"""


def system_fonts(font_dir: Path | None = None) -> list[FontChoice]:
    """本机有的系统字体；不是 Windows 且没有给出目录时为空。"""
    if font_dir is None:
        if sys.platform != "win32":
            return []
        font_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    return [
        FontChoice(tag, name, font_dir / file)
        for tag, name, file in SYSTEM_FONTS
        if (font_dir / file).is_file()
    ]


def available_fonts(*, system: bool = True) -> list[FontChoice]:
    """系统字体，没有（或 `system` 为假）时只有合成游戏的字体子集。"""
    found = system_fonts() if system else []
    return found or [FALLBACK_FONT]


def coverage(font: FontChoice) -> frozenset[str]:
    """字体的字表里有的字。"""
    with TTFont(font.path, fontNumber=font.index, lazy=True) as tt:
        cmap: dict[int, str] = tt.getBestCmap() or {}
        return frozenset(chr(code) for code in cmap)


def _synthetic_texts() -> list[str]:
    """合成游戏的台词、选项提问与坏结局的文字：都在它的字体子集里。"""
    texts: list[str] = []
    for node in NODES.values():
        match node:
            case Dialogue(lines=lines):
                texts += [line.text for line in lines]
            case Choice(prompt=prompt):
                texts.append(prompt)
            case Ending(text=text):
                texts.append(text)
    return texts


def texts_for(characters: frozenset[str], count: int) -> list[str]:
    """`count` 句字体覆盖得到的对白：先取 `LINES` 里的，不够时补上合成游戏的文字，再不够时轮换。"""
    covered = [t for t in LINES if set(t) <= characters | {" "}]
    if len(covered) < count:
        extra = [t for t in _synthetic_texts() if set(t) <= characters | {" "}]
        covered += [t for t in extra if t not in covered]
    if not covered:
        raise ValueError("字体一句对白也覆盖不到")
    return [covered[i % len(covered)] for i in range(count)]


type Color = tuple[int, int, int]
type Pixels = NDArray[np.uint8]

WHITE: Color = (245, 245, 240)
DARK_INK: Color = (34, 34, 40)
EDGE: Color = (16, 16, 22)


@dataclass(frozen=True)
class Variant:
    font: FontChoice
    size: int
    """客户区高 720 时的像素。"""
    background: str
    style: str
    text: str
    partial: bool
    seed: int
    """字号与底色的组合的序号，也是底图的随机种子。"""


@dataclass(frozen=True)
class Rendered:
    bgra: bytes
    rect: PixelRect
    truth: str
    """各行以换行分隔。"""
    size: int
    """实际的字号（像素）。"""


def box(width: int, height: int) -> PixelRect:
    """对白框：客户区宽的 8%–92%、高的 60%–95%。"""
    return PixelRect(
        round(0.08 * width), round(0.60 * height), round(0.92 * width), round(0.95 * height)
    )


def _lerp(a: Color, b: Color, t: NDArray[np.float64]) -> Pixels:
    start = np.asarray(a, np.float64)
    end = np.asarray(b, np.float64)
    return np.clip(start + (end - start) * t[..., None], 0, 255).astype(np.uint8)


def _value_noise(rng: np.random.Generator, width: int, height: int) -> NDArray[np.float64]:
    """几层放大的随机网格相加，归一到 [0, 1]：云雾、纸张一类的纹理。"""
    total = np.zeros((height, width))
    for cells, weight in ((4, 0.5), (16, 0.3), (64, 0.2)):
        grid = rng.random((max(2, height * cells // width), cells))
        total += weight * cv2.resize(grid, (width, height), interpolation=cv2.INTER_CUBIC)
    low, high = float(total.min()), float(total.max())
    return (total - low) / max(high - low, 1e-9)


def background(kind: str, width: int, height: int, rng: np.random.Generator) -> Pixels:
    """RGB 的底图。"""
    if kind == "dark":
        return np.full((height, width, 3), (22, 24, 34), np.uint8)
    if kind == "light":
        return np.full((height, width, 3), (236, 232, 220), np.uint8)
    if kind == "gradient":
        y, x = np.mgrid[0:height, 0:width]
        return _lerp((34, 52, 110), (140, 60, 96), (x / width + y / height) / 2)
    noise = _value_noise(rng, width, height)
    grain = rng.normal(0.0, 0.05, (height, width))
    if kind == "texture":
        return _lerp((40, 48, 64), (128, 136, 150), np.clip(noise + grain, 0, 1))
    if kind == "translucent":
        image = _lerp((30, 40, 30), (225, 214, 170), np.clip(noise + grain, 0, 1))
        # 画面里的物件：随机的亮块与暗块。
        for _ in range(12):
            x0, y0 = int(rng.integers(width)), int(rng.integers(height))
            w, h = (
                int(rng.integers(width // 20, width // 5)),
                int(rng.integers(height // 20, height // 4)),
            )
            color = (255, 250, 230) if rng.random() < 0.5 else (20, 20, 24)
            image[y0 : y0 + h, x0 : x0 + w] = color
        return image
    raise ValueError(f"没有底色 {kind}")


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """按字折行，同合成游戏的渲染。"""
    lines: list[str] = []
    current = ""
    for char in text:
        if current and font.getlength(current + char) > width:
            lines.append(current)
            current = char
        else:
            current += char
    if current or not lines:
        lines.append(current)
    return lines


def _prefix(lines: Sequence[str], count: int) -> list[str]:
    """排好的行里的前 `count` 个字，行的划分不变（逐字显示时字的位置与显示完时相同）。"""
    result: list[str] = []
    for line in lines:
        if count <= 0:
            break
        result.append(line[:count])
        count -= len(line)
    return result


def render(variant: Variant, width: int, height: int) -> Rendered:
    rng = np.random.default_rng(variant.seed)
    pixels = background(variant.background, width, height, rng)
    rect = box(width, height)
    image = Image.fromarray(pixels, "RGB").convert("RGBA")
    if variant.background == "translucent":
        shade = Image.new("RGBA", image.size, (0, 0, 0, 0))
        ImageDraw.Draw(shade).rectangle(
            (rect.left, rect.top, rect.right - 1, rect.bottom - 1), fill=(12, 14, 24, 150)
        )
        image = Image.alpha_composite(image, shade)
    size = max(8, round(variant.size * height / 720))
    font = ImageFont.truetype(
        variant.font.path, size, index=variant.font.index, layout_engine=ImageFont.Layout.BASIC
    )
    pad_x, pad_y = round(0.025 * width), round(0.03 * height)
    lines = _wrap(variant.text, font, rect.right - rect.left - 2 * pad_x)
    if variant.partial:
        lines = _prefix(lines, len(variant.text) // 2)
    light = variant.background == "light"
    ink = WHITE if variant.style == "outline" or not light else DARK_INK
    draw = ImageDraw.Draw(image)
    line_height = round(size * 1.45)
    for index, line in enumerate(lines):
        x, y = rect.left + pad_x, rect.top + pad_y + index * line_height
        if variant.style == "outline":
            stroke = max(1, round(size / 12))
            draw.text((x, y), line, font=font, fill=ink, stroke_width=stroke, stroke_fill=EDGE)
        elif variant.style == "shadow":
            offset = max(1, round(size / 14))
            shadow = (150, 150, 150) if light else (0, 0, 0)
            draw.text((x + offset, y + offset), line, font=font, fill=shadow)
            draw.text((x, y), line, font=font, fill=ink)
        else:
            draw.text((x, y), line, font=font, fill=ink)
    bgra = image.convert("RGBA").tobytes("raw", "BGRA")
    return Rendered(bgra, rect, "\n".join(lines), size)


def variants(fonts: Sequence[FontChoice]) -> list[Variant]:
    """每种字体：字号 × 底色的每一组合一个样本，字形按组合轮换（第 i 个字号、第 j 种底色用第
    (i + 2j) mod 3 种字形，各字号、各底色与逐字显示的样本里三种字形都有）；每 `PARTIAL_EVERY` 个
    另有一个逐字显示到一半的。各字体所用的对白、字号、底色与字形相同（字体缺字时除外）。"""
    cells = [(i, j) for i in range(len(SIZES)) for j in range(len(BACKGROUNDS))]
    result: list[Variant] = []
    for font in fonts:
        texts = texts_for(coverage(font), len(cells))
        for index, ((i, j), text) in enumerate(zip(cells, texts, strict=True)):
            size, bg = SIZES[i], BACKGROUNDS[j]
            style = STYLES[(i + 2 * j) % len(STYLES)]
            result.append(Variant(font, size, bg, style, text, False, index))
            if index % PARTIAL_EVERY == 0:
                result.append(Variant(font, size, bg, style, text, True, index))
    return result
