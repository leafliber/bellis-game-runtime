"""`uv run check-adapters`：目录内容的核对、退出码与输出。声明都是测试里写的合成数据。"""

from pathlib import Path

import pytest

from game_runtime.adapter import ADAPTER_FILE, AdapterError
from game_runtime.adapter.check import check_games_dir, check_package, main

VALID = """
id = "{id}"
name = "样例"
version = "1"
screens = []

[window]
process_name = "sample.exe"
title = "Sample Game"

[resolution]
width = 640
height = 360

[language]
tag = "zh-Hans"
fixed_texts = []

[compliance]
basis = "测试用的合成声明"
text_source = "screen"
training = false
demonstrations = false
recording_quota_gb = 0
models = []

[whitelist]
click_buttons = []
press_keys = []
scroll = false
click_regions = []
press_duration = 0.05
max_rate = 1
hold_keys = []
hold_buttons = []

[risk]

[rois]
speaker = {{ title = "名字栏" }}
"""


def write_package(root: Path, name: str, text: str | None = None) -> Path:
    directory = root / name
    directory.mkdir()
    declaration = VALID.format(id=name) if text is None else text
    (directory / ADAPTER_FILE).write_text(declaration, encoding="utf-8", newline="\n")
    return directory


def test_a_valid_package_passes_and_its_uncalibrated_items_are_listed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = write_package(tmp_path, "sample")

    assert main([str(directory)]) == 0

    out = capsys.readouterr().out
    assert "ok   sample：样例 1" in out
    assert "尚未标定：ROI speaker（名字栏）" in out
    # 空的固定文字与没写的阅读节奏不是标定项。
    assert "固定文字" not in out
    assert "阅读节奏" not in out
    assert out.count("尚未标定") == 1


def test_an_invalid_package_fails_with_every_problem(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = write_package(tmp_path, "good")
    text = VALID.format(id="bad").replace("[risk]\n", "").replace("quota_gb = 0", "quota_gb = -1")
    bad = write_package(tmp_path, "bad", text)

    assert main([str(good), str(bad)]) == 1

    captured = capsys.readouterr()
    assert "ok   good" in captured.out
    assert f"FAIL 适配包 {bad.resolve()} 不合法" in captured.err
    assert "compliance.recording_quota_gb：Input should be greater than or equal to 0" in (
        captured.err
    )
    assert "risk：Field required" in captured.err


def test_files_other_than_the_declaration_are_rejected(tmp_path: Path) -> None:
    directory = write_package(tmp_path, "sample")
    (directory / "anchor.png").write_bytes(b"not a real template")
    (directory / "captures").mkdir()
    (directory / ".editorconfig").write_text("root = true\n", encoding="utf-8")

    with pytest.raises(AdapterError) as caught:
        check_package(directory)

    assert caught.value.problems == (
        f"anchor.png：适配包目录里只能有 {ADAPTER_FILE}，从游戏派生的东西放本地数据目录",
        f"captures：适配包目录里只能有 {ADAPTER_FILE}，从游戏派生的东西放本地数据目录",
    )


def test_stray_files_are_reported_together_with_declaration_problems(tmp_path: Path) -> None:
    directory = write_package(tmp_path, "sample", "id = 1\n")
    (directory / "frame.bmp").write_bytes(b"")

    with pytest.raises(AdapterError) as caught:
        check_package(directory)

    assert caught.value.problems[0].startswith("frame.bmp：")
    assert "id：Input should be a valid string" in caught.value.problems


def test_a_missing_directory_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(tmp_path / "absent")]) == 1
    assert "不是目录" in capsys.readouterr().err


def test_games_dir_holds_only_package_directories(tmp_path: Path) -> None:
    write_package(tmp_path, "sample")
    (tmp_path / "screenshot.png").write_bytes(b"")
    (tmp_path / ".gitkeep").write_bytes(b"")

    assert check_games_dir(tmp_path) == [
        f"{tmp_path}：screenshot.png 不是适配包目录，games/ 下只能有适配包目录"
    ]


def test_without_arguments_every_package_in_the_repository_is_checked(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 0

    out = capsys.readouterr().out
    assert "ok   synthetic-game" in out
    assert "ok   witch-trial" in out
