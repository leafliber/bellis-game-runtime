"""计划任务 XML 的生成、编码与读回核对（GS-03 第 2 项的纯逻辑部分，跨平台）。

注册需要管理员，这里不尝试；只验证 XML 结构正确、能被解析、核对逻辑正确。
"""

import sys

import pytest

from gs03_probe.task_xml import (
    OnDemandTask,
    check_task,
    current_user_id,
    decode_query_output,
    encode_task_xml,
    quote_windows_arg,
    read_task_xml,
    split_windows_args,
    task_xml,
)

TASK = OnDemandTask(
    user_id="MACHINE\\user",
    command="C:\\probe\\gs03-core-probe.exe",
    args=("--from-cred",),
    working_directory="C:\\probe",
)


def test_xml_declares_the_required_settings() -> None:
    readback = read_task_xml(task_xml(TASK))
    assert readback.triggers == ()  # 无触发器
    assert readback.run_level == "HighestAvailable"  # 以最高权限运行
    assert readback.logon_type == "InteractiveToken"  # 交互令牌
    assert readback.multiple_instances == "IgnoreNew"  # 重复启动忽略新实例
    assert readback.allow_start_on_demand == "true"  # 只按需启动
    assert readback.execution_time_limit == "PT0S"
    assert readback.priority == "5"
    assert readback.principal_user == "MACHINE\\user"


def test_check_task_all_ok_on_round_trip() -> None:
    readback = read_task_xml(task_xml(TASK))
    checks = check_task(readback, TASK, same_path=lambda a, b: a == b)
    bad = [check for check in checks if check.verdict == "mismatch"]
    assert bad == [], bad
    labels = {check.label for check in checks}
    assert {"触发器", "运行级别", "登录类型", "多实例", "按需启动", "命令", "参数"} <= labels


def test_check_task_flags_wrong_run_level() -> None:
    xml = task_xml(TASK).replace("HighestAvailable", "LeastPrivilege")
    checks = check_task(read_task_xml(xml), TASK, same_path=lambda a, b: a == b)
    run_level = next(check for check in checks if check.label == "运行级别")
    assert run_level.verdict == "mismatch"


def test_encode_is_utf16le_with_bom_and_round_trips() -> None:
    xml = task_xml(TASK)
    encoded = encode_task_xml(xml)
    assert encoded[:2] == b"\xff\xfe"
    assert encoded[2:4] == b"<\x00"  # '<' 的 UTF-16LE
    assert decode_query_output(encoded) == xml


def test_decode_recognizes_utf8_and_utf16() -> None:
    assert decode_query_output("héllo".encode("utf-16-le")) == "héllo"
    assert decode_query_output(b"\xef\xbb\xbf" + "héllo".encode()) == "héllo"


@pytest.mark.skipif(sys.platform != "win32", reason="OEM 代码页只在 Windows 上有")
def test_decode_falls_back_to_oem_code_page() -> None:
    # 重定向时 schtasks 按 OEM 代码页输出；“é”在 936 与 437 中都有，编码后都不是合法的 UTF-8。
    encoded = "héllo".encode("oem")
    assert decode_query_output(encoded) == "héllo"


def test_current_user_id_requires_domain_and_name() -> None:
    assert current_user_id("user", "MACHINE") == "MACHINE\\user"
    with pytest.raises(ValueError, match="当前用户"):
        current_user_id("user", None)
    with pytest.raises(ValueError, match="当前用户"):
        current_user_id(None, "MACHINE")


@pytest.mark.parametrize(
    "args",
    [
        ("--from-cred",),
        ("--service", "bellis", "--name", "gs03-probe"),
        ("a b", "c\\", "d\\\\"),
    ],
)
def test_quote_and_split_round_trip(args: tuple[str, ...]) -> None:
    line = " ".join(quote_windows_arg(arg) for arg in args)
    assert split_windows_args(line) == list(args)


def test_task_xml_rejects_relative_and_percent_paths() -> None:
    with pytest.raises(ValueError, match="绝对路径"):
        task_xml(OnDemandTask("M\\u", "probe.exe", (), "C:\\probe"))
    with pytest.raises(ValueError, match="环境变量"):
        task_xml(OnDemandTask("M\\u", "C:\\p\\%X%.exe", (), "C:\\probe"))
    with pytest.raises(ValueError, match="域"):
        task_xml(OnDemandTask("nodomain", "C:\\probe.exe", (), "C:\\probe"))
