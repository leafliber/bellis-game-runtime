"""GS-04 评测工具：口径（编辑距离、字符准确率、时延分位）、样本格式、合成样本、评测与报告。

引擎用一个按真值回答的假引擎，不需要模型；另有两项在本机有模型或有 Windows OCR 时跑真实引擎。
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from game_runtime.adapter import PixelRect
from game_runtime.ocr import EngineOptions, FrameLike
from gs04_ocr.cli import run
from gs04_ocr.engines import create_engine
from gs04_ocr.errors import EngineUnavailableError
from gs04_ocr.evaluate import TOTAL, build_report, run_engine, summarize, write_report
from gs04_ocr.metrics import accuracy, edit_distance, latency, nfkc_text, percentile, strict_text
from gs04_ocr.samples import (
    SAMPLES_FILE,
    LoadedSample,
    SampleError,
    load_samples,
    read_png,
    write_png,
)
from gs04_ocr.synth import write_synthetic_samples
from synthetic_game.story import NODES, Dialogue

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


# 合成样本


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("synthetic")
    write_synthetic_samples(directory, 640, 360)
    return directory


def test_synthetic_samples_cover_every_line_with_known_truth(synthetic: Path) -> None:
    samples = load_samples(synthetic)

    texts = {(s.sample.category, s.sample.text) for s in samples}
    for node in NODES.values():
        if isinstance(node, Dialogue):
            for line in node.lines:
                assert ("dialogue", line.text) in texts
                assert ("name", line.speaker) in texts
    assert {"dialogue", "dialogue-partial", "name", "prompt", "choice", "ending", "menu"} == {
        s.sample.category for s in samples
    }
    for loaded in samples:
        assert (loaded.frame.width, loaded.frame.height) == (640, 360)


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

    report, summaries = build_report(engine, synthetic, results, "gs04 eval x")
    json_path, md_path = write_report(report, summaries, tmp_path / "reports")
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved["spike"] == "GS-04"
    assert saved["summary"][TOTAL]["strict"]["distance"] == doors
    assert len(saved["results"]) == len(samples)
    markdown = md_path.read_text(encoding="utf-8")
    assert "| 总计 |" in markdown
    assert "们" in markdown


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
    total = json.loads(report.read_text(encoding="utf-8"))["summary"][TOTAL]
    assert 1 - total["strict"]["distance"] / total["strict"]["characters"] >= 0.95


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
