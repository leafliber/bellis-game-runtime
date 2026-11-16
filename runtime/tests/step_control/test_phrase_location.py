"""合成 OCR 证据下的词项定位与同帧复读。"""

import asyncio
from dataclasses import replace

import numpy as np
import pytest

from game_runtime.adapter import GAMES_DIR, PixelRect, load_adapter
from game_runtime.frame_ring import RingFrame
from game_runtime.ocr import FrameLike, OcrLine, OcrResult, OcrService, RecognizedLine
from game_runtime.ocr.engine import Image
from game_runtime.ocr.fake import ScriptedRecognizer
from game_runtime.step_control.phrase_location import (
    PhraseCandidate,
    PhraseLocation,
    PhraseLocationError,
    find_phrase,
    phrase_text_key,
    verify_phrase,
)

AREA = PixelRect(20, 10, 160, 90)
LINE = OcrLine("请先关门吧", PixelRect(30, 20, 80, 40), 0.9, horizontal=True)


def _source(*lines: OcrLine) -> OcrResult:
    return OcrResult(42, 5, "text", AREA, lines)


def _candidate(*, min_confidence: float = 0.5) -> PhraseCandidate:
    return find_phrase(_source(LINE), "关门", source_text=LINE.text, min_confidence=min_confidence)


def _frame() -> RingFrame:
    width, height, padding = 180, 100, 8
    stride = width * 4 + padding
    pixels = np.zeros((height, stride), np.uint8)
    pixels[:, : width * 4 : 4] = np.arange(width, dtype=np.uint8)
    pixels[:, 1 : width * 4 : 4] = np.arange(height, dtype=np.uint8)[:, np.newaxis]
    return RingFrame(42, 10, 5, width, height, stride, pixels.tobytes())


@pytest.mark.parametrize(
    ("line", "target", "expected_text", "expected_crop"),
    [
        pytest.param(LINE, "关门", "关门", PixelRect(50, 20, 70, 40), id="han"),
        pytest.param(
            OcrLine("请先关门。", PixelRect(30, 20, 74, 40), 0.9, horizontal=True),
            "关门",
            "关门",
            PixelRect(50, 20, 70, 40),
            id="visible-trailing-punctuation",
        ),
        pytest.param(
            OcrLine("Wi Lock mm", PixelRect(40, 20, 100, 40), 0.9, horizontal=True),
            "lock",
            "Lock",
            PixelRect(55, 20, 79, 40),
            id="proportional-latin",
        ),
        pytest.param(
            OcrLine("Wi Lock   Door mm", PixelRect(40, 20, 133, 40), 0.9, horizontal=True),
            "lock door",
            "Lock   Door",
            PixelRect(55, 20, 112, 40),
            id="proportional-phrase-with-spaces",
        ),
        pytest.param(
            OcrLine("送出 ＬＯＣＫ 吧", PixelRect(30, 20, 106, 40), 0.9, horizontal=True),
            "lock",
            "ＬＯＣＫ",
            PixelRect(53, 20, 93, 40),
            id="fullwidth-source-characters",
        ),
    ],
)
def test_unique_phrase_uses_source_glyph_widths(
    line: OcrLine, target: str, expected_text: str, expected_crop: PixelRect
) -> None:
    source = _source(line)
    found = find_phrase(source, target, source_text=line.text)

    assert found.source is source
    assert found.source_line is line
    assert found.phrase == expected_text
    assert found.rect == expected_crop


def test_identity_can_rebind_the_full_line_in_a_fresh_roi() -> None:
    before = replace(LINE, text="Wi ＬＯＣＫ   Door mm")
    after = replace(LINE, text="wi lock door mm", box=PixelRect(40, 30, 130, 50))
    nearby = replace(LINE, text="Lock door later", box=PixelRect(40, 55, 130, 75))
    opened = find_phrase(_source(before), "LOCK Door", source_text=before.text)
    fresh = replace(_source(nearby, after), frame_number=43)
    current = find_phrase(fresh, opened.phrase, source_text=opened.source_line.text)

    assert current.source_line is after
    assert current.identity == opened.identity == ("text", "wi lock door mm", "lock door")
    assert current.rect != opened.rect
    assert phrase_text_key("LockDoor") != phrase_text_key("Lock Door")
    moved_roi = find_phrase(replace(fresh, roi="another"), "lock door", source_text=after.text)
    assert moved_roi.identity != opened.identity


@pytest.mark.parametrize(
    ("line_text", "source_text", "target", "raw"),
    [
        ("Cafe\u0301 dehors", "Café dehors", "CAFÉ", "Cafe\u0301"),
        ("Straße weiter", "Straße weiter", "STRASSE", "Straße"),
        ("猫\ufe0f咪", "猫\ufe0f咪", "咪", "咪"),
    ],
)
def test_comparison_keeps_complete_source_characters(
    line_text: str, source_text: str, target: str, raw: str
) -> None:
    source = _source(replace(LINE, text=line_text))

    assert find_phrase(source, target, source_text=source_text).phrase == raw


@pytest.mark.parametrize(
    ("lines", "target", "reason"),
    [
        pytest.param(
            (replace(LINE, text="关门或关门"),), "关门", "ambiguous-target", id="repeat-in-line"
        ),
        pytest.param(
            (replace(LINE, text="哈哈哈"),),
            "哈哈",
            "ambiguous-target",
            id="overlapping-occurrences",
        ),
        pytest.param(
            (LINE, replace(LINE, box=PixelRect(30, 45, 80, 65), confidence=0.4)),
            "关门",
            "ambiguous-target",
            id="repeat-across-lines",
        ),
        pytest.param(
            (replace(LINE, text="Unlock"),), "lock", "target-not-found", id="partial-latin-word"
        ),
        pytest.param(
            (replace(LINE, text="cat\u0301foo"),),
            "cat",
            "target-not-found",
            id="prefix-before-noncomposing-mark",
        ),
        pytest.param(
            (replace(LINE, text="cat\u0301foo"),),
            "foo",
            "target-not-found",
            id="suffix-after-noncomposing-mark",
        ),
        pytest.param(
            (replace(LINE, text="猫\ufe0f咪"),),
            "猫",
            "target-not-found",
            id="before-chinese-variation-selector",
        ),
        pytest.param(
            (replace(LINE, text="关"), replace(LINE, text="门", box=PixelRect(30, 45, 80, 65))),
            "关门",
            "target-not-found",
            id="across-line-boxes",
        ),
        pytest.param(
            (replace(LINE, text="关闭\n房间"),),
            "关闭 房间",
            "unsupported-phrase-layout",
            id="multiline-source",
        ),
        pytest.param(
            (replace(LINE, horizontal=False),), "关门", "unsupported-phrase-layout", id="rotated"
        ),
        pytest.param(
            (replace(LINE, horizontal=None),),
            "关门",
            "unsupported-phrase-layout",
            id="no-direction",
        ),
        pytest.param(
            (replace(LINE, confidence=0.4),), "关门", "low-ocr-confidence", id="low-confidence"
        ),
        pytest.param(
            (replace(LINE, confidence=float("nan")),),
            "关门",
            "low-ocr-confidence",
            id="invalid-confidence",
        ),
        pytest.param(
            (replace(LINE, text="㍿"),),
            "会社",
            "unsupported-phrase-layout",
            id="partial-normalized-glyph",
        ),
        pytest.param(
            (replace(LINE, box=PixelRect(0, 5, 80, 45)),), "关门", "stale-ocr", id="outside-source"
        ),
    ],
)
def test_invalid_source_does_not_locate(
    lines: tuple[OcrLine, ...], target: str, reason: str
) -> None:
    full_line = " ".join(lines[0].text.splitlines())
    with pytest.raises(PhraseLocationError, match=reason):
        find_phrase(_source(*lines), target, source_text=full_line)


def test_same_frame_crop_returns_ocr_box_center_in_client_pixels() -> None:
    candidate = _candidate()
    frame = _frame()

    def visible_crop(image: Image) -> list[RecognizedLine]:
        assert image.shape == (20, 20, 3)
        assert image[0, 0].tolist() == [candidate.rect.left, candidate.rect.top, 0]
        assert image[-1, -1].tolist() == [candidate.rect.right - 1, candidate.rect.bottom - 1, 0]
        return [RecognizedLine("关门", PixelRect(1, 3, 9, 11), 0.9, horizontal=True)]

    recognizer = ScriptedRecognizer(answer=visible_crop)
    ocr = OcrService(load_adapter(GAMES_DIR / "synthetic-game"), recognizer, model="fake")

    async def locate() -> PhraseLocation:
        try:
            return await verify_phrase(frame, candidate, ocr.recognize_rect, timeout=1)
        finally:
            await ocr.aclose()

    result = asyncio.run(locate())
    assert result.candidate is candidate
    assert result.verification.frame_number == frame.frame_number
    assert result.verification.transform_version == frame.transform_version
    assert result.verification.rect == candidate.rect
    assert result.verification.lines[0].box == PixelRect(51, 23, 59, 31)
    assert result.point == (55, 27)
    assert len(recognizer.images) == 1


@pytest.mark.parametrize(
    "frame", [replace(_frame(), frame_number=43), replace(_frame(), transform_version=6)]
)
def test_stale_frame_is_refused_before_cropping(frame: RingFrame) -> None:
    candidate = _candidate()

    async def unexpected_read(_frame: FrameLike, _rect: PixelRect) -> OcrResult:
        pytest.fail("不应为过期证据启动 OCR")

    with pytest.raises(PhraseLocationError, match="stale-ocr"):
        asyncio.run(verify_phrase(frame, candidate, unexpected_read, timeout=1))


def test_source_line_identity_cannot_be_replaced_after_location() -> None:
    candidate = _candidate()
    changed = replace(candidate, source=replace(candidate.source, lines=(replace(LINE),)))

    async def unexpected_read(_frame: FrameLike, _rect: PixelRect) -> OcrResult:
        pytest.fail("不应为已换源行的证据启动 OCR")

    with pytest.raises(PhraseLocationError, match="stale-ocr"):
        asyncio.run(verify_phrase(_frame(), changed, unexpected_read, timeout=1))


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        pytest.param({"frame_number": 43}, "stale-ocr", id="frame"),
        pytest.param({"transform_version": 6}, "stale-ocr", id="transform"),
        pytest.param({"rect": AREA}, "stale-ocr", id="different-crop"),
        pytest.param({"lines": ()}, "phrase-ocr-mismatch", id="empty"),
        pytest.param({"lines": (LINE, LINE)}, "phrase-ocr-mismatch", id="multiple-lines"),
    ],
)
def test_bad_crop_result_is_refused(change: dict[str, object], reason: str) -> None:
    candidate = _candidate()

    async def read(frame: FrameLike, rect: PixelRect) -> OcrResult:
        result = OcrResult(
            frame.frame_number,
            frame.transform_version,
            None,
            rect,
            (OcrLine("关门", rect, 0.9, horizontal=True),),
        )
        return replace(result, **change)

    with pytest.raises(PhraseLocationError, match=reason):
        asyncio.run(verify_phrase(_frame(), candidate, read, timeout=1))


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        pytest.param(
            OcrLine("阖门", PixelRect(51, 23, 59, 31), 0.9, horizontal=True),
            "phrase-ocr-mismatch",
            id="different-word",
        ),
        pytest.param(
            OcrLine("关门", PixelRect(51, 23, 59, 31), 0.79, horizontal=True),
            "low-ocr-confidence",
            id="below-configured-confidence",
        ),
        pytest.param(
            OcrLine("关门", PixelRect(51, 23, 59, 31), 0.9, horizontal=False),
            "unsupported-phrase-layout",
            id="cropped-direction",
        ),
        pytest.param(
            OcrLine("关门", PixelRect(51, 23, 59, 31), 0.9),
            "unsupported-phrase-layout",
            id="missing-cropped-direction",
        ),
        pytest.param(
            OcrLine("关门", PixelRect(48, 23, 59, 31), 0.9, horizontal=True),
            "stale-ocr",
            id="outside-crop",
        ),
    ],
)
def test_invalid_rechecked_line_is_refused(line: OcrLine, reason: str) -> None:
    candidate = _candidate(min_confidence=0.8)

    async def read(frame: FrameLike, rect: PixelRect) -> OcrResult:
        return OcrResult(frame.frame_number, frame.transform_version, None, rect, (line,))

    with pytest.raises(PhraseLocationError, match=reason):
        asyncio.run(verify_phrase(_frame(), candidate, read, timeout=1))


def test_crop_times_out_with_no_fallback() -> None:
    candidate = _candidate()

    async def stalled_read(_frame: FrameLike, _rect: PixelRect) -> OcrResult:
        return await asyncio.Future[OcrResult]()

    with pytest.raises(PhraseLocationError, match="phrase-ocr-timeout"):
        asyncio.run(verify_phrase(_frame(), candidate, stalled_read, timeout=0.01))
