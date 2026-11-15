"""锁定副本损坏或被增加文件时，统一检查必须失败。"""

import json
import shutil
from pathlib import Path

import pytest

from game_runtime_check.vendor import SOURCES, VENDOR_DIR, differences


@pytest.fixture
def pinned(tmp_path: Path) -> Path:
    target = tmp_path / "pinned"
    shutil.copytree(VENDOR_DIR, target)
    return target


def test_committed_pin_is_complete_and_unchanged() -> None:
    assert differences(VENDOR_DIR) == []


@pytest.mark.parametrize("name", SOURCES)
def test_changed_or_missing_artifact_fails(pinned: Path, name: str) -> None:
    artifact = pinned / name
    artifact.write_bytes(artifact.read_bytes() + b"\n")
    assert differences(pinned) == [f"digest mismatch: {name}"]
    artifact.unlink()
    assert differences(pinned)[0].startswith(f"cannot read {name}:")


def test_unlisted_file_fails(pinned: Path) -> None:
    (pinned / "stale.py").write_bytes(b"stale\n")
    assert differences(pinned) == ["unexpected file: stale.py"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("format", True),
        ("repository", "https://example.invalid/fork"),
        ("commit", "4274d54"),
        ("files", {"../outside": {"source": "outside", "sha256": "0" * 64}}),
    ],
)
def test_invalid_provenance_fails(pinned: Path, field: str, value: object) -> None:
    path = pinned / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest[field] = value
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert differences(pinned)


def test_missing_or_malformed_manifest_fails(pinned: Path) -> None:
    path = pinned / "manifest.json"
    path.write_bytes(b"{")
    assert differences(pinned)[0].startswith("cannot read manifest:")
    path.unlink()
    assert differences(pinned)[0].startswith("cannot read manifest:")
