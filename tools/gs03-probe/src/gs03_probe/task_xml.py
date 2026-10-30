"""“以最高权限运行”的按需计划任务（GR-06、GS-03 的第 2 项），参照 Bellis R-26 的写法。

与 Bellis 的“登录时运行”任务不同：本任务**无触发器、只按需启动、以最高权限运行**，用来验证非提权
进程能用 `schtasks /Run` 触发一个提权的实时核。全是纯函数，不调用 `schtasks`；注册由维护者在管理员
终端里做。读回核对只认 `schtasks /Query /XML` 导出的任务 XML。

要求（见 `docs/modules/realtime-core.md` 的“提权时的计划任务”）：以最高权限运行、无触发器、交互
令牌、以当前用户运行、重复启动时忽略新实例、`ExecutionTimeLimit` 为 `PT0S`、优先级 4–6。
"""

import ntpath
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

_TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
#: 普通优先级（4–6）。
TASK_PRIORITY = 5
#: 本探针的计划任务名。
DEFAULT_TASK_NAME = "BellisGameRuntimeGS03Probe"
_TASK_DESCRIPTION = (
    "Bellis Game Runtime GS-03 探针：按需、以最高权限运行的实时核探针（由 uv run gs03 注册）"
)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class OnDemandTask:
    """一个按需、以最高权限运行的任务。"""

    user_id: str
    """运行身份，形如 `域\\用户名`（见 `current_user_id`）。"""
    command: str
    """可执行文件的绝对路径。"""
    args: tuple[str, ...]
    """参数，逐个按 Windows 命令行规则加引号。"""
    working_directory: str
    """工作目录的绝对路径。"""
    description: str = _TASK_DESCRIPTION


def current_user_id(username: str | None, domain: str | None) -> str:
    """当前用户，形如 `域\\用户名`：计划任务以这个用户运行（参照 Bellis R-26）。

    取不到时报错，不放宽成“任意用户”。
    """

    def usable(part: str | None) -> bool:
        return part is not None and part.strip() != "" and not re.search(r"[\\/]|[\x00-\x1f]", part)

    if not usable(domain) or not usable(username):
        raise ValueError("取不到当前用户（USERDOMAIN 与用户名），无法把任务限定为当前用户")
    return f"{domain}\\{username}"


def current_user_id_from_env(env: Mapping[str, str] | None = None) -> str:
    """从环境变量取当前用户 `域\\用户名`。"""
    source = os.environ if env is None else env
    return current_user_id(source.get("USERNAME"), source.get("USERDOMAIN"))


def _check_text(label: str, text: str) -> None:
    if _CONTROL.search(text):
        raise ValueError(f"{label}含控制字符：{text!r}")


def _check_exec_field(label: str, text: str) -> None:
    _check_text(label, text)
    if text == "":
        raise ValueError(f"{label}为空")
    if "%" in text:
        raise ValueError(f"{label}含 %，计划任务会把它当环境变量展开：{text}")
    if '"' in text:
        raise ValueError(f"{label}含引号：{text}")


def quote_windows_arg(arg: str) -> str:
    """一个参数的 Windows 命令行写法：总加引号，结尾的反斜杠加倍。"""
    if '"' in arg:
        raise ValueError(f"参数含引号：{arg}")
    trailing = len(re.search(r"\\*$", arg).group(0))  # type: ignore[union-attr]
    return f'"{arg}{"\\" * trailing}"'


def split_windows_args(line: str) -> list[str]:
    """按 Windows 命令行规则拆分参数，是 `quote_windows_arg` 的逆运算。"""
    args: list[str] = []
    current: list[str] = []
    quoted = False
    started = False
    i = 0
    while i < len(line):
        char = line[i]
        if char == "\\":
            count = 0
            while i < len(line) and line[i] == "\\":
                count += 1
                i += 1
            if i < len(line) and line[i] == '"':
                current.append("\\" * (count // 2))
                if count % 2 == 1:
                    current.append('"')
                    i += 1
            else:
                current.append("\\" * count)
            started = True
        elif char == '"':
            quoted = not quoted
            started = True
            i += 1
        elif not quoted and char in " \t":
            if started:
                args.append("".join(current))
            current = []
            started = False
            i += 1
        else:
            current.append(char)
            started = True
            i += 1
    if started:
        args.append("".join(current))
    return args


def escape_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _unescape_xml(text: str) -> str:
    named = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}

    def replace(match: re.Match[str]) -> str:
        body = match.group(1)
        if body[0] == "#":
            code = int(body[2:], 16) if body[1] in "xX" else int(body[1:])
            return chr(code)
        return named.get(body, match.group(0))

    return re.sub(r"&(#x[0-9a-fA-F]+|#[0-9]+|[a-zA-Z]+);", replace, text)


def task_xml(task: OnDemandTask) -> str:
    """生成任务 XML。路径不合规（相对路径、含 `%` `"` 或控制字符）时报错。"""
    _check_text("用户", task.user_id)
    if re.fullmatch(r"[^\\]+\\[^\\]+", task.user_id) is None:
        raise ValueError(f"用户不是“域\\用户名”的形式：{task.user_id}")
    _check_text("任务说明", task.description)
    for label, path in (("可执行文件", task.command), ("工作目录", task.working_directory)):
        _check_exec_field(label, path)
        if not ntpath.isabs(path):
            raise ValueError(f"{label}不是绝对路径：{path}")
    for arg in task.args:
        _check_exec_field("参数", arg)
    command = quote_windows_arg(task.command)
    arguments = " ".join(quote_windows_arg(arg) for arg in task.args)
    lines = (
        '<?xml version="1.0" encoding="UTF-16"?>',
        f'<Task version="1.2" xmlns="{_TASK_NAMESPACE}">',
        "  <RegistrationInfo>",
        f"    <Description>{escape_xml(task.description)}</Description>",
        "  </RegistrationInfo>",
        "  <Triggers />",
        "  <Principals>",
        '    <Principal id="Author">',
        f"      <UserId>{escape_xml(task.user_id)}</UserId>",
        "      <LogonType>InteractiveToken</LogonType>",
        "      <RunLevel>HighestAvailable</RunLevel>",
        "    </Principal>",
        "  </Principals>",
        "  <Settings>",
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>",
        "    <StartWhenAvailable>false</StartWhenAvailable>",
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>",
        "    <AllowStartOnDemand>true</AllowStartOnDemand>",
        "    <Enabled>true</Enabled>",
        "    <Hidden>false</Hidden>",
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>",
        "    <WakeToRun>false</WakeToRun>",
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>",
        f"    <Priority>{TASK_PRIORITY}</Priority>",
        "  </Settings>",
        '  <Actions Context="Author">',
        "    <Exec>",
        f"      <Command>{escape_xml(command)}</Command>",
        f"      <Arguments>{escape_xml(arguments)}</Arguments>",
        f"      <WorkingDirectory>{escape_xml(task.working_directory)}</WorkingDirectory>",
        "    </Exec>",
        "  </Actions>",
        "</Task>",
    )
    return "\n".join(lines) + "\n"


def encode_task_xml(xml: str) -> bytes:
    """交给 `schtasks /Create /XML` 的文件内容：UTF-16 LE 带 BOM，与声明一致（同 Bellis R-26）。"""
    return b"\xff\xfe" + xml.encode("utf-16-le")


def decode_query_output(data: bytes) -> str:
    """解码 `schtasks /Query /XML` 的输出：按 BOM、UTF-16LE 特征、UTF-8 依次识别，不猜代码页。"""
    if data[:2] == b"\xff\xfe":
        return data[2:].decode("utf-16-le")
    if data[:2] == b"\xfe\xff":
        return data[2:].decode("utf-16-be")
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8")
    if len(data) >= 2 and data[0] != 0 and data[1] == 0:
        return data.decode("utf-16-le")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("读回的任务 XML 既不是 UTF-16 也不是 UTF-8，无法核对") from error


_ELEMENT = re.compile(r"<([A-Za-z][\w.-]*)(?:\s[^>]*?)?(?:/>|>([\s\S]*?)</\1\s*>)")


def _children(xml: str | None) -> list[tuple[str, str]]:
    if xml is None:
        return []
    return [(match.group(1), match.group(2) or "") for match in _ELEMENT.finditer(xml)]


def _child(xml: str | None, name: str) -> str | None:
    for element_name, inner in _children(xml):
        if element_name == name:
            return inner
    return None


def _text(xml: str | None, name: str) -> str | None:
    inner = _child(xml, name)
    return None if inner is None else _unescape_xml(inner.strip())


@dataclass(frozen=True, slots=True)
class TaskReadback:
    """读回任务中与核对有关的字段。"""

    triggers: tuple[str, ...]
    principal_user: str | None
    logon_type: str | None
    run_level: str | None
    exec_command: str | None
    exec_arguments: str | None
    exec_working_directory: str | None
    multiple_instances: str | None
    execution_time_limit: str | None
    allow_start_on_demand: str | None
    priority: str | None
    enabled: str | None


def read_task_xml(xml: str) -> TaskReadback:
    """解析 `schtasks /Query /XML` 导出的任务 XML。"""
    task = _child(xml, "Task")
    if task is None:
        raise ValueError("读回的内容不是任务 XML")
    principal = _child(_child(task, "Principals"), "Principal")
    settings = _child(task, "Settings")
    actions = _child(task, "Actions")
    exec_action = _child(actions, "Exec")
    triggers = tuple(name for name, _ in _children(_child(task, "Triggers")))
    return TaskReadback(
        triggers=triggers,
        principal_user=_text(principal, "UserId"),
        logon_type=_text(principal, "LogonType"),
        run_level=_text(principal, "RunLevel"),
        exec_command=_text(exec_action, "Command"),
        exec_arguments=_text(exec_action, "Arguments"),
        exec_working_directory=_text(exec_action, "WorkingDirectory"),
        multiple_instances=_text(settings, "MultipleInstancesPolicy"),
        execution_time_limit=_text(settings, "ExecutionTimeLimit"),
        allow_start_on_demand=_text(settings, "AllowStartOnDemand"),
        priority=_text(settings, "Priority"),
        enabled=_text(settings, "Enabled"),
    )


Verdict = Literal["ok", "mismatch", "unverified"]


@dataclass(frozen=True, slots=True)
class FieldCheck:
    label: str
    actual: str
    expected: str
    verdict: Verdict
    note: str | None = None


_SID = re.compile(r"S-1-\d+(-\d+)+", re.IGNORECASE)
_MISSING = "（缺失）"


def _unquote(command: str) -> str:
    if len(command) >= 2 and command.startswith('"') and command.endswith('"'):
        return command[1:-1]
    return command


def check_task(
    task: TaskReadback,
    expected: OnDemandTask,
    *,
    same_path: Callable[[str, str], bool] | None = None,
) -> list[FieldCheck]:
    """逐项核对读回的任务是否满足 GR-06 的要求。缺失的设置按 schema 缺省值计。

    `same_path` 为可选的路径比较器；缺省按大小写不敏感的 `ntpath.normpath` 比较（Windows 路径）。
    """
    checks: list[FieldCheck] = []

    def add(
        label: str, actual: str, wanted: str, verdict: Verdict, note: str | None = None
    ) -> None:
        checks.append(FieldCheck(label, actual, wanted, verdict, note))

    def equal(label: str, actual: str | None, wanted: str) -> None:
        add(label, actual or _MISSING, wanted, "ok" if actual == wanted else "mismatch")

    def paths_equal(actual: str, wanted: str) -> bool:
        if same_path is not None:
            return same_path(actual, wanted)
        return ntpath.normcase(ntpath.normpath(actual)) == ntpath.normcase(ntpath.normpath(wanted))

    def path(label: str, actual: str | None, wanted: str) -> None:
        ok = actual is not None and paths_equal(actual, wanted)
        add(label, actual or _MISSING, wanted, "ok" if ok else "mismatch")

    # 无触发器。
    add(
        "触发器",
        "、".join(task.triggers) or "（无）",
        "（无）",
        "ok" if not task.triggers else "mismatch",
    )
    # 运行用户：读回为 SID 时无法与“域\用户名”比较，记未核对。
    if task.principal_user is not None and _SID.fullmatch(task.principal_user):
        add("运行用户", task.principal_user, expected.user_id, "unverified", "读回为 SID")
    else:
        actual_user = task.principal_user
        ok = actual_user is not None and actual_user.lower() == expected.user_id.lower()
        add("运行用户", actual_user or _MISSING, expected.user_id, "ok" if ok else "mismatch")
    equal("登录类型", task.logon_type, "InteractiveToken")
    equal("运行级别", task.run_level, "HighestAvailable")
    path(
        "命令", None if task.exec_command is None else _unquote(task.exec_command), expected.command
    )
    args = split_windows_args(task.exec_arguments or "")
    add(
        "参数",
        " ".join(args) or "（无）",
        " ".join(expected.args),
        "ok" if args == list(expected.args) else "mismatch",
    )
    path("工作目录", task.exec_working_directory, expected.working_directory)
    equal("多实例", task.multiple_instances or "IgnoreNew", "IgnoreNew")
    equal("按需启动", task.allow_start_on_demand or "true", "true")
    equal("执行时限", task.execution_time_limit or "PT72H", "PT0S")
    priority = task.priority or "7"
    add("优先级", priority, "4–6", "ok" if re.fullmatch(r"[4-6]", priority) else "mismatch")
    equal("任务已启用", task.enabled or "true", "true")
    return checks


def schtasks_path(env: Mapping[str, str] | None = None) -> str:
    """`schtasks.exe` 的绝对路径：取自 SystemRoot，不按 PATH 或当前目录查找。"""
    source = os.environ if env is None else env
    root = source.get("SystemRoot") or source.get("SYSTEMROOT")
    if not root:
        raise ValueError("缺少环境变量 SystemRoot，找不到 schtasks.exe")
    return ntpath.join(root, "System32", "schtasks.exe")


def create_command(task_name: str, xml_path: str, *, schtasks: str = "schtasks.exe") -> list[str]:
    """注册命令（管理员终端运行）：覆盖同名任务。"""
    return [schtasks, "/Create", "/TN", task_name, "/XML", xml_path, "/F"]


def query_command(task_name: str, *, schtasks: str = "schtasks.exe") -> list[str]:
    """读回核对命令（`/Query /XML`，非提权即可运行）。"""
    return [schtasks, "/Query", "/TN", task_name, "/XML"]


def run_command(task_name: str, *, schtasks: str = "schtasks.exe") -> list[str]:
    """触发命令（非提权运行，验证 GS-03 第 2 项）。"""
    return [schtasks, "/Run", "/TN", task_name]


def delete_command(task_name: str, *, schtasks: str = "schtasks.exe") -> list[str]:
    """注销命令。"""
    return [schtasks, "/Delete", "/TN", task_name, "/F"]


def format_command(argv: Sequence[str]) -> str:
    """把 argv 拼成可在终端里照抄的一行（仅展示用，实际调用一律用 argv 列表、不经 shell）。"""
    return " ".join(quote_windows_arg(arg) if " " in arg else arg for arg in argv)
