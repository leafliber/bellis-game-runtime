"""在一条 OCR 行内找词项，再用同帧小框复读取得落点。"""

import asyncio
import math
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from game_runtime.adapter import PixelRect
from game_runtime.ocr import EngineOptions, FrameLike, OcrLine, OcrResult

type PhraseIdentity = tuple[str | None, str, str]


class PhraseLocationError(ValueError):
    """词项定位缺少可供提交的证据。"""


@dataclass(frozen=True, slots=True)
class PhraseCandidate:
    source: OcrResult
    source_line: OcrLine
    phrase: str
    """源行中的原始词项。"""
    rect: PixelRect
    """待复读的小框，坐标为客户区物理像素。"""
    min_confidence: float

    @property
    def identity(self) -> PhraseIdentity:
        """跨帧比较用的 ROI、完整源行和词项。"""
        return self.source.roi, phrase_text_key(self.source_line.text), phrase_text_key(self.phrase)


@dataclass(frozen=True, slots=True)
class PhraseLocation:
    candidate: PhraseCandidate
    verification: OcrResult

    @property
    def point(self) -> tuple[int, int]:
        (line,) = self.verification.lines
        box = line.box
        return (box.left + box.right) // 2, (box.top + box.bottom) // 2


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def phrase_text_key(text: str) -> str:
    """统一宽度、大小写和连续空白，保留词与词之间的边界。"""
    return " ".join(_fold(text).split())


def _single_line(text: str) -> bool:
    return text.splitlines() == [text]


def _inside(outer: PixelRect, inner: PixelRect) -> bool:
    return (
        outer.left <= inner.left < inner.right <= outer.right
        and outer.top <= inner.top < inner.bottom <= outer.bottom
    )


def _word_char(char: str) -> bool:
    # 拉丁单词与数字须在词界匹配。连续的全宽文字可直接匹配行内词。
    return char == "_" or (char.isalnum() and unicodedata.east_asian_width(char) not in {"F", "W"})


def _is_mark(char: str) -> bool:
    return unicodedata.category(char).startswith("M")


def _word_boundary(text: str, index: int) -> bool:
    if index == len(text):
        return True
    if _is_mark(text[index]):
        return False
    if index == 0:
        return True
    left = index - 1
    while left >= 0 and _is_mark(text[left]):
        left -= 1
    return left >= 0 and not (_word_char(text[left]) and _word_char(text[index]))


def _source_span(text: str, start: int, end: int) -> tuple[int, int]:
    # 基字符及其附标是一组。规范化后每个字符保留整组的范围。
    parts: list[tuple[int, int, str]] = []
    begin = 0
    for stop in range(1, len(text) + 1):
        if stop == len(text) or not _is_mark(text[stop]):
            parts.append((begin, stop, _fold(text[begin:stop])))
            begin = stop
    if "".join(part for _, _, part in parts) != _fold(text):
        raise PhraseLocationError("unsupported-phrase-layout")
    owners: list[tuple[int, int]] = []
    space = True
    for begin, stop, part in parts:
        for char in part:
            if not char.isspace() or not space:
                owners.append((begin, stop))
            space = char.isspace()
    if space and owners:
        owners.pop()
    if (start > 0 and owners[start - 1] == owners[start]) or (
        end < len(owners) and owners[end - 1] == owners[end]
    ):
        raise PhraseLocationError("unsupported-phrase-layout")
    return owners[start][0], owners[end - 1][1]


def _glyph_width(char: str) -> int:
    """只用于估算，按原字符区分全宽、常见窄字和宽字。"""
    if _is_mark(char):
        return 0
    if unicodedata.east_asian_width(char) in {"F", "W"}:
        return 10
    if char.isspace() or char in "iIl1.,:!'|`":
        return 3
    if char in "mMwW@%":
        return 9
    return 6


def _estimate(line: OcrLine, start: int, end: int) -> PixelRect:
    weights = [_glyph_width(char) for char in line.text]
    if line.text.endswith(("。", "，", "、")):
        # 行框止于行末标点的印迹，给它预留约四成字格。
        weights[-1] = 4
    box = line.box
    width = box.right - box.left
    total = sum(weights)
    left = box.left + width * sum(weights[:start]) // total
    right = box.left + (width * sum(weights[:end]) + total - 1) // total
    return PixelRect(left, box.top, right, box.bottom)


def _check_line(line: OcrLine, rect: PixelRect, min_confidence: float) -> None:
    if not _inside(rect, line.box):
        raise PhraseLocationError("stale-ocr")
    if (
        line.horizontal is not True
        or not _single_line(line.text)
        or any(
            unicodedata.bidirectional(char) in {"R", "AL", "AN"}
            or unicodedata.category(char) == "Cf"
            for char in line.text
        )
    ):
        raise PhraseLocationError("unsupported-phrase-layout")
    if not min_confidence <= line.confidence <= 1:
        raise PhraseLocationError("low-ocr-confidence")


def find_phrase(
    source: OcrResult,
    phrase: str,
    *,
    source_text: str,
    min_confidence: float = EngineOptions.min_confidence,
) -> PhraseCandidate:
    """从一次 OCR 中找唯一完整源行和该行内唯一词项，并估算待复读框。"""
    if not 0 <= min_confidence <= 1:
        raise ValueError("OCR 置信度门槛须在 [0, 1]")
    if not _single_line(phrase) or not _single_line(source_text):
        raise PhraseLocationError("unsupported-phrase-layout")
    key, line_key = phrase_text_key(phrase), phrase_text_key(source_text)
    if not key or not line_key:
        raise PhraseLocationError("target-not-found")

    matches = tuple(line for line in source.lines if phrase_text_key(line.text) == line_key)
    if len(matches) > 1:
        raise PhraseLocationError("ambiguous-target")
    if not matches:
        raise PhraseLocationError("target-not-found")
    (line,) = matches
    match: int | None = None
    start = line_key.find(key)
    while start >= 0:
        end = start + len(key)
        if _word_boundary(line_key, start) and _word_boundary(line_key, end):
            if match is not None:
                raise PhraseLocationError("ambiguous-target")
            match = start
        start = line_key.find(key, start + 1)
    if match is None:
        raise PhraseLocationError("target-not-found")
    _check_line(line, source.rect, min_confidence)
    raw_start, raw_end = _source_span(line.text, match, match + len(key))
    return PhraseCandidate(
        source,
        line,
        line.text[raw_start:raw_end],
        _estimate(line, raw_start, raw_end),
        min_confidence,
    )


async def verify_phrase(
    frame: FrameLike,
    candidate: PhraseCandidate,
    recognize_rect: Callable[[FrameLike, PixelRect], Awaitable[OcrResult]],
    *,
    timeout: float,
) -> PhraseLocation:
    """限时复读一次小框，保留对应帧、源行和实际识别框供提交端核对。"""
    source = candidate.source
    if (
        frame.frame_number != source.frame_number
        or frame.transform_version != source.transform_version
        or not _inside(PixelRect(0, 0, frame.width, frame.height), source.rect)
        or not any(line is candidate.source_line for line in source.lines)
        or not _inside(source.rect, candidate.source_line.box)
        or not _inside(candidate.source_line.box, candidate.rect)
    ):
        raise PhraseLocationError("stale-ocr")
    if not math.isfinite(timeout) or timeout <= 0:
        raise PhraseLocationError("phrase-ocr-timeout")
    try:
        async with asyncio.timeout(timeout):
            verified = await recognize_rect(frame, candidate.rect)
    except TimeoutError:
        raise PhraseLocationError("phrase-ocr-timeout") from None
    if (
        verified.frame_number != source.frame_number
        or verified.transform_version != source.transform_version
        or verified.roi is not None
        or verified.rect != candidate.rect
    ):
        raise PhraseLocationError("stale-ocr")
    if len(verified.lines) != 1:
        raise PhraseLocationError("phrase-ocr-mismatch")
    (line,) = verified.lines
    _check_line(line, candidate.rect, candidate.min_confidence)
    if phrase_text_key(line.text) != phrase_text_key(candidate.phrase):
        raise PhraseLocationError("phrase-ocr-mismatch")
    return PhraseLocation(candidate, verified)
