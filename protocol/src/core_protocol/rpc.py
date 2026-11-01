"""JSON-RPC 方法的描述类型。各方法由生成的 `core_protocol.methods` 列出。

核连接上实时核是通道客户端，主进程是服务端，两边都可以发请求与通知（JSON-RPC 2.0）；
消息集见 `docs/modules/core-protocol.md` 的“消息集”，操作员许可连接与只用于测试的测试设备连接见同一
文档的“操作员许可连接”与“测试设备连接”。
"""

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel


class Direction(StrEnum):
    """消息的发送方。"""

    CORE_TO_MAIN = "core_to_main"
    """实时核发往主进程。"""
    MAIN_TO_CORE = "main_to_core"
    """主进程发往实时核。"""
    CORE_TO_TEST = "core_to_test"
    """实时核发往测试一方（测试设备连接，只用于测试）。"""
    TEST_TO_CORE = "test_to_core"
    """测试一方发往实时核（测试设备连接，只用于测试）。"""
    CORE_TO_OPERATOR = "core_to_operator"
    """实时核发往操作员控制台（操作员许可连接）。"""
    OPERATOR_TO_CORE = "operator_to_core"
    """操作员控制台发往实时核（操作员许可连接）。"""


@dataclass(frozen=True)
class Request[P: BaseModel, R: BaseModel]:
    """有响应的方法（JSON-RPC 请求）。"""

    name: str
    direction: Direction
    params: type[P]
    result: type[R]


@dataclass(frozen=True)
class Notification[P: BaseModel]:
    """没有响应的方法（JSON-RPC 通知）。"""

    name: str
    direction: Direction
    params: type[P]


type Method = Request[BaseModel, BaseModel] | Notification[BaseModel]
"""任一方法。"""
