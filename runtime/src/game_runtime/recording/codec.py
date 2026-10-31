"""帧像素的无损编码：关键帧为 PNG，差分帧为与上一帧逐字节异或后的 zlib 压缩。

规则与依据见 `docs/modules/offline-tools.md` 的“样本格式”。像素一律是 BGRA8、行间无填充的
`宽 × 高 × 4` 字节。这里的函数都可以在工作线程里调用（OpenCV、numpy 与 zlib 在大块数据上释放 GIL）。
"""

import zlib
from typing import Final

import cv2
import numpy as np

from game_runtime.frame_ring import RingFrame
from game_runtime.frame_ring.ring import BYTES_PER_PIXEL

PNG_LEVEL: Final = 1
"""PNG 的压缩级别。"""
ZLIB_LEVEL: Final = 1
"""差分帧的 zlib 压缩级别。"""
KEY_CHANGED_FRACTION: Final = 0.5
"""差分里变化的像素超过这个比例时改写成关键帧。"""


class CodecError(Exception):
    """像素文件解不开或与索引不符。"""


def packed_pixels(frame: RingFrame) -> bytes:
    """帧的像素去掉行尾的填充；没有填充时就是原对象。"""
    row = frame.width * BYTES_PER_PIXEL
    length = row * frame.height
    if frame.row_stride == row:
        return frame.pixels if len(frame.pixels) == length else frame.pixels[:length]
    rows = np.frombuffer(frame.pixels, np.uint8, count=frame.row_stride * frame.height)
    return rows.reshape(frame.height, frame.row_stride)[:, :row].tobytes()


def crc32(pixels: bytes) -> int:
    return zlib.crc32(pixels)


def _array(pixels: bytes, width: int, height: int) -> np.ndarray:
    expected = width * height * BYTES_PER_PIXEL
    if len(pixels) != expected:
        raise CodecError(f"像素有 {len(pixels)} 字节，{width}×{height} 应为 {expected}")
    return np.frombuffer(pixels, np.uint8).reshape(height, width, BYTES_PER_PIXEL)


def encode_png(pixels: bytes, width: int, height: int) -> bytes:
    """BGRA8 编码成 RGBA 的 PNG，alpha 照原样保留。"""
    ok, encoded = cv2.imencode(
        ".png", _array(pixels, width, height), [cv2.IMWRITE_PNG_COMPRESSION, PNG_LEVEL]
    )
    if not ok:
        raise CodecError("OpenCV 没能编码 PNG")
    return encoded.tobytes()


def decode_png(data: bytes, width: int, height: int) -> bytes:
    """解开 `encode_png` 写的 PNG，得到 BGRA8；尺寸或通道数不符即 `CodecError`。"""
    decoded = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    if decoded is None:
        raise CodecError("PNG 解不开")
    image = np.asarray(decoded)
    if image.dtype != np.uint8 or image.shape != (height, width, BYTES_PER_PIXEL):
        raise CodecError(
            f"PNG 解出 {image.shape}、{image.dtype}，应为 ({height}, {width}, 4) 的 uint8"
        )
    return image.tobytes()


def _xor(pixels: bytes, previous: bytes) -> np.ndarray:
    if len(pixels) != len(previous):
        raise CodecError(f"差分的两帧长度不同：{len(pixels)} 与 {len(previous)}")
    # 每个像素 4 字节：按 uint32 异或，非零的元素就是变化的像素。
    return np.bitwise_xor(np.frombuffer(pixels, np.uint32), np.frombuffer(previous, np.uint32))


def encode_delta(pixels: bytes, previous: bytes) -> bytes | None:
    """与上一帧逐字节异或后以 zlib 压缩；变化的像素超过 `KEY_CHANGED_FRACTION` 时为 None，
    应改写成关键帧。"""
    diff = _xor(pixels, previous)
    if np.count_nonzero(diff) > KEY_CHANGED_FRACTION * diff.size:
        return None
    return zlib.compress(diff.tobytes(), ZLIB_LEVEL)


def apply_delta(data: bytes, previous: bytes) -> bytes:
    """`encode_delta` 的逆：解压后与上一帧异或。"""
    try:
        diff = zlib.decompress(data)
    except zlib.error as error:
        raise CodecError(f"差分帧解压失败：{error}") from None
    return _xor(diff, previous).tobytes()
