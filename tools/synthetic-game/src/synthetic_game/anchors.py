"""合成游戏适配包的锚点模板：从渲染裁出，编码成逐字节确定的 PNG。

模板与其它从画面裁出的东西一样不进版本库（`games/` 下只有 `adapter.toml`）；测试与工具调用
`anchor_templates()` 得到模板，写进本地数据目录，适配包按 SHA-256 引用它们。PNG 不压缩（deflate 的
存储块），所以同样的像素在任何平台上都得到同样的字节、同样的摘要，不依赖 zlib 的实现。
"""

import hashlib
import struct
import zlib
from dataclasses import dataclass

from synthetic_game.clock import ManualClock
from synthetic_game.game import GameConfig, SyntheticGame
from synthetic_game.inputs import Click
from synthetic_game.layout import Layout, Rect

CLOCK_FACE_MARGIN = 10
"""钟面模板在钟面外接矩形四周多裁的像素（设计稿 1280×720），裁出的部分仍在钟楼里。"""


@dataclass(frozen=True)
class AnchorTemplate:
    """一个锚点模板：在缺省分辨率 1280×720 的帧上裁出的矩形与它的 PNG。"""

    name: str
    rect: Rect
    png: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.png).hexdigest()


def anchor_templates() -> tuple[AnchorTemplate, ...]:
    """合成游戏适配包引用的全部锚点模板。"""
    config = GameConfig()
    game = SyntheticGame(config, ManualClock())
    start = next(t for t in game.truth().targets if t.id == "menu.start")
    game.handle(Click(*start.rect.center))
    frame = game.frame()
    face = Layout(config.width, config.height).clock_face()
    margin = CLOCK_FACE_MARGIN
    rect = Rect(face.left - margin, face.top - margin, face.right + margin, face.bottom + margin)
    return (AnchorTemplate("clock-face", rect, _crop_png(frame.data, frame.width, rect)),)


def _crop_png(bgra: bytes, width: int, rect: Rect) -> bytes:
    """从 BGRA8、行跨度为 `width × 4` 的像素里裁出 `rect`，编码成 RGB 的 PNG。"""
    rows: list[bytes] = []
    for y in range(rect.top, rect.bottom):
        start = (y * width + rect.left) * 4
        row = bgra[start : start + rect.width * 4]
        rgb = bytearray(rect.width * 3)
        rgb[0::3] = row[2::4]
        rgb[1::3] = row[1::4]
        rgb[2::3] = row[0::4]
        rows.append(bytes(rgb))
    return encode_png(rect.width, rect.height, rows)


def encode_png(width: int, height: int, rgb_rows: list[bytes]) -> bytes:
    """8 位 RGB 的 PNG，每行不加滤波，IDAT 是只含存储块的 zlib 流，输出只取决于像素。"""
    if len(rgb_rows) != height or any(len(row) != width * 3 for row in rgb_rows):
        raise ValueError("像素行数或行长与尺寸不符")
    raw = b"".join(b"\x00" + row for row in rgb_rows)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", _stored_zlib(raw))
        + _chunk(b"IEND", b"")
    )


def _chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data)
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def _stored_zlib(data: bytes) -> bytes:
    """zlib 流：头（deflate、32K 窗口、不压缩）、每块至多 65535 字节的存储块、Adler-32。"""
    limit = 0xFFFF
    blocks = [data[i : i + limit] for i in range(0, len(data), limit)] or [b""]
    out = bytearray(b"\x78\x01")
    for index, block in enumerate(blocks):
        final = 1 if index == len(blocks) - 1 else 0
        out += struct.pack("<BHH", final, len(block), len(block) ^ 0xFFFF) + block
    out += struct.pack(">I", zlib.adler32(data))
    return bytes(out)
