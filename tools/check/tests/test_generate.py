"""生成物核对的比较与写回：缺失、多余与内容不同都算不一致；写回删除不再生成的文件。"""

from pathlib import Path

from game_runtime_check.generate import (
    GENERATED_FILES,
    MODELS_PATH,
    collect,
    differences,
    write_back,
)


def test_identical_files_have_no_differences() -> None:
    files = {"schema/a.json": b"{}\n", MODELS_PATH: b"x = 1\n"}

    assert differences(files, dict(files)) == []


def test_missing_stale_and_outdated_files_are_reported() -> None:
    generated = {"fixtures/new.json": b"{}\n", "schema/a.json": b"{}\n"}
    committed = {"fixtures/old.json": b"{}\n", "schema/a.json": b"{ }\n"}

    assert differences(generated, committed) == [
        "missing: protocol/fixtures/new.json",
        "stale: protocol/fixtures/old.json",
        "outdated: protocol/schema/a.json",
    ]


def test_collect_reads_only_generated_paths(tmp_path: Path) -> None:
    (tmp_path / "fixtures" / "m").mkdir(parents=True)
    (tmp_path / "fixtures" / "m" / "params.json").write_bytes(b"{}\n")
    (tmp_path / "src" / "core_protocol").mkdir(parents=True)
    (tmp_path / MODELS_PATH).write_bytes(b"x = 1\n")
    (tmp_path / "src" / "core_protocol" / "frame_ring.py").write_bytes(b"handwritten\n")

    assert collect(tmp_path) == {"fixtures/m/params.json": b"{}\n", MODELS_PATH: b"x = 1\n"}
    assert MODELS_PATH in GENERATED_FILES


def test_write_back_replaces_generated_files_and_removes_stale_ones(tmp_path: Path) -> None:
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "old.json").write_bytes(b"{}\n")
    (tmp_path / "pyproject.toml").write_bytes(b"[project]\n")
    generated = {"fixtures/new.json": b"{}\n", "schema/a.json": b"{}\n"}

    write_back(generated, tmp_path)

    assert collect(tmp_path) == generated
    assert (tmp_path / "pyproject.toml").read_bytes() == b"[project]\n"
