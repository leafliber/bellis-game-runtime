# 核协议与本机通道

主进程与实时核之间的消息、动作块和共享内存的布局，以及本仓库全部本机通道的实现。G0 实现。连接的划分与各自承载的内容见[总体架构](../architecture.md#进程与连接)；与 Bellis 之间的游戏桥消息见[游戏桥](game-bridge.md#契约)。

## 本机通道

本仓库按 Bellis R-05 的规范自写 `bellis-channel/1`（GR-28），不依赖 Bellis 的 `bellis_sidecar` 包：

| 实现 | 位置 | 用于 |
| --- | --- | --- |
| Python 客户端 | `runtime/src/game_runtime/channel/`（asyncio） | 主进程 → Bellis 宿主（语义连接） |
| Python 服务端 | 同上 | 主进程 ← 实时核（核连接） |
| Rust 客户端 | `core/crates/bellis-channel`（标准线程加阻塞 IO，不用 tokio） | 实时核 → Bellis 宿主（控制连接）、实时核 → 主进程（核连接） |

- Windows 上用命名管道，其它平台用 UDS，同一抽象（Bellis D-12）。
- 双向 HMAC 认证先于任何数据；有帧长上限与超时；篡改、重放、超长帧与超时都拒绝。
- 正确性分三层：握手与帧格式的已知答案测试；本仓库 Python 与 Rust 的互通测试，Windows 真机实际跑命名管道，Linux 容器跑 UDS；G3 联调前与 Bellis 宿主真机互通（PENDING）。

### 帧格式与握手

以 Bellis `main` 的提交 `e4f9c91` 为准并与之逐字节兼容，规范不在这里重抄：

| 内容 | Bellis 的源 |
| --- | --- |
| 帧：4 字节大端负载长度、1 字节类型、负载；先查类型再查长度 | [`packages/channel/src/frame.ts`](https://github.com/leafliber/bellis/blob/e4f9c91/packages/channel/src/frame.ts) |
| 握手的四条消息、证明的输入与顺序、状态机、上限与超时 | [`packages/channel/src/connection.ts`](https://github.com/leafliber/bellis/blob/e4f9c91/packages/channel/src/connection.ts) |
| 握手消息的模式 | [`packages/protocol/src/channel.ts`](https://github.com/leafliber/bellis/blob/e4f9c91/packages/protocol/src/channel.ts) |
| 监听、连接与未认证握手的上限 | [`packages/channel/src/endpoint.ts`](https://github.com/leafliber/bellis/blob/e4f9c91/packages/channel/src/endpoint.ts) |
| JSON-RPC 2.0 对端与消息模式 | [`packages/channel/src/rpc.ts`](https://github.com/leafliber/bellis/blob/e4f9c91/packages/channel/src/rpc.ts)、[`packages/protocol/src/jsonrpc.ts`](https://github.com/leafliber/bellis/blob/e4f9c91/packages/protocol/src/jsonrpc.ts) |
| 地址与会话密钥的编码 | [`packages/channel/src/address.ts`](https://github.com/leafliber/bellis/blob/e4f9c91/packages/channel/src/address.ts) |
| 错误码 | [`packages/channel/src/errors.ts`](https://github.com/leafliber/bellis/blob/e4f9c91/packages/channel/src/errors.ts) |

实现时容易走样、由测试向量覆盖的几点：

- 证明的输入是 Node `JSON.stringify([域, 方向, 角色, 客户端随机数, 服务端随机数])` 的 UTF-8 字节：紧凑、按此顺序，域为 `bellis-channel/1`，方向为 `server` 或 `client`。随机数与证明都是 32 字节的 base64url 无填充文本；核对证明时比较解码后的字节，最后一个字符未用到的低 2 位不同也通过，与 Node 一致。
- 消息校验与 Bellis 用的 TypeBox 等价：对象不允许多余的键；`v` 只认数 1，JSON 的 `1.0`、`1e0` 也算，布尔值不算；字符串模式整串匹配，结尾的换行不算匹配；字符串长度按码位计；JSON-RPC 的整数 id 限在安全整数范围内，`1.0` 这样的整数值也算整数。
- JSON 负载按 `JSON.parse(payload.toString('utf8'))` 解析：非法的 UTF-8 替换为 U+FFFD，不跳过 BOM，不接受 NaN 与 Infinity，同名键取最后一个。
- JSON-RPC 消息依次按请求、通知、成功应答、失败应答分类，都不是时回 InvalidRequest（id 为 null）；参数只用按名传递的对象；`rpc.` 前缀保留给协议，`rpc.cancel` 是取消通知。

### 长度上限与超时

| 项 | 取值 | 说明 |
| --- | --- | --- |
| 认证前单帧负载 | 1024 字节 | 只凭帧头就判定超长，以 `frame-too-large` 断开 |
| 认证后单帧负载 | 缺省 1 MiB，可配置 | 收发两个方向都查：发送超长只报错，连接不受影响；收到超长即断开 |
| 握手 | 缺省 2000ms，可配置 | 客户端从连上到收到 accept，服务端从接受连接到核验完证明。客户端连管道时等待忙实例也以它为限，超时报 `connect-failed` |
| 读 | 由调用方给出 | `receive(timeout)` / `recv(timeout)` 超时报 `timeout`，连接仍可用；读到一半的帧留在缓冲区，下次接着读，不失步。通道没有连接级的空闲超时（Bellis 也没有），核连接的存活由控制产出者心跳判断（[启动与生命线](realtime-core.md#启动与生命线)） |
| 请求 | 由调用方给出 | 超时报 `timeout` 并向对端发 `rpc.cancel` |
| 关闭宽限 | 1000ms | Python 正常关闭时等对端收完已写出的数据，超过即强制断开 |
| 未认证的握手 | 16 个 | 服务端超出的连接立即断开，报 `busy` |
| 同时处理的对端请求 | 64 个 | Python 超出时回 ServerBusy；Rust 的处理函数在读线程上同步执行，没有并发 |

### 地址与密钥

- 地址形如 `bellis-<会话编号>-<16 字节随机数的 base32>`（RFC 4648，小写、无填充），会话编号匹配 `^[a-z0-9]{1,16}$`，与 Bellis 相同。Windows 上为 `\\.\pipe\<名字>`。其它平台为 `<名字>.sock`，放在主进程以 `tempfile.mkdtemp` 新建、只有当前用户能访问的私有目录里，路径不超过 103 字节，会话结束时删除该目录。Bellis 把 UDS 直接放在系统临时目录下；这里多一层私有目录只为减少暴露，访问控制仍靠握手（Bellis D-12）。
- 会话密钥是 32 字节随机数，以 base64url 无填充文本交接；只接受规范编码，格式不对报 `address-invalid`，与 Bellis 的 TS 实现一致。
- 主进程以 `session_credentials(会话编号)` 生成本次会话的地址与密钥（`ChannelCredentials`）；实时核以 `ChannelCredentials::decode(地址, 密钥文本)` 接收。怎样交给实时核见[启动与生命线](realtime-core.md#启动与生命线)。

### 安全设置

- Windows 的管道由 Python 服务端自己建：asyncio 自带的服务端以消息模式、缺省安全描述符建管道，也不拒绝远程客户端。这里每个实例都用字节模式（与 libuv 一致）并带 `PIPE_REJECT_REMOTE_CLIENTS`；第一个实例带 `FILE_FLAG_FIRST_PIPE_INSTANCE`，同名管道已存在时以 `address-in-use` 失败，不用同名重试；安全描述符的 DACL 为 `D:P(A;;GA;;;<当前用户 SID>)`，只允许当前用户；地址上始终留一个尚未连接的实例。接受连接仍交给 asyncio 的 IOCP proactor，用到它的内部接口（与 `start_serving_pipe` 自身相同），所以 Python 版本锁定在 3.12。
- 提权的实时核以同一用户运行时满足这个 DACL；跨完整性级别的可达性（非提权主进程建的管道，提权的实时核连接与读写）PENDING，由 [GS-03](../plan/spikes.md) 真机验证。
- Rust 客户端以 `SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION` 打开管道：连到被抢注的管道时，对端只能识别、不能冒充提权实时核的令牌。
- POSIX 上服务端自己 bind，socket 文件已存在时报 `address-in-use`，不像 asyncio 那样删掉旧的；关闭时删除 socket 文件。
- 这些设置只是减少暴露，认证仍只靠握手；同一用户下的恶意进程不在威胁模型内（Bellis D-12）。

### 错误处理

错误码与 Bellis `errors.ts` 逐字相同：`address-in-use`、`address-invalid`、`invalid-key`、`busy`、`connect-failed`、`auth-failed`、`handshake-timeout`、`frame-too-large`、`protocol-error`、`timeout`、`closed`。

| 情形 | 结果 |
| --- | --- |
| 握手中任何失败：证明不符、消息不合法、认证前收到业务帧、超长、超时、对端关闭 | 断开连接；服务端以 `on_rejected` 报告且不交出连接；客户端核验服务端失败时不回送证明 |
| 角色名不合法 | 客户端在连接之前就报 `protocol-error` |
| 认证后帧超长、帧类型未知、收到握手帧、JSON 不合法 | 断开连接，`receive` / `recv` 报对应的错误 |
| 读超时、发送超长 | 只报错，连接仍可用 |
| 服务端的 `on_connection` 抛错 | 断开该连接，交给 `on_error` |
| 对端请求未知方法、消息不合法、处理函数抛出 RpcError、其它异常 | 依次回 MethodNotFound、InvalidRequest（id 为 null）、原样的错误、InternalError（不外泄细节）；应答超过帧长上限时改回 InternalError |
| 连接关闭 | 未完成的请求报 `closed`；Python 中正在处理的请求被取消 |

RPC 的处理函数：Python 中每个请求在自己的任务里处理，收到 `rpc.cancel` 时取消它且不再应答；Rust 中处理函数在读线程上同步执行，须很快返回，也不能在里面向同一对端发请求，`rpc.cancel` 到达时处理早已结束，所以忽略它。

### 与 Bellis 实现的出入

以下出入都不影响与 Bellis 互通：

- UDS 放在私有临时目录里（见上）。
- 角色名不合法时客户端在连接之前就失败；Bellis 先连上再断开。
- Python 中被 `rpc.cancel` 取消的处理不再应答；Bellis 的处理函数不理会中止信号时照常应答，请求方本就忽略。
- Rust 用 serde_json 解析：不接受孤立代理项的 `\uD800` 转义，嵌套超过 128 层也拒绝，都以 `protocol-error` 断开；Node 都接受。实时核宁可断开，也不在栈上无限递归。
- Rust 发出的 JSON-RPC 信封按 Bellis 的键顺序编码，`params` 等内层对象的键序由 serde_json 决定（启用 `preserve_order` 时为插入顺序，否则为字典序）；对端解析后相同。
- Windows 管道的安全设置与 Rust 客户端的 SQOS（见上）；Bellis 用 libuv 的缺省值。

### 测试向量

`core/crates/bellis-channel/testdata/vectors.json` 由同目录的 `generate-vectors.ts` 从 Bellis 提交 `e4f9c91` 的 TypeScript 实现导出，文件里记有来源提交：握手与 JSON-RPC 的字节取自真实的 `Connection` 与 `RpcPeer`（以固定值替换 `node:crypto` 的 `randomBytes`），消息校验的结论取自 Bellis 用的同一份 TypeBox 校验，长度上限与超时从源码读出。向量含帧编解码、地址与密钥、一次完整会话（握手与各类 JSON-RPC 往来）、服务端与客户端两侧的握手结局（篡改、反射、重放、错误密钥、协议标识与版本不符、超长帧、认证前收到业务数据、提前关闭）、负载解析与消息分类。

Bellis 改动通道后重新生成：

1. `git -C <Bellis 仓库> archive <提交> packages/channel packages/protocol` 导出到一个空目录；
2. 在该目录的 `node_modules/` 放入 Bellis 锁定版本的 `typebox`，并把 `node_modules/@bellis/protocol` 链接（Windows 上用目录联接）到导出的 `packages/protocol`；
3. 在本仓库根目录运行 `node core/crates/bellis-channel/testdata/generate-vectors.ts <导出目录> <提交的完整哈希> > core/crates/bellis-channel/testdata/vectors.json`。生成是确定的，两次输出应逐字节相同。

Node 不在检查镜像里，生成不属于 `uv run check`；检查只用入库的向量。

### 测试覆盖

| 层 | Python | Rust |
| --- | --- | --- |
| 已知答案 | 全部向量，服务端与客户端两侧 | 客户端一侧的握手结局与会话、全部校验规则；负载解析的两处出入按出入断言 |
| 拒绝 | 认证前发数据、篡改与反射证明、错误密钥、抢注者、重放握手、认证前与认证后超长帧、握手超时、读超时与半帧、协议标识与版本不符、未认证握手过多、地址被占用 | 客户端一侧的同类拒绝：错误密钥、篡改证明、重放 challenge、认证前业务数据、认证前与认证后超长帧、握手超时、读超时与半帧、版本与协议标识不符 |
| 传输 | Windows 跑命名管道，另验证 DACL 与经 SMB 回环的远程客户端被拒；Linux 容器跑 UDS | 测试服务端在命名管道或 UDS 上收发原始帧 |
| 互通 | pytest 编译并拉起 Rust 示例 `interop_client`，与 Python 服务端握手、双向请求与通知；错误密钥被拒 | 同左 |

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
