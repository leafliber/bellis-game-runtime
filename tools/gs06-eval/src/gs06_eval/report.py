"""GS-06 的报告：JSON 写进仓库根的 `reports/gs06/`（不入库），另打印给人读的摘要。"""

import hashlib
import json
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
"""本文件位于 tools/gs06-eval/src/gs06_eval/。"""


def reports_dir(repo_root: Path = REPO_ROOT) -> Path:
    return repo_root / "reports" / "gs06"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def environment() -> dict[str, str]:
    """日期、系统与 Python 的版本。"""
    from importlib.metadata import version

    return {
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python": sys.version.split()[0],
        "onnxruntime": version("onnxruntime"),
        "numpy": version("numpy"),
    }


def write_report(report: dict[str, Any], directory: Path) -> Path:
    """写成带时间戳的 JSON，返回路径。"""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"gs06-{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _ms(value: object) -> str:
    return "-" if not isinstance(value, int | float) else f"{1000 * value:.0f}"


def _percent(value: object) -> str:
    return "-" if not isinstance(value, int | float) else f"{100 * value:.1f}%"


def format_summary(results: dict[str, Any]) -> str:
    """每种情形一行：开始与结束延迟（ms，p50/p95/最大）、漏判与多判的段数与时长占比、句中判闲。"""
    truth = results["truth"]
    lines = [
        f"真值：{len(truth['segments'])} 段，共 {truth['busy_seconds']:.1f} 秒，"
        f"阈值 {truth['threshold_db']:.1f} dBFS，录音 {truth['voice_duration']:.1f} 秒",
        "电平：" + "，".join(f"{k} {v:.1f} dBFS" for k, v in results["levels_db"].items()),
        "",
        f"{'情形':<28}{'开始 p50/p95/max':>18}{'结束 p50/p95/max':>18}"
        f"{'漏判段':>6}{'多判段':>6}{'句中判闲':>8}{'漏判时长':>9}{'多判时长':>9}",
    ]
    for scenario in results["scenarios"]:
        m = scenario["metrics"]
        on, off = m["onset_latency"], m["offset_latency"]
        onset = f"{_ms(on['p50'])}/{_ms(on['p95'])}/{_ms(on['max'])}"
        offset = f"{_ms(off['p50'])}/{_ms(off['p95'])}/{_ms(off['max'])}"
        lines.append(
            f"{scenario['name']:<28}{onset:>18}{offset:>18}"
            f"{m['missed_segments']:>6}{m['false_segments']:>6}{m['early_idle']:>8}"
            f"{_percent(m['missed_fraction']):>9}{_percent(m['false_fraction']):>9}"
        )
    timing = results["scenarios"][0]["timing"] if results["scenarios"] else None
    if timing:
        lines.append("")
        lines.append(
            f"推理：每帧 {timing['ms_per_frame']:.3f} ms，实时率 {timing['real_time_factor']:.4f}"
        )
    mixed = results.get("normal_mix")
    if mixed:
        lines.append(
            f"正常混音：{mixed['duration']:.1f} 秒，判为忙 {len(mixed['busy'])} 段、"
            f"{mixed['busy_seconds']:.1f} 秒（{_percent(mixed['busy_fraction'])}）"
        )
    return "\n".join(lines)
