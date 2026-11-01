"""语言核对的文字系统一层：语言标签到文字系统、字母的计数与比例、`ocr@1` 的 `check_script`。

规则见 `docs/modules/game-adapter.md` 的“语言核对”。样本都是为测试写的合成文本：中文相符，日文、
韩文与拉丁字母的文本在中文版上不符。
"""

import asyncio

import pytest

from game_runtime.adapter import Adapter, Language
from game_runtime.ocr import (
    SCRIPT_MIN_RATIO,
    OcrService,
    Script,
    UnsupportedLanguageError,
    UnsupportedScriptError,
    check_script,
    script_of,
)
from game_runtime.ocr.fake import ScriptedRecognizer
from game_runtime.ocr.script import count_letters

CHINESE = [
    "雨停了，钟楼的指针停在七点。",
    "门后也许有路，也许什么都没有。",
    "“你听见了吗？”",
    "好的～♪",  # 符号不计
    "第３层，还剩 12 级台阶。",  # 数字不计
    "嗯。",
]
JAPANESE = [
    "これはテストです。",
    "今日は雨が降っています。",  # 汉字与假名混写：汉字不到八成
    "ドアの向こうに、誰かいる？",
]
KOREAN = ["안녕하세요, 반갑습니다.", "문 뒤에 길이 있을지도 몰라."]
LATIN = ["The rain has stopped.", "Peut-être qu'il y a un chemin.", "Tiếng chuông đã ngừng."]


@pytest.mark.parametrize(
    ("tag", "script"),
    [
        ("zh-Hans", Script.HAN),
        ("zh-Hans-CN", Script.HAN),
        ("zh-Hant-TW", Script.HAN),
        ("zh", Script.HAN),
        ("ja", Script.JAPANESE),
        ("ja-Jpan-JP", Script.JAPANESE),
        ("ko", Script.KOREAN),
        ("ko-Kore", Script.KOREAN),
        ("en", Script.LATIN),
        ("en-US", Script.LATIN),
        ("en-Latn", Script.LATIN),
        ("fr-CA", Script.LATIN),
        ("vi", Script.LATIN),
        ("zh-Latn", None),  # 文字子标签与语言不一致
        ("ja-Latn", None),
        ("en-Hans", None),
        ("ru", None),  # 西里尔字母不受支持
        ("ar", None),
        ("sr-Latn", None),
    ],
)
def test_the_script_follows_the_language_and_must_agree_with_its_subtag(
    tag: str, script: Script | None
) -> None:
    assert script_of(tag) is script


@pytest.mark.parametrize("text", CHINESE)
def test_chinese_matches_the_chinese_version(text: str) -> None:
    check = check_script(text, "zh-Hans")

    assert check.matches is True
    assert check.ratio == 1.0
    assert (check.tag, check.script, check.text) == ("zh-Hans", Script.HAN, text)


@pytest.mark.parametrize("text", JAPANESE + KOREAN + LATIN)
def test_other_scripts_do_not_match_the_chinese_version(text: str) -> None:
    check = check_script(text, "zh-Hans")

    assert check.matches is False
    assert check.ratio is not None
    assert check.ratio < SCRIPT_MIN_RATIO


@pytest.mark.parametrize(
    ("tag", "own", "others"),
    [
        ("ja", JAPANESE, KOREAN + LATIN),
        ("ko", KOREAN, JAPANESE[:1] + LATIN),
        ("en", LATIN, CHINESE[:2] + JAPANESE + KOREAN),
    ],
)
def test_each_script_matches_its_own_text_only(tag: str, own: list[str], others: list[str]) -> None:
    assert all(check_script(text, tag).matches for text in own)
    assert not any(check_script(text, tag).matches for text in others)


def test_chinese_text_matches_japanese_since_han_is_part_of_it() -> None:
    """同一文字系统内分不出变体：汉字是日文的一部分，简体与繁体都是汉字。"""
    assert check_script(CHINESE[0], "ja").matches
    assert check_script("雨停了，鐘樓的指針停在七點。", "zh-Hans").matches


@pytest.mark.parametrize("text", ["", "……", "！？", "123", "～♪ ★", " 　\n"])
def test_text_without_letters_is_undecided(text: str) -> None:
    check = check_script(text, "zh-Hans")

    assert (check.letters, check.ratio, check.matches) == (0, None, None)


def test_the_threshold_is_four_fifths_of_the_letters() -> None:
    assert check_script("雨停了雨停A", "zh-Hans").ratio == pytest.approx(5 / 6)
    assert check_script("雨停了雨A", "zh-Hans").matches  # 恰为 0.8
    assert not check_script("雨停了AB", "zh-Hans").matches  # 0.6


def test_letters_are_counted_after_nfkc() -> None:
    """全角拉丁字母与半角片假名归一后照常计数；长音符算假名。"""
    assert count_letters("ＡＢＣ", Script.LATIN) == (3, 3)
    assert count_letters("ｶﾀｶﾅ", Script.JAPANESE) == (4, 4)
    assert count_letters("ラーメン", Script.JAPANESE) == (4, 4)
    assert count_letters("ラーメン", Script.HAN) == (4, 0)


def test_an_unsupported_tag_is_an_error() -> None:
    with pytest.raises(UnsupportedScriptError, match="语言 ru 的文字系统不受支持"):
        check_script("Дождь", "ru")


def test_the_service_checks_against_the_adapter_language(synthetic_adapter: Adapter) -> None:
    service = OcrService(synthetic_adapter, ScriptedRecognizer(), model="fake")
    try:
        assert service.script is Script.HAN
        assert service.check_script(CHINESE[0]).matches
        japanese = service.check_script(JAPANESE[0])
        assert (japanese.tag, japanese.matches) == ("zh-Hans", False)
        assert service.check_script("……").matches is None
    finally:
        asyncio.run(service.aclose())
    # 纯计算：停用之后也照样可用，不经工作线程。
    assert service.check_script(CHINESE[1]).matches


def test_the_service_refuses_an_unsupported_script(synthetic_adapter: Adapter) -> None:
    adapter = synthetic_adapter.model_copy(
        update={"language": Language(tag="zh-Latn", fixed_texts=())}
    )

    with pytest.raises(UnsupportedLanguageError, match="语言 zh-Latn 的文字系统不受支持"):
        OcrService(adapter, ScriptedRecognizer(), model="fake")
