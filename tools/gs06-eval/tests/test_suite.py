"""整组样本的清单 `suite.toml`：读写与校验。"""

from pathlib import Path

import pytest

from gs06_eval.suite import (
    SUITE_FILE,
    Suite,
    SuiteBackground,
    SuiteError,
    SuiteLine,
    read_suite,
    write_suite,
)


def test_round_trip(tmp_path: Path) -> None:
    suite = Suite(
        voice="voice.wav",
        mix="mix.wav",
        backgrounds=(
            SuiteBackground(name="music", file="music.wav"),
            SuiteBackground(name="vocal-music", file="vocal music.wav", vocal=True),
        ),
        lines=(
            SuiteLine(
                voice="Voice “A”", culture="zh-CN", rate=-3, start=1.0, end=2.5, text="好的。\\"
            ),
        ),
    )
    path = write_suite(tmp_path, suite, ["第一行", "第二行"])
    assert path == tmp_path / SUITE_FILE
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# 第一行\n# 第二行\n")
    assert "\r" not in text
    assert read_suite(tmp_path) == suite


@pytest.mark.parametrize(
    ("body", "message"),
    [
        pytest.param('mix = "m.wav"\n', "voice", id="no voice"),
        pytest.param('voice = "v.wav"\nspeed = 1\n', "Extra inputs", id="unknown field"),
        pytest.param(
            'voice = "v.wav"\n[[backgrounds]]\nname = "a"\nfile = "a.wav"\n'
            '[[backgrounds]]\nname = "a"\nfile = "b.wav"\n',
            "重复",
            id="duplicate background",
        ),
        pytest.param(
            'voice = "v.wav"\n[[backgrounds]]\nname = "Music"\nfile = "a.wav"\n',
            "pattern",
            id="bad name",
        ),
        pytest.param(
            'voice = "v.wav"\n[[lines]]\nvoice = "a"\nrate = 0\nstart = 2.0\nend = 1.0\n'
            'text = "x"\n',
            "end 须大于 start",
            id="reversed line",
        ),
        pytest.param('voice = "v.wav"\n[[backgrounds]]\nname = ', "TOML", id="not toml"),
    ],
)
def test_invalid_suites_are_rejected(tmp_path: Path, body: str, message: str) -> None:
    (tmp_path / SUITE_FILE).write_text(body, encoding="utf-8")
    with pytest.raises(SuiteError, match=message):
        read_suite(tmp_path)


def test_a_missing_suite_is_reported(tmp_path: Path) -> None:
    with pytest.raises(SuiteError, match=SUITE_FILE):
        read_suite(tmp_path)
