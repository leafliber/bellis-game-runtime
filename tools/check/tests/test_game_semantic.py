"""语义 v2 的固定来源、原始 JSON 与 Runtime 载荷入口；不签发输入许可。"""

import json
from typing import Any, NotRequired, TypedDict, cast

import pytest
from pydantic import BaseModel

from game_runtime.channel import ChannelError, RpcError
from game_runtime.channel.rpc import is_notification, is_request
from game_runtime.game_bridge.wire import decode_request, params, v1, v2
from game_runtime_check.vendor import VENDOR_DIR, differences


class WireCase(TypedDict):
    name: str
    sender: str
    valid: bool
    message: NotRequired[dict[str, Any]]
    rawJson: NotRequired[str]
    responseTo: NotRequired[str]


_SCHEMA = json.loads((VENDOR_DIR / "schemas/game-bridge-v2.schema.json").read_bytes())
_METHODS = cast(dict[str, dict[str, str | None]], _SCHEMA["x-methods"]["game-runtime.main"])
_CASES = cast(list[WireCase], json.loads((VENDOR_DIR / "fixtures/game-main-v2.json").read_bytes()))


def _accepts(case: WireCase) -> bool:
    raw = case.get("rawJson")
    if raw is None:
        assert "message" in case
        raw = json.dumps(case["message"], ensure_ascii=False, allow_nan=False)
    try:
        message = v2.parse_game_bridge_json(raw)
        if not isinstance(message, dict):
            return False
        fields = cast(dict[str, object], message)
        response_to = case.get("responseTo")
        if response_to is not None:
            spec = _METHODS.get(response_to)
            if spec is None or spec["sender"] == case["sender"] or spec["result"] is None:
                return False
            payload = v2.RpcSuccess.model_validate(message).result
            if response_to == "game.describe" and v2.read_game_bridge_major(payload) != 2:
                return False
            name = spec["result"]
        else:
            method = fields.get("method")
            spec = _METHODS.get(method) if isinstance(method, str) else None
            if spec is None or spec["sender"] != case["sender"]:
                return False
            valid_envelope = is_notification if spec["result"] is None else is_request
            if not valid_envelope(fields):
                return False
            if case["sender"] == "bellis":
                assert decode_request(raw.encode(), major=2) == message
            payload = fields.get("params")
            name = spec["params"]
        assert name is not None
        model = cast(type[BaseModel], getattr(v2, name))
        validated = params(model, payload)
        assert validated.model_dump(mode="json") == payload, case["name"]
        return True
    except (ValueError, ChannelError, RpcError):
        return False


def test_semantic_contract_has_one_verified_source_and_no_core_role() -> None:
    assert differences(VENDOR_DIR) == []
    assert _SCHEMA["x-major"] == v2.GAME_BRIDGE_MAJOR == 2
    assert v1.GAME_BRIDGE_MAJOR == 1
    assert set(_SCHEMA["x-methods"]) == {"game-runtime.main"}
    assert "lease.renew" not in _METHODS
    assert "input.stop" not in _METHODS
    assert not hasattr(v1, "GameTaskOpenParams")
    assert not hasattr(v1, "GameActionResolveOutcomeParams")


@pytest.mark.parametrize("case", _CASES, ids=[case["name"] for case in _CASES])
def test_frozen_semantic_fixture_through_runtime_payload_entry(case: WireCase) -> None:
    assert _accepts(case) is case["valid"]


@pytest.mark.parametrize("major", [0, 3, 65535, True])
def test_decoder_rejects_unsupported_selected_major(major: int) -> None:
    with pytest.raises(ChannelError, match="主版本不受支持"):
        decode_request(b'{"jsonrpc":"2.0","id":1,"method":"task.open","params":{}}', major=major)


@pytest.mark.parametrize("spelling", ["-0", "1.0", "1e0", "9007199254740993"])
def test_task_deadline_keeps_raw_integer_lexical_checks(spelling: str) -> None:
    raw = (
        '{"jsonrpc":"2.0","id":1,"method":"task.open","params":'
        '{"taskId":"task-1","objective":"synthetic objective",'
        '"expiresAtMs":DEADLINE,"overallCompletion":null}}'
    ).replace("DEADLINE", spelling)
    with pytest.raises(ChannelError, match="JSON 无效"):
        decode_request(raw.encode(), major=2)


def test_task_and_resolution_identity_cannot_arrive_as_extra_authority_fields() -> None:
    task = {
        "taskId": "task-1",
        "objective": "synthetic objective",
        "expiresAtMs": 2000,
        "overallCompletion": None,
    }
    for extra in ("permit", "budgetReset", "controlTargetId"):
        with pytest.raises(RpcError, match="参数无效"):
            params(v2.GameTaskOpenParams, {**task, extra: True})
    resolution = {
        "sessionId": "session-1",
        "attemptId": "attempt-1",
        "resolution": "reconciled_unknown",
    }
    with pytest.raises(RpcError, match="参数无效"):
        params(v2.GameActionResolveOutcomeParams, {**resolution, "actionCommitted": True})


def test_host_request_decoder_rejects_runtime_health_notification() -> None:
    raw = b'{"jsonrpc":"2.0","method":"health.reported","params":{}}'
    with pytest.raises(ChannelError, match="只接收宿主请求"):
        decode_request(raw, major=2)
