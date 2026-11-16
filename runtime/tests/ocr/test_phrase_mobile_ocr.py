"""用本机已核对摘要的 mobile OCR 对合成词项做同帧复读。"""

import asyncio
import math

import pytest
from PIL import Image, ImageDraw, ImageFont

from game_runtime.adapter import Adapter, PixelRect
from game_runtime.frame_ring import RingFrame
from game_runtime.ocr import OcrEngine, OcrService
from game_runtime.step_control.phrase_location import (
    PhraseLocationError,
    find_phrase,
    phrase_text_key,
    verify_phrase,
)
from synthetic_game import GameConfig, Key, ManualClock, Press, SyntheticGame
from synthetic_game.render import FONT_FILE
from synthetic_game.story import NODES, Dialogue

pytestmark = pytest.mark.ocr_model
WIDTH, HEIGHT = 1280, 720


def _frame(pixels: bytes) -> RingFrame:
    return RingFrame(1, 0, 1, WIDTH, HEIGHT, WIDTH * 4, pixels)


def _line_image(roi: PixelRect, text: str, size: int, angle: int = 0) -> bytes:
    font = ImageFont.truetype(FONT_FILE, size, layout_engine=ImageFont.Layout.BASIC)
    image = Image.new("RGBA", (WIDTH, HEIGHT), (22, 24, 34, 255))
    at = roi.left + 20, roi.top + 18
    ink = (245, 245, 240, 255)
    if angle == 0:
        ImageDraw.Draw(image).text(at, text, fill=ink, font=font, anchor="lt")
    else:
        layer = Image.new("RGBA", (math.ceil(font.getlength(text)) + 12, size + 16))
        ImageDraw.Draw(layer).text((5, 7), text, fill=ink, font=font, anchor="lt")
        rotated = layer.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)
        image.alpha_composite(rotated, at)
    return image.tobytes("raw", "BGRA")


async def _verify(
    adapter: Adapter, engine: OcrEngine, frame: RingFrame, source_text: str, phrase: str
) -> None:
    ocr = OcrService(adapter, engine, model=engine.model)
    try:
        source = await ocr.recognize(frame, "dialogue")
        candidate = find_phrase(source, phrase, source_text=source_text)
        assert candidate.source_line.horizontal is True
        result = await verify_phrase(frame, candidate, ocr.recognize_rect, timeout=1)
    finally:
        await ocr.aclose()
    (verified,) = result.verification.lines
    assert verified.horizontal is True
    assert phrase_text_key(verified.text) == phrase_text_key(phrase)
    assert result.verification.frame_number == source.frame_number
    assert result.verification.transform_version == source.transform_version
    box = verified.box
    assert result.point == ((box.left + box.right) // 2, (box.top + box.bottom) // 2)


@pytest.mark.parametrize(("index", "phrase"), [(0, "钟楼"), (1, "敲门声"), (2, "什么都没有")])
def test_mobile_ocr_locates_chinese_words(
    synthetic_adapter: Adapter, real_engine: OcrEngine, index: int, phrase: str
) -> None:
    clock = ManualClock()
    game = SyntheticGame(GameConfig(WIDTH, HEIGHT), clock)
    for _ in range(index + 1):
        game.handle(Press(Key.ENTER))
        clock.advance(1.0)
    opening = NODES["intro"]
    assert isinstance(opening, Dialogue)
    asyncio.run(
        _verify(
            synthetic_adapter,
            real_engine,
            _frame(game.frame().data),
            opening.lines[index].text,
            phrase,
        )
    )


@pytest.mark.parametrize(
    ("text", "phrase", "size"),
    [
        ("Wi Lock Door mm", "Lock Door", 42),
        ("Wi Lock Door mm", "Lock Door", 32),
        ("Open the side door", "side door", 36),
    ],
)
def test_mobile_ocr_locates_phrases_with_proportional_glyph_widths(
    synthetic_adapter: Adapter, real_engine: OcrEngine, text: str, phrase: str, size: int
) -> None:
    roi = synthetic_adapter.roi("dialogue").to_pixels(WIDTH, HEIGHT)
    frame = _frame(_line_image(roi, text, size))
    asyncio.run(_verify(synthetic_adapter, real_engine, frame, text, phrase))


@pytest.mark.parametrize(
    ("text", "phrase", "size", "angle"),
    [
        ("Wi Lock Door mm", "Lock Door", 42, 8),
        ("雨停了，钟楼的指针停在七点。", "钟楼", 36, -10),
    ],
)
def test_mobile_ocr_rejects_rotated_text_it_can_read(
    synthetic_adapter: Adapter,
    real_engine: OcrEngine,
    text: str,
    phrase: str,
    size: int,
    angle: int,
) -> None:
    roi = synthetic_adapter.roi("dialogue").to_pixels(WIDTH, HEIGHT)
    frame = _frame(_line_image(roi, text, size, angle))

    async def check() -> None:
        ocr = OcrService(synthetic_adapter, real_engine, model=real_engine.model)
        try:
            source = await ocr.recognize(frame, "dialogue")
        finally:
            await ocr.aclose()
        (line,) = source.lines
        assert phrase_text_key(line.text) == phrase_text_key(text)
        assert line.horizontal is False
        with pytest.raises(PhraseLocationError, match="unsupported-phrase-layout"):
            find_phrase(source, phrase, source_text=text)

    asyncio.run(check())
