"""连接之上的最小 JSON-RPC 2.0 对端，对应 Bellis `packages/channel/src/rpc.ts`。

双方都可以请求和响应；参数只用按名传递的对象。每个请求都有截止时间，超时或调用方取消时向对端发
`rpc.cancel`，对端取消对应的处理且不再应答。连接关闭时，未完成的请求以 `closed` 失败，
正在处理的请求被取消。
消息的分类与 Bellis 用的 TypeBox 模式等价（`packages/protocol/src/jsonrpc.ts`），顺序为
请求、通知、成功应答、失败应答；都不是时回 InvalidRequest（id 为 null）。二进制帧不属于 RPC，忽略。
"""

import asyncio
import contextlib
import inspect
import re
from collections.abc import Awaitable, Callable
from typing import Any, cast

from .connection import BinaryMessage, Connection
from .errors import ChannelError

type RpcId = str | int
type RpcParams = dict[str, Any]
type RpcHandler = Callable[[RpcParams], Awaitable[object] | object]
type NotificationHandler = Callable[[RpcParams], None]

RPC_CANCEL = "rpc.cancel"
DEFAULT_MAX_INFLIGHT = 64


class RpcErrorCode:
    """JSON-RPC 2.0 预定义错误码，以及 Bellis 在保留区间内定义的错误码。"""

    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    SERVER_BUSY = -32000


_MISSING = object()


class RpcError(Exception):
    """对端返回的 JSON-RPC 错误；处理函数抛出它即可指定错误码。`data` 缺省时不发送该字段。"""

    def __init__(self, code: int, message: str, data: object = _MISSING) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = None if data is _MISSING else data
        self._has_data = data is not _MISSING

    def to_object(self) -> dict[str, object]:
        error: dict[str, object] = {"code": self.code, "message": self.message}
        if self._has_data:
            error["data"] = self.data
        return error


# ---------------------------------------------------------------------------
# 消息校验：与 TypeBox 的检查等价。JSON 的整数值浮点（如 1.0）在 JS 中就是整数。
# ---------------------------------------------------------------------------
_METHOD = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")
_MAX_SAFE_INTEGER = 2**53 - 1
_ID_MAX_CODE_POINTS = 128


def _is_integer(value: object) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, float) and value.is_integer())


def _rpc_id(value: object) -> RpcId | None:
    """合法的 id（1–128 个码位的字符串，或安全整数范围内的整数）规范化后返回，否则返回 None。"""
    if isinstance(value, str):
        return value if 1 <= len(value) <= _ID_MAX_CODE_POINTS else None
    if _is_integer(value):
        number = int(cast("int | float", value))
        return number if -_MAX_SAFE_INTEGER <= number <= _MAX_SAFE_INTEGER else None
    return None


def _fields(message: object, required: frozenset[str], optional: frozenset[str]) -> dict[str, Any]:
    """必需键齐全、没有多余键的对象，否则为空字典（调用方以 `not` 判断）。"""
    if not isinstance(message, dict):
        return {}
    fields = cast("dict[str, Any]", message)
    keys = fields.keys()
    if not required <= keys or not keys <= required | optional:
        return {}
    return fields


def _is_version(fields: dict[str, Any]) -> bool:
    return fields["jsonrpc"] == "2.0"


def _is_method(value: object) -> bool:
    return isinstance(value, str) and _METHOD.fullmatch(value) is not None


def _params_ok(fields: dict[str, Any]) -> bool:
    return "params" not in fields or isinstance(fields["params"], dict)


_NONE: frozenset[str] = frozenset()
_PARAMS = frozenset({"params"})


def is_request(message: object) -> bool:
    fields = _fields(message, frozenset({"jsonrpc", "id", "method"}), _PARAMS)
    return (
        bool(fields)
        and _is_version(fields)
        and _rpc_id(fields["id"]) is not None
        and _is_method(fields["method"])
        and _params_ok(fields)
    )


def is_notification(message: object) -> bool:
    fields = _fields(message, frozenset({"jsonrpc", "method"}), _PARAMS)
    return (
        bool(fields) and _is_version(fields) and _is_method(fields["method"]) and _params_ok(fields)
    )


def is_success(message: object) -> bool:
    fields = _fields(message, frozenset({"jsonrpc", "id", "result"}), _NONE)
    return bool(fields) and _is_version(fields) and _rpc_id(fields["id"]) is not None


def _error_object(value: object) -> bool:
    fields = _fields(value, frozenset({"code", "message"}), frozenset({"data"}))
    return bool(fields) and _is_integer(fields["code"]) and isinstance(fields["message"], str)


def is_failure(message: object) -> bool:
    fields = _fields(message, frozenset({"jsonrpc", "id", "error"}), _NONE)
    return (
        bool(fields)
        and _is_version(fields)
        and (fields["id"] is None or _rpc_id(fields["id"]) is not None)
        and _error_object(fields["error"])
    )


def classify(message: object) -> str:
    """`request`、`notification`、`success`、`failure` 或 `invalid`。

    判断顺序与 Bellis 的 RpcPeer 相同。
    """
    if is_request(message):
        return "request"
    if is_notification(message):
        return "notification"
    if is_success(message):
        return "success"
    if is_failure(message):
        return "failure"
    return "invalid"


def cancel_target(params: object) -> RpcId | None:
    """`rpc.cancel` 的参数恰为 `{"id": <合法 id>}` 时返回该 id。"""
    fields = _fields(params, frozenset({"id"}), _NONE)
    return _rpc_id(fields["id"]) if fields else None


def _with_params(message: dict[str, object], params: RpcParams | None) -> dict[str, object]:
    if params is not None:
        message["params"] = params
    return message


def _reserved(method: str) -> None:
    if method.startswith("rpc."):
        raise ValueError("rpc. 前缀保留给协议自身")


class RpcPeer:
    """连接之上的 JSON-RPC 2.0 对端。

    构造时即开始读取连接（须在事件循环中构造）；处理函数须在构造后、让出事件循环前注册，
    与 Bellis 要求在同一个同步段内挂上监听器相同。连接关闭后 `closed` 完成。
    """

    def __init__(
        self, connection: Connection, *, timeout: float, max_inflight: int = DEFAULT_MAX_INFLIGHT
    ) -> None:
        self.connection = connection
        self._timeout = timeout
        self._max_inflight = max_inflight
        self._loop = asyncio.get_running_loop()
        self._pending: dict[RpcId, asyncio.Future[object]] = {}
        self._handlers: dict[str, RpcHandler] = {}
        self._notification_handlers: dict[str, NotificationHandler] = {}
        self._inflight: dict[RpcId, asyncio.Task[None]] = {}
        self._next_id = 1
        self.closed: asyncio.Task[ChannelError | None] = self._loop.create_task(self._read())

    def handle(self, method: str, handler: RpcHandler) -> Callable[[], None]:
        """注册请求处理函数，返回撤销函数。同名方法只能有一个处理函数。"""
        _reserved(method)
        return _register(self._handlers, method, handler)

    def on_notification(self, method: str, handler: NotificationHandler) -> Callable[[], None]:
        """注册通知处理函数，返回撤销函数。"""
        _reserved(method)
        return _register(self._notification_handlers, method, handler)

    async def request(
        self, method: str, params: RpcParams | None = None, *, timeout: float | None = None
    ) -> object:
        """发出请求并等待应答。超过截止时间为 `timeout`；对端返回错误时抛出 RpcError。"""
        request_id = self._next_id
        self._next_id += 1
        deadline = self._timeout if timeout is None else timeout
        future: asyncio.Future[object] = self._loop.create_future()
        self._pending[request_id] = future
        try:
            self.connection.send(
                _with_params({"jsonrpc": "2.0", "id": request_id, "method": method}, params)
            )
            async with asyncio.timeout(deadline):
                return await future
        except TimeoutError:
            self._cancel(request_id)
            raise ChannelError(
                "timeout", f"{method} 未在 {round(deadline * 1000)}ms 内响应"
            ) from None
        except asyncio.CancelledError:
            self._cancel(request_id)
            raise
        finally:
            self._pending.pop(request_id, None)

    def notify(self, method: str, params: RpcParams | None = None) -> None:
        self.connection.send(_with_params({"jsonrpc": "2.0", "method": method}, params))

    async def close(self) -> None:
        """关闭连接并等待读取结束。"""
        await self.connection.close()
        await asyncio.wait({self.closed})

    def _cancel(self, request_id: RpcId) -> None:
        if self.connection.is_open:
            with contextlib.suppress(ChannelError):
                self.notify(RPC_CANCEL, {"id": request_id})

    async def _read(self) -> ChannelError | None:
        error: ChannelError | None = None
        try:
            while True:
                message = await self.connection.receive()
                if isinstance(message, BinaryMessage):
                    continue
                self._receive(message.value)
        except ChannelError as caught:
            error = None if caught.code == "closed" else caught
        finally:
            self._on_close()
        return error

    def _receive(self, message: object) -> None:
        kind = classify(message)
        if kind == "invalid":
            self._reply(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": RpcErrorCode.INVALID_REQUEST, "message": "Invalid Request"},
                }
            )
            return
        fields = cast("dict[str, Any]", message)
        if kind == "request":
            self._serve(fields)
        elif kind == "notification":
            self._notification(fields["method"], fields.get("params"))
        elif kind == "success":
            future = self._pending.get(cast("RpcId", _rpc_id(fields["id"])))
            if future is not None and not future.done():
                future.set_result(fields["result"])
        else:
            if fields["id"] is None:
                return
            future = self._pending.get(cast("RpcId", _rpc_id(fields["id"])))
            if future is not None and not future.done():
                error: dict[str, Any] = fields["error"]
                code = int(error["code"])
                if "data" in error:
                    future.set_exception(RpcError(code, error["message"], error["data"]))
                else:
                    future.set_exception(RpcError(code, error["message"]))

    def _notification(self, method: str, params: object) -> None:
        if method == RPC_CANCEL:
            target = cancel_target(params)
            task = None if target is None else self._inflight.get(target)
            if task is not None:
                task.cancel("请求方已取消")
            return
        handler = self._notification_handlers.get(method)
        if handler is None:
            return
        try:
            handler(cast("RpcParams", params) if params is not None else {})
        except Exception as error:
            self._loop.call_exception_handler(
                {"message": f"通知 {method} 的处理函数出错", "exception": error}
            )

    def _serve(self, request: dict[str, Any]) -> None:
        request_id = cast("RpcId", _rpc_id(request["id"]))
        method: str = request["method"]
        handler = self._handlers.get(method)
        if handler is None:
            self._fail(request_id, RpcErrorCode.METHOD_NOT_FOUND, f"Method not found: {method}")
        elif request_id in self._inflight:
            self._fail(request_id, RpcErrorCode.INVALID_REQUEST, "Duplicate request id")
        elif len(self._inflight) >= self._max_inflight:
            self._fail(request_id, RpcErrorCode.SERVER_BUSY, "Server busy")
        else:
            params = cast("RpcParams", request.get("params", {}))
            self._inflight[request_id] = self._loop.create_task(
                self._run(request_id, handler, params)
            )

    async def _run(self, request_id: RpcId, handler: RpcHandler, params: RpcParams) -> None:
        try:
            result = handler(params)
            if inspect.isawaitable(result):
                result = await result
            # 结果超过帧长上限时与 Bellis 一样改回 InternalError。
            if self.connection.is_open:
                self.connection.send({"jsonrpc": "2.0", "id": request_id, "result": result})
        except RpcError as error:
            self._reply({"jsonrpc": "2.0", "id": request_id, "error": error.to_object()})
        except asyncio.CancelledError:
            # 请求方已取消（rpc.cancel）或连接已关闭：请求方不再等这个应答，不发。
            pass
        except Exception:
            # 处理函数的内部错误不外泄细节。
            self._fail(request_id, RpcErrorCode.INTERNAL_ERROR, "Internal error")
        finally:
            self._inflight.pop(request_id, None)

    def _fail(self, request_id: RpcId, code: int, message: str) -> None:
        self._reply(
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        )

    def _reply(self, message: dict[str, object]) -> None:
        if self.connection.is_open:
            with contextlib.suppress(ChannelError):
                self.connection.send(message)

    def _on_close(self) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(ChannelError("closed", "连接已关闭，请求未完成"))
        for task in list(self._inflight.values()):
            task.cancel("连接已关闭")


def _register[T](registry: dict[str, T], method: str, handler: T) -> Callable[[], None]:
    if method in registry:
        raise ValueError(f"方法 {method} 已有处理函数")
    registry[method] = handler

    def dispose() -> None:
        if registry.get(method) is handler:
            del registry[method]

    return dispose
