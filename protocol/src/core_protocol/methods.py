# 由 `uv run generate` 从 core/crates/core-protocol 生成，不要手改。
"""核协议的 JSON-RPC 方法目录、协议版本与自定义错误码。"""

from collections.abc import Mapping
from enum import IntEnum
from typing import Final

from core_protocol import models
from core_protocol.rpc import Direction, Method, Notification, Request

PROTOCOL_VERSION: Final = 1
"""核协议版本。握手时两端都核对，不相等即拒绝。"""

CORE_HELLO: Final = Request(
    "core.hello", Direction.CORE_TO_MAIN, models.CoreHelloParams, models.CoreHelloResult
)
FRAME_RING_ATTACH: Final = Request(
    "frame_ring.attach", Direction.MAIN_TO_CORE, models.FrameRingAttachParams, models.Empty
)
CAPTURE_SET_TARGET: Final = Request(
    "capture.set_target", Direction.MAIN_TO_CORE, models.CaptureSetTargetParams, models.Empty
)
PRODUCER_HEARTBEAT: Final = Notification(
    "producer.heartbeat", Direction.MAIN_TO_CORE, models.ProducerHeartbeatParams
)
ACTION_SUBMIT: Final = Request(
    "action.submit", Direction.MAIN_TO_CORE, models.ActionBlock, models.ActionSubmitResult
)
PRODUCER_REVOKE: Final = Request(
    "producer.revoke", Direction.MAIN_TO_CORE, models.ProducerRevokeParams, models.Empty
)
CORE_STOP: Final = Request("core.stop", Direction.MAIN_TO_CORE, models.Empty, models.Empty)
ACTION_RECEIPT: Final = Notification(
    "action.receipt", Direction.CORE_TO_MAIN, models.ActionReceiptParams
)
INPUT_RELEASED: Final = Notification(
    "input.released", Direction.CORE_TO_MAIN, models.InputReleasedParams
)
CORE_HEALTH: Final = Notification("core.health", Direction.CORE_TO_MAIN, models.CoreHealthParams)

METHODS: Final[Mapping[str, Method]] = {
    CORE_HELLO.name: CORE_HELLO,
    FRAME_RING_ATTACH.name: FRAME_RING_ATTACH,
    CAPTURE_SET_TARGET.name: CAPTURE_SET_TARGET,
    PRODUCER_HEARTBEAT.name: PRODUCER_HEARTBEAT,
    ACTION_SUBMIT.name: ACTION_SUBMIT,
    PRODUCER_REVOKE.name: PRODUCER_REVOKE,
    CORE_STOP.name: CORE_STOP,
    ACTION_RECEIPT.name: ACTION_RECEIPT,
    INPUT_RELEASED.name: INPUT_RELEASED,
    CORE_HEALTH.name: CORE_HEALTH,
}
"""全部方法，按方法名索引。"""


class ErrorCode(IntEnum):
    """核协议自定义的 JSON-RPC 错误码；其它错误用 JSON-RPC 2.0 的标准错误码。"""

    PROTOCOL_VERSION_MISMATCH = -32001
    FRAME_RING_MISMATCH = -32002
