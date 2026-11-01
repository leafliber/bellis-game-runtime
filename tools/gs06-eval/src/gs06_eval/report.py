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


CHECK_NAMES = {
    "missed_segments": "漏判段",
    "false_fraction": "多判时长",
    "onset_p90": "开始 p90",
    "offset_p90": "结束 p90",
}


def _verdict(scenario: dict[str, Any]) -> str:
    verdict = scenario["verdict"]
    if not verdict["gated"]:
        return "只记录"
    if verdict["pass"]:
        return "达标"
    failed = [CHECK_NAMES[k] for k, check in verdict["checks"].items() if not check["pass"]]
    return "不达标：" + "、".join(failed)


def _criteria(criteria: dict[str, Any]) -> str:
    return (
        f"口径（设门槛的情形）：漏判段 ≤ {_percent(criteria['missed_segments'])}，"
        f"多判时长 ≤ {_percent(criteria['false_fraction'])}，"
        f"开始延迟 p90 ≤ {_ms(criteria['onset_p90'])} ms，"
        f"结束延迟 p90 ≤ {_ms(criteria['offset_p90'])} ms；"
        f"增益 ≤ {criteria['max_gated_gain_db']:+g} dB 且背景不带人声的情形与纯配音设门槛"
    )


def format_summary(results: dict[str, Any]) -> str:
    """每种情形一行：漏判与多判、开始与结束延迟（ms，p50/p90/最大）、句中判闲、每秒声音的处理耗时
    与判定；其后是是否达标的合计、占用与按语音、语速的分组。"""
    truth = results["truth"]
    levels = results["levels_db"]
    lines = [
        f"真值：{len(truth['segments'])} 段，共 {truth['busy_seconds']:.1f} 秒，"
        f"阈值 {truth['threshold_db']:.1f} dBFS，录音 {truth['voice_duration']:.1f} 秒",
        "电平：" + "，".join(f"{k} {v:.1f} dBFS" for k, v in levels.items()),
        _criteria(results["criteria"]),
        "",
        f"{'情形':<28}{'漏判段':>8}{'多判时长':>9}{'开始 p50/p90/max':>18}"
        f"{'结束 p50/p90/max':>18}{'句中判闲':>8}{'ms/秒':>7}  判定",
    ]
    for scenario in results["scenarios"]:
        m = scenario["metrics"]
        on, off = m["onset_latency"], m["offset_latency"]
        onset = f"{_ms(on['p50'])}/{_ms(on['p90'])}/{_ms(on['max'])}"
        offset = f"{_ms(off['p50'])}/{_ms(off['p90'])}/{_ms(off['max'])}"
        missed = f"{m['missed_segments']}/{m['truth_segments']}" if scenario["with_voice"] else "-"
        early = str(m["early_idle"]) if scenario["with_voice"] else "-"
        lines.append(
            f"{scenario['name']:<28}{missed:>8}{_percent(m['false_fraction']):>9}"
            f"{onset:>18}{offset:>18}{early:>8}"
            f"{scenario['timing']['ms_per_second']:>7.1f}  {_verdict(scenario)}"
        )
    verdict = results["verdict"]
    lines.append("")
    line = f"设门槛的 {verdict['gated']} 个情形：{verdict['passed']} 个达标"
    if verdict["failed"]:
        line += "；不达标：" + "、".join(verdict["failed"])
    lines.append(line)
    occupancy = results["occupancy"]
    if occupancy["ms_per_second"] is not None:
        meets = "达到" if occupancy["meets_target"] else "未达到"
        lines.append(
            f"占用：每秒声音 {occupancy['ms_per_second']:.2f} ms"
            f"（目标 ≤ {occupancy['target_ms_per_second']:g} ms，{meets}；"
            f"各情形合计 {occupancy['audio_seconds']:.0f} 秒声音），"
            f"最慢的情形 {occupancy['slowest']} {occupancy['slowest_ms_per_second']:.2f} ms；"
            f"每帧 {occupancy['ms_per_frame']:.3f} ms"
        )
    groups = results.get("breakdown")
    if groups:
        for key, title in (("voice", "按语音"), ("rate", "按语速")):
            lines.append(f"{title}（设门槛的情形合计）：")
            for value, stats in groups[key].items():
                lines.append(
                    f"  {value}：{stats['segments']} 段，漏判 {stats['missed']}，"
                    f"开始 p90 {_ms(stats['onset_p90'])} ms，结束 p90 {_ms(stats['offset_p90'])} ms"
                )
    mixed = results.get("normal_mix")
    if mixed:
        lines.append(
            f"正常混音：{mixed['duration']:.1f} 秒，判为忙 {len(mixed['busy'])} 段、"
            f"{mixed['busy_seconds']:.1f} 秒（{_percent(mixed['busy_fraction'])}）"
        )
    return "\n".join(lines)
