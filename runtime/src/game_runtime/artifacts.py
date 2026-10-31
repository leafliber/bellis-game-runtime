"""按版本和摘要管理的制品（模型文件等），放在本地数据目录（`game_runtime.local_data`）里。

布局与核对见 `docs/modules/game-adapter.md` 的“模型制品”。这里只读本地文件、不联网；下载由离线
工具 `uv run fetch-models` 完成。
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

MODELS_DIR = "models"
"""本地数据目录下放模型制品的子目录。"""

_CHUNK = 1 << 20


@dataclass(frozen=True)
class Artifact:
    """一个钉住来源与摘要的制品。清单进版本库，文件本身只在本地数据目录。"""

    name: str
    """制品名，小写字母、数字与 `-`，即本地目录名。"""
    version: str
    """钉住的发布版本，即本地的版本目录名。"""
    file: str
    """文件名。"""
    sha256: str
    """文件的 SHA-256，小写十六进制。"""
    size: int
    """文件的字节数。"""
    url: str
    """下载地址：发布者的 https 地址，钉在 `version` 上。"""
    source: str
    """给人读的来源：发布者、发布与原始出处。"""
    license: str
    """许可。"""

    def path(self, data_dir: Path) -> Path:
        """在本地数据目录里的位置：`models/<制品名>/<版本>/<文件名>`。"""
        return data_dir / MODELS_DIR / self.name / self.version / self.file


class ArtifactError(Exception):
    """制品不能使用。"""

    def __init__(self, artifact: Artifact, path: Path, message: str) -> None:
        super().__init__(f"制品 {artifact.name} {artifact.version}（{path}）{message}")
        self.artifact = artifact
        self.path = path


class ArtifactMissingError(ArtifactError):
    """本地数据目录里没有这个制品。"""


class ArtifactDigestError(ArtifactError):
    """制品的大小或 SHA-256 与钉住的不符。"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def verify_artifact(artifact: Artifact, data_dir: Path) -> Path:
    """核对本地的制品并返回其路径；没有即 `ArtifactMissingError`，不符即 `ArtifactDigestError`。

    每次取用都核对，不缓存结果。
    """
    path = artifact.path(data_dir)
    hint = "，先运行 uv run fetch-models"
    if not path.is_file():
        raise ArtifactMissingError(artifact, path, f"不存在{hint}")
    size = path.stat().st_size
    if size != artifact.size:
        raise ArtifactDigestError(artifact, path, f"有 {size} 字节，应为 {artifact.size}{hint}")
    actual = sha256_file(path)
    if actual != artifact.sha256:
        raise ArtifactDigestError(
            artifact, path, f"的 SHA-256 为 {actual}，应为 {artifact.sha256}{hint}"
        )
    return path
