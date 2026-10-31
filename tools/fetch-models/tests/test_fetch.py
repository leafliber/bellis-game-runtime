"""`fetch-models`：下载并核对、已有即跳过、不符即重下、摘要不符即失败且不留文件、只认 https、
`--check` 不联网。下载换成内存里的假响应，不联网。
"""

import hashlib
import io
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from fetch_models import FetchError, Opener, Response, fetch, known_artifacts, run
from game_runtime.artifacts import Artifact

CONTENT = b"pinned model bytes" * 1000


def pinned(content: bytes = CONTENT, url: str = "https://example.invalid/m.onnx") -> Artifact:
    return Artifact(
        name="test-model",
        version="v1",
        file="m.onnx",
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
        url=url,
        source="测试",
        license="测试",
    )


class FakeResponse:
    def __init__(self, body: bytes, final_url: str) -> None:
        self._body = io.BytesIO(body)
        self._url = final_url

    def read(self, size: int, /) -> bytes:
        return self._body.read(size)

    def geturl(self) -> str:
        return self._url


def serving(body: bytes, final_url: str | None = None) -> tuple[Opener, list[str]]:
    """一个总是回应 `body` 的假下载，记下请求的地址。"""
    requested: list[str] = []

    @contextmanager
    def open_url(url: str, timeout: float) -> Generator[Response]:
        requested.append(url)
        yield FakeResponse(body, final_url or url)

    def opener(url: str, timeout: float) -> AbstractContextManager[Response]:
        return open_url(url, timeout)

    return opener, requested


def test_a_missing_artifact_is_downloaded_and_verified(tmp_path: Path) -> None:
    opener, requested = serving(CONTENT)
    item = pinned()

    assert fetch(item, tmp_path, opener=opener, log=lambda _: None) == "downloaded"

    assert item.path(tmp_path).read_bytes() == CONTENT
    assert requested == [item.url]
    assert list(item.path(tmp_path).parent.iterdir()) == [item.path(tmp_path)]


def test_a_present_artifact_is_not_downloaded_again(tmp_path: Path) -> None:
    item = pinned()
    item.path(tmp_path).parent.mkdir(parents=True)
    item.path(tmp_path).write_bytes(CONTENT)
    opener, requested = serving(b"unused")

    assert fetch(item, tmp_path, opener=opener, log=lambda _: None) == "present"
    assert requested == []


def test_a_corrupted_artifact_is_replaced(tmp_path: Path) -> None:
    item = pinned()
    item.path(tmp_path).parent.mkdir(parents=True)
    item.path(tmp_path).write_bytes(b"x" * len(CONTENT))
    opener, _ = serving(CONTENT)
    messages: list[str] = []

    assert fetch(item, tmp_path, opener=opener, log=messages.append) == "downloaded"

    assert item.path(tmp_path).read_bytes() == CONTENT
    assert "重新下载" in messages[0]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        pytest.param(b"y" * len(CONTENT), "SHA-256", id="wrong digest"),
        pytest.param(CONTENT[:-1], "字节", id="too short"),
        pytest.param(CONTENT + b"z", "超过", id="too long"),
    ],
)
def test_a_wrong_download_fails_and_leaves_nothing(
    tmp_path: Path, body: bytes, message: str
) -> None:
    item = pinned()
    opener, _ = serving(body)

    with pytest.raises(FetchError, match=message):
        fetch(item, tmp_path, opener=opener, log=lambda _: None)

    assert list(item.path(tmp_path).parent.iterdir()) == []


def test_only_https_is_accepted(tmp_path: Path) -> None:
    opener, requested = serving(CONTENT)
    with pytest.raises(FetchError, match="不是 https"):
        fetch(pinned(url="http://example.invalid/m.onnx"), tmp_path, opener=opener)
    assert requested == []

    redirected, _ = serving(CONTENT, final_url="http://mirror.invalid/m.onnx")
    with pytest.raises(FetchError, match="重定向到非 https"):
        fetch(pinned(), tmp_path, opener=redirected, log=lambda _: None)
    assert not pinned().path(tmp_path).exists()


def test_check_reports_without_network(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def no_network(url: str, timeout: float) -> AbstractContextManager[Response]:
        raise AssertionError("--check 不应联网")

    assert (
        run(["--check", "--data-dir", str(tmp_path)], artifacts=[pinned()], opener=no_network) == 1
    )
    assert "FAIL test-model v1" in capsys.readouterr().out

    pinned().path(tmp_path).parent.mkdir(parents=True)
    pinned().path(tmp_path).write_bytes(CONTENT)
    assert (
        run(["--check", "--data-dir", str(tmp_path)], artifacts=[pinned()], opener=no_network) == 0
    )


def test_run_downloads_the_selected_artifacts(tmp_path: Path) -> None:
    opener, requested = serving(CONTENT)
    other = replace(pinned(), name="other-model")

    code = run(
        ["--data-dir", str(tmp_path), "--only", "test-model"],
        artifacts=[pinned(), other],
        opener=opener,
    )

    assert code == 0
    assert requested == [pinned().url]
    assert not other.path(tmp_path).exists()


def test_unknown_names_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="清单里没有：nothing"):
        run(["--data-dir", str(tmp_path), "--only", "nothing"], artifacts=[pinned()])


def test_the_catalog_lists_the_ocr_models() -> None:
    names = {artifact.name for artifact in known_artifacts()}

    assert {"ppocrv5-mobile-det", "ppocrv5-mobile-rec", "ppocrv5-dict"} <= names
