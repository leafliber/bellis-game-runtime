"""对一批样本跑一个引擎，按类别与总计统计字符准确率与时延，写报告。

报告写到不入库的 `reports/gs04/`，内容见 `docs/modules/perception.md` 的“GS-04 的评测工具”。
"""

import json
import os
import platform
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gs04_ocr.engines import Engine
from gs04_ocr.metrics import (
    Accuracy,
    Latency,
    accuracy,
    edit_distance,
    latency,
    nfkc_text,
    strict_text,
)
from gs04_ocr.samples import LoadedSample

# 本文件位于 tools/gs04-ocr/src/gs04_ocr/，工作区成员以可编辑方式安装。
REPO_ROOT = Path(__file__).resolve().parents[4]
REPORTS_DIR = REPO_ROOT / "reports" / "gs04"
TOTAL = "总计"


@dataclass(frozen=True)
class SampleResult:
    index: int
    image: str
    category: str
    rect: tuple[int, int, int, int]
    truth: str
    lines: tuple[str, ...]
    milliseconds: float
    warmup: bool
    """第一次调用，含预热；时延不计入分布。"""

    @property
    def predicted(self) -> str:
        return "".join(self.lines)


@dataclass(frozen=True)
class Summary:
    samples: int
    strict: Accuracy
    nfkc: Accuracy
    exact: int
    """严格口径下与真值完全相同的样本数。"""
    latency: Latency | None


def run_engine(
    engine: Engine,
    samples: Sequence[LoadedSample],
    *,
    clock: Callable[[], int] = time.perf_counter_ns,
) -> list[SampleResult]:
    """每个样本识别一次，记墙钟时间；第一次调用标为预热。"""
    results: list[SampleResult] = []
    for position, loaded in enumerate(samples):
        start = clock()
        lines = engine.recognize(loaded.frame, loaded.rect)
        elapsed = (clock() - start) / 1e6
        sample = loaded.sample
        results.append(
            SampleResult(
                index=loaded.index,
                image=sample.image,
                category=sample.category,
                rect=(loaded.rect.left, loaded.rect.top, loaded.rect.right, loaded.rect.bottom),
                truth=sample.text,
                lines=tuple(lines),
                milliseconds=elapsed,
                warmup=position == 0,
            )
        )
    return results


def summarize(results: Sequence[SampleResult]) -> dict[str, Summary]:
    """按类别（出现的顺序）与总计汇总。"""
    groups: dict[str, list[SampleResult]] = {}
    for result in results:
        groups.setdefault(result.category, []).append(result)
    groups[TOTAL] = list(results)
    return {name: _summary(group) for name, group in groups.items()}


def _summary(results: Sequence[SampleResult]) -> Summary:
    strict = [(strict_text(r.truth), strict_text(r.predicted)) for r in results]
    nfkc = [(nfkc_text(r.truth), nfkc_text(r.predicted)) for r in results]
    return Summary(
        samples=len(results),
        strict=accuracy(strict),
        nfkc=accuracy(nfkc),
        exact=sum(expected == actual for expected, actual in strict),
        latency=latency([r.milliseconds for r in results if not r.warmup]),
    )


def environment() -> dict[str, Any]:
    return {
        "system": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "python": platform.python_version(),
    }


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def _ms(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


def markdown(report: Mapping[str, Any], summaries: Mapping[str, Summary]) -> str:
    lines = [
        f"# GS-04 OCR 评测：{report['engine']['engine']}",
        "",
        f"- 时间：{report['date']}",
        f"- 命令：`{report['command']}`",
        f"- 样本：{report['samples_dir']}（{report['sample_count']} 个）",
        f"- 系统：{report['environment']['system']}，{report['environment']['processor']}，"
        f"{report['environment']['cpu_count']} 个逻辑处理器",
        f"- 引擎：`{json.dumps(report['engine'], ensure_ascii=False)}`",
        f"- 预热（第一次调用）：{_ms(report['warmup_ms'])} ms，不计入时延分布",
        "",
        "| 类别 | 样本 | 字符准确率 | NFKC 后 | 完全相同 | p50 ms | p90 | p95 | p99 | 最大 "
        "| 均值 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, summary in summaries.items():
        lat = summary.latency
        lines.append(
            f"| {name} | {summary.samples} | {_percent(summary.strict.value)} | "
            f"{_percent(summary.nfkc.value)} | {summary.exact} | "
            f"{_ms(lat.p50 if lat else None)} | {_ms(lat.p90 if lat else None)} | "
            f"{_ms(lat.p95 if lat else None)} | {_ms(lat.p99 if lat else None)} | "
            f"{_ms(lat.max if lat else None)} | {_ms(lat.mean if lat else None)} |"
        )
    lines += ["", "## 不完全相同的样本", ""]
    mismatched = [r for r in report["results"] if r["strict_distance"] > 0]
    if not mismatched:
        lines.append("无。")
    for result in mismatched:
        lines.append(
            f"- #{result['index'] + 1} {result['image']} {result['category']}："
            f"真值 {json.dumps(result['truth'], ensure_ascii=False)}，"
            f"识别 {json.dumps(result['predicted'], ensure_ascii=False)}"
        )
    lines.append("")
    return "\n".join(lines)


def build_report(
    engine: Engine,
    samples_dir: Path,
    results: Sequence[SampleResult],
    command: str,
    date: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Summary]]:
    summaries = summarize(results)
    when = (date or datetime.now(UTC)).isoformat(timespec="seconds")
    warmup = next((r.milliseconds for r in results if r.warmup), None)
    report: dict[str, Any] = {
        "spike": "GS-04",
        "date": when,
        "command": command,
        "samples_dir": str(samples_dir),
        "sample_count": len(results),
        "environment": environment(),
        "engine": dict(engine.describe()),
        "warmup_ms": warmup,
        "summary": {name: asdict(summary) for name, summary in summaries.items()},
        "results": [
            {
                **asdict(result),
                "predicted": result.predicted,
                "strict_distance": edit_distance(
                    strict_text(result.truth), strict_text(result.predicted)
                ),
            }
            for result in results
        ],
    }
    return report, summaries


def write_report(
    report: Mapping[str, Any], summaries: Mapping[str, Summary], directory: Path
) -> tuple[Path, Path]:
    """写 JSON 与 Markdown，文件名为 UTC 时间、引擎与样本目录名。"""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(report["date"]).strftime("%Y%m%dT%H%M%SZ")
    stem = f"{stamp}-{report['engine']['engine']}-{Path(report['samples_dir']).name}"
    json_path = directory / f"{stem}.json"
    md_path = directory / f"{stem}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    md_path.write_text(markdown(report, summaries), encoding="utf-8", newline="\n")
    return json_path, md_path
