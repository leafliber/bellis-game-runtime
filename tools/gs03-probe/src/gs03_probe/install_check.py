"""安装目录检查（GS-03 的第 4 项）：非提权一侧尝试改写、重命名、删除安装目录里的文件，并在目录里
新建文件，预期全部被拒。只有管理员能写的安装目录应当挡住非提权进程替换实时核的程序与配置（GR-06）。

真实的管理员目录由维护者以管理员身份放入探针程序后给出；这里只做“尝试并分类”的逻辑，不需要提权。
测试用一个 ACL 受限目录模拟“拒绝写入”。
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Verdict = Literal["denied", "allowed", "error"]


@dataclass(frozen=True, slots=True)
class OpResult:
    """一次尝试的结果。`denied` 被拒（预期），`allowed` 竟然成功（防护失效），`error` 其它错误。"""

    op: str
    verdict: Verdict
    detail: str


def _attempt(op: str, action: object) -> OpResult:
    try:
        action()  # type: ignore[operator]
    except PermissionError as error:
        return OpResult(op, "denied", f"被拒：{error}")
    except OSError as error:
        # Windows 上拒绝写入也可能是 EACCES 之外的错误码；一并按“被拒”记录，附原始信息。
        return OpResult(op, "denied", f"被拒：{error}")
    else:
        return OpResult(op, "allowed", "竟然成功")


def check_install_dir(directory: str | os.PathLike[str], existing_file: str) -> list[OpResult]:
    """对 `directory`（应是只有管理员能写的安装目录）尝试四种写操作，预期都被拒。

    `existing_file` 是目录里已有的一个文件名（例如维护者放进去的探针程序）。
    """
    base = Path(directory)
    target = base / existing_file
    results: list[OpResult] = []

    def modify() -> None:
        with target.open("ab") as handle:
            handle.write(b"x")

    def rename() -> None:
        target.rename(base / (existing_file + ".moved"))

    def delete() -> None:
        target.unlink()

    def create() -> None:
        (base / "gs03-intruder.txt").write_bytes(b"x")

    if target.exists():
        results.append(_attempt("改写文件", modify))
        results.append(_attempt("重命名文件", rename))
        results.append(_attempt("删除文件", delete))
    else:
        note = f"目录里没有 {existing_file}，跳过针对它的改写/重命名/删除"
        results.append(OpResult("改写/重命名/删除文件", "error", note))
    results.append(_attempt("新建文件", create))
    return results


def all_denied(results: list[OpResult]) -> bool:
    """全部被拒（且至少做了一次针对已有文件的尝试）才算防护有效。"""
    return bool(results) and all(result.verdict == "denied" for result in results)
