"""冻结的 Bellis 游戏桥控制契约与 Rust 类型的离线双向核对。"""

import importlib.util
import json
import re
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from types import ModuleType
from typing import Any, Literal, NotRequired, TypedDict, cast

import pytest
from pydantic import BaseModel

from game_runtime.channel.rpc import is_notification, is_request
from game_runtime_check.generate import run
from game_runtime_check.vendor import REPO_ROOT, VENDOR_DIR, differences

type Sender = Literal["bellis", "runtime"]
type JsonObject = dict[str, object]
type Schema = dict[str, Any]


class Fixture(TypedDict):
    name: str
    sender: Sender
    rawJson: NotRequired[str]
    message: NotRequired[JsonObject]
    responseTo: NotRequired[str]
    valid: bool


class Method(TypedDict):
    sender: Sender
    params: str
    result: str | None


class Report(TypedDict):
    messages: list[str | None]
    now_ms: int


@dataclass(frozen=True)
class Case:
    name: str
    sender: Sender
    raw: str
    valid: bool
    request: str | None


@dataclass(frozen=True)
class Contract:
    models: ModuleType
    major: int
    methods: dict[str, Method]
    schemas: dict[str, Schema]
    fixtures: list[Case]


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _raw(fixture: Fixture) -> str:
    assert ("rawJson" in fixture) != ("message" in fixture), fixture["name"]
    if "rawJson" in fixture:
        return fixture["rawJson"]
    assert "message" in fixture
    return _dump(fixture["message"])


@pytest.fixture(scope="module")
def contract(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Contract]:
    assert not differences(VENDOR_DIR)
    # 从临时目录加载相同字节，让 Python 将 __pycache__ 留在临时目录。
    path = tmp_path_factory.mktemp("bellis-game-bridge") / "game_bridge.py"
    path.write_bytes((VENDOR_DIR / "models/game_bridge.py").read_bytes())
    spec = importlib.util.spec_from_file_location("frozen_bellis_game_bridge", path)
    assert spec is not None
    assert spec.loader is not None
    models = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = models
    try:
        spec.loader.exec_module(models)
        schema = cast(
            Schema, json.loads((VENDOR_DIR / "schemas/game-bridge.schema.json").read_bytes())
        )
        raw_cases = cast(
            list[Fixture], json.loads((VENDOR_DIR / "fixtures/game-control.json").read_bytes())
        )
        requests: dict[object, tuple[str, object]] = {}
        for case in raw_cases:
            if case["valid"] and case["sender"] == "bellis":
                raw = _raw(case)
                request = cast(JsonObject, json.loads(raw))
                requests[request["id"]] = (raw, request["method"])
        fixtures: list[Case] = []
        for case in raw_cases:
            raw = _raw(case)
            pending = None
            if "responseTo" in case:
                response = cast(JsonObject, json.loads(raw))
                pending, method = requests[response["id"]]
                assert method == case["responseTo"], case["name"]
            fixtures.append(Case(case["name"], case["sender"], raw, case["valid"], pending))
        assert schema["x-major"] == models.GAME_BRIDGE_MAJOR
        yield Contract(
            models,
            cast(int, schema["x-major"]),
            cast(dict[str, Method], schema["x-methods"]["game-runtime.core"]),
            cast(dict[str, Schema], schema["$defs"]),
            fixtures,
        )
    finally:
        del sys.modules[spec.name]


def _schema_accepts(schema: Schema, value: object) -> bool:
    """按本组控制载荷所用的 Schema 关键字核对值。新增关键字会使检查失败。"""
    if "anyOf" in schema:
        assert set(schema) == {"anyOf"}
        return any(_schema_accepts(part, value) for part in cast(list[Schema], schema["anyOf"]))
    if "enum" in schema:
        assert set(schema) == {"enum"}
        return value in cast(list[str], schema["enum"])
    match schema["type"]:
        case "object":
            assert set(schema) <= {"type", "properties", "required", "additionalProperties"}
            if not isinstance(value, dict):
                return False
            fields = cast(JsonObject, value)
            properties = cast(dict[str, Schema], schema["properties"])
            required = cast(list[str], schema.get("required", []))
            if not fields.keys() >= set(required):
                return False
            if (
                schema.get("additionalProperties") is False
                and not fields.keys() <= properties.keys()
            ):
                return False
            return all(
                _schema_accepts(rule, fields[key])
                for key, rule in properties.items()
                if key in fields
            )
        case "integer":
            assert set(schema) <= {"type", "minimum", "maximum"}
            if type(value) is int:
                number = value
            elif isinstance(value, float) and value.is_integer():
                number = int(value)
            else:
                return False
            return bool(schema["minimum"] <= number <= schema["maximum"])
        case "string":
            assert set(schema) <= {"type", "pattern", "minLength", "maxLength"}
            if not isinstance(value, str):
                return False
            pattern = cast(str | None, schema.get("pattern"))
            if pattern is not None:
                assert pattern.startswith("^")
                assert pattern.endswith("$")
                if not re.fullmatch(pattern, value):
                    return False
            return bool(
                schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", len(value))
            )
        case "null":
            assert set(schema) == {"type"}
            return value is None
        case _:
            pytest.fail(f"Unsupported control payload schema: {schema!r}")


def _payload_binding(
    contract: Contract, case: Case, message: JsonObject
) -> tuple[Literal["result", "params"], str] | None:
    if case.request is None:
        method = message.get("method")
        spec = contract.methods.get(method) if isinstance(method, str) else None
        if spec is None or case.sender != spec["sender"]:
            return None
        envelope_matches = is_notification if spec["result"] is None else is_request
        return ("params", spec["params"]) if envelope_matches(message) else None

    request = cast(JsonObject, json.loads(case.request))
    spec = contract.methods[cast(str, request["method"])]
    if (
        case.sender == spec["sender"]
        or spec["result"] is None
        or message.get("id") != request["id"]
    ):
        return None
    return "result", spec["result"]


def _python_encode(contract: Contract, case: Case) -> JsonObject | None:
    try:
        decoded: object = contract.models.parse_game_bridge_json(case.raw)
        if not isinstance(decoded, dict):
            return None
        message = cast(JsonObject, decoded)
        binding = _payload_binding(contract, case, message)
        if binding is None:
            return None
        field, name = binding
        if field == "result":
            contract.models.RpcSuccess.model_validate(message)
            if (
                name == "GameCoreDescription"
                and contract.models.read_game_bridge_major(message[field]) != contract.major
            ):
                return None
        model = cast(type[BaseModel], getattr(contract.models, name))
        data = model.model_validate(message.get(field)).model_dump(mode="json")
    except ValueError:
        return None
    assert data == message[field], case.name
    assert _schema_accepts(contract.schemas[name], data), case.name
    return {**message, field: data}


def _rust(cases: Sequence[Case]) -> Report:
    output = run(
        (
            "cargo",
            "run",
            "--offline",
            "--locked",
            "--quiet",
            "-p",
            "game-bridge-protocol",
            "--example",
            "control-interop",
        ),
        cwd=REPO_ROOT / "core",
        stdin=_dump(
            [{"sender": case.sender, "raw": case.raw, "request": case.request} for case in cases]
        ).encode(),
    )
    return cast(Report, json.loads(output))


def _roundtrip(contract: Contract, cases: Sequence[Case]) -> None:
    from_python: list[Case] = []
    expected: list[JsonObject] = []
    for case, rust in zip(cases, _rust(cases)["messages"], strict=True):
        python = _python_encode(contract, case)
        assert (rust is not None) == case.valid, case.name
        assert (python is not None) == case.valid, case.name
        if rust is not None and python is not None:
            # Rust 编码结果回到冻结模型和 Schema，并把 Python 编码结果送回 Rust。
            assert _python_encode(contract, replace(case, raw=rust)) == python, case.name
            from_python.append(replace(case, raw=_dump(python)))
            expected.append(python)
    for case, rust, python in zip(
        from_python, _rust(from_python)["messages"], expected, strict=True
    ):
        assert rust is not None, case.name
        assert json.loads(rust) == python, case.name


def test_locked_control_fixtures_roundtrip(contract: Contract) -> None:
    _roundtrip(contract, contract.fixtures)


def _boundary_values(rule: Schema) -> Iterator[object]:
    if "anyOf" in rule:
        for part in cast(list[Schema], rule["anyOf"]):
            yield from _boundary_values(part)
    elif "enum" in rule:
        yield from cast(list[str], rule["enum"])
        yield "unknown"
    else:
        match rule["type"]:
            case "integer":
                minimum = cast(int, rule["minimum"])
                maximum = cast(int, rule["maximum"])
                yield from (minimum - 1, minimum, maximum, maximum + 1, True, str(maximum))
            case "string" if "maxLength" in rule:
                maximum = cast(int, rule["maxLength"])
                yield from ("", "🍀", "🍀" * maximum, "🍀" * (maximum + 1), False)
            case "string":
                yield from ("", "A_.:-", "a" * 64, "a" * 65, "-id", "x\n", "非ASCII")
            case "null":
                yield None
            case _:
                pytest.fail(f"Unsupported control field schema: {rule!r}")


def _payload_examples(schema: Schema, initial: JsonObject) -> Iterator[tuple[str, object]]:
    yield "extra field", {**initial, "unknown": None}
    yield "positional", list(initial.values())
    yield "null payload", None
    for field, rule in cast(dict[str, Schema], schema["properties"]).items():
        yield f"missing {field}", {key: value for key, value in initial.items() if key != field}
        for value in _boundary_values(rule):
            yield f"{field}={value!r}", {**initial, field: value}


def test_control_schema_boundaries_roundtrip(contract: Contract) -> None:
    cases: list[Case] = []
    covered: set[str] = set()
    for case in contract.fixtures:
        if not case.valid:
            continue
        initial = cast(JsonObject, json.loads(case.raw))
        binding = _payload_binding(contract, case, initial)
        assert binding is not None, case.name
        field, name = binding
        if name in covered:
            continue
        covered.add(name)
        schema = contract.schemas[name]
        for label, data in _payload_examples(schema, cast(JsonObject, initial[field])):
            accepted = _schema_accepts(schema, data)
            if accepted and name == "GameCoreDescription":
                accepted = cast(JsonObject, data)["major"] == contract.major
            cases.append(
                replace(
                    case,
                    name=f"{name}: {label}",
                    raw=_dump({**initial, field: data}),
                    valid=accepted,
                )
            )
    assert covered == {
        name
        for spec in contract.methods.values()
        for name in (spec["params"], spec["result"])
        if name is not None
    }
    _roundtrip(contract, cases)


def test_rust_monotonic_ms_uses_bellis_clock_domain() -> None:
    # pyright[nodejs] 的锁定依赖提供同一 Node 入口，容器不需要在 PATH 上另装 Node。
    bellis_clock = (
        sys.executable,
        "-m",
        "nodejs_wheel",
        "-p",
        "String(process.hrtime.bigint() / 1000000n)",
    )
    before = int(run(bellis_clock, cwd=REPO_ROOT))
    now = _rust(())["now_ms"]
    after = int(run(bellis_clock, cwd=REPO_ROOT))
    assert 0 < before <= now <= after
