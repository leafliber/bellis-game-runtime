"""锚点模板文件：本地数据目录里按 SHA-256 存放，读取时核对。

布局与规则见 `docs/modules/game-adapter.md` 的“模板文件”。模板从游戏派生，只在本机，不进版本库。
"""

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Final

import cv2
import numpy as np
from numpy.typing import NDArray

TEMPLATES_DIR: Final = "templates"
"""本地数据目录里存放模板的子目录。"""
PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_GAME = re.compile(r"[a-z][a-z0-9-]*")

type Bgr = NDArray[np.uint8]
"""BGR 像素，形状为（高，宽，3）。"""


class TemplateError(Exception):
    """模板文件用不了：不在、摘要不符、不是 PNG 或解不开、全部像素相同。"""


def template_path(data_dir: Path, game: str, sha256: str) -> Path:
    """模板文件的路径：`<本地数据目录>/templates/<适配包 id>/<SHA-256>.png`。"""
    if _GAME.fullmatch(game) is None:
        raise ValueError(f"适配包 id 不合法：{game}")
    if _SHA256.fullmatch(sha256) is None:
        raise ValueError(f"SHA-256 须为 64 位小写十六进制：{sha256}")
    return data_dir / TEMPLATES_DIR / game / f"{sha256}.png"


def load_template(data_dir: Path, game: str, sha256: str) -> Bgr:
    """读出模板并核对摘要，解成 BGR 像素（alpha 丢弃）。用不了时抛 `TemplateError`。"""
    path = template_path(data_dir, game, sha256)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise TemplateError(f"没有模板文件 {path}") from None
    except OSError as error:
        raise TemplateError(f"读不出模板文件 {path}：{error}") from None
    actual = hashlib.sha256(data).hexdigest()
    if actual != sha256:
        raise TemplateError(f"模板文件 {path} 的摘要不符：内容的 SHA-256 为 {actual}")
    if not data.startswith(PNG_SIGNATURE):
        raise TemplateError(f"模板文件 {path} 不是 PNG")
    decoded = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if decoded is None:
        raise TemplateError(f"模板文件 {path} 解不开")
    image = np.asarray(decoded, dtype=np.uint8)
    if image.ndim != 3 or image.shape[2] != 3 or image.size == 0:
        raise TemplateError(f"模板文件 {path} 解出的不是 BGR 图像：{image.shape}")
    if is_uniform(image):
        raise TemplateError(f"模板文件 {path} 的全部像素相同，归一化相关没有意义")
    return image


def store_template(data_dir: Path, game: str, png: bytes) -> str:
    """把 PNG 按内容的 SHA-256 存进本地数据目录，返回摘要。

    先写同目录的临时文件再改名；同名文件已在且内容相符时不再写。供标定工具与测试使用。
    """
    if not png.startswith(PNG_SIGNATURE):
        raise ValueError("模板须为 PNG")
    sha256 = hashlib.sha256(png).hexdigest()
    path = template_path(data_dir, game, sha256)
    try:
        if hashlib.sha256(path.read_bytes()).hexdigest() == sha256:
            return sha256
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(png)
        Path(temporary).replace(path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return sha256


def is_uniform(image: Bgr) -> bool:
    """全部像素都相同。"""
    return bool((image == image[0, 0]).all())
