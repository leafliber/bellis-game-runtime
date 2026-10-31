"""制品的核对：没有、大小不符、摘要不符与相符；OCR 的清单格式正确。

规则见 `docs/modules/game-adapter.md` 的“模型制品”。本地数据目录的位置由 `game_runtime.local_data`
给出，测试在 `runtime/tests/scene/test_templates.py`。
"""

import hashlib
from pathlib import Path

import pytest

from game_runtime.artifacts import (
    Artifact,
    ArtifactDigestError,
    ArtifactMissingError,
    verify_artifact,
)
from game_runtime.ocr.models import MODEL_SETS, all_artifacts

CONTENT = b"synthetic model bytes\n"


def artifact(content: bytes = CONTENT) -> Artifact:
    return Artifact(
        name="test-model",
        version="v1",
        file="model.onnx",
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
        url="https://example.invalid/model.onnx",
        source="测试",
        license="测试",
    )


def place(data_dir: Path, item: Artifact, content: bytes) -> Path:
    path = item.path(data_dir)
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    return path


def test_an_artifact_lives_under_models_name_and_version(tmp_path: Path) -> None:
    assert artifact().path(tmp_path) == tmp_path / "models" / "test-model" / "v1" / "model.onnx"


def test_a_matching_artifact_verifies(tmp_path: Path) -> None:
    item = artifact()
    path = place(tmp_path, item, CONTENT)

    assert verify_artifact(item, tmp_path) == path


def test_a_missing_artifact_names_the_fetch_command(tmp_path: Path) -> None:
    with pytest.raises(ArtifactMissingError, match="不存在，先运行 uv run fetch-models") as caught:
        verify_artifact(artifact(), tmp_path)

    assert caught.value.path == artifact().path(tmp_path)


def test_a_file_of_the_wrong_size_is_rejected(tmp_path: Path) -> None:
    item = artifact()
    place(tmp_path, item, CONTENT + b"x")

    with pytest.raises(ArtifactDigestError, match=f"应为 {len(CONTENT)}"):
        verify_artifact(item, tmp_path)


def test_a_file_with_the_wrong_digest_is_rejected(tmp_path: Path) -> None:
    item = artifact()
    tampered = bytes([CONTENT[0] ^ 1]) + CONTENT[1:]
    place(tmp_path, item, tampered)

    with pytest.raises(ArtifactDigestError, match=item.sha256):
        verify_artifact(item, tmp_path)


# OCR 的清单


def test_the_ocr_pins_are_well_formed() -> None:
    items = all_artifacts()

    assert len({(a.name, a.version) for a in items}) == len(items)
    for item in items:
        assert item.url.startswith("https://")
        assert len(item.sha256) == 64
        assert int(item.sha256, 16) >= 0
        assert item.sha256 == item.sha256.lower()
        assert item.size > 0
        # 版本是“发布者-标签”，地址钉在那个标签上。
        assert f"/{item.version.split('-', 1)[1]}/" in item.url
        assert item.url.endswith(item.file)
    # 两组模型共用同一个字表。
    assert len({model_set.dictionary for model_set in MODEL_SETS.values()}) == 1
