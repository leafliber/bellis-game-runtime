"""生成的 Pydantic 模型与 Rust 类型的配合（GR-03）。

示例消息由 Rust 从其类型产出（`protocol/fixtures/`）。这里用生成的模型解析每一条并往返序列化，
结果须与原值相等；在每个对象节点加未知字段都须被拒绝；联合类型按 `type` 标签选分支。
"""

import copy
import inspect
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel, RootModel, ValidationError

from core_protocol import models
from core_protocol.methods import METHODS, OPERATOR_METHODS, TEST_DEVICE_METHODS
from core_protocol.rpc import Direction, Method, Request

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
FIXTURES = sorted(FIXTURES_DIR.glob("*/*.json"))
ALL_METHODS: dict[str, Method] = {**OPERATOR_METHODS, **TEST_DEVICE_METHODS, **METHODS}
"""三组连接上的全部方法，按方法名。操作员许可连接与核连接共有的 `input.released` 两处 params 相同，
示例消息也只有一份。"""


def fixture_id(path: Path) -> str:
    return path.relative_to(FIXTURES_DIR).as_posix()


def load(name: str) -> Any:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def model_for(path: Path) -> type[BaseModel]:
    """示例消息的类型：目录名是方法名，文件名以 `params` 或 `result` 开头。"""
    method = ALL_METHODS[path.parent.name]
    part = path.name.split(".", 1)[0]
    if part == "params":
        return method.params
    assert part == "result", path
    assert isinstance(method, Request), path
    return method.result


def object_paths(value: Any, path: tuple[str | int, ...] = ()) -> Iterator[tuple[str | int, ...]]:
    """`value` 中每个对象节点的路径。"""
    if isinstance(value, dict):
        yield path
        for key, child in cast(dict[str, Any], value).items():
            yield from object_paths(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(cast(list[Any], value)):
            yield from object_paths(child, (*path, index))


def node(value: Any, path: tuple[str | int, ...]) -> Any:
    for key in path:
        value = value[key]
    return value


def test_every_method_has_its_fixtures() -> None:
    parts = {(path.parent.name, path.name.split(".", 1)[0]) for path in FIXTURES}
    for name, method in ALL_METHODS.items():
        assert (name, "params") in parts, name
        assert ((name, "result") in parts) == isinstance(method, Request), name
    assert {name for name, _ in parts} <= ALL_METHODS.keys()


def test_the_test_device_group_is_separate() -> None:
    """测试设备连接上的方法单独成组，不与其它两组的方法同名，方向只在测试一方与实时核之间。"""
    assert not METHODS.keys() & TEST_DEVICE_METHODS.keys()
    assert not OPERATOR_METHODS.keys() & TEST_DEVICE_METHODS.keys()
    test_directions = {Direction.CORE_TO_TEST, Direction.TEST_TO_CORE}
    assert {method.direction for method in TEST_DEVICE_METHODS.values()} == test_directions
    assert not {method.direction for method in METHODS.values()} & test_directions


def test_the_operator_group_shares_only_input_released() -> None:
    """操作员许可连接上的方法单独成组，方向只在控制台与实时核之间；与核连接只共有 `input.released`
    与 `input.residual`，params 相同。许可的 params 与测试许可源的相同。"""
    operator_directions = {Direction.CORE_TO_OPERATOR, Direction.OPERATOR_TO_CORE}
    assert {method.direction for method in OPERATOR_METHODS.values()} == operator_directions
    assert not {method.direction for method in METHODS.values()} & operator_directions
    # 与核连接只共有发往控制台的两条通知：input.released 与 input.residual。
    assert METHODS.keys() & OPERATOR_METHODS.keys() == {"input.released", "input.residual"}
    assert OPERATOR_METHODS["input.released"].params is METHODS["input.released"].params
    assert OPERATOR_METHODS["input.residual"].params is METHODS["input.residual"].params
    for name in ("grant", "revoke"):
        operator = OPERATOR_METHODS[f"operator_lease.{name}"]
        test = TEST_DEVICE_METHODS[f"test_lease.{name}"]
        assert operator.params is test.params


@pytest.mark.parametrize("path", FIXTURES, ids=fixture_id)
def test_fixture_round_trips(path: Path) -> None:
    original = json.loads(path.read_text(encoding="utf-8"))

    parsed = model_for(path).model_validate_json(path.read_bytes())

    assert parsed.model_dump(mode="json") == original


@pytest.mark.parametrize("path", FIXTURES, ids=fixture_id)
def test_unknown_fields_are_rejected_at_every_level(path: Path) -> None:
    original = json.loads(path.read_text(encoding="utf-8"))
    model = model_for(path)
    for object_path in object_paths(original):
        changed = copy.deepcopy(original)
        node(changed, object_path)["unexpected"] = True
        with pytest.raises(ValidationError, match="extra_forbidden"):
            model.model_validate_json(json.dumps(changed))


def test_every_model_forbids_extra_fields() -> None:
    generated = [
        cls
        for _, cls in inspect.getmembers(models, inspect.isclass)
        if issubclass(cls, BaseModel) and not issubclass(cls, RootModel) and cls is not BaseModel
    ]
    assert generated
    for cls in generated:
        assert cls.model_config.get("extra") == "forbid", cls


def test_type_tag_selects_the_union_branch() -> None:
    target = models.CaptureSetTargetParams.model_validate
    window = target(load("capture.set_target/params.window.json"))
    fake = target(load("capture.set_target/params.fake.json"))
    step = models.ActionBlock.model_validate(load("action.submit/params.step.json"))
    continuous = models.ActionBlock.model_validate(load("action.submit/params.continuous.json"))
    accepted = models.ActionSubmitResult.model_validate(load("action.submit/result.accepted.json"))
    rejected = models.ActionSubmitResult.model_validate(load("action.submit/result.rejected.json"))
    receipt = models.ActionReceiptParams.model_validate(load("action.receipt/params.json"))

    assert isinstance(window.target.root, models.WindowTarget)
    assert isinstance(fake.target.root, models.FakeTarget)
    assert isinstance(fake.target.root.surface, models.FrameRingAttachParams)
    assert [type(action.root) for action in step.steps[0].actions] == [models.Click]
    assert [[type(held.root) for held in step.held] for step in continuous.steps] == [
        [models.HeldKey, models.HeldMouseButton, models.HeldAxis],
        [models.HeldKey],
        [],
    ]
    assert [[type(action.root) for action in step.actions] for step in continuous.steps] == [
        [models.MouseMove],
        [models.KeyPress, models.Scroll],
        [],
    ]
    assert isinstance(accepted.root, models.Accepted)
    assert isinstance(rejected.root, models.Rejected)
    assert [type(outcome.root) for outcome in receipt.outcomes] == [
        models.Executed,
        models.Dropped,
    ]
    events = {
        "key-down": models.FakeKeyDown,
        "key-up": models.FakeKeyUp,
        "mouse-down": models.FakeMouseDown,
        "mouse-up": models.FakeMouseUp,
        "axis-set": models.FakeAxisSet,
        "axis-release": models.FakeAxisRelease,
        "click": models.FakeClick,
        "key-press": models.FakeKeyPress,
        "scroll": models.FakeScroll,
        "mouse-move": models.FakeMouseMove,
        "release-all": models.FakeReleaseAll,
    }
    for case, branch in events.items():
        params = load(f"fake_input.event/params.{case}.json")
        assert isinstance(models.FakeInputEventParams.model_validate(params).event.root, branch)


@pytest.mark.parametrize("path", FIXTURES, ids=fixture_id)
def test_unknown_or_swapped_type_tag_is_rejected_by_the_discriminator(path: Path) -> None:
    """未知标签按判别联合报 `union_tag_invalid`，说明按标签选分支而不是逐个分支去试；把标签换成
    同一联合的另一个分支，按那个分支校验，因字段不符而被拒绝。"""
    original = json.loads(path.read_text(encoding="utf-8"))
    model = model_for(path)
    for object_path in object_paths(original):
        if "type" not in node(original, object_path):
            continue
        changed = copy.deepcopy(original)
        node(changed, object_path)["type"] = "no_such_type"
        with pytest.raises(ValidationError) as unknown:
            model.model_validate_json(json.dumps(changed))
        assert [error["type"] for error in unknown.value.errors()] == ["union_tag_invalid"]

        for tag in ("window", "fake", "key", "axis", "click", "scroll", "accepted", "dropped"):
            if tag == node(original, object_path)["type"]:
                continue
            node(changed, object_path)["type"] = tag
            with pytest.raises(ValidationError):
                model.model_validate_json(json.dumps(changed))
