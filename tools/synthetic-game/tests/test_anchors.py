"""锚点模板：从开场对话的帧上裁出钟面，PNG 能被标准解码器读回同样的像素，字节只取决于像素。

适配包按摘要引用它、`scene@1` 用它判别，见 `runtime/tests/scene`。
"""

import io
import zlib

from PIL import Image

from synthetic_game import Click, GameConfig, ManualClock, SyntheticGame
from synthetic_game.anchors import CLOCK_FACE_MARGIN, anchor_templates, encode_png
from synthetic_game.layout import Layout, Rect
from synthetic_game.render import PANEL, WHITE


def test_the_clock_face_template_is_cut_from_the_opening_dialogue() -> None:
    [template] = anchor_templates()
    face = Layout(1280, 720).clock_face()
    m = CLOCK_FACE_MARGIN

    assert template.name == "clock-face"
    assert template.rect == Rect(face.left - m, face.top - m, face.right + m, face.bottom + m)
    # 裁出的部分在钟楼里，背景随节点变化的部分不在模板里。
    tower = Layout(1280, 720).clock_tower()
    assert tower.left <= template.rect.left
    assert template.rect.right <= tower.right
    assert tower.top <= template.rect.top
    assert template.rect.bottom <= tower.bottom

    game = SyntheticGame(GameConfig(), ManualClock())
    start = next(t for t in game.truth().targets if t.id == "menu.start")
    game.handle(Click(*start.rect.center))
    frame = game.frame()
    image = Image.open(io.BytesIO(template.png))
    image.load()
    assert (image.mode, image.size) == ("RGB", (template.rect.width, template.rect.height))
    for x, y in ((0, 0), (60, 60), (119, 119), (15, 60)):
        offset = (template.rect.top + y) * frame.stride + (template.rect.left + x) * 4
        blue, green, red = frame.data[offset : offset + 3]
        assert image.getpixel((x, y)) == (red, green, blue)
    assert image.getpixel((0, 0)) == PANEL
    assert image.getpixel((60, 60)) == WHITE


def test_templates_are_byte_for_byte_reproducible() -> None:
    first = anchor_templates()
    second = anchor_templates()

    assert [t.png for t in first] == [t.png for t in second]
    assert [t.sha256 for t in first] == [t.sha256 for t in second]


def test_png_uses_stored_blocks_only() -> None:
    rows = [bytes(range(i, i + 6)) for i in range(3)]
    png = encode_png(2, 3, rows)

    # IHDR 之后的 IDAT：zlib 头 0x78 0x01，单个最终存储块，原样解出带滤波字节的行。
    idat = png.index(b"IDAT")
    length = int.from_bytes(png[idat - 4 : idat], "big")
    stream = png[idat + 4 : idat + 4 + length]
    assert stream[:2] == b"\x78\x01"
    assert stream[2] == 1  # BFINAL=1、BTYPE=00
    assert zlib.decompress(stream) == b"".join(b"\x00" + row for row in rows)
    image = Image.open(io.BytesIO(png))
    image.load()
    assert image.getpixel((1, 2)) == (5, 6, 7)


def test_large_images_are_split_into_blocks() -> None:
    rows = [bytes([i % 256]) * 300 for i in range(300)]  # 100×300，原始数据超过 65535 字节
    png = encode_png(100, 300, rows)

    image = Image.open(io.BytesIO(png))
    image.load()
    assert image.size == (100, 300)
    assert image.getpixel((50, 299)) == (299 % 256,) * 3
