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


class AudioRingAttachParams(BaseModel):
    """
    `audio_ring.attach` 的 params：主进程创建的音频环的共享内存名与布局参数。
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
    音频环的布局版本。
    """
    block_count: Annotated[int, Field(ge=0, le=4294967295)]
    """
    块槽数。
    """
    block_stride: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    相邻两块槽起点之间的字节数。
    """
    block_capacity: Annotated[int, Field(ge=0, le=4294967295)]
    """
    一块最多的采样点数。
    """
    sample_rate: Annotated[int, Field(ge=0, le=4294967295)]
    """
    采样率（Hz）。
    """
    channels: Annotated[int, Field(ge=0, le=4294967295)]
    """
    声道数。
    """
    sample_format: Annotated[int, Field(ge=0, le=4294967295)]
    """
    样本格式。
    """


class AudioWindowTarget(BaseModel):
    """
    采这个窗口所属进程及其子进程的声音。识别规则与 `capture.set_target` 的 `window` 相同。
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


class FakeAudioTarget(BaseModel):
    """
    假音频源，只用于自动测试：从测试一侧创建并写入的假音频输出取声音。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: Literal["fake"] = "fake"
    output: AudioRingAttachParams
    """
    假音频输出的共享内存名与布局参数。假音频输出与音频环同一布局，参数的含义与核对同
    `audio_ring.attach`。
    """


class AudioTarget(RootModel[AudioWindowTarget | FakeAudioTarget]):
    root: Annotated[AudioWindowTarget | FakeAudioTarget, Field(discriminator="type")]
    """
    音频目标：按窗口识别规则认定的游戏进程，或测试用的假音频源。
    """


class PressKey(RootModel[int]):
    root: Annotated[int, Field(ge=0, le=65535)]


class HoldKey(RootModel[int]):
    root: Annotated[int, Field(ge=0, le=65535)]


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


class NormRect(BaseModel):
    """
    归一化矩形：客户区的相对坐标，各分量取值 [0, 1]，左上小于右下。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    left: float
    """
    左边界（含）。
    """
    top: float
    """
    上边界（含）。
    """
    right: float
    """
    右边界（不含）。
    """
    bottom: float
    """
    下边界（不含）。
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


class HeldInput(RootModel[HeldKey | HeldMouseButton | HeldAxis]):
    root: Annotated[HeldKey | HeldMouseButton | HeldAxis, Field(discriminator="type")]
    """
    持续状态的一项。
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
    NOT_ALLOWED = "not_allowed"
    """
    持续状态或一次性动作不在输入策略的白名单内，或以真实后端运行时还没有输入策略。
    """
    STOPPING = "stopping"
    """
    实时核正在停止，不再接受动作块。
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


class Uncertain(BaseModel):
    """
    真实后端已注入，但注入过程中前台窗口变了，游戏是否收到不确定；实时核随即释放全部输入，不重试。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: Literal["uncertain"] = "uncertain"
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
    GEOMETRY_CHANGED = "geometry_changed"
    """
    带坐标的动作：基准帧之后客户区的尺寸或 DPI 变了，或基准帧的几何已查不到。
    """
    NOT_ALLOWED = "not_allowed"
    """
    落点不在输入策略的 `click_regions` 内，或在 `forbidden_zones` 内。
    """
    RATE_LIMITED = "rate_limited"
    """
    超过输入策略的 `max_rate`。
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
    帧环中最新的帧超过了动作允许的年龄，或本实例还没有写过帧。
    """
    LEASE_EXPIRED = "lease_expired"
    """
    输入许可过了到期时刻，没有续约。
    """
    LEASE_REVOKED = "lease_revoked"
    """
    输入许可被许可源吊销，或许可源换发了更新的令牌。
    """
    LEASE_SOURCE_LOST = "lease_source_lost"
    """
    与许可源的连接断开：操作员许可连接或测试设备连接。
    """
    NO_LEASE = "no_lease"
    """
    没有输入许可：许可源从未授予，或没有许可源。
    """
    FOCUS_LOST = "focus_lost"
    """
    真实后端：目标窗口不在前台、已最小化、输入桌面不是当前桌面，或注入过程中前台变了。
    """
    TAKEOVER = "takeover"
    """
    真实后端：许可有效时钩子看到真人输入或第三方注入；同时在本地撤权。
    """
    EMERGENCY_STOP = "emergency_stop"
    """
    真实后端：按下急停组合键；同时在本地撤权。
    """
    INJECTION_BLOCKED = "injection_blocked"
    """
    真实后端：SendInput 注入的事件少于给出的；同时在本地撤权。
    """
    HOOK_LOST = "hook_lost"
    """
    真实后端：本实例的注入没有经过自己的钩子，钩子已失效；同时在本地撤权，随后重新装钩子。
    """
    MAX_HOLD = "max_hold"
    """
    持续状态里的一项按住超过输入策略的 `max_hold_ms`。
    """
    STOP = "stop"
    """
    收到 `core.stop`。
    """
    CORE_CONNECTION_LOST = "core_connection_lost"
    """
    生命线：核连接断开。主进程收不到，只出现在测试设备的事件与日志里。
    """
    STDIN_CLOSED = "stdin_closed"
    """
    生命线：stdin 读到 EOF 或读取失败。
    """


class ResidualCause(StrEnum):
    """
    `input.residual` 的核验缘由。
    """

    RELEASE = "release"
    """
    释放全部输入之后的核验，`reason` 同那次 `input.released`。
    """
    CORE_START = "core_start"
    """
    新实例收到输入策略时的核验，旧实例可能留下了按住的键。
    """


class CaptureState(StrEnum):
    """
    采集状态，画面与声音各报一个。
    """

    IDLE = "idle"
    """
    还没有挂接环（帧环或音频环）或设定目标。
    """
    SEARCHING = "searching"
    """
    没有采集源或音频源：找不到目标窗口、目标进程已退出或源已失效，正在定期重新识别；没有新的帧或声音。
    """
    CAPTURING = "capturing"
    """
    有采集源或音频源，写入环中；画面在窗口最小化等可恢复的情形下暂时没有新帧。
    """


class LatestFrame(BaseModel):
    """
    实时核最近写进帧环的一帧，是判断帧龄的依据。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    frame_number: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    帧环中的帧号。
    """
    capture_ns: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    采集时刻，与帧环的采集时刻同一时钟。
    """


class LeaseStatus(StrEnum):
    """
    输入许可的状态。只报状态，不报令牌。
    """

    NOT_GRANTED = "not_granted"
    """
    没有许可：许可源从未授予，或没有许可源。
    """
    VALID = "valid"
    """
    有效：令牌等于当前值且未到期。
    """
    EXPIRED = "expired"
    """
    过了到期时刻，没有续约。
    """
    REVOKED = "revoked"
    """
    被吊销，或许可源换发了更新的令牌。
    """
    SOURCE_LOST = "source_lost"
    """
    与许可源的连接已断开。
    """


class LatestAudio(BaseModel):
    """
    实时核最近写进音频环的一块。只用于诊断与测试。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    block_number: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    音频环中的块号。
    """
    capture_ns: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    这一块第一个采样点的采集时刻，与帧环的采集时刻同一时钟。
    """


class LeaseGrantParams(BaseModel):
    """
    `operator_lease.grant` 与 `test_lease.grant` 的 params：许可源授予或续约输入许可。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    token: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    许可令牌，只增不减：等于当前令牌即续约，大于当前令牌即换发新的许可。
    """
    expires_ns: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    到期时刻，与帧环同一时钟。
    """


class LeaseRevokeParams(BaseModel):
    """
    `operator_lease.revoke` 与 `test_lease.revoke` 的 params：许可源吊销输入许可。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    token: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    吊销这个令牌及更早的全部令牌。
    """


class FakeKeyDown(BaseModel):
    """
    按下一个键并保持。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: Literal["key_down"] = "key_down"
    scan_code: Annotated[int, Field(ge=0, le=65535)]
    """
    扫描码（Set 1）。
    """


class FakeKeyUp(BaseModel):
    """
    抬起一个按住的键。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: Literal["key_up"] = "key_up"
    scan_code: Annotated[int, Field(ge=0, le=65535)]
    """
    扫描码（Set 1）。
    """


class FakeMouseDown(BaseModel):
    """
    按下一个鼠标键并保持。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: Literal["mouse_down"] = "mouse_down"
    button: MouseButton
    """
    鼠标键。
    """


class FakeMouseUp(BaseModel):
    """
    抬起一个按住的鼠标键。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: Literal["mouse_up"] = "mouse_up"
    button: MouseButton
    """
    鼠标键。
    """


class FakeAxisSet(BaseModel):
    """
    把一个轴设为某个取值并保持。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: Literal["axis_set"] = "axis_set"
    axis: str
    """
    轴名。
    """
    value: float
    """
    取值，范围 [-1, 1]。
    """


class FakeAxisRelease(BaseModel):
    """
    释放一个轴，回到中立。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: Literal["axis_release"] = "axis_release"
    axis: str
    """
    轴名。
    """


class FakeClick(BaseModel):
    """
    点击，即动作块的 `click`。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: Literal["click"] = "click"
    id: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    动作身份。
    """
    x: Annotated[int, Field(ge=0, le=4294967295)]
    """
    基准帧客户区的横坐标（物理像素）。
    """
    y: Annotated[int, Field(ge=0, le=4294967295)]
    """
    基准帧客户区的纵坐标（物理像素）。
    """
    button: MouseButton
    """
    鼠标键。
    """


class FakeKeyPress(BaseModel):
    """
    短按，即动作块的 `key_press`。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: Literal["key_press"] = "key_press"
    id: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    动作身份。
    """
    scan_code: Annotated[int, Field(ge=0, le=65535)]
    """
    扫描码（Set 1）。
    """


class FakeScroll(BaseModel):
    """
    滚动，即动作块的 `scroll`。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: Literal["scroll"] = "scroll"
    id: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    动作身份。
    """
    x: Annotated[int, Field(ge=0, le=4294967295)]
    """
    基准帧客户区的横坐标（物理像素）。
    """
    y: Annotated[int, Field(ge=0, le=4294967295)]
    """
    基准帧客户区的纵坐标（物理像素）。
    """
    notches: Annotated[int, Field(ge=-2147483648, le=2147483647)]
    """
    滚轮格数，正数向前。
    """


class FakeMouseMove(BaseModel):
    """
    鼠标相对位移，即动作块的 `mouse_move`。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: Literal["mouse_move"] = "mouse_move"
    id: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    动作身份。
    """
    dx: Annotated[int, Field(ge=-2147483648, le=2147483647)]
    """
    横向位移（鼠标计数）。
    """
    dy: Annotated[int, Field(ge=-2147483648, le=2147483647)]
    """
    纵向位移（鼠标计数）。
    """


class FakeReleaseAll(BaseModel):
    """
    释放全部输入：此前按住的每一项都已逐一发出抬起或释放，这一条随后发出。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    type: Literal["release_all"] = "release_all"
    reason: ReleaseReason
    """
    释放的原因。
    """


class FakeInputEvent(
    RootModel[
        FakeKeyDown
        | FakeKeyUp
        | FakeMouseDown
        | FakeMouseUp
        | FakeAxisSet
        | FakeAxisRelease
        | FakeClick
        | FakeKeyPress
        | FakeScroll
        | FakeMouseMove
        | FakeReleaseAll
    ]
):
    root: Annotated[
        FakeKeyDown
        | FakeKeyUp
        | FakeMouseDown
        | FakeMouseUp
        | FakeAxisSet
        | FakeAxisRelease
        | FakeClick
        | FakeKeyPress
        | FakeScroll
        | FakeMouseMove
        | FakeReleaseAll,
        Field(discriminator="type"),
    ]
    """
    假输入后端发出的事件：按下、抬起、轴、一次性动作与释放全部。
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


class AudioSetTargetParams(BaseModel):
    """
    `audio.set_target` 的 params。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    target: AudioTarget
    """
    音频目标。
    """


class InputPolicy(BaseModel):
    """
    `input.configure` 的 params：主进程把游戏适配包的动作白名单与静态禁区换算成的输入策略。

    实时核怎样执行见 `docs/modules/realtime-core.md` 的“输入策略”：提交时核对键与鼠标键，执行时
    核对落点与速率，按住超时即释放。每个实例只设定一次。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    click_buttons: list[MouseButton]
    """
    可点击的鼠标键。
    """
    press_keys: list[PressKey]
    """
    可短按的扫描码（Set 1）。
    """
    scroll: bool
    """
    是否允许滚动。
    """
    click_regions: list[NormRect]
    """
    点击与滚动的落点须在其中之一内；允许点击或滚动时不能为空。
    """
    forbidden_zones: list[NormRect]
    """
    静态禁区，落点不能在其中之一内；可为空。
    """
    press_duration_ms: Annotated[int, Field(ge=0, le=4294967295)]
    """
    点击与短按的按住时长，1–500 毫秒。
    """
    max_rate: float
    """
    每秒至多几个一次性动作，大于 0。
    """
    hold_keys: list[HoldKey]
    """
    可按住的扫描码（Set 1）。
    """
    hold_buttons: list[MouseButton]
    """
    可按住的鼠标键。
    """
    max_hold_ms: Annotated[int | None, Field(ge=0, le=4294967295)] = None
    """
    最长按住时间（毫秒）；有可按住的项时必需，没有时为 `null`。
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


class ActionOutcome(RootModel[Executed | Dropped | Uncertain]):
    root: Annotated[Executed | Dropped | Uncertain, Field(discriminator="type")]
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


class InputResidualParams(BaseModel):
    """
    `input.residual` 的 params：只在真实后端上。释放全部输入之后、以及收到输入策略时，实时核以
    `GetAsyncKeyState` 核验输入策略里的键与鼠标键，仍读作按住的项已补发抬起，以这条通知报出。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    cause: ResidualCause
    """
    核验的缘由。
    """
    reason: ReleaseReason | None = None
    """
    `cause` 为 `release` 时那次释放的原因，否则为 `null`。
    """
    items: list[HeldInput]
    """
    读作按住、已补发抬起的项，至少一项；只有 `key` 与 `mouse_button`。
    """
    checked_at_ns: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    读按键状态的时刻。
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
    latest_frame: LatestFrame | None = None
    """
    本实例最近写进帧环的一帧；本实例还没写过帧时为 `null`。
    """
    lease: LeaseStatus
    """
    输入许可的状态。
    """
    audio: CaptureState
    """
    音频的采集状态。
    """
    latest_audio: LatestAudio | None = None
    """
    本实例最近写进音频环的一块；本实例还没写过块时为 `null`。
    """


class FakeInputEventParams(BaseModel):
    """
    `fake_input.event` 的 params：假输入后端交给假输入设备的一个事件。只用于测试。
    """

    model_config = ConfigDict(
        extra="forbid",
    )
    seq: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    事件序号，在实时核的一个实例内从 1 起逐个加一。
    """
    at_ns: Annotated[int, Field(ge=0, le=18446744073709551615)]
    """
    交给假输入后端的时刻。
    """
    event: FakeInputEvent
    """
    事件。
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
