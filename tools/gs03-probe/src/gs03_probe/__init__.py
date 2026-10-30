"""GS-03 真机探针（`docs/plan/spikes.md` 的 GS-03）的非提权一侧与编排。

提权一侧是 Rust 程序 `gs03-core-probe`（`core/crates/gs03-probe`）。本包提供 `uv run gs03`
的子命令：编译探针、开通道与帧环、写凭据、生成并核对计划任务、检查安装目录、清理。执行步骤由
`uv run gs03 <子命令>` 自己打印，不另写文档。
"""

from .conn import CONN_INFO_VERSION, DEFAULT_ROLE, build_conn_info, conn_info_json
from .host import Host, HostOutcome
from .install_check import OpResult, all_denied, check_install_dir
from .pattern import check_frame, pattern
from .report import Report, write_report
from .task_xml import OnDemandTask, check_task, read_task_xml, task_xml

__all__ = [
    "CONN_INFO_VERSION",
    "DEFAULT_ROLE",
    "Host",
    "HostOutcome",
    "OnDemandTask",
    "OpResult",
    "Report",
    "all_denied",
    "build_conn_info",
    "check_frame",
    "check_install_dir",
    "check_task",
    "conn_info_json",
    "pattern",
    "read_task_xml",
    "task_xml",
    "write_report",
]
