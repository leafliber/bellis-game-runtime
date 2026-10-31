"""把钉住版本与摘要的模型制品下载到本地数据目录并核对。

设计见 `docs/modules/game-adapter.md` 的“模型制品”。`uv run fetch-models` 下载清单里全部
还没有或不符的制品；`--check` 只核对、不联网；`--only` 只处理指定的制品。只从清单写死的 https
地址下载，边下载边算 SHA-256，大小与摘要都相符才改名为正式文件，不符即删除并失败。清单在运行时
的代码里（`game_runtime.ocr.models`），文件不进版本库。
"""

import argparse
import hashlib
import sys
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Literal, Protocol

from game_runtime.artifacts import (
    Artifact,
    ArtifactDigestError,
    ArtifactMissingError,
    verify_artifact,
)
from game_runtime.local_data import local_data_dir
from game_runtime.ocr.models import all_artifacts

DOWNLOAD_TIMEOUT_SECONDS = 120
_CHUNK = 1 << 20

type Status = Literal["present", "downloaded"]


class Response(Protocol):
    def read(self, size: int, /) -> bytes: ...
    def geturl(self) -> str: ...


type Opener = Callable[[str, float], AbstractContextManager[Response]]


class FetchError(Exception):
    """下载失败或下载到的文件与清单不符。"""


def _urlopen(url: str, timeout: float) -> AbstractContextManager[Response]:
    # S310：只打开清单里写死的 https 地址，调用方已核对协议。
    return urllib.request.urlopen(url, timeout=timeout)  # noqa: S310


def known_artifacts() -> tuple[Artifact, ...]:
    """清单里的全部制品。"""
    return all_artifacts()


def fetch(
    artifact: Artifact,
    data_dir: Path,
    *,
    opener: Opener = _urlopen,
    log: Callable[[str], None] = print,
) -> Status:
    """确保本地有相符的 `artifact`：已有且相符即不下载，没有或不符即下载并核对。"""
    path = artifact.path(data_dir)
    try:
        verify_artifact(artifact, data_dir)
    except ArtifactMissingError:
        pass
    except ArtifactDigestError as error:
        log(f"{error}；重新下载")
    else:
        return "present"
    if not artifact.url.startswith("https://"):
        raise FetchError(f"{artifact.name} 的地址不是 https：{artifact.url}")
    log(f"下载 {artifact.name} {artifact.version}：{artifact.url}")
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.part")
    try:
        _download(artifact, partial, opener)
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)
    return "downloaded"


def _download(artifact: Artifact, partial: Path, opener: Opener) -> None:
    digest = hashlib.sha256()
    size = 0
    with opener(artifact.url, DOWNLOAD_TIMEOUT_SECONDS) as response, partial.open("wb") as file:
        final = response.geturl()
        if not final.startswith("https://"):
            raise FetchError(f"{artifact.name} 被重定向到非 https 的地址：{final}")
        while chunk := response.read(_CHUNK):
            size += len(chunk)
            if size > artifact.size:
                raise FetchError(f"{artifact.name} 超过了清单的 {artifact.size} 字节")
            digest.update(chunk)
            file.write(chunk)
    if size != artifact.size:
        raise FetchError(f"{artifact.name} 下载到 {size} 字节，应为 {artifact.size}")
    actual = digest.hexdigest()
    if actual != artifact.sha256:
        raise FetchError(f"{artifact.name} 的 SHA-256 为 {actual}，应为 {artifact.sha256}")


def check(artifact: Artifact, data_dir: Path) -> str | None:
    """只核对、不联网；相符为 None，否则为问题。"""
    try:
        verify_artifact(artifact, data_dir)
    except (ArtifactMissingError, ArtifactDigestError) as error:
        return str(error)
    return None


def _select(artifacts: Sequence[Artifact], only: Iterable[str]) -> list[Artifact]:
    wanted = list(only)
    if not wanted:
        return list(artifacts)
    names = {artifact.name for artifact in artifacts}
    unknown = [name for name in wanted if name not in names]
    if unknown:
        raise SystemExit(f"清单里没有：{'、'.join(unknown)}；可选：{'、'.join(sorted(names))}")
    return [artifact for artifact in artifacts if artifact.name in wanted]


def run(
    argv: Sequence[str] | None = None,
    *,
    artifacts: Sequence[Artifact] | None = None,
    opener: Opener = _urlopen,
) -> int:
    parser = argparse.ArgumentParser(
        prog="fetch-models",
        description="Download the pinned model artifacts into the local data directory and verify.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="local data directory (default: BELLIS_GAME_RUNTIME_DATA or the platform default)",
    )
    parser.add_argument("--check", action="store_true", help="only verify; no network access")
    parser.add_argument("--only", action="append", default=[], help="artifact name; repeatable")
    parser.add_argument("--list", action="store_true", help="list the pinned artifacts")
    args = parser.parse_args(argv)
    catalog = known_artifacts() if artifacts is None else artifacts
    if args.list:
        for artifact in catalog:
            print(f"{artifact.name} {artifact.version} {artifact.file} {artifact.size} 字节")
            print(f"  {artifact.url}")
            print(f"  sha256 {artifact.sha256}")
            print(f"  来源：{artifact.source}")
            print(f"  许可：{artifact.license}")
        return 0
    data_dir: Path = args.data_dir or local_data_dir()
    if not data_dir.is_absolute():
        parser.error(f"--data-dir 须为绝对路径：{data_dir}")
    selected = _select(catalog, args.only)
    failures = 0
    for artifact in selected:
        if args.check:
            problem = check(artifact, data_dir)
            print(f"{'OK  ' if problem is None else 'FAIL'} {artifact.name} {artifact.version}")
            if problem is not None:
                print(f"  {problem}")
                failures += 1
            continue
        try:
            status = fetch(artifact, data_dir, opener=opener)
        except (OSError, FetchError) as error:
            print(f"FAIL {artifact.name} {artifact.version}：{error}", file=sys.stderr)
            failures += 1
        else:
            print(f"OK   {artifact.name} {artifact.version}（{status}）")
    print(f"本地数据目录：{data_dir}")
    return 1 if failures else 0


def main() -> int:
    return run()
