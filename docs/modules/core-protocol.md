# 核协议与本机通道

主进程与实时核之间的消息、动作块和共享内存的布局，以及本仓库全部本机通道的实现。G0 实现。连接的划分与各自承载的内容见[总体架构](../architecture.md#进程与连接)；与 Bellis 之间的游戏桥消息见[游戏桥](game-bridge.md#契约)。

## 本机通道

本仓库按 Bellis R-05 的规范自写 `bellis-channel/1`（GR-28），不依赖 Bellis 的 `bellis_sidecar` 包：

| 实现 | 用于 |
| --- | --- |
| Python 客户端 | 主进程 → Bellis 宿主（语义连接） |
| Python 服务端 | 主进程 ← 实时核（核连接） |
| Rust 客户端 | 实时核 → Bellis 宿主（控制连接）、实时核 → 主进程（核连接） |

- Windows 上用命名管道，其它平台用 UDS，同一抽象（Bellis D-12）。
- 双向 HMAC 认证先于任何数据；有帧长上限与超时；篡改、重放、超长帧与超时都拒绝。
- 正确性分三层：握手与帧格式的已知答案测试；本仓库 Python 与 Rust 的互通测试，Windows 真机实际跑命名管道，Linux 容器跑 UDS；G3 联调前与 Bellis 宿主真机互通。

## 核协议

核连接上的消息（GR-03）：

- 以 Rust 类型为源（`core/crates/core-protocol`，serde，拒绝未知字段），schemars 生成 JSON Schema 2020-12，再由 datamodel-code-generator 生成 Pydantic 模型，见[生成与核对](#生成与核对)。
- 消息走 `bellis-channel/1` 帧，内容为 JSON-RPC 2.0（同 Bellis D-21）。实时核是通道客户端，主进程是服务端；两边都可以发请求与通知。
- 握手校验协议版本；两端同仓库同发布，仍然校验。

核连接承载：动作块、控制产出者心跳与代次、执行回执、输入已释放的确认、实时核健康，以及帧环挂接、采集目标与正常停止。

### 约定

- 方法名为“对象.动作”。有 result 的方法是请求，没有的是通知。params 一律是对象，不带参数的方法也发 `{}`。
- 结构体拒绝未知字段，缺少必需字段即拒绝；可以为空的字段显式写 `null`。整数的范围按其 Rust 类型（`u16`、`u32`、`u64`、`i32`），越界即拒绝。
- 联合类型统一以 `type` 字段为内部标签，每个分支是一个具名结构体，Rust 与 Python 用同一名字。
- 以 `_ns` 结尾的时刻字段是实时核单调时钟的纳秒读数，与帧环的采集时刻同一时钟（Windows 上由 QPC 换算）；`offset_ns` 是相对起始时刻的纳秒数。主进程读取同一时钟的方式在实现动作块时确定并实测（PENDING）。
- 方法名到 params 与 result 类型的对应只有一处源：Rust 的 `core_protocol::methods`。它导出到 Schema 的 `x-methods`（每个方法的方向与 params、result 的 `$ref`，通知的 `result` 为 `null`），再生成为 Python 的 `core_protocol.methods`。
- 错误用 JSON-RPC 2.0 的标准错误码（解析失败、无效请求、方法不存在、参数无效），另有两个自定义错误码：

| 错误码 | 名字 | 含义 |
| --- | --- | --- |
| -32001 | `protocol_version_mismatch` | `core.hello` 的协议版本与主进程的不相等 |
| -32002 | `frame_ring_mismatch` | `frame_ring.attach` 打不开共享内存，或帧环头部与参数不符 |

### 会话

1. 实时核通过通道认证后先发 `core.hello`；主进程核对协议版本，不相等即以 -32001 回错并断开。握手完成前双方不发其它消息。
2. 主进程发 `frame_ring.attach` 与 `capture.set_target`；实时核此后定期发 `core.health`。
3. 当前控制产出者定期发 `producer.heartbeat`，以 `action.submit` 提交动作块；实时核以 `action.receipt` 报一次性动作的结果，每次释放全部输入后发 `input.released`。
4. 更换控制产出者（[QUIESCING](plugin-kernel.md#生命周期)）：停止提交动作块 → `producer.revoke`，等到它的回应，即输入已释放的确认 → 撤销注册。
5. 正常停止：主进程发 `core.stop`；实时核释放全部输入，发 `input.released`，回应后退出。这是 Bellis R-06 停止顺序中的 RPC 一步。

`core.health` 的周期与心跳超时的取值在实现生命线时确定（PENDING）。

### 消息集

| 方法 | 方向 | 类型 | params → result | 用途 |
| --- | --- | --- | --- | --- |
| `core.hello` | 实时核 → 主进程 | 请求 | `CoreHelloParams` → `CoreHelloResult` | 握手 |
| `frame_ring.attach` | 主进程 → 实时核 | 请求 | `FrameRingAttachParams` → `Empty` | 帧环挂接 |
| `capture.set_target` | 主进程 → 实时核 | 请求 | `CaptureSetTargetParams` → `Empty` | 设定采集目标 |
| `producer.heartbeat` | 主进程 → 实时核 | 通知 | `ProducerHeartbeatParams` | 控制产出者心跳 |
| `action.submit` | 主进程 → 实时核 | 请求 | `ActionBlock` → `ActionSubmitResult` | 提交[动作块](#动作块) |
| `producer.revoke` | 主进程 → 实时核 | 请求 | `ProducerRevokeParams` → `Empty` | 作废代次；回应即输入已释放的确认 |
| `core.stop` | 主进程 → 实时核 | 请求 | `Empty` → `Empty` | 正常停止 |
| `action.receipt` | 实时核 → 主进程 | 通知 | `ActionReceiptParams` | 执行回执 |
| `input.released` | 实时核 → 主进程 | 通知 | `InputReleasedParams` | 输入已释放的确认 |
| `core.health` | 实时核 → 主进程 | 通知 | `CoreHealthParams` | 实时核健康，也是主进程判断实时核存活的依据 |

输入许可的消息在实现输入许可（G0 的测试许可源）时加入。

**`core.hello`**

| 字段 | 含义 |
| --- | --- |
| `protocol_version` | params：实时核实现的协议版本；result：主进程实现的协议版本。两端都核对 |
| `core_version` | 实时核的程序版本，只用于日志与诊断 |

**`frame_ring.attach`**：主进程创建帧环并写好头部后发出；实时核打开共享内存，核对头部与参数一致后开始写帧，见[帧环](#帧环)。

| 字段 | 含义 |
| --- | --- |
| `name` | 共享内存名 |
| `size_bytes` | 共享内存的总字节数，不小于 `SLOTS_OFFSET + slot_count × slot_stride` |
| `layout_version`、`slot_count`、`slot_stride`、`max_width`、`max_height` | 与帧环头部的同名字段相等 |

**`capture.set_target`**：`target` 是联合类型 `CaptureTarget`，G0 由配置给出，游戏适配包在 G1 接入。

| 分支 | 字段 | 含义 |
| --- | --- | --- |
| `window`（`WindowTarget`） | `process_name` | 进程的可执行文件名，不区分大小写 |
| | `window_class`、`title` | 窗口类名与窗口标题，全等匹配；`null` 表示不按它匹配，两者至少给出一个 |
| `fake`（`FakeTarget`） | 无 | 假采集源，只用于自动测试 |

**控制产出者**：`Producer` 由身份 `id` 与代次 `generation` 组成。代次由主进程分配，在一个实时核的生命期内严格递增；实时核记住当前代次与已作废的最高代次。

- 心跳或动作块的代次不大于已作废的代次，或小于当前代次：拒绝。代次等于当前代次而身份不同：拒绝。
- 代次大于当前代次：先释放全部输入（`producer_changed`），再以它为当前代次。
- `producer.heartbeat` 的 `seq` 在同一代次内严格递增，不递增的心跳不算数；心跳超时即释放全部输入（`heartbeat_timeout`）。
- `producer.revoke` 作废 `generation` 及更早的全部代次，之后这些代次的心跳与动作块都被拒绝；当前代次在其中时先释放全部输入（`generation_revoked`）。实时核在释放完成后才回应，回应即这些代次的输入已释放的确认。

**`action.submit`** 的 params 见[动作块](#动作块)。result 是联合类型 `ActionSubmitResult`：

| 分支 | 字段 | 含义 |
| --- | --- | --- |
| `accepted`（`Accepted`） | 无 | 已接受，按时间执行 |
| `rejected`（`Rejected`） | `reason` | `stale_generation`（代次早于当前或已作废）、`producer_mismatch`（代次相同、身份不同）、`stale_seq`（序号不大于已接受的）、`expired`（有效截止时刻已过）、`invalid`（内容不合[动作块](#动作块)的规则） |
| | `message` | 给人看的说明，只用于日志与诊断 |

**`action.receipt`**：报告一块动作中一次性动作的结果，每个动作在一块中只报一次。`producer` 与 `block_seq` 指明动作块，`outcomes` 是联合类型 `ActionOutcome` 的列表：

| 分支 | 字段 | 含义 |
| --- | --- | --- |
| `executed`（`Executed`） | `id`、`at_ns` | 已交给输入后端及其时刻；不代表游戏中的效果已发生 |
| `dropped`（`Dropped`） | `id`、`reason` | 没有执行：`superseded`（新的动作块已取代本块未执行的部分）、`duplicate`（同一身份在本代次已执行过）、`released`（到时实时核已释放输入） |

**`input.released`**：实时核每次释放全部输入后发出，不论释放前是否持有输入。

| 字段 | 含义 |
| --- | --- |
| `reason` | `core_start`（启动时先释放）、`generation_revoked`、`producer_changed`、`heartbeat_timeout`、`deadline_expired`（到截止时刻没有新的动作块）、`frame_stale`（最新的帧超过动作允许的年龄）、`stop` |
| `producer` | 释放时的当前控制产出者；没有时为 `null` |
| `released_at_ns` | 释放完成的时刻 |

**`core.health`**

| 字段 | 含义 |
| --- | --- |
| `now_ns` | 发出时实时核的时钟读数 |
| `capture` | `idle`（还没有挂接帧环或设定采集目标）、`searching`（找不到目标窗口，没有帧）、`capturing` |
| `inputs_held` | 当前是否持有输入 |
| `producer` | 当前控制产出者；没有时为 `null` |

## 动作块

主进程向实时核提交的唯一动作形式（GD-01），即 `action.submit` 的 params `ActionBlock`。

| 字段 | 含义 |
| --- | --- |
| `producer` | 产出者身份与代次；实时核拒绝其它产出者和旧代次的动作块 |
| `seq` | 动作块序号，同一代次内严格递增 |
| `base_frame` | 基准帧号：产出者做决定时依据的帧，坐标按这一帧的客户区解释 |
| `start_ns` | 起始时刻，与帧环同一时钟 |
| `deadline_ns` | 有效截止时刻：到时还没有新的动作块，实时核即释放 |
| `steps` | 按时间排列的控制步，至少一步 |

每个控制步（`ControlStep`）在 `start_ns + offset_ns` 生效，动作取自[统一动作空间](../architecture.md#控制的组织)中会到实时核的部分：

| 字段 | 含义 |
| --- | --- |
| `offset_ns` | 相对起始时刻的纳秒数，在块内不减 |
| `held` | 完整的持续状态，联合类型 `HeldInput`：`key`（`scan_code`）、`mouse_button`（`button`）、`axis`（`axis` 轴名、`value` 取值 [-1, 1]）。从这一步起保持到下一步，最后一步保持到截止时刻 |
| `actions` | 一次性动作，按列出的顺序执行，联合类型 `Action`：`click`（`x`、`y`、`button`）、`key_press`（`scan_code`）、`scroll`（`x`、`y`、`notches`）、`mouse_move`（`dx`、`dy`）。每个都带 `id`，在同一代次内唯一 |

- 坐标是基准帧客户区的物理像素；`scan_code` 是 Set 1 扫描码，扩展键在高字节带 `0xE0`；`button` 为 `left`、`right` 或 `middle`；`notches` 正数向前；`dx`、`dy` 是鼠标计数，正数向右、向下。点击与短按的按住时长、轴名的白名单由游戏适配包给出。
- **持续状态**（按住的键、轴）每块完整给出，缺项按释放处理，不沿用旧值。同一步中同一键、鼠标键或轴只能出现一次。
- **一次性动作**（点击、短按、滚动、相对位移）按身份去重，重传不重复执行、不延长持续时间。块内身份不得重复。
- 新接受的动作块从它的起始时刻起取代上一块未执行的部分。到达时时刻已过的步立即执行；截止时刻之后不再执行任何步。
- 不合以上规则、步的时刻晚于截止时刻或没有步的动作块整块拒绝（`invalid`）。
- 剧情游戏的动作块只含一次点击或短按，没有持续状态；即时游戏的动作块是按固定频率排列的完整控制序列。

## 帧环与音频环

两者都是主进程创建的共享内存环形缓冲，实时核连接并写入（非提权一方创建全部进程间对象，GD-02）。

| 环 | 每项内容 |
| --- | --- |
| 帧环 | 帧（BGRA8，客户区）、帧号、采集时刻（QPC）、窗口与 DPI 变换版本 |
| 音频环 | 游戏进程的 PCM 与采集时刻，与帧环同一时钟 |

头部是二进制布局，以 Rust 的 `#[repr(C)]` 结构为源（`core_protocol::frame_ring`），由 Rust 导出字段偏移表 `protocol/frame-ring-layout.json`；Python 的 `core_protocol.frame_ring` 手写同一布局，由测试与偏移表逐项核对；偏移表不一致时 `uv run check` 失败。音频环的布局在 G1 加入。

### 帧环

共享内存的起点是环头部，从 `SLOTS_OFFSET` 起是 `slot_count` 个跨度为 `slot_stride` 的槽；每个槽的起点是槽头部，从 `SLOT_PIXELS_OFFSET` 起是像素。整数按本机字节序。结构没有隐式填充，Windows 与 Linux 上布局相同。

| 常量 | 值 | 含义 |
| --- | --- | --- |
| `MAGIC` | 小端读出的 `b"BGRTRING"` | 环头部的标识 |
| `LAYOUT_VERSION` | 1 | 头部、槽头部或这些常量变化时加一 |
| `SLOTS_OFFSET` | 64 | 槽 0 的偏移 |
| `SLOT_PIXELS_OFFSET` | 64 | 像素相对槽起点的偏移 |
| `SLOT_ALIGN` | 64 | 槽跨度的对齐 |
| `PIXEL_FORMAT_BGRA8` | 1 | 每像素 4 字节，依次为 B、G、R、A |

环头部 `FrameRingHeader`（40 字节）：主进程创建时写入除 `write_seq` 外的全部字段，此后不变。

| 偏移 | 字段 | 类型 | 含义 |
| --- | --- | --- | --- |
| 0 | `magic` | u64 | `MAGIC` |
| 8 | `layout_version` | u32 | `LAYOUT_VERSION` |
| 12 | `slot_count` | u32 | 槽数，至少 2 |
| 16 | `slot_stride` | u64 | 相邻两槽起点之间的字节数，是 `SLOT_ALIGN` 的倍数，不小于 `SLOT_PIXELS_OFFSET + max_width × max_height × 4` |
| 24 | `max_width` | u32 | 帧的最大宽度（物理像素） |
| 28 | `max_height` | u32 | 帧的最大高度（物理像素） |
| 32 | `write_seq` | 原子 u64 | 最新一帧的帧号，0 表示还没有帧；只由实时核写 |

槽头部 `FrameSlotHeader`（48 字节）：只由实时核写。

| 偏移 | 字段 | 类型 | 含义 |
| --- | --- | --- | --- |
| 0 | `seq` | 原子 u64 | seqlock 序号：奇数表示正在写，偶数表示稳定 |
| 8 | `frame_number` | u64 | 帧号，从 1 起在本帧环内严格递增；槽号为 `(frame_number − 1) mod slot_count` |
| 16 | `capture_ns` | u64 | 采集时刻，与核协议的 `_ns` 时刻同一时钟 |
| 24 | `transform_version` | u64 | 窗口与 DPI 变换版本，窗口移动、缩放或 DPI 变化时加一 |
| 32 | `width` | u32 | 帧宽，不超过 `max_width` |
| 36 | `height` | u32 | 帧高，不超过 `max_height` |
| 40 | `row_stride` | u32 | 相邻两行起点之间的字节数，不小于 `width × 4`；`row_stride × height` 不超过 `slot_stride − SLOT_PIXELS_OFFSET` |
| 44 | `pixel_format` | u32 | `PIXEL_FORMAT_BGRA8` |

单写者多读者：

- 实时核写一帧：把槽的 `seq` 置为下一个奇数（崩溃后遗留的奇数也跳过），写槽头部其余字段与像素，再把 `seq` 加一成为偶数，最后把 `write_seq` 置为该帧号；`seq` 与 `write_seq` 的写入带释放语义。实时核重启后挂接同一帧环时从 `write_seq` 续写，帧号不回退。
- 读者：读 `write_seq` 得到帧号与槽号；读槽的 `seq`，为奇数则重读；复制槽头部与像素；再读 `seq`，与先前不等或 `frame_number` 不是所求帧号即放弃这次读取，重读最新的帧。

## 生成与核对

| 生成物（`protocol/` 下） | 来源 |
| --- | --- |
| `schema/core-protocol.schema.json` | schemars 从 Rust 类型生成；`x-methods`、`x-protocol-version`、`x-error-codes` 来自 `core_protocol::methods` 与协议版本常量 |
| `frame-ring-layout.json` | Rust 以 `std::mem::offset_of!` 导出的常量与各结构的大小、对齐、字段偏移、大小与类型 |
| `fixtures/<方法名>/params[.<用例>].json`、`result[.<用例>].json` | Rust 从类型的值序列化出的示例消息，每个方法的 params 与 result 至少一条，每个联合类型的每个分支至少出现一次 |
| `src/core_protocol/models.py` | datamodel-code-generator 从 Schema 生成的 Pydantic 模型 |
| `src/core_protocol/methods.py` | 从 Schema 的 `x-methods` 生成的方法目录、协议版本与自定义错误码 |

- 重新生成用 `uv run generate`：先运行 Rust 的 `core-protocol-export` 写出 Schema、偏移表与示例消息，再运行 datamodel-code-generator，两份 Python 文件经仓库的 ruff 配置修正与格式化，然后写回 `protocol/`，删除不再生成的示例消息。改了 `core/crates/core-protocol` 后运行它，生成物与源放在同一个提交里。
- `uv run check` 的生成物核对把全部生成物重新生成到临时目录，与入库的逐字节比较，缺少、多余或内容不同都失败，并提示运行 `uv run generate`。
- 生成是确定的：不含时间戳与路径，键按声明顺序，换行一律 LF，Windows 与 Linux 上的结果逐字节相同。
- 联合类型：schemars 把每个分支内联为带 `type` 常量的对象；分支结构体以 `#[schemars(title = …)]` 写明自己的名字，生成的 Pydantic 类与 Rust 同名；导出时给以 `type` 为标签的 `oneOf` 加上 `discriminator`，datamodel-code-generator 据此生成按 `type` 选分支的 Pydantic 判别联合。整数补上 Rust 类型的上下界，`additionalProperties: false` 生成为 `extra="forbid"`。
- 测试：Rust 与 Python 各自解析每条示例消息并往返序列化，结果与原值相等；在每个对象节点加一个未知字段都被拒绝；`type` 标签选出联合类型的分支，未知标签被拒绝。Python 的帧环布局与偏移表逐项核对；Rust 侧核对偏移表列出了每个字段且没有填充。

## 验收

| 阶段 | 内容 |
| --- | --- |
| G0 | 生成物过期与偏移表不一致时检查失败；本机通道未认证即拒绝，篡改、重放、超长帧与超时都被拒绝；Python 与 Rust 互通，Windows 真机跑命名管道，Linux 容器跑 UDS |
| G3 | 与 Bellis 宿主真机互通 |

## 技术路线

已批准：GD-01、GD-02、GR-03、GR-28；继承 Bellis D-12、D-21、R-05。
