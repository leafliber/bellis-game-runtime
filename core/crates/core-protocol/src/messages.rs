//! 核连接上各方法的 params 与 result 类型，字段含义见 `docs/modules/core-protocol.md` 的“消息集”；
//! 文件末尾是许可源授予与吊销输入许可的消息（同一文档的“操作员许可连接”与“测试设备连接”共用），以及
//! 只用于测试的测试设备连接上的其余消息。
//!
//! 约定：结构体一律拒绝未知字段；联合类型一律以 `type` 字段为内部标签，每个分支是一个拒绝未知
//! 字段的具名结构体；以 `_ns` 结尾的时刻字段是与帧环同一单调时钟的纳秒读数。

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

/// 不带字段的 params 或 result。
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Empty {}

// ---------------------------------------------------------------- 握手

/// `core.hello` 的 params：实时核通过通道认证后发出的第一条消息。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct CoreHelloParams {
    /// 实时核实现的核协议版本。
    pub protocol_version: u32,
    /// 实时核的程序版本，只用于日志与诊断。
    pub core_version: String,
}

/// `core.hello` 的 result：主进程接受握手。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct CoreHelloResult {
    /// 主进程实现的核协议版本。
    pub protocol_version: u32,
}

// ---------------------------------------------------------------- 帧环挂接

/// `frame_ring.attach` 的 params：主进程创建的帧环的共享内存名与布局参数。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct FrameRingAttachParams {
    /// 共享内存名。
    pub name: String,
    /// 共享内存的总字节数。
    pub size_bytes: u64,
    /// 帧环的布局版本。
    pub layout_version: u32,
    /// 槽数。
    pub slot_count: u32,
    /// 相邻两槽起点之间的字节数。
    pub slot_stride: u64,
    /// 帧的最大宽度（物理像素）。
    pub max_width: u32,
    /// 帧的最大高度（物理像素）。
    pub max_height: u32,
}

// ---------------------------------------------------------------- 采集目标

/// `capture.set_target` 的 params。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct CaptureSetTargetParams {
    /// 采集目标。
    pub target: CaptureTarget,
}

/// 采集目标：按规则识别的游戏窗口，或测试用的假采集源。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum CaptureTarget {
    Window(WindowTarget),
    Fake(FakeTarget),
}

/// 窗口识别规则：进程名加窗口类或标题，至少给出窗口类与标题之一。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "WindowTarget")]
pub struct WindowTarget {
    /// 进程的可执行文件名，不区分大小写。
    pub process_name: String,
    /// 窗口类名，全等匹配；`null` 表示不按窗口类匹配。
    pub window_class: Option<String>,
    /// 窗口标题，全等匹配；`null` 表示不按标题匹配。
    pub title: Option<String>,
}

/// 假采集源，只用于自动测试：从测试一侧创建并写入的假显示面取帧。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "FakeTarget")]
pub struct FakeTarget {
    /// 假显示面的共享内存名与布局参数。假显示面与帧环同一布局，参数的含义与核对同 `frame_ring.attach`。
    pub surface: FrameRingAttachParams,
}

// ---------------------------------------------------------------- 音频环挂接与音频目标

/// `audio_ring.attach` 的 params：主进程创建的音频环的共享内存名与布局参数。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct AudioRingAttachParams {
    /// 共享内存名。
    pub name: String,
    /// 共享内存的总字节数。
    pub size_bytes: u64,
    /// 音频环的布局版本。
    pub layout_version: u32,
    /// 块槽数。
    pub block_count: u32,
    /// 相邻两块槽起点之间的字节数。
    pub block_stride: u64,
    /// 一块最多的采样点数。
    pub block_capacity: u32,
    /// 采样率（Hz）。
    pub sample_rate: u32,
    /// 声道数。
    pub channels: u32,
    /// 样本格式。
    pub sample_format: u32,
}

/// `audio.set_target` 的 params。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct AudioSetTargetParams {
    /// 音频目标。
    pub target: AudioTarget,
}

/// 音频目标：按窗口识别规则认定的游戏进程，或测试用的假音频源。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum AudioTarget {
    Window(AudioWindowTarget),
    Fake(FakeAudioTarget),
}

/// 采这个窗口所属进程及其子进程的声音。识别规则与 `capture.set_target` 的 `window` 相同。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "AudioWindowTarget")]
pub struct AudioWindowTarget {
    /// 进程的可执行文件名，不区分大小写。
    pub process_name: String,
    /// 窗口类名，全等匹配；`null` 表示不按窗口类匹配。
    pub window_class: Option<String>,
    /// 窗口标题，全等匹配；`null` 表示不按标题匹配。
    pub title: Option<String>,
}

/// 假音频源，只用于自动测试：从测试一侧创建并写入的假音频输出取声音。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "FakeAudioTarget")]
pub struct FakeAudioTarget {
    /// 假音频输出的共享内存名与布局参数。假音频输出与音频环同一布局，参数的含义与核对同
    /// `audio_ring.attach`。
    pub output: AudioRingAttachParams,
}

// ---------------------------------------------------------------- 输入策略

/// 归一化矩形：客户区的相对坐标，各分量取值 [0, 1]，左上小于右下。
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "NormRect")]
pub struct NormRect {
    /// 左边界（含）。
    pub left: f64,
    /// 上边界（含）。
    pub top: f64,
    /// 右边界（不含）。
    pub right: f64,
    /// 下边界（不含）。
    pub bottom: f64,
}

/// `input.configure` 的 params：主进程把游戏适配包的动作白名单与静态禁区换算成的输入策略。
///
/// 实时核怎样执行见 `docs/modules/realtime-core.md` 的“输入策略”：提交时核对键与鼠标键，执行时
/// 核对落点与速率，按住超时即释放。每个实例只设定一次。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct InputPolicy {
    /// 可点击的鼠标键。
    pub click_buttons: Vec<MouseButton>,
    /// 可短按的扫描码（Set 1）。
    pub press_keys: Vec<u16>,
    /// 是否允许滚动。
    pub scroll: bool,
    /// 点击与滚动的落点须在其中之一内；允许点击或滚动时不能为空。
    pub click_regions: Vec<NormRect>,
    /// 静态禁区，落点不能在其中之一内；可为空。
    pub forbidden_zones: Vec<NormRect>,
    /// 点击与短按的按住时长，1–500 毫秒。
    pub press_duration_ms: u32,
    /// 每秒至多几个一次性动作，大于 0。
    pub max_rate: f64,
    /// 可按住的扫描码（Set 1）。
    pub hold_keys: Vec<u16>,
    /// 可按住的鼠标键。
    pub hold_buttons: Vec<MouseButton>,
    /// 最长按住时间（毫秒）；有可按住的项时必需，没有时为 `null`。
    pub max_hold_ms: Option<u32>,
}

// ---------------------------------------------------------------- 控制产出者

/// 控制产出者的身份与代次。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Producer {
    /// 控制产出者的身份。
    pub id: String,
    /// 代次，由主进程分配，在一个实时核的生命期内严格递增。
    pub generation: u64,
}

/// `producer.heartbeat` 的 params：当前控制产出者的心跳。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ProducerHeartbeatParams {
    /// 发出心跳的控制产出者。
    pub producer: Producer,
    /// 心跳序号，同一代次内严格递增。
    pub seq: u64,
}

/// `producer.revoke` 的 params：作废代次。实时核释放完成后才回应，回应即输入已释放的确认。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ProducerRevokeParams {
    /// 作废此代次及更早的全部代次。
    pub generation: u64,
}

// ---------------------------------------------------------------- 动作块

/// `action.submit` 的 params：主进程向实时核提交的唯一动作形式。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ActionBlock {
    /// 产出这块动作的控制产出者。
    pub producer: Producer,
    /// 动作块序号，同一代次内严格递增。
    pub seq: u64,
    /// 基准帧号：产出者做决定时依据的帧，坐标按这一帧的客户区解释。
    pub base_frame: u64,
    /// 起始时刻。
    pub start_ns: u64,
    /// 有效截止时刻：到时还没有新的动作块，实时核即释放全部输入。
    pub deadline_ns: u64,
    /// 按时间排列的控制步，至少一步。
    pub steps: Vec<ControlStep>,
}

/// 动作块中的一步：从这一步的时刻起生效的完整持续状态，以及在这一时刻执行的一次性动作。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ControlStep {
    /// 相对起始时刻的纳秒数，在块内不减。
    pub offset_ns: u64,
    /// 完整的持续状态：列出的保持按下或保持取值，没列出的一律释放。
    pub held: Vec<HeldInput>,
    /// 一次性动作，按列出的顺序执行。
    pub actions: Vec<Action>,
}

/// 持续状态的一项。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum HeldInput {
    Key(HeldKey),
    MouseButton(HeldMouseButton),
    Axis(HeldAxis),
}

/// 按住的键。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "HeldKey")]
pub struct HeldKey {
    /// 扫描码（Set 1）；扩展键在高字节带 `0xE0`。
    pub scan_code: u16,
}

/// 按住的鼠标键。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "HeldMouseButton")]
pub struct HeldMouseButton {
    /// 鼠标键。
    pub button: MouseButton,
}

/// 轴的取值。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "HeldAxis")]
pub struct HeldAxis {
    /// 轴名，由游戏适配包的白名单定义。
    pub axis: String,
    /// 取值，范围 [-1, 1]。
    pub value: f64,
}

/// 一次性动作。每个都带身份，供实时核去重：重传不重复执行。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Action {
    Click(Click),
    KeyPress(KeyPress),
    Scroll(Scroll),
    MouseMove(MouseMove),
}

/// 点击：移到客户区坐标处按下并抬起；按住时长由游戏适配包给出。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "Click")]
pub struct Click {
    /// 动作身份，在同一代次内唯一。
    pub id: u64,
    /// 客户区横坐标（物理像素）。
    pub x: u32,
    /// 客户区纵坐标（物理像素）。
    pub y: u32,
    /// 鼠标键。
    pub button: MouseButton,
}

/// 短按：按下并抬起一个键；按住时长由游戏适配包给出。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "KeyPress")]
pub struct KeyPress {
    /// 动作身份，在同一代次内唯一。
    pub id: u64,
    /// 扫描码（Set 1）；扩展键在高字节带 `0xE0`。
    pub scan_code: u16,
}

/// 滚动：移到客户区坐标处滚动滚轮。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "Scroll")]
pub struct Scroll {
    /// 动作身份，在同一代次内唯一。
    pub id: u64,
    /// 客户区横坐标（物理像素）。
    pub x: u32,
    /// 客户区纵坐标（物理像素）。
    pub y: u32,
    /// 滚轮格数，正数向前（远离用户）。
    pub notches: i32,
}

/// 鼠标相对位移。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "MouseMove")]
pub struct MouseMove {
    /// 动作身份，在同一代次内唯一。
    pub id: u64,
    /// 横向位移（鼠标计数），正数向右。
    pub dx: i32,
    /// 纵向位移（鼠标计数），正数向下。
    pub dy: i32,
}

/// 鼠标键。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum MouseButton {
    /// 左键。
    Left,
    /// 右键。
    Right,
    /// 中键。
    Middle,
}

/// `action.submit` 的 result：实时核是否接受这块动作。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ActionSubmitResult {
    Accepted(Accepted),
    Rejected(Rejected),
}

/// 动作块已接受，按时间执行。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "Accepted")]
pub struct Accepted {}

/// 动作块被拒绝，其中任何动作都不执行。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "Rejected")]
pub struct Rejected {
    /// 拒绝的原因。
    pub reason: RejectReason,
    /// 给人看的说明，只用于日志与诊断。
    pub message: String,
}

/// 动作块被拒绝的原因。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum RejectReason {
    /// 代次早于当前代次，或已被作废。
    StaleGeneration,
    /// 代次等于当前代次，身份却不同。
    ProducerMismatch,
    /// 序号不大于同一代次已接受的动作块。
    StaleSeq,
    /// 有效截止时刻已过。
    Expired,
    /// 内容不合规则，例如步的时刻倒序、同一项持续状态出现两次、动作身份在块内重复。
    Invalid,
    /// 持续状态或一次性动作不在输入策略的白名单内，或以真实后端运行时还没有输入策略。
    NotAllowed,
    /// 实时核正在停止，不再接受动作块。
    Stopping,
}

// ---------------------------------------------------------------- 回执与释放

/// `action.receipt` 的 params：一块动作中一次性动作的执行结果。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ActionReceiptParams {
    /// 动作块所属的控制产出者。
    pub producer: Producer,
    /// 动作块的序号。
    pub block_seq: u64,
    /// 各一次性动作的结果，每个动作在一块中只报一次。
    pub outcomes: Vec<ActionOutcome>,
}

/// 一个一次性动作的结果。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ActionOutcome {
    Executed(Executed),
    Dropped(Dropped),
    Uncertain(Uncertain),
}

/// 动作已交给输入后端；不代表游戏中的效果已发生。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "Executed")]
pub struct Executed {
    /// 动作身份。
    pub id: u64,
    /// 交给输入后端的时刻。
    pub at_ns: u64,
}

/// 动作没有执行。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "Dropped")]
pub struct Dropped {
    /// 动作身份。
    pub id: u64,
    /// 没有执行的原因。
    pub reason: DropReason,
}

/// 一次性动作没有执行的原因。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum DropReason {
    /// 到它的时刻之前，新的动作块已取代本块未执行的部分。
    Superseded,
    /// 同一身份的动作在本代次已执行过。
    Duplicate,
    /// 到它的时刻时实时核已释放输入，见 `input.released`。
    Released,
    /// 带坐标的动作：基准帧之后客户区的尺寸或 DPI 变了，或基准帧的几何已查不到。
    GeometryChanged,
    /// 落点不在输入策略的 `click_regions` 内，或在 `forbidden_zones` 内。
    NotAllowed,
    /// 超过输入策略的 `max_rate`。
    RateLimited,
}

/// 真实后端已注入，但注入过程中前台窗口变了，游戏是否收到不确定；实时核随即释放全部输入，不重试。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "Uncertain")]
pub struct Uncertain {
    /// 动作身份。
    pub id: u64,
    /// 交给输入后端的时刻。
    pub at_ns: u64,
}

/// `input.released` 的 params：实时核释放了全部输入。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct InputReleasedParams {
    /// 释放的原因。
    pub reason: ReleaseReason,
    /// 释放时的当前控制产出者；没有时为 `null`。
    pub producer: Option<Producer>,
    /// 释放完成的时刻。
    pub released_at_ns: u64,
}

/// 释放全部输入的原因。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum ReleaseReason {
    /// 实时核启动，接受动作之前先释放。
    CoreStart,
    /// 当前代次被 `producer.revoke` 作废。
    GenerationRevoked,
    /// 出现了更新的代次，更换控制产出者前先释放。
    ProducerChanged,
    /// 当前控制产出者的心跳超时。
    HeartbeatTimeout,
    /// 动作块到了有效截止时刻，没有新的动作块。
    DeadlineExpired,
    /// 帧环中最新的帧超过了动作允许的年龄，或本实例还没有写过帧。
    FrameStale,
    /// 输入许可过了到期时刻，没有续约。
    LeaseExpired,
    /// 输入许可被许可源吊销，或许可源换发了更新的令牌。
    LeaseRevoked,
    /// 与许可源的连接断开：操作员许可连接或测试设备连接。
    LeaseSourceLost,
    /// 没有输入许可：许可源从未授予，或没有许可源。
    NoLease,
    /// 真实后端：目标窗口不在前台、已最小化、输入桌面不是当前桌面，或注入过程中前台变了。
    FocusLost,
    /// 真实后端：许可有效时钩子看到真人输入或第三方注入；同时在本地撤权。
    Takeover,
    /// 真实后端：按下急停组合键；同时在本地撤权。
    EmergencyStop,
    /// 真实后端：SendInput 注入的事件少于给出的；同时在本地撤权。
    InjectionBlocked,
    /// 真实后端：本实例的注入没有经过自己的钩子，钩子已失效；同时在本地撤权，随后重新装钩子。
    HookLost,
    /// 持续状态里的一项按住超过输入策略的 `max_hold_ms`。
    MaxHold,
    /// 收到 `core.stop`。
    Stop,
    /// 生命线：核连接断开。主进程收不到，只出现在测试设备的事件与日志里。
    CoreConnectionLost,
    /// 生命线：stdin 读到 EOF 或读取失败。
    StdinClosed,
}

/// `input.residual` 的 params：只在真实后端上。释放全部输入之后、以及收到输入策略时，实时核以
/// `GetAsyncKeyState` 核验输入策略里的键与鼠标键，仍读作按住的项已补发抬起，以这条通知报出。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct InputResidualParams {
    /// 核验的缘由。
    pub cause: ResidualCause,
    /// `cause` 为 `release` 时那次释放的原因，否则为 `null`。
    pub reason: Option<ReleaseReason>,
    /// 读作按住、已补发抬起的项，至少一项；只有 `key` 与 `mouse_button`。
    pub items: Vec<HeldInput>,
    /// 读按键状态的时刻。
    pub checked_at_ns: u64,
}

/// `input.residual` 的核验缘由。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum ResidualCause {
    /// 释放全部输入之后的核验，`reason` 同那次 `input.released`。
    Release,
    /// 新实例收到输入策略时的核验，旧实例可能留下了按住的键。
    CoreStart,
}

// ---------------------------------------------------------------- 健康

/// `core.health` 的 params：实时核定期发出的健康状态，也是主进程判断实时核存活的依据。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct CoreHealthParams {
    /// 发出时实时核的时钟读数。
    pub now_ns: u64,
    /// 采集状态。
    pub capture: CaptureState,
    /// 当前是否持有输入。
    pub inputs_held: bool,
    /// 当前控制产出者；没有时为 `null`。
    pub producer: Option<Producer>,
    /// 本实例最近写进帧环的一帧；本实例还没写过帧时为 `null`。
    pub latest_frame: Option<LatestFrame>,
    /// 输入许可的状态。
    pub lease: LeaseStatus,
    /// 音频的采集状态。
    pub audio: CaptureState,
    /// 本实例最近写进音频环的一块；本实例还没写过块时为 `null`。
    pub latest_audio: Option<LatestAudio>,
}

/// 实时核最近写进音频环的一块。只用于诊断与测试。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct LatestAudio {
    /// 音频环中的块号。
    pub block_number: u64,
    /// 这一块第一个采样点的采集时刻，与帧环的采集时刻同一时钟。
    pub capture_ns: u64,
}

/// 实时核最近写进帧环的一帧，是判断帧龄的依据。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct LatestFrame {
    /// 帧环中的帧号。
    pub frame_number: u64,
    /// 采集时刻，与帧环的采集时刻同一时钟。
    pub capture_ns: u64,
}

/// 输入许可的状态。只报状态，不报令牌。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum LeaseStatus {
    /// 没有许可：许可源从未授予，或没有许可源。
    NotGranted,
    /// 有效：令牌等于当前值且未到期。
    Valid,
    /// 过了到期时刻，没有续约。
    Expired,
    /// 被吊销，或许可源换发了更新的令牌。
    Revoked,
    /// 与许可源的连接已断开。
    SourceLost,
}

/// 采集状态，画面与声音各报一个。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum CaptureState {
    /// 还没有挂接环（帧环或音频环）或设定目标。
    Idle,
    /// 没有采集源或音频源：找不到目标窗口、目标进程已退出或源已失效，正在定期重新识别；没有新的帧或声音。
    Searching,
    /// 有采集源或音频源，写入环中；画面在窗口最小化等可恢复的情形下暂时没有新帧。
    Capturing,
}

// ---------------------------------------------------------------- 输入许可
//
// 许可源直接交给实时核的许可，不经主进程：操作员许可连接上的 `operator_lease.*` 与只用于测试的测试设备
// 连接上的 `test_lease.*` 用同样的 params。这些消息不是游戏桥控制连接的契约（那份契约的源在 Bellis）。

/// `operator_lease.grant` 与 `test_lease.grant` 的 params：许可源授予或续约输入许可。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct LeaseGrantParams {
    /// 许可令牌，只增不减：等于当前令牌即续约，大于当前令牌即换发新的许可。
    pub token: u64,
    /// 到期时刻，与帧环同一时钟。
    pub expires_ns: u64,
}

/// `operator_lease.revoke` 与 `test_lease.revoke` 的 params：许可源吊销输入许可。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct LeaseRevokeParams {
    /// 吊销这个令牌及更早的全部令牌。
    pub token: u64,
}

// ---------------------------------------------------------------- 测试设备连接（只用于测试）
//
// 实时核以假输入后端（`--input-backend fake`）启动时，另作为通道客户端连到测试一方；以下是这条连接上的
// 消息。测试一方是假输入设备，没有操作员许可连接时也是测试许可源（`test_lease.*`，params 见上）。生产中
// 没有这条连接，这些消息也不是游戏桥控制连接的契约。

/// `fake_input.event` 的 params：假输入后端交给假输入设备的一个事件。只用于测试。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct FakeInputEventParams {
    /// 事件序号，在实时核的一个实例内从 1 起逐个加一。
    pub seq: u64,
    /// 交给假输入后端的时刻。
    pub at_ns: u64,
    /// 事件。
    pub event: FakeInputEvent,
}

/// 假输入后端发出的事件：按下、抬起、轴、一次性动作与释放全部。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum FakeInputEvent {
    KeyDown(FakeKeyDown),
    KeyUp(FakeKeyUp),
    MouseDown(FakeMouseDown),
    MouseUp(FakeMouseUp),
    AxisSet(FakeAxisSet),
    AxisRelease(FakeAxisRelease),
    Click(FakeClick),
    KeyPress(FakeKeyPress),
    Scroll(FakeScroll),
    MouseMove(FakeMouseMove),
    ReleaseAll(FakeReleaseAll),
}

/// 按下一个键并保持。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "FakeKeyDown")]
pub struct FakeKeyDown {
    /// 扫描码（Set 1）。
    pub scan_code: u16,
}

/// 抬起一个按住的键。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "FakeKeyUp")]
pub struct FakeKeyUp {
    /// 扫描码（Set 1）。
    pub scan_code: u16,
}

/// 按下一个鼠标键并保持。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "FakeMouseDown")]
pub struct FakeMouseDown {
    /// 鼠标键。
    pub button: MouseButton,
}

/// 抬起一个按住的鼠标键。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "FakeMouseUp")]
pub struct FakeMouseUp {
    /// 鼠标键。
    pub button: MouseButton,
}

/// 把一个轴设为某个取值并保持。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "FakeAxisSet")]
pub struct FakeAxisSet {
    /// 轴名。
    pub axis: String,
    /// 取值，范围 [-1, 1]。
    pub value: f64,
}

/// 释放一个轴，回到中立。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "FakeAxisRelease")]
pub struct FakeAxisRelease {
    /// 轴名。
    pub axis: String,
}

/// 点击，即动作块的 `click`。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "FakeClick")]
pub struct FakeClick {
    /// 动作身份。
    pub id: u64,
    /// 基准帧客户区的横坐标（物理像素）。
    pub x: u32,
    /// 基准帧客户区的纵坐标（物理像素）。
    pub y: u32,
    /// 鼠标键。
    pub button: MouseButton,
}

/// 短按，即动作块的 `key_press`。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "FakeKeyPress")]
pub struct FakeKeyPress {
    /// 动作身份。
    pub id: u64,
    /// 扫描码（Set 1）。
    pub scan_code: u16,
}

/// 滚动，即动作块的 `scroll`。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "FakeScroll")]
pub struct FakeScroll {
    /// 动作身份。
    pub id: u64,
    /// 基准帧客户区的横坐标（物理像素）。
    pub x: u32,
    /// 基准帧客户区的纵坐标（物理像素）。
    pub y: u32,
    /// 滚轮格数，正数向前。
    pub notches: i32,
}

/// 鼠标相对位移，即动作块的 `mouse_move`。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "FakeMouseMove")]
pub struct FakeMouseMove {
    /// 动作身份。
    pub id: u64,
    /// 横向位移（鼠标计数）。
    pub dx: i32,
    /// 纵向位移（鼠标计数）。
    pub dy: i32,
}

/// 释放全部输入：此前按住的每一项都已逐一发出抬起或释放，这一条随后发出。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
#[schemars(title = "FakeReleaseAll")]
pub struct FakeReleaseAll {
    /// 释放的原因。
    pub reason: ReleaseReason,
}
