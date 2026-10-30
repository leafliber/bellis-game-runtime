"""`uv run gs03 <子命令>`：GS-03 探针的一键入口与操作步骤。

每个子命令完成后打印“下一步在哪种终端里执行什么”。凡是需要管理员权限的动作（注册计划任务、把程序放进
管理员目录）一律只打印命令，由维护者在管理员终端里执行；探针自己不提权、不改系统安全设置。

子命令：
- `prepare`      编译提权一侧探针，生成计划任务 XML，打印注册命令与后续步骤。
- `host`         非提权一侧：开通道与帧环、写凭据、等提权一侧连上并核对，写报告。
- `task-command` 生成计划任务 XML 并打印 /Create、/Query、/Run、/Delete 命令。
- `check-task`   以 /Query /XML 读回计划任务并逐项核对（非提权即可）。
- `install-check` 非提权一侧尝试改写管理员安装目录里的文件，预期全部被拒。
- `cleanup`      删除凭据条目，打印注销任务与清理安装目录的命令。
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from . import secret
from .build import build_probe
from .conn import DEFAULT_ROLE
from .host import DEFAULT_FRAMES, DEFAULT_MAX_HEIGHT, DEFAULT_MAX_WIDTH, Host
from .install_check import all_denied, check_install_dir
from .report import Report, reports_dir, write_report
from .task_xml import (
    DEFAULT_TASK_NAME,
    OnDemandTask,
    check_task,
    create_command,
    current_user_id_from_env,
    decode_query_output,
    delete_command,
    encode_task_xml,
    format_command,
    query_command,
    read_task_xml,
    run_command,
    schtasks_path,
    task_xml,
)

REPO_ROOT = Path(__file__).resolve().parents[4]


def _print(line: str = "") -> None:
    print(line, flush=True)


def _schtasks() -> str:
    return schtasks_path() if sys.platform == "win32" else "schtasks.exe"


def _probe_task(binary: Path) -> OnDemandTask:
    """计划任务：按需、最高权限运行提权探针，动作为 `<binary> --from-cred`。"""
    return OnDemandTask(
        user_id=current_user_id_from_env(),
        command=str(binary),
        args=("--from-cred",),
        working_directory=str(binary.parent),
    )


def _write_task_xml(binary: Path, task_name: str) -> Path:
    path = reports_dir(REPO_ROOT) / f"{task_name}.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode_task_xml(task_xml(_probe_task(binary))))
    return path


def _cmd(argv: list[str]) -> str:
    return format_command(argv)


def cmd_prepare(args: argparse.Namespace) -> int:
    binary = build_probe(REPO_ROOT, release=args.release)
    xml_path = _write_task_xml(binary, args.task_name)
    _print(f"已编译提权一侧探针：{binary}")
    _print(f"已生成计划任务 XML：{xml_path}")
    _print()
    _print("下一步：")
    _print("  1)【管理员终端】注册“以最高权限运行”的按需任务（验证 GS-03 第 2 项）：")
    _print(f"       {_cmd(create_command(args.task_name, str(xml_path), schtasks=_schtasks()))}")
    _print("     读回核对（非提权终端即可）：uv run gs03 check-task")
    _print(f"  2) 要验证安装目录防护（第 4 项）：把 {binary.name} 复制进一个只有管理员能写的目录，")
    _print("     用 uv run gs03 task-command --binary <该目录下的路径> 重新生成并注册任务。")
    _print("  3)【非提权终端】运行：uv run gs03 host")
    return 0


def cmd_task_command(args: argparse.Namespace) -> int:
    binary = Path(args.binary) if args.binary else build_probe(REPO_ROOT, release=args.release)
    xml_path = _write_task_xml(binary, args.task_name)
    _print(f"计划任务 XML：{xml_path}")
    _print(f"任务动作：{binary} --from-cred（工作目录 {binary.parent}）")
    _print()
    _print("【管理员终端】注册：")
    _print(f"  {_cmd(create_command(args.task_name, str(xml_path), schtasks=_schtasks()))}")
    _print("读回核对（非提权即可，或用 uv run gs03 check-task）：")
    _print(f"  {_cmd(query_command(args.task_name, schtasks=_schtasks()))}")
    _print("触发（非提权，验证第 2 项）：")
    _print(f"  {_cmd(run_command(args.task_name, schtasks=_schtasks()))}")
    _print("注销：")
    _print(f"  {_cmd(delete_command(args.task_name, schtasks=_schtasks()))}")
    return 0


def cmd_check_task(args: argparse.Namespace) -> int:
    if sys.platform != "win32":
        _print("计划任务只在 Windows 上可用（unsupported）")
        return 1
    binary = Path(args.binary) if args.binary else build_probe(REPO_ROOT, release=args.release)
    argv = query_command(args.task_name, schtasks=_schtasks())
    try:
        completed = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, check=False)
    except OSError as error:
        _print(f"无法运行 schtasks：{error}")
        return 1
    if completed.returncode != 0:
        _print(f"读不到任务 {args.task_name}：schtasks 退出码 {completed.returncode}（可能未注册）")
        return 1
    readback = read_task_xml(decode_query_output(completed.stdout))
    checks = check_task(readback, _probe_task(binary))
    _print(f"计划任务 {args.task_name} 核对：")
    mismatched = 0
    for check in checks:
        if check.verdict == "ok":
            _print(f"  [一致]   {check.label}：{check.actual}")
        elif check.verdict == "unverified":
            _print(f"  [未核对] {check.label}：{check.actual}（{check.note}）")
        else:
            mismatched += 1
            _print(f"  [不一致] {check.label}：{check.actual}（应为 {check.expected}）")
    _print("各项一致" if mismatched == 0 else f"{mismatched} 项不一致")
    return 0 if mismatched == 0 else 1


def cmd_install_check(args: argparse.Namespace) -> int:
    results = check_install_dir(args.directory, args.file)
    _print(f"安装目录检查：{args.directory}（已有文件 {args.file}）")
    for result in results:
        mark = {"denied": "被拒", "allowed": "未拒", "error": "错误"}[result.verdict]
        _print(f"  [{mark}] {result.op}：{result.detail}")
    ok = all_denied(results)
    _print("结论：防护有效，全部被拒" if ok else "结论：有操作未被拒或无法判定，见上")
    return 0 if ok else 1


async def _run_host(args: argparse.Namespace) -> int:
    report = Report()
    async with Host(
        max_width=args.max_width,
        max_height=args.max_height,
        frames=args.frames,
        role=args.role,
        write_cred=not args.no_cred,
        cred_service=args.service,
        cred_name=args.name,
    ) as host:
        info = host.conn_info()
        target = secret.target_name(args.service, args.name)
        _print("非提权一侧已就绪：")
        _print(f"  通道地址     {host.credentials.path}")
        size = f"{args.max_width}×{args.max_height}"
        _print(f"  帧环         {host.ring.name}（{size}，写 {args.frames} 帧）")
        _print(f"  主进程 PID   {host.outcome.host_pid}")
        _print(f"  主进程 SID   {host.outcome.host_user_sid}")
        if not args.no_cred:
            _print(f"  已写凭据     {target}（UTF-16LE 存储，退出时删除）")
        _print()
        if args.self_check:
            await _self_check(host, info, args)
        else:
            binary = build_probe(REPO_ROOT, release=args.release)
            _print("下一步：在【管理员终端】用以下任一方式拉起提权一侧（它会连回本进程）：")
            _print("  A) 直接运行：")
            _print(f"       {binary} --from-cred --service {args.service} --name {args.name}")
            _print("  B) 触发计划任务（先 prepare/task-command 注册过）：")
            _print(f"       {_cmd(run_command(args.task_name, schtasks=_schtasks()))}")
            if args.other_user:
                _print()
                _print(f"  可选（第 4 项，其他用户）：在账户 {args.other_user} 下运行探针，")
                _print("     预期在打开管道时被拒（connect-failed / 拒绝访问），本进程收不到连接。")
                _print(f"       以 runas /user:{args.other_user} 运行：")
                _print(f"       {binary} --conn-info <把下面整段 JSON 作为一个参数>")
                _print(f"       连接信息：{json.dumps(info)}")
            _print()
            _print(f"本进程将等待至多 {args.timeout:.0f}s……")
            await host.wait(args.timeout)
        _record_host_outcome(report, host, args)
    path = write_report(report, REPO_ROOT)
    _print()
    _print(report.summary())
    _print()
    _print(f"报告已写入：{path}")
    return 0 if host.outcome.frames_verified else 2


async def _self_check(host: Host, info: dict[str, object], args: argparse.Namespace) -> None:
    _print("自检：在【非提权】下拉起提权一侧探针，验证工具链本身（不是 GS-03 的提权结论）。")
    binary = build_probe(REPO_ROOT, release=args.release)
    env = {**os.environ, "GS03_CONN_INFO": json.dumps(info)}
    with subprocess.Popen(
        [os.fspath(binary), "--conn-info-env", "GS03_CONN_INFO"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    ) as process:
        await host.wait(args.timeout)
        out, err = process.communicate(timeout=30)
    _print("提权一侧（实为非提权自检）输出：")
    for line in out.splitlines():
        _print(f"    {line}")
    if err.strip():
        _print(f"    [stderr] {err.strip()}")


def _record_host_outcome(report: Report, host: Host, args: argparse.Namespace) -> None:
    outcome = host.outcome
    identity = outcome.elevated_identity
    rid = identity.get("integrity_rid")
    level = identity.get("integrity_level")
    # 第 1 项：跨完整性级别的连接 + 写入（远程由已有通道测试覆盖，这里只看连接与写帧）。
    if outcome.connected and outcome.hello_received and outcome.frames_verified:
        if args.self_check:
            conclusion, detail = (
                "pending",
                f"工具链自检通过：非提权连接、握手、RPC、写 {outcome.frames_expected} 帧均成功，"
                f"完整性级别报为 {level}；跨完整性级别的真机结论需提权运行",
            )
        else:
            conclusion, detail = (
                "pass",
                f"提权一侧（完整性级别 {level}）连上、握手、RPC，"
                f"并把 {outcome.frames_expected} 帧写进帧环，非提权一侧读回核对一致",
            )
    else:
        conclusion, detail = "fail", f"未完成：{outcome.frames_detail}；错误 {outcome.errors}"
    report.set("cross_integrity", conclusion, detail, outcome.to_dict())

    # 第 2 项：计划任务触发时的完整性级别。
    if not args.self_check and outcome.connected and level == "high":
        report.set(
            "scheduled_task", "pass", "被拉起的提权一侧完整性级别为 high", {"identity": identity}
        )
    elif args.self_check:
        report.set(
            "scheduled_task",
            "pending",
            f"自检为非提权，完整性级别报为 {level}；经计划任务触发后应为 high，需维护者跑该路径",
            {"identity": identity},
        )
    else:
        report.set(
            "scheduled_task",
            "pending" if outcome.connected else "fail",
            f"完整性级别为 {level}（rid={rid}）；若非经“以最高权限运行”的任务触发，不构成第 2 项",
            {"identity": identity},
        )

    # 第 3 项：SecretStore 读取。
    if args.no_cred:
        report.set("secret_store", "pending", "本次未写凭据（--no-cred），未验证 SecretStore 读取")
    elif outcome.hello_received:
        extra = "（自检为非提权，结论待提权运行）" if args.self_check else ""
        report.set(
            "secret_store",
            "pending" if args.self_check else "pass",
            "提权一侧从凭据读到连接信息并连上" + extra,
            {"cred_target": secret.target_name(args.service, args.name)},
        )
    else:
        report.set("secret_store", "fail", "提权一侧没有连上，无法确认它读到了凭据")

    # 第 4 项：安装目录与“其他用户/远程”。安装目录由维护者复制程序后另跑；远程已由通道测试覆盖；
    # 其他用户需另一本机账户，未提供则记 PENDING（不自建用户）。
    report.set(
        "install_dir",
        "pending",
        "把探针复制进管理员目录后，用 uv run gs03 install-check <目录> <文件名> 验证非提权改写被拒",
    )
    remote = "远程客户端被拒已由通道测试 test_pipe_rejects_remote_clients（SMB 回环）覆盖"
    if args.other_user:
        report.set(
            "other_user_and_remote",
            "pending",
            f"{remote}；其他用户请以 runas /user:{args.other_user} 运行探针后记录是否被拒",
        )
    else:
        report.set(
            "other_user_and_remote",
            "pending",
            f"{remote}；其他用户需另一本机账户，未提供则记 PENDING（探针不自建用户）",
        )

    report.notes.append(
        "远程客户端被拒已由通道测试 test_pipe_rejects_remote_clients（经 SMB 回环）覆盖，"
        "见 docs/modules/core-protocol.md 的“测试覆盖”。"
    )
    if args.self_check:
        report.notes.append(
            "本次为非提权自检，只证明工具链可用；GS-03 的提权结论须由维护者在提权下跑出。"
        )


def cmd_host(args: argparse.Namespace) -> int:
    return asyncio.run(_run_host(args))


def cmd_cleanup(args: argparse.Namespace) -> int:
    removed = False
    try:
        removed = secret.delete_secret(args.service, args.name)
    except OSError as error:
        _print(f"删除凭据出错：{error}")
    target = secret.target_name(args.service, args.name)
    _print(f"凭据 {target}：{'已删除' if removed else '本就不存在'}")
    _print()
    _print("共享内存与命名管道随 host 进程退出而消失，无需单独清理。")
    _print("仍需在【管理员终端】做的清理：")
    _print(f"  注销计划任务：{_cmd(delete_command(args.task_name, schtasks=_schtasks()))}")
    _print("  删除管理员安装目录里放入的探针程序（若做过第 4 项）。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gs03", description="GS-03 真机探针的一键入口")
    parser.add_argument("--task-name", default=DEFAULT_TASK_NAME, help="计划任务名")
    parser.add_argument("--release", action="store_true", help="以 release 配置编译提权一侧探针")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("prepare", help="编译探针并生成计划任务 XML 与注册命令")

    host = sub.add_parser("host", help="非提权一侧：开通道与帧环、写凭据、等提权一侧并核对")
    host.add_argument("--frames", type=int, default=DEFAULT_FRAMES, help="写入帧环的帧数")
    host.add_argument("--max-width", type=int, default=DEFAULT_MAX_WIDTH)
    host.add_argument("--max-height", type=int, default=DEFAULT_MAX_HEIGHT)
    host.add_argument("--role", default=DEFAULT_ROLE, help="连接角色")
    host.add_argument("--service", default=secret.DEFAULT_SERVICE, help="凭据服务名")
    host.add_argument("--name", default=secret.DEFAULT_NAME, help="凭据条目名")
    host.add_argument("--timeout", type=float, default=300.0, help="等待提权一侧的秒数")
    host.add_argument(
        "--no-cred", action="store_true", help="不写凭据（经环境变量或命令行传连接信息时）"
    )
    host.add_argument(
        "--self-check",
        action="store_true",
        help="在非提权下自动拉起提权一侧探针，验证工具链本身（不是 GS-03 的提权结论）",
    )
    host.add_argument(
        "--other-user", help="可选：另一本机账户名，打印用它运行探针（预期被拒）的 runas 命令"
    )

    task = sub.add_parser("task-command", help="生成计划任务 XML 并打印 schtasks 命令")
    task.add_argument("--binary", help="任务要运行的探针程序路径（缺省为编译出的调试版）")

    check = sub.add_parser("check-task", help="读回计划任务并逐项核对")
    check.add_argument("--binary", help="核对用的期望探针路径（缺省为编译出的调试版）")

    install = sub.add_parser("install-check", help="非提权一侧尝试改写管理员安装目录里的文件")
    install.add_argument("directory", help="管理员安装目录")
    install.add_argument("file", help="目录里已有的探针程序文件名")

    cleanup = sub.add_parser("cleanup", help="删除凭据，打印注销任务与清理安装目录的命令")
    cleanup.add_argument("--service", default=secret.DEFAULT_SERVICE)
    cleanup.add_argument("--name", default=secret.DEFAULT_NAME)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "prepare": cmd_prepare,
        "host": cmd_host,
        "task-command": cmd_task_command,
        "check-task": cmd_check_task,
        "install-check": cmd_install_check,
        "cleanup": cmd_cleanup,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
