"""GS-04 评测工具：口径（编辑距离、字符准确率、时延分位）、样本格式与标签、合成游戏与泛化的
合成样本、按类别与标签的评测与报告。

引擎用一个按真值回答的假引擎，不需要模型；另有两项在本机有模型或有 Windows OCR 时跑真实引擎。泛化
样本在自动测试里用合成游戏的字体子集，与本机装了哪些字体无关；系统字体只核对能找到并渲染。
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from game_runtime.adapter import PixelRect
from game_runtime.ocr import EngineOptions, FrameLike
from gs04_ocr.cli import run
from gs04_ocr.engines import create_engine
from gs04_ocr.errors import EngineUnavailableError
from gs04_ocr.evaluate import (
    TOTAL,
    build_report,
    run_engine,
    summarize,
    summarize_tags,
    write_report,
)
from gs04_ocr.general import (
    BACKGROUNDS,
    FALLBACK_FONT,
    LINES,
    SIZES,
    STYLES,
    SYSTEM_FONTS,
    FontChoice,
    Variant,
    available_fonts,
    coverage,
    render,
    system_fonts,
    texts_for,
    variants,
)
from gs04_ocr.metrics import accuracy, edit_distance, latency, nfkc_text, percentile, strict_text
from gs04_ocr.samples import (
    SAMPLES_FILE,
    LoadedSample,
    Sample,
    SampleError,
    load_samples,
    read_png,
    write_png,
    write_sample_set,
)
from gs04_ocr.synth import write_synthetic_samples
from synthetic_game.story import NODES, Dialogue, all_text

# 口径


@pytest.mark.parametrize(
    ("expected", "actual", "distance"),
    [
        ("", "", 0),
        ("开始", "开始", 0),
        ("开始", "开", 1),
        ("开始", "开始了", 1),
        ("开始", "关始", 1),
        ("雨停了，钟楼", "雨停了钟楼", 1),
        ("abc", "", 3),
        ("", "xy", 2),
    ],
)
def test_edit_distance(expected: str, actual: str, distance: int) -> None:
    assert edit_distance(expected, actual) == distance
    assert edit_distance(actual, expected) == distance


def test_character_accuracy_sums_distances_over_characters() -> None:
    result = accuracy([("开始", "开始"), ("商店", "商"), ("说明", "说明书")])

    assert (result.distance, result.characters) == (2, 6)
    assert result.value == pytest.approx(1 - 2 / 6)
    assert accuracy([]).value is None


def test_normalizations() -> None:
    assert strict_text(" 剩余 4.2 秒\n") == "剩余4.2秒"
    assert nfkc_text("门吗？！") == "门吗?!"
    assert strict_text("门吗？") != strict_text("门吗?")


def test_latency_percentiles_use_nearest_rank() -> None:
    values = [float(v) for v in range(1, 101)]

    assert percentile(values, 50) == 50
    assert percentile(values, 95) == 95
    assert percentile(values, 99) == 99
    assert percentile([7.0], 99) == 7.0
    summary = latency([3.0, 1.0, 2.0])
    assert summary is not None
    assert (summary.count, summary.p50, summary.max, summary.mean) == (3, 2.0, 3.0, 2.0)
    assert latency([]) is None


# 样本


def write(directory: Path, text: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / SAMPLES_FILE).write_text(text, encoding="utf-8")


def blank_png(path: Path, width: int = 64, height: int = 32) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_png(path, bytes([255] * width * height * 4), width, height)


def test_png_round_trip(tmp_path: Path) -> None:
    pixels = bytes(range(256)) * 2  # 16×8 BGRA
    write_png(tmp_path / "图像.png", pixels, 16, 8)

    frame = read_png(tmp_path / "图像.png", 3)

    assert (frame.width, frame.height, frame.row_stride, frame.frame_number) == (16, 8, 64, 3)
    assert frame.pixels == pixels


def test_samples_with_pixel_rects_load_without_an_adapter(tmp_path: Path) -> None:
    blank_png(tmp_path / "a.png")
    write(
        tmp_path,
        '[[samples]]\nimage = "a.png"\ncategory = "name"\nrect = [1, 2, 30, 20]\ntext = "旁白"\n',
    )

    (loaded,) = load_samples(tmp_path)

    assert loaded.rect == PixelRect(1, 2, 30, 20)
    assert loaded.sample.text == "旁白"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        pytest.param(
            '[[samples]]\nimage = "a.png"\ncategory = "x"\ntext = "t"\n',
            "恰好写一个",
            id="no region",
        ),
        pytest.param(
            '[[samples]]\nimage = "a.png"\ncategory = "x"\nroi = "speaker"\nrect = [0, 0, 1, 1]\n'
            'text = "t"\n',
            "恰好写一个",
            id="both regions",
        ),
        pytest.param(
            '[[samples]]\nimage = "a.png"\ncategory = "x"\nrect = [0, 0, 1, 1]\ntext = "t"\n'
            'speaker = "旁白"\n',
            "Extra inputs",
            id="unknown field",
        ),
        pytest.param(
            '[[samples]]\nimage = "a.png"\ncategory = "x"\nroi = "speaker"\ntext = "t"\n',
            "顶层写 game",
            id="roi without game",
        ),
        pytest.param(
            '[[samples]]\nimage = "a.png"\ncategory = "x"\nrect = [0, 0, 999, 1]\ntext = "t"\n',
            "超出图像",
            id="rect outside the image",
        ),
        pytest.param(
            '[[samples]]\nimage = "missing.png"\ncategory = "x"\nrect = [0, 0, 1, 1]\ntext = "t"\n',
            "读不出",
            id="missing image",
        ),
        pytest.param("samples = []\n", "at least 1", id="no samples"),
    ],
)
def test_invalid_sample_sets_are_rejected(tmp_path: Path, body: str, message: str) -> None:
    blank_png(tmp_path / "a.png")
    write(tmp_path, body)

    with pytest.raises(SampleError, match=message):
        load_samples(tmp_path)


def test_a_missing_samples_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(SampleError, match=SAMPLES_FILE):
        load_samples(tmp_path)


def test_tags_round_trip_and_are_validated(tmp_path: Path) -> None:
    blank_png(tmp_path / "a.png")
    tags = {"font": "msyh-bold", "size": "22", "background": "dark"}
    sample = Sample(image="a.png", category="general", rect=(0, 0, 8, 8), text="好", tags=tags)
    write_sample_set(tmp_path, None, [sample])

    (loaded,) = load_samples(tmp_path)

    assert loaded.sample.tags == tags
    write(
        tmp_path,
        '[[samples]]\nimage = "a.png"\ncategory = "x"\nrect = [0, 0, 1, 1]\ntext = "t"\n'
        'tags = { Font = "a" }\n',
    )
    with pytest.raises(SampleError, match="pattern"):
        load_samples(tmp_path)


# 合成样本


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("synthetic")
    write_synthetic_samples(directory, 640, 360, fonts=[FALLBACK_FONT])
    return directory


def test_synthetic_samples_cover_every_line_with_known_truth(synthetic: Path) -> None:
    samples = load_samples(synthetic)

    texts = {(s.sample.category, s.sample.text) for s in samples}
    for node in NODES.values():
        if isinstance(node, Dialogue):
            for line in node.lines:
                assert ("dialogue", line.text) in texts
                assert ("name", line.speaker) in texts
    assert {
        "dialogue",
        "dialogue-partial",
        "name",
        "prompt",
        "choice",
        "ending",
        "menu",
        "general",
        "general-partial",
    } == {s.sample.category for s in samples}
    for loaded in samples:
        assert (loaded.frame.width, loaded.frame.height) == (640, 360)
    general = [s for s in samples if s.sample.category.startswith("general")]
    cells = len(SIZES) * len(BACKGROUNDS)
    assert len(general) == cells + cells // 4
    assert {s.sample.tags["font"] for s in general} == {FALLBACK_FONT.tag}
    assert {s.sample.tags["background"] for s in general} == set(BACKGROUNDS)
    assert {s.sample.tags["style"] for s in general} == set(STYLES)
    # 字号按客户区的高等比缩放：360 行是 720 行的一半。
    assert [int(v) for v in dict.fromkeys(s.sample.tags["size"] for s in general)] == [
        round(size / 2) for size in SIZES
    ]
    assert all(s.sample.roi is None and s.sample.rect is not None for s in general)


# 泛化样本


def test_the_fallback_font_only_gets_lines_it_covers() -> None:
    characters = coverage(FALLBACK_FONT)
    # 合成游戏的文字都在它的字体子集里。
    assert all(set(text) <= characters for text in all_text() if "{" not in text)
    texts = texts_for(characters, 20)
    assert len(texts) == 20
    assert all(set(text) <= characters | {" "} for text in texts)
    with pytest.raises(ValueError, match="覆盖不到"):
        texts_for(frozenset("☃"), 3)
    full = frozenset("".join(LINES))
    assert texts_for(full, len(LINES)) == list(LINES)


def test_variants_share_texts_and_balance_styles() -> None:
    other = FontChoice("other", "同一字体的另一份", FALLBACK_FONT.path)
    items = variants([FALLBACK_FONT, other])
    per_font = len(SIZES) * len(BACKGROUNDS) * 5 // 4
    assert len(items) == 2 * per_font
    first = [(v.size, v.background, v.style, v.text, v.partial) for v in items[:per_font]]
    second = [(v.size, v.background, v.style, v.text, v.partial) for v in items[per_font:]]
    assert first == second
    full = [v for v in items[:per_font] if not v.partial]
    partial = [v for v in items[:per_font] if v.partial]
    for group in (full, partial):
        counts = [sum(v.style == style for v in group) for style in STYLES]
        assert max(counts) - min(counts) <= 1
    assert {v.size for v in partial} == set(SIZES)
    assert {v.background for v in partial} == set(BACKGROUNDS)


def test_rendering_is_deterministic_and_partial_text_keeps_its_lines() -> None:
    text = "".join(
        line.text for node in NODES.values() if isinstance(node, Dialogue) for line in node.lines
    )[:61]
    full = Variant(FALLBACK_FONT, 42, "translucent", "outline", text, False, 3)
    half = Variant(FALLBACK_FONT, 42, "translucent", "outline", text, True, 3)
    a, b = render(full, 640, 360), render(full, 640, 360)
    assert a == b
    assert len(a.bgra) == 640 * 360 * 4
    assert a.size == 21
    lines = a.truth.split("\n")
    assert len(lines) == 3
    assert "".join(lines) == text
    partial = render(half, 640, 360)
    # 逐字显示到一半：前一半的字，行的划分与显示完时相同。
    assert partial.truth.split("\n") == [lines[0], lines[1][: len(text) // 2 - len(lines[0])]]
    # 字只画在对白框里：框外的像素与逐字显示到一半的相同。
    image = np.frombuffer(a.bgra, np.uint8).reshape(360, 640, 4)
    other = np.frombuffer(partial.bgra, np.uint8).reshape(360, 640, 4)
    rect = a.rect
    outside = np.ones((360, 640), bool)
    outside[rect.top : rect.bottom, rect.left : rect.right] = False
    assert np.array_equal(image[outside], other[outside])
    assert not np.array_equal(image, other)


@pytest.mark.parametrize("kind", BACKGROUNDS)
def test_every_background_renders(kind: str) -> None:
    for style in STYLES:
        rendered = render(Variant(FALLBACK_FONT, 22, kind, style, "雨停了。", False, 0), 320, 180)
        assert rendered.truth == "雨停了。"


def test_system_fonts_are_found_in_a_font_directory(tmp_path: Path) -> None:
    for _, _, file in SYSTEM_FONTS[:2]:
        (tmp_path / file).write_bytes(b"")
    found = system_fonts(tmp_path)
    assert [f.tag for f in found] == [tag for tag, _, _ in SYSTEM_FONTS[:2]]
    assert available_fonts(system=False) == [FALLBACK_FONT]


def test_installed_system_fonts_render_every_line() -> None:
    fonts = system_fonts()
    if not fonts:
        pytest.skip("本机没有 Windows 自带的中文字体")
    for font in fonts:
        assert set("".join(LINES)) <= coverage(font) | {" "}, font.name
        rendered = render(Variant(font, 30, "dark", "plain", LINES[0], False, 0), 320, 180)
        assert rendered.truth == LINES[0]


# 评测与报告


class TruthEngine:
    """按样本的真值回答，只把每个“门”读成“们”。"""

    name = "truth"

    def __init__(self, samples: list[LoadedSample]) -> None:
        self._truths = {(id(s.frame), s.rect): s.sample.text for s in samples}

    def describe(self) -> Mapping[str, Any]:
        return {"engine": self.name}

    def recognize(self, frame: FrameLike, rect: PixelRect) -> list[str]:
        return self._truths[(id(frame), rect)].replace("门", "们").split("\n")


def test_evaluation_counts_errors_per_category(synthetic: Path, tmp_path: Path) -> None:
    samples = load_samples(synthetic)
    engine = TruthEngine(samples)
    ticks = iter(range(0, 10_000_000_000, 5_000_000))

    results = run_engine(engine, samples, clock=lambda: next(ticks))
    summaries = summarize(results)

    total = summaries[TOTAL]
    doors = sum(s.sample.text.count("门") for s in samples)
    characters = sum(len(strict_text(s.sample.text)) for s in samples)
    assert total.samples == len(samples)
    assert total.strict.distance == doors
    assert total.strict.characters == characters
    assert summaries["menu"].strict.value == 1.0
    # 每次识别 5 ms（假时钟），第一次是预热，不计入分布。
    assert results[0].warmup
    assert total.latency is not None
    assert total.latency.count == len(samples) - 1
    assert total.latency.p50 == pytest.approx(5.0)

    by_tag = summarize_tags(results)
    assert list(by_tag) == ["font", "size", "background", "style"]
    assert list(by_tag["background"]) == list(BACKGROUNDS)
    general = [s for s in samples if s.sample.tags]
    assert sum(v.samples for v in by_tag["style"].values()) == len(general)
    assert by_tag["font"][FALLBACK_FONT.tag].samples == len(general)

    report, summaries = build_report(engine, synthetic, results, "gs04 eval x")
    json_path, md_path = write_report(report, summaries, tmp_path / "reports", by_tag)
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved["spike"] == "GS-04"
    assert saved["summary"][TOTAL]["strict"]["distance"] == doors
    assert len(saved["results"]) == len(samples)
    assert saved["by_tag"]["style"]["plain"]["samples"] == by_tag["style"]["plain"].samples
    markdown = md_path.read_text(encoding="utf-8")
    assert "| 总计 |" in markdown
    assert "们" in markdown
    assert "## 按标签" in markdown
    assert "### 底色（`background`）" in markdown
    assert "| translucent |" in markdown


def test_unknown_or_unavailable_engines_are_reported() -> None:
    with pytest.raises(EngineUnavailableError, match="没有引擎"):
        create_engine("tesseract", EngineOptions())


def test_eval_without_models_exits_with_a_reason(
    synthetic: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("BELLIS_GAME_RUNTIME_DATA", str(tmp_path / "empty"))

    code = run(["eval", str(synthetic), "--report-dir", str(tmp_path / "reports")])

    assert code == 2
    assert "uv run fetch-models" in capsys.readouterr().err
    assert not (tmp_path / "reports").exists()


@pytest.mark.ocr_model
def test_eval_with_the_real_engine(synthetic: Path, tmp_path: Path) -> None:
    try:
        create_engine("ppocrv5-mobile", EngineOptions())
    except EngineUnavailableError as error:
        pytest.skip(f"OCR 模型不可用：{error}")

    assert run(["eval", str(synthetic), "--report-dir", str(tmp_path)]) == 0

    (report,) = tmp_path.glob("*.json")
    data = json.loads(report.read_text(encoding="utf-8"))
    # 合成游戏的样本用来验证工具本身，应几乎全对；泛化样本在 360 行时有 8 像素的字，只核对能评测。
    game = [r for r in data["results"] if not r["category"].startswith("general")]
    distance = sum(r["strict_distance"] for r in game)
    characters = sum(len(strict_text(r["truth"])) for r in game)
    assert 1 - distance / characters >= 0.95
    assert data["by_tag"]["font"][FALLBACK_FONT.tag]["strict"]["characters"] > 0


def test_the_windows_baseline_runs_when_available(synthetic: Path) -> None:
    try:
        engine = create_engine("windows", EngineOptions())
    except EngineUnavailableError as error:
        pytest.skip(f"Windows 自带 OCR 不可用：{error}")
    samples = [s for s in load_samples(synthetic) if s.sample.category == "dialogue"]

    results = run_engine(engine, samples)

    # 只核对能跑通并读出字；它的准确率是 GS-04 要测的，不在这里断言。
    assert any(result.predicted for result in results)
    assert engine.describe()["engine"] == "windows"
