"""语言核对的文字系统一层：一段文字里属于适配包语言的文字系统的字母比例。纯文本计算，不做
OCR。

规则见 `docs/modules/game-adapter.md` 的“语言核对”，执行见 `docs/modules/perception.md` 的
“语言核对的执行”。`ocr@1` 的 `check_script` 用这里的计数；只读运行的 `observer` 按剧情记录里
完整的句子取用它。
"""

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Script(StrEnum):
    """支持的文字系统。"""

    HAN = "han"
    """汉字：`zh-*`。"""
    JAPANESE = "japanese"
    """汉字、平假名与片假名：`ja`。"""
    KOREAN = "korean"
    """谚文与汉字：`ko`。"""
    LATIN = "latin"
    """拉丁字母：`en` 等以拉丁字母书写的语言。"""

    @property
    def label(self) -> str:
        """给人读的名字。"""
        return _LABELS[self]


_LABELS: Final = {
    Script.HAN: "汉字",
    Script.JAPANESE: "汉字与假名",
    Script.KOREAN: "谚文与汉字",
    Script.LATIN: "拉丁字母",
}

SCRIPT_MIN_RATIO: Final = 0.8
"""一句的字母里属于这个文字系统的比例不低于它即这一句相符。"""
SCRIPT_SENTENCES: Final = 3
"""连续这么多句判断了的完整句子都相符才算通过；任一句不符即不符。"""

LATIN_LANGUAGES: Final = frozenset({
    "af", "ca", "cs", "cy", "da", "de", "en", "eo", "es", "et", "eu", "fi", "fil", "fr",
    "ga", "gl", "hr", "hu", "id", "is", "it", "lt", "lv", "ms", "mt", "nb", "nl", "nn",
    "no", "pl", "pt", "ro", "sk", "sl", "sq", "sv", "sw", "tl", "tr", "vi",
})  # fmt: skip
"""以拉丁字母书写的语言（BCP 47 的语言子标签）。按需要加，加了即支持。"""

_LANGUAGES: Final = {
    "zh": Script.HAN,
    "ja": Script.JAPANESE,
    "ko": Script.KOREAN,
    **dict.fromkeys(LATIN_LANGUAGES, Script.LATIN),
}
_SCRIPT_SUBTAGS: Final = {
    Script.HAN: frozenset({"Hans", "Hant", "Hani"}),
    Script.JAPANESE: frozenset({"Jpan"}),
    Script.KOREAN: frozenset({"Kore", "Hang"}),
    Script.LATIN: frozenset({"Latn"}),
}

type _Ranges = Sequence[tuple[int, int]]

_HAN: Final[_Ranges] = (
    (0x2E80, 0x2FDF),  # 部首（NFKC 后多为统一汉字）
    (0x3005, 0x3005),  # 々
    (0x303B, 0x303B),  # 〻
    (0x3400, 0x4DBF),  # 扩展 A
    (0x4E00, 0x9FFF),  # 统一汉字
    (0xF900, 0xFAFF),  # 兼容汉字
    (0x20000, 0x3FFFF),  # 扩展 B 及以后、兼容补充
)
_KANA: Final[_Ranges] = (
    (0x3040, 0x309F),  # 平假名
    (0x30A0, 0x30FF),  # 片假名，含长音符“ー”
    (0x31F0, 0x31FF),  # 片假名音标扩展
    (0x1B000, 0x1B16F),  # 假名补充与扩展
)
_HANGUL: Final[_Ranges] = (
    (0x1100, 0x11FF),  # 谚文字母
    (0x3130, 0x318F),  # 兼容字母
    (0xA960, 0xA97F),
    (0xAC00, 0xD7AF),  # 音节
    (0xD7B0, 0xD7FF),
)
_LATIN: Final[_Ranges] = (
    (0x0041, 0x005A),
    (0x0061, 0x007A),
    (0x00AA, 0x00AA),
    (0x00BA, 0x00BA),
    (0x00C0, 0x024F),  # 拉丁补充、扩展 A 与 B（× ÷ 是符号，不计入）
    (0x0250, 0x02AF),  # 国际音标
    (0x1D00, 0x1DBF),
    (0x1E00, 0x1EFF),  # 拉丁扩展附加（越南文）
    (0x2C60, 0x2C7F),
    (0xA720, 0xA7FF),
    (0xAB30, 0xAB6F),
)
_RANGES: Final[dict[Script, _Ranges]] = {
    Script.HAN: _HAN,
    Script.JAPANESE: (*_HAN, *_KANA),
    Script.KOREAN: (*_HANGUL, *_HAN),
    Script.LATIN: _LATIN,
}


def script_of(tag: str) -> Script | None:
    """语言标签 `tag`（BCP 47 的语言加可选的文字与地区）所指的文字系统；不支持即 None。

    按语言子标签定；带文字子标签时须与之一致，如 `zh-Hans`、`ja-Jpan`、`en-Latn`，`zh-Latn` 不支持。
    """
    language, *rest = tag.split("-")
    script = _LANGUAGES.get(language)
    if script is None:
        return None
    subtag = rest[0] if rest and len(rest[0]) == 4 else None
    if subtag is not None and subtag not in _SCRIPT_SUBTAGS[script]:
        return None
    return script


def supported_languages() -> str:
    """支持的语言，给人读。"""
    return "zh-*、ja、ko 与以拉丁字母书写的语言（" + "、".join(sorted(LATIN_LANGUAGES)) + "）"


def _within(code: int, ranges: _Ranges) -> bool:
    return any(low <= code <= high for low, high in ranges)


def is_letter(char: str) -> bool:
    """计数的字符：Unicode 一般类别 L（字母）。空白、数字、标点、符号与组合记号都不计。"""
    return unicodedata.category(char).startswith("L")


def count_letters(text: str, script: Script) -> tuple[int, int]:
    """NFKC 归一后的字母数，以及其中属于 `script` 的个数。"""
    ranges = _RANGES[script]
    letters = matching = 0
    for char in unicodedata.normalize("NFKC", text):
        if not is_letter(char):
            continue
        letters += 1
        if _within(ord(char), ranges):
            matching += 1
    return letters, matching


@dataclass(frozen=True, slots=True)
class ScriptCheck:
    """一段文字的文字系统核对。"""

    tag: str
    """适配包声明的语言版本。"""
    script: Script
    text: str
    letters: int
    """计数的字母数（NFKC 归一后）。"""
    matching: int
    """其中属于 `script` 的个数。"""

    @property
    def ratio(self) -> float | None:
        """属于这个文字系统的比例；没有可数的字母时为 None（无从判断）。"""
        return self.matching / self.letters if self.letters else None

    @property
    def matches(self) -> bool | None:
        """比例不低于 `SCRIPT_MIN_RATIO` 即相符；无从判断时为 None。"""
        ratio = self.ratio
        return None if ratio is None else ratio >= SCRIPT_MIN_RATIO


class UnsupportedScriptError(ValueError):
    """语言标签所指的文字系统不受支持。"""


def check_script(text: str, tag: str) -> ScriptCheck:
    """核对 `text` 是否属于语言 `tag` 的文字系统。文字系统不受支持即 `UnsupportedScriptError`。"""
    script = script_of(tag)
    if script is None:
        raise UnsupportedScriptError(f"语言 {tag} 的文字系统不受支持；支持 {supported_languages()}")
    letters, matching = count_letters(text, script)
    return ScriptCheck(tag, script, text, letters, matching)
