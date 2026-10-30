"""GS-03 的结构化报告。写进仓库根的 `reports/gs03/`（不入库），每一项结论为 pass、fail 或 pending，
并附原始数据（见 `docs/plan/spikes.md`：记录日期、系统、命令、样本、原始结果与限制）。

结论由维护者跑完后据报告写回技术基线与模块文档，探针不改 `spikes.md`。
"""

import json
import platform
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

Conclusion = Literal["pass", "fail", "pending"]

GS03_ITEMS = {
    "cross_integrity": (
        "跨完整性级别的可达性：提权实时核能连接非提权主进程建的管道、写入共享内存；"
        "其他用户与远程客户端拿不到数据"
    ),
    "scheduled_task": (
        "计划任务：非提权进程能 schtasks /Run 触发“以最高权限运行”的任务，"
        "被拉起的进程完整性级别为 High"
    ),
    "secret_store": "SecretStore：提权实时核能读取同一用户的 SecretStore 条目",
    "install_dir": "安装目录：只有管理员能写的安装目录挡住非提权进程替换实时核的程序与配置",
    "other_user_and_remote": "其他用户与远程：其他本机用户与远程客户端拿不到数据",
}


@dataclass
class Item:
    key: str
    conclusion: Conclusion
    detail: str
    data: dict[str, Any] = field(default_factory=dict[str, Any])

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": GS03_ITEMS.get(self.key, self.key),
            "conclusion": self.conclusion,
            "detail": self.detail,
            "data": self.data,
        }


@dataclass
class Report:
    """一次 GS-03 运行的报告。"""

    items: list[Item] = field(default_factory=list[Item])
    environment: dict[str, str] = field(default_factory=dict[str, str])
    notes: list[str] = field(default_factory=list[str])

    def __post_init__(self) -> None:
        if not self.environment:
            self.environment = {
                "generated_at": datetime.now(UTC).isoformat(),
                "host": socket.gethostname(),
                "platform": platform.platform(),
                "python": platform.python_version(),
            }

    def set(
        self, key: str, conclusion: Conclusion, detail: str, data: Mapping[str, Any] | None = None
    ) -> None:
        self.items = [item for item in self.items if item.key != key]
        self.items.append(Item(key, conclusion, detail, dict(data or {})))

    def to_dict(self) -> dict[str, Any]:
        return {
            "spike": "GS-03",
            "environment": self.environment,
            "notes": self.notes,
            "items": [item.to_dict() for item in self.items],
        }

    def summary(self) -> str:
        lines = ["GS-03 探针结果："]
        order = {key: index for index, key in enumerate(GS03_ITEMS)}
        for item in sorted(self.items, key=lambda it: order.get(it.key, len(order))):
            mark = {"pass": "通过", "fail": "失败", "pending": "PENDING"}[item.conclusion]
            lines.append(f"  [{mark}] {GS03_ITEMS.get(item.key, item.key)}")
            lines.append(f"         {item.detail}")
        for note in self.notes:
            lines.append(f"  注：{note}")
        return "\n".join(lines)


def reports_dir(repo_root: Path) -> Path:
    return repo_root / "reports" / "gs03"


def write_report(report: Report, repo_root: Path) -> Path:
    """把报告写成带时间戳的 JSON，返回路径。`reports/` 不入库。"""
    directory = reports_dir(repo_root)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"gs03-{stamp}.json"
    path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path
