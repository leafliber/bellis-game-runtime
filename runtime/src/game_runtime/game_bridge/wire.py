"""从工作区只读加载 GD-03 锁定的模型与原始帧解码器。"""

import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ValidationError

from game_runtime.channel import ChannelError, RpcError, RpcErrorCode
from game_runtime.channel.rpc import RPC_CANCEL, cancel_target, classify

__all__ = ["decode_request", "ms", "ns", "params", "selection_source", "v1"]

if TYPE_CHECKING:
    import game_bridge as v1
else:
    _source = Path(__file__).resolve().parents[4] / "vendor/bellis-protocol/models/game_bridge.py"
    v1 = ModuleType("_bellis_game_bridge_v1")
    v1.__file__ = str(_source)
    sys.modules[v1.__name__] = v1
    exec(compile(_source.read_bytes(), str(_source), "exec"), v1.__dict__)  # noqa: S102


def decode_request(raw: bytes) -> object:
    try:
        message = v1.parse_game_bridge_json(raw)
    except (ValueError, RecursionError):
        raise ChannelError("protocol-error", "Bellis 游戏桥 JSON 无效") from None
    kind = classify(message)
    if kind == "request" or kind == "invalid":
        return message
    fields = cast("dict[str, object]", message)
    if (
        kind == "notification"
        and fields["method"] == RPC_CANCEL
        and cancel_target(fields.get("params")) is not None
    ):
        return message
    raise ChannelError("protocol-error", "Bellis 语义连接只接收宿主请求")


def params[T: BaseModel](model: type[T], value: object) -> T:
    try:
        return model.model_validate(value)
    except ValidationError:
        raise RpcError(RpcErrorCode.INVALID_PARAMS, "游戏桥请求参数无效") from None


def ms(ns: int) -> int:
    return ns // 1_000_000


def ns(ms: int) -> int:
    return ms * 1_000_000


def selection_source(value: str | None) -> Literal["director", "vote", "default"] | None:
    match value:
        case "director":
            return "director"
        case "vote":
            return "vote"
        case "default":
            return "default"
        case _:
            return None
