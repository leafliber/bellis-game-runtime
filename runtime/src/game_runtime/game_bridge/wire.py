"""从工作区只读加载 GD-03 锁定的模型与原始帧解码器。"""

import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ValidationError

from game_runtime.channel import ChannelError, RpcError, RpcErrorCode
from game_runtime.channel.rpc import RPC_CANCEL, cancel_target, classify

__all__ = ["decode_request", "ms", "ns", "params", "selection_source", "v1", "v2"]


def _load_pinned(filename: str, name: str) -> ModuleType:
    source = Path(__file__).resolve().parents[4] / "vendor/bellis-protocol/models" / filename
    module = ModuleType(name)
    module.__file__ = str(source)
    sys.modules[name] = module
    exec(compile(source.read_bytes(), str(source), "exec"), module.__dict__)  # noqa: S102
    return module


if TYPE_CHECKING:
    import game_bridge as v1
    import game_bridge_v2 as v2
else:
    v1 = _load_pinned("game_bridge.py", "_bellis_game_bridge_v1")
    v2 = _load_pinned("game_bridge_v2.py", "_bellis_game_bridge_v2")


def decode_request(raw: bytes, *, major: int = 1) -> object:
    """按已选择的语义版本解码；方法路由与载荷仍须由接纳入口校验。"""
    if type(major) is not int or major not in (1, 2):
        raise ChannelError("protocol-error", "Bellis 语义主版本不受支持")
    bridge = v1 if major == 1 else v2
    try:
        message = bridge.parse_game_bridge_json(raw)
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
