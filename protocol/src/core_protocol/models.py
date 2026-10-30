# 由 `uv run generate` 从 core/crates/core-protocol 生成，不要手改。

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel


class CoreHelloParams(BaseModel):
    """
    `core.hello` 的 params：实时核通过通道认证后发出的第一条消息。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    protocol_version: Annotated[int, Field(ge=0, le=4294967295)]
    """
    实时核实现的核协议版本。
    """
    core_version: str
    """
    实时核的程序版本，只用于日志与诊断。
    """


class CoreHelloResult(BaseModel):
    """
    `core.hello` 的 result：主进程接受握手。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    protocol_version: Annotated[int, Field(ge=0, le=4294967295)]
    """
    主进程实现的核协议版本。
    """


class FrameRingAttachParams(BaseModel):
    """
    `frame_ring.attach` 的 params：主进程创建的帧环的共享内存名与布局参数。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    name: str
    """
    共享内存名。
    """
    size_bytes: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    共享内存的总字节数。
    """
    layout_version: Annotated[int, Field(ge=0, le=4294967295)]
    """
    帧环的布局版本。
    """
    slot_count: Annotated[int, Field(ge=0, le=4294967295)]
    """
    槽数。
    """
    slot_stride: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    相邻两槽起点之间的字节数。
    """
    max_width: Annotated[int, Field(ge=0, le=4294967295)]
    """
    帧的最大宽度（物理像素）。
    """
    max_height: Annotated[int, Field(ge=0, le=4294967295)]
    """
    帧的最大高度（物理像素）。
    """


class Empty(BaseModel):
    """
    不带字段的 params 或 result。
    """

    model_config = ConfigDict(
        extra="forbid",
    )


class WindowTarget(BaseModel):
    """
    窗口识别规则：进程名加窗口类或标题，至少给出窗口类与标题之一。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: Literal["window"] = "window"
    process_name: str
    """
    进程的可执行文件名，不区分大小写。
    """
    window_class: str | None = None
    """
    窗口类名，全等匹配；`null` 表示不按窗口类匹配。
    """
    title: str | None = None
    """
    窗口标题，全等匹配；`null` 表示不按标题匹配。
    """


class FakeTarget(BaseModel):
    """
    假采集源，只用于自动测试：从测试一侧创建并写入的假显示面取帧。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: Literal["fake"] = "fake"
    surface: FrameRingAttachParams
    """
    假显示面的共享内存名与布局参数。假显示面与帧环同一布局，参数的含义与核对同 `frame_ring.attach`。
    """


class CaptureTarget(RootModel[WindowTarget | FakeTarget]):
    root: Annotated[WindowTarget | FakeTarget, Field(discriminator="type")]
    """
    采集目标：按规则识别的游戏窗口，或测试用的假采集源。
    """


class Producer(BaseModel):
    """
    控制产出者的身份与代次。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    id: str
    """
    控制产出者的身份。
    """
    generation: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    代次，由主进程分配，在一个实时核的生命期内严格递增。
    """


class HeldKey(BaseModel):
    """
    按住的键。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: Literal["key"] = "key"
    scan_code: Annotated[int, Field(ge=0, le=65535)]
    """
    扫描码（Set 1）；扩展键在高字节带 `0xE0`。
    """


class HeldAxis(BaseModel):
    """
    轴的取值。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: Literal["axis"] = "axis"
    axis: str
    """
    轴名，由游戏适配包的白名单定义。
    """
    value: float
    """
    取值，范围 [-1, 1]。
    """


class MouseButton(StrEnum):
    """
    鼠标键。
    """

    LEFT = "left"
    """
    左键。
    """
    RIGHT = "right"
    """
    右键。
    """
    MIDDLE = "middle"
    """
    中键。
    """


class Click(BaseModel):
    """
    点击：移到客户区坐标处按下并抬起；按住时长由游戏适配包给出。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: Literal["click"] = "click"
    id: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    动作身份，在同一代次内唯一。
    """
    x: Annotated[int, Field(ge=0, le=4294967295)]
    """
    客户区横坐标（物理像素）。
    """
    y: Annotated[int, Field(ge=0, le=4294967295)]
    """
    客户区纵坐标（物理像素）。
    """
    button: MouseButton
    """
    鼠标键。
    """


class KeyPress(BaseModel):
    """
    短按：按下并抬起一个键；按住时长由游戏适配包给出。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: Literal["key_press"] = "key_press"
    id: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    动作身份，在同一代次内唯一。
    """
    scan_code: Annotated[int, Field(ge=0, le=65535)]
    """
    扫描码（Set 1）；扩展键在高字节带 `0xE0`。
    """


class Scroll(BaseModel):
    """
    滚动：移到客户区坐标处滚动滚轮。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: Literal["scroll"] = "scroll"
    id: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    动作身份，在同一代次内唯一。
    """
    x: Annotated[int, Field(ge=0, le=4294967295)]
    """
    客户区横坐标（物理像素）。
    """
    y: Annotated[int, Field(ge=0, le=4294967295)]
    """
    客户区纵坐标（物理像素）。
    """
    notches: Annotated[int, Field(ge=-2147483648, le=2147483647)]
    """
    滚轮格数，正数向前（远离用户）。
    """


class MouseMove(BaseModel):
    """
    鼠标相对位移。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: Literal["mouse_move"] = "mouse_move"
    id: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    动作身份，在同一代次内唯一。
    """
    dx: Annotated[int, Field(ge=-2147483648, le=2147483647)]
    """
    横向位移（鼠标计数），正数向右。
    """
    dy: Annotated[int, Field(ge=-2147483648, le=2147483647)]
    """
    纵向位移（鼠标计数），正数向下。
    """


class Action(RootModel[Click | KeyPress | Scroll | MouseMove]):
    root: Annotated[Click | KeyPress | Scroll | MouseMove, Field(discriminator="type")]
    """
    一次性动作。每个都带身份，供实时核去重：重传不重复执行。
    """


class Accepted(BaseModel):
    """
    动作块已接受，按时间执行。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: Literal["accepted"] = "accepted"


class RejectReason(StrEnum):
    """
    动作块被拒绝的原因。
    """

    STALE_GENERATION = "stale_generation"
    """
    代次早于当前代次，或已被作废。
    """
    PRODUCER_MISMATCH = "producer_mismatch"
    """
    代次等于当前代次，身份却不同。
    """
    STALE_SEQ = "stale_seq"
    """
    序号不大于同一代次已接受的动作块。
    """
    EXPIRED = "expired"
    """
    有效截止时刻已过。
    """
    INVALID = "invalid"
    """
    内容不合规则，例如步的时刻倒序、同一项持续状态出现两次、动作身份在块内重复。
    """


class ProducerRevokeParams(BaseModel):
    """
    `producer.revoke` 的 params：作废代次。实时核释放完成后才回应，回应即输入已释放的确认。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    generation: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    作废此代次及更早的全部代次。
    """


class Executed(BaseModel):
    """
    动作已交给输入后端；不代表游戏中的效果已发生。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: Literal["executed"] = "executed"
    id: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    动作身份。
    """
    at_ns: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    交给输入后端的时刻。
    """


class DropReason(StrEnum):
    """
    一次性动作没有执行的原因。
    """

    SUPERSEDED = "superseded"
    """
    到它的时刻之前，新的动作块已取代本块未执行的部分。
    """
    DUPLICATE = "duplicate"
    """
    同一身份的动作在本代次已执行过。
    """
    RELEASED = "released"
    """
    到它的时刻时实时核已释放输入，见 `input.released`。
    """


class ReleaseReason(StrEnum):
    """
    释放全部输入的原因。
    """

    CORE_START = "core_start"
    """
    实时核启动，接受动作之前先释放。
    """
    GENERATION_REVOKED = "generation_revoked"
    """
    当前代次被 `producer.revoke` 作废。
    """
    PRODUCER_CHANGED = "producer_changed"
    """
    出现了更新的代次，更换控制产出者前先释放。
    """
    HEARTBEAT_TIMEOUT = "heartbeat_timeout"
    """
    当前控制产出者的心跳超时。
    """
    DEADLINE_EXPIRED = "deadline_expired"
    """
    动作块到了有效截止时刻，没有新的动作块。
    """
    FRAME_STALE = "frame_stale"
    """
    帧环中最新的帧超过了动作允许的年龄。
    """
    STOP = "stop"
    """
    收到 `core.stop`。
    """


class CaptureState(StrEnum):
    """
    采集状态。
    """

    IDLE = "idle"
    """
    还没有挂接帧环或设定采集目标。
    """
    SEARCHING = "searching"
    """
    正在寻找目标窗口，没有帧。
    """
    CAPTURING = "capturing"
    """
    正在采集，帧写入帧环。
    """


class CaptureSetTargetParams(BaseModel):
    """
    `capture.set_target` 的 params。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    target: CaptureTarget
    """
    采集目标。
    """


class ProducerHeartbeatParams(BaseModel):
    """
    `producer.heartbeat` 的 params：当前控制产出者的心跳。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    producer: Producer
    """
    发出心跳的控制产出者。
    """
    seq: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    心跳序号，同一代次内严格递增。
    """


class HeldMouseButton(BaseModel):
    """
    按住的鼠标键。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: Literal["mouse_button"] = "mouse_button"
    button: MouseButton
    """
    鼠标键。
    """


class HeldInput(RootModel[HeldKey | HeldMouseButton | HeldAxis]):
    root: Annotated[HeldKey | HeldMouseButton | HeldAxis, Field(discriminator="type")]
    """
    持续状态的一项。
    """


class Rejected(BaseModel):
    """
    动作块被拒绝，其中任何动作都不执行。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: Literal["rejected"] = "rejected"
    reason: RejectReason
    """
    拒绝的原因。
    """
    message: str
    """
    给人看的说明，只用于日志与诊断。
    """


class ActionSubmitResult(RootModel[Accepted | Rejected]):
    root: Annotated[Accepted | Rejected, Field(discriminator="type")]
    """
    `action.submit` 的 result：实时核是否接受这块动作。
    """


class Dropped(BaseModel):
    """
    动作没有执行。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: Literal["dropped"] = "dropped"
    id: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    动作身份。
    """
    reason: DropReason
    """
    没有执行的原因。
    """


class ActionOutcome(RootModel[Executed | Dropped]):
    root: Annotated[Executed | Dropped, Field(discriminator="type")]
    """
    一个一次性动作的结果。
    """


class InputReleasedParams(BaseModel):
    """
    `input.released` 的 params：实时核释放了全部输入。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    reason: ReleaseReason
    """
    释放的原因。
    """
    producer: Producer | None = None
    """
    释放时的当前控制产出者；没有时为 `null`。
    """
    released_at_ns: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    释放完成的时刻。
    """


class CoreHealthParams(BaseModel):
    """
    `core.health` 的 params：实时核定期发出的健康状态，也是主进程判断实时核存活的依据。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    now_ns: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    发出时实时核的时钟读数。
    """
    capture: CaptureState
    """
    采集状态。
    """
    inputs_held: bool
    """
    当前是否持有输入。
    """
    producer: Producer | None = None
    """
    当前控制产出者；没有时为 `null`。
    """


class ControlStep(BaseModel):
    """
    动作块中的一步：从这一步的时刻起生效的完整持续状态，以及在这一时刻执行的一次性动作。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    offset_ns: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    相对起始时刻的纳秒数，在块内不减。
    """
    held: list[HeldInput]
    """
    完整的持续状态：列出的保持按下或保持取值，没列出的一律释放。
    """
    actions: list[Action]
    """
    一次性动作，按列出的顺序执行。
    """


class ActionReceiptParams(BaseModel):
    """
    `action.receipt` 的 params：一块动作中一次性动作的执行结果。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    producer: Producer
    """
    动作块所属的控制产出者。
    """
    block_seq: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    动作块的序号。
    """
    outcomes: list[ActionOutcome]
    """
    各一次性动作的结果，每个动作在一块中只报一次。
    """


class ActionBlock(BaseModel):
    """
    `action.submit` 的 params：主进程向实时核提交的唯一动作形式。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    producer: Producer
    """
    产出这块动作的控制产出者。
    """
    seq: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    动作块序号，同一代次内严格递增。
    """
    base_frame: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    基准帧号：产出者做决定时依据的帧，坐标按这一帧的客户区解释。
    """
    start_ns: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    起始时刻。
    """
    deadline_ns: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    有效截止时刻：到时还没有新的动作块，实时核即释放全部输入。
    """
    steps: list[ControlStep]
    """
    按时间排列的控制步，至少一步。
    """
