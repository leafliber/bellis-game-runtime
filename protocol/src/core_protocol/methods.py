# 由 `uv run generate` 从 core/crates/core-protocol 生成，不要手改。
"""核协议的 JSON-RPC 方法目录、协议版本与自定义错误码。

`METHODS` 是核连接上的方法；`OPERATOR_METHODS` 是操作员许可连接上的方法；
`TEST_DEVICE_METHODS` 是只用于测试的测试设备连接上的方法。
"""

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
AUDIO_RING_ATTACH: Final = Request(
    "audio_ring.attach", Direction.MAIN_TO_CORE, models.AudioRingAttachParams, models.Empty
)
AUDIO_SET_TARGET: Final = Request(
    "audio.set_target", Direction.MAIN_TO_CORE, models.AudioSetTargetParams, models.Empty
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
    AUDIO_RING_ATTACH.name: AUDIO_RING_ATTACH,
    AUDIO_SET_TARGET.name: AUDIO_SET_TARGET,
    PRODUCER_HEARTBEAT.name: PRODUCER_HEARTBEAT,
    ACTION_SUBMIT.name: ACTION_SUBMIT,
    PRODUCER_REVOKE.name: PRODUCER_REVOKE,
    CORE_STOP.name: CORE_STOP,
    ACTION_RECEIPT.name: ACTION_RECEIPT,
    INPUT_RELEASED.name: INPUT_RELEASED,
    CORE_HEALTH.name: CORE_HEALTH,
}
"""核连接上的全部方法，按方法名索引。"""

OPERATOR_LEASE_GRANT: Final = Request(
    "operator_lease.grant", Direction.OPERATOR_TO_CORE, models.LeaseGrantParams, models.Empty
)
OPERATOR_LEASE_REVOKE: Final = Request(
    "operator_lease.revoke", Direction.OPERATOR_TO_CORE, models.LeaseRevokeParams, models.Empty
)
OPERATOR_INPUT_RELEASED: Final = Notification(
    "input.released", Direction.CORE_TO_OPERATOR, models.InputReleasedParams
)

OPERATOR_METHODS: Final[Mapping[str, Method]] = {
    OPERATOR_LEASE_GRANT.name: OPERATOR_LEASE_GRANT,
    OPERATOR_LEASE_REVOKE.name: OPERATOR_LEASE_REVOKE,
    OPERATOR_INPUT_RELEASED.name: OPERATOR_INPUT_RELEASED,
}
"""操作员许可连接上的全部方法，按方法名索引。"""

TEST_LEASE_GRANT: Final = Request(
    "test_lease.grant", Direction.TEST_TO_CORE, models.LeaseGrantParams, models.Empty
)
TEST_LEASE_REVOKE: Final = Request(
    "test_lease.revoke", Direction.TEST_TO_CORE, models.LeaseRevokeParams, models.Empty
)
FAKE_INPUT_EVENT: Final = Notification(
    "fake_input.event", Direction.CORE_TO_TEST, models.FakeInputEventParams
)

TEST_DEVICE_METHODS: Final[Mapping[str, Method]] = {
    TEST_LEASE_GRANT.name: TEST_LEASE_GRANT,
    TEST_LEASE_REVOKE.name: TEST_LEASE_REVOKE,
    FAKE_INPUT_EVENT.name: FAKE_INPUT_EVENT,
}
"""测试设备连接上的全部方法，按方法名索引。只用于测试。"""


class ErrorCode(IntEnum):
    """核协议自定义的 JSON-RPC 错误码；其它错误用 JSON-RPC 2.0 的标准错误码。"""

    PROTOCOL_VERSION_MISMATCH = -32001
    FRAME_RING_MISMATCH = -32002
    CAPTURE_UNSUPPORTED = -32003
    LEASE_REFUSED = -32004
    AUDIO_RING_MISMATCH = -32005
