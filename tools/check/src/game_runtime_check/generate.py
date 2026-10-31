"""核协议生成物的重新生成与核对（`docs/modules/core-protocol.md` 的“生成与核对”）。

`uv run generate` 重新生成全部生成物并写回 `protocol/`；`uv run check` 的 `generated files` 一项以
`--check` 运行：重新生成到临时目录，与入库的逐字节比较，不一致即失败。

管线：Rust 的 `core-protocol-export` 写出 JSON Schema、帧环与音频环的偏移表与示例消息；
datamodel-codegen 从 Schema 生成 Pydantic 模型；本模块从 Schema 的 `x-methods` 写出方法目录。
两份 Python 文件都经 ruff 整理。输出不含时间戳与路径，换行一律 LF，Windows 与 Linux 上逐字节相同。
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

# 本文件位于 tools/check/src/game_runtime_check/，工作区成员以可编辑方式安装。
REPO_ROOT = Path(__file__).resolve().parents[4]
PROTOCOL_DIR = REPO_ROOT / "protocol"
PYTHON = sys.executable
REGENERATE_COMMAND = "uv run generate"

# 生成物（相对 protocol/）。目录下的文件全部是生成物，多出来的也算不一致。
SCHEMA_PATH = "schema/core-protocol.schema.json"
MODELS_PATH = "src/core_protocol/models.py"
METHODS_PATH = "src/core_protocol/methods.py"
GENERATED_DIRS = ("schema", "fixtures")
GENERATED_FILES = ("frame-ring-layout.json", "audio-ring-layout.json", MODELS_PATH, METHODS_PATH)

PYTHON_HEADER = "# 由 `uv run generate` 从 core/crates/core-protocol 生成，不要手改。"

# datamodel-codegen 的选项。联合类型由 Schema 中的 discriminator 生成为按 `type` 选分支的
# 判别联合；额外字段一律禁止，与 Rust 的 deny_unknown_fields 一致。
DATAMODEL_CODEGEN_OPTIONS = (
    "--input-file-type=jsonschema",
    "--output-model-type=pydantic_v2.BaseModel",
    "--target-python-version=3.12",
    "--target-pydantic-version=2.14",
    "--extra-fields=forbid",
    "--use-title-as-name",
    "--use-schema-description",
    "--use-field-description",
    "--use-annotated",
    "--use-one-literal-as-default",
    "--capitalise-enum-members",
    "--use-double-quotes",
    "--skip-root-model",
    "--formatters=builtin",
    "--disable-timestamp",
    "--disable-warnings",
    "--ignore-pyproject",
    "--encoding=utf-8",
)


class GenerationError(Exception):
    """某一步生成失败。"""


def run(argv: Sequence[str], *, cwd: Path, stdin: bytes | None = None) -> bytes:
    """以参数列表启动程序并返回其标准输出；退出码非 0 时连同标准错误抛出。"""
    program = shutil.which(argv[0])
    if program is None:
        raise GenerationError(f"{argv[0]}: not found on PATH")
    with subprocess.Popen(
        [program, *argv[1:]],
        cwd=cwd,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        stdout, stderr = process.communicate(stdin)
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise GenerationError(f"{' '.join(argv)} exited with {process.returncode}\n{detail}")
    return stdout


def to_lf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def tidy_python(source: bytes, repo_path: str) -> bytes:
    """用仓库的 ruff 配置修正并格式化；`repo_path` 决定 ruff 读哪份配置。"""
    stdin_options = ("--stdin-filename", str(PROTOCOL_DIR / repo_path), "-")
    ruff = (PYTHON, "-m", "ruff")
    fixed = run(
        (*ruff, "check", "--fix", "--exit-zero", "--quiet", *stdin_options),
        cwd=REPO_ROOT,
        stdin=source,
    )
    formatted = run((*ruff, "format", *stdin_options), cwd=REPO_ROOT, stdin=fixed)
    return to_lf(formatted)


def export_rust(out: Path, *, locked: bool) -> None:
    """运行 `core-protocol-export`，写出 Schema、帧环与音频环的偏移表与示例消息。"""
    argv = ["cargo", "run", "--quiet", "--package", "core-protocol"]
    if locked:
        argv.append("--locked")
    argv += ["--bin", "core-protocol-export", "--", str(out)]
    run(argv, cwd=REPO_ROOT / "core")


def generate_models(out: Path, work: Path) -> bytes:
    """用 datamodel-codegen 从 Schema 生成 Pydantic 模型。"""
    raw = work / "models.py"
    run(
        (
            PYTHON,
            "-m",
            "datamodel_code_generator",
            f"--input={out / SCHEMA_PATH}",
            f"--output={raw}",
            f"--custom-file-header={PYTHON_HEADER}",
            *DATAMODEL_CODEGEN_OPTIONS,
        ),
        cwd=work,
    )
    return tidy_python(to_lf(raw.read_bytes()), MODELS_PATH)


def ref_name(reference: Mapping[str, Any]) -> str:
    """`{"$ref": "#/$defs/Name"}` 中的 `Name`，即生成的模型类名。"""
    target = str(reference["$ref"])
    prefix = "#/$defs/"
    if not target.startswith(prefix):
        raise GenerationError(f"unexpected reference {target!r}")
    return target.removeprefix(prefix)


def method_constants(methods: Mapping[str, Mapping[str, Any]]) -> tuple[list[str], list[str]]:
    """一组方法的常量定义行与常量名。"""
    lines: list[str] = []
    constants: list[str] = []
    for name, method in methods.items():
        constant = name.replace(".", "_").upper()
        direction = str(method["direction"]).upper()
        params = f"models.{ref_name(method['params'])}"
        result = method["result"]
        if result is None:
            call = f'Notification("{name}", Direction.{direction}, {params})'
        else:
            call = f'Request("{name}", Direction.{direction}, {params}, models.{ref_name(result)})'
        lines.append(f"{constant}: Final = {call}")
        constants.append(constant)
    return lines, constants


def method_index(variable: str, constants: list[str], doc: str) -> list[str]:
    """按方法名索引一组方法的 `Mapping`。"""
    return [
        f"{variable}: Final[Mapping[str, Method]] = {{",
        *(f"    {constant}.name: {constant}," for constant in constants),
        "}",
        f'"""{doc}"""',
    ]


def render_methods(schema: Mapping[str, Any]) -> bytes:
    """从 Schema 的 `x-methods`、`x-test-device-methods`、`x-protocol-version` 与 `x-error-codes`
    写出方法目录。"""
    core_lines, core_constants = method_constants(schema["x-methods"])
    test_lines, test_constants = method_constants(schema["x-test-device-methods"])
    lines = [
        PYTHON_HEADER,
        '"""核协议的 JSON-RPC 方法目录、协议版本与自定义错误码。',
        "",
        "`METHODS` 是核连接上的方法；`TEST_DEVICE_METHODS` 是只用于测试的测试设备连接上的方法。",
        '"""',
        "",
        "from collections.abc import Mapping",
        "from enum import IntEnum",
        "from typing import Final",
        "",
        "from core_protocol import models",
        "from core_protocol.rpc import Direction, Method, Notification, Request",
        "",
        f"PROTOCOL_VERSION: Final = {int(schema['x-protocol-version'])}",
        '"""核协议版本。握手时两端都核对，不相等即拒绝。"""',
        "",
        *core_lines,
        "",
        *method_index("METHODS", core_constants, "核连接上的全部方法，按方法名索引。"),
        "",
        *test_lines,
        "",
        *method_index(
            "TEST_DEVICE_METHODS",
            test_constants,
            "测试设备连接上的全部方法，按方法名索引。只用于测试。",
        ),
        "",
        "",
        "class ErrorCode(IntEnum):",
        '    """核协议自定义的 JSON-RPC 错误码；其它错误用 JSON-RPC 2.0 的标准错误码。"""',
        "",
    ]
    error_codes: Mapping[str, int] = schema["x-error-codes"]
    lines += [f"    {name.upper()} = {int(code)}" for name, code in error_codes.items()]
    return tidy_python("\n".join(lines).encode() + b"\n", METHODS_PATH)


def generate(out: Path, *, locked: bool) -> None:
    """把全部生成物写到空目录 `out`，`out` 的布局与 `protocol/` 相同。

    `locked` 让 cargo 在 `Cargo.lock` 过期时报错而不是改写它，核对时使用。
    """
    export_rust(out, locked=locked)
    schema = json.loads((out / SCHEMA_PATH).read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="datamodel-codegen-") as work:
        models = generate_models(out, Path(work))
    for path, contents in ((MODELS_PATH, models), (METHODS_PATH, render_methods(schema))):
        target = out / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)


def collect(root: Path) -> dict[str, bytes]:
    """`root` 中全部生成物的内容，键为以 `/` 分隔的相对路径。"""
    files: dict[str, bytes] = {}
    for directory in GENERATED_DIRS:
        for path in sorted((root / directory).rglob("*")):
            if path.is_file():
                files[path.relative_to(root).as_posix()] = path.read_bytes()
    for name in GENERATED_FILES:
        path = root / name
        if path.is_file():
            files[name] = path.read_bytes()
    return files


def differences(generated: Mapping[str, bytes], committed: Mapping[str, bytes]) -> list[str]:
    """逐个列出入库的生成物与重新生成的不一致之处。"""
    problems: list[str] = []
    for path in sorted(generated.keys() | committed.keys()):
        if path not in committed:
            problems.append(f"missing: protocol/{path}")
        elif path not in generated:
            problems.append(f"stale: protocol/{path}")
        elif generated[path] != committed[path]:
            problems.append(f"outdated: protocol/{path}")
    return problems


def write_back(generated: Mapping[str, bytes], protocol_dir: Path) -> None:
    """把生成物写回 `protocol_dir`，并删除生成物目录中不再生成的文件。"""
    for path in collect(protocol_dir).keys() - generated.keys():
        (protocol_dir / path).unlink()
    for path, contents in generated.items():
        target = protocol_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="generate", description="Regenerate the core protocol artifacts under protocol/."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="regenerate into a temporary directory and fail if protocol/ differs",
    )
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="core-protocol-") as out:
        try:
            generate(Path(out), locked=args.check)
        except GenerationError as error:
            print(f"generate: {error}", file=sys.stderr)
            return 1
        generated = collect(Path(out))
    if not args.check:
        write_back(generated, PROTOCOL_DIR)
        print(f"generate: wrote {len(generated)} files under protocol/")
        return 0
    problems = differences(generated, collect(PROTOCOL_DIR))
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(
            f"generate: protocol/ is out of date; run `{REGENERATE_COMMAND}` and commit the result",
            file=sys.stderr,
        )
        return 1
    print(f"generate: {len(generated)} generated files are up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
