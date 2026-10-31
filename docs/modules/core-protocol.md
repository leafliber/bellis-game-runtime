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
- 提权的实时核以同一用户运行时满足这个 DACL；跨完整性级别的可达性（非提权主进程建的管道，提权的实时核连接与读写）经 [GS-03](../plan/spikes.md) 实测成立（2026-10-31，见[提权时的计划任务](realtime-core.md#提权时的计划任务gr-06)）。
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

RPC 的处理函数：Python 中每个请求在自己的任务里处理，收到 `rpc.cancel` 时取消它且不再应答；Rust 中处理函数在读线程上同步执行，须很快返回，也不能在里面向同一对端发请求，`rpc.cancel` 到达时处理早已结束，所以忽略它。Rust 中需要先应答再收尾的请求以 `Handlers::request_then` 登记：应答写出之后（写失败也算）才在读线程上调用收尾函数，例如收到 `core.stop` 后退出进程，对应 Bellis `handleShutdown` 在下一轮事件循环才收尾的做法。

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

核连接承载：动作块、控制产出者心跳与代次、执行回执、输入已释放的确认、实时核健康，以及帧环与音频环的挂接、采集目标、音频目标与正常停止。输入许可不经核连接：主进程只能让输入停下，不能让输入继续；许可的语义见[输入许可](#输入许可)，G0 唯一的许可源在只用于测试的[测试设备连接](#测试设备连接)上。

### 约定

- 方法名为“对象.动作”。有 result 的方法是请求，没有的是通知。params 一律是对象，不带参数的方法也发 `{}`。
- 结构体拒绝未知字段，缺少必需字段即拒绝；可以为空的字段显式写 `null`。整数的范围按其 Rust 类型（`u16`、`u32`、`u64`、`i32`），越界即拒绝。
- 联合类型统一以 `type` 字段为内部标签，每个分支是一个具名结构体，Rust 与 Python 用同一名字。
- 以 `_ns` 结尾的时刻字段是实时核单调时钟的纳秒读数，与帧环的采集时刻同一时钟；`offset_ns` 是相对起始时刻的纳秒数。主进程以 `time.perf_counter_ns()` 读同一时钟（Windows 上是 QPC，Linux 上是 `CLOCK_MONOTONIC`），已在 Windows 与 Linux 上实测核对，见[时钟](realtime-core.md#时钟)。
- 方法名到 params 与 result 类型的对应只有一处源：Rust 的 `core_protocol::methods`。它导出到 Schema 的 `x-methods`（每个方法的方向与 params、result 的 `$ref`，通知的 `result` 为 `null`），再生成为 Python 的 `core_protocol.methods`（`METHODS`）。测试设备连接上的方法单独成组：`x-test-device-methods`，Python 的 `TEST_DEVICE_METHODS`。
- 错误用 JSON-RPC 2.0 的标准错误码（解析失败、无效请求、方法不存在、参数无效），另有以下自定义错误码：

| 错误码 | 名字 | 含义 |
| --- | --- | --- |
| -32001 | `protocol_version_mismatch` | `core.hello` 的协议版本与主进程的不相等 |
| -32002 | `frame_ring_mismatch` | `frame_ring.attach` 打不开共享内存，或帧环头部与参数不符；`capture.set_target` 的假显示面同样 |
| -32003 | `capture_unsupported` | `capture.set_target` 或 `audio.set_target` 的目标在这个平台或系统上不支持：非 Windows 上的 `window`，或系统不支持 WGC |
| -32004 | `lease_refused` | 只用于测试：`test_lease.grant` 不被接受，见[输入许可](#输入许可) |
| -32005 | `audio_ring_mismatch` | `audio_ring.attach` 打不开共享内存，或音频环头部与参数不符；`audio.set_target` 的假音频输出同样 |

### 会话

1. 实时核通过通道认证后先发 `core.hello`；主进程核对协议版本，不相等即以 -32001 回错并断开。握手完成前双方不发其它消息。
2. 实时核随即发 `input.released(core_start)`（启动时已先释放全部输入），之后才处理任何产出者消息；主进程收到它才算实时核就绪。实时核立即发第一条 `core.health`，此后按周期发。
3. 主进程先发 `frame_ring.attach`，再发 `capture.set_target`；配置了音频时接着发 `audio_ring.attach` 与 `audio.set_target`；之后才开始发心跳（主进程一侧见帧环与音频环各自的挂接：[帧环](#读写规则)、[音频环](#音频环的读写规则)）。
4. 当前控制产出者定期发 `producer.heartbeat`，以 `action.submit` 提交动作块；实时核以 `action.receipt` 报一次性动作的结果，每次释放全部输入后发 `input.released`。
5. 更换控制产出者（[QUIESCING](plugin-kernel.md#生命周期)）：停止提交动作块 → `producer.revoke`，等到它的回应，即输入已释放的确认 → 撤销注册。
6. 正常停止：主进程发 `core.stop`；实时核释放全部输入，发 `input.released`，回应写出后退出。这是 Bellis R-06 停止顺序中的 RPC 一步。

心跳周期 100 ms、心跳超时 500 ms，`core.health` 周期 1 秒、主进程判定失联 3 秒，动作允许的帧龄 500 ms，都可配置；依据与真实输入下的实测（PENDING，G2）见[时限](realtime-core.md#时限)。

### 消息集

| 方法 | 方向 | 类型 | params → result | 用途 |
| --- | --- | --- | --- | --- |
| `core.hello` | 实时核 → 主进程 | 请求 | `CoreHelloParams` → `CoreHelloResult` | 握手 |
| `frame_ring.attach` | 主进程 → 实时核 | 请求 | `FrameRingAttachParams` → `Empty` | 帧环挂接 |
| `capture.set_target` | 主进程 → 实时核 | 请求 | `CaptureSetTargetParams` → `Empty` | 设定采集目标 |
| `audio_ring.attach` | 主进程 → 实时核 | 请求 | `AudioRingAttachParams` → `Empty` | 音频环挂接 |
| `audio.set_target` | 主进程 → 实时核 | 请求 | `AudioSetTargetParams` → `Empty` | 设定音频目标 |
| `producer.heartbeat` | 主进程 → 实时核 | 通知 | `ProducerHeartbeatParams` | 控制产出者心跳 |
| `action.submit` | 主进程 → 实时核 | 请求 | `ActionBlock` → `ActionSubmitResult` | 提交[动作块](#动作块) |
| `producer.revoke` | 主进程 → 实时核 | 请求 | `ProducerRevokeParams` → `Empty` | 作废代次；回应即输入已释放的确认 |
| `core.stop` | 主进程 → 实时核 | 请求 | `Empty` → `Empty` | 正常停止 |
| `action.receipt` | 实时核 → 主进程 | 通知 | `ActionReceiptParams` | 执行回执 |
| `input.released` | 实时核 → 主进程 | 通知 | `InputReleasedParams` | 输入已释放的确认 |
| `core.health` | 实时核 → 主进程 | 通知 | `CoreHealthParams` | 实时核健康，也是主进程判断实时核存活的依据 |

输入许可的消息不在核连接上，见[测试设备连接](#测试设备连接)。

**`core.hello`**

| 字段 | 含义 |
| --- | --- |
| `protocol_version` | params：实时核实现的协议版本；result：主进程实现的协议版本。两端都核对 |
| `core_version` | 实时核的程序版本，只用于日志与诊断 |

**`frame_ring.attach`**：主进程创建帧环并写好头部后发出；实时核打开共享内存，核对头部与参数一致后交给采集线程，帧号从头部的 `write_seq` 续写，见[帧环](#帧环)。再次挂接即换成新的帧环。

| 字段 | 含义 |
| --- | --- |
| `name` | 共享内存名 |
| `size_bytes` | 共享内存的总字节数，不小于 `SLOTS_OFFSET + slot_count × slot_stride` |
| `layout_version`、`slot_count`、`slot_stride`、`max_width`、`max_height` | 与帧环头部的同名字段相等 |

**`capture.set_target`**：`target` 是联合类型 `CaptureTarget`，G0 由配置给出（见[读写规则](#读写规则)中主进程一侧的实现），游戏适配包在 G1 接入。

| 分支 | 字段 | 含义 |
| --- | --- | --- |
| `window`（`WindowTarget`） | `process_name` | 进程的可执行文件名，不区分大小写 |
| | `window_class`、`title` | 窗口类名与窗口标题，全等匹配；`null` 表示不按它匹配，两者至少给出一个 |
| `fake`（`FakeTarget`） | `surface` | 假采集源，只用于自动测试：假显示面的共享内存名与布局参数，形式与核对同 `frame_ring.attach`，见[假采集源](realtime-core.md#假采集源) |

- 回应表示目标已核对并交给采集线程，不等采集源建立。找不到窗口不是错误：`core.health.capture` 为 `searching`，实时核定期重新识别（[采集主循环](realtime-core.md#采集主循环)）。
- 再次设定时先停掉旧的采集源。与 `frame_ring.attach` 的先后不限，两者都到了才开始采集。
- 错误：`process_name` 为空或 `window_class` 与 `title` 都是 `null` 时回参数无效（-32602）；`window` 在非 Windows 上（unsupported）或系统不支持 WGC 时回 -32003；假显示面按 `frame_ring.attach` 的规则核对，不符回 -32002。被拒绝的请求不改变当前的目标。

**`audio_ring.attach`**：主进程创建音频环并写好头部后发出；实时核打开共享内存，核对头部与参数一致后交给音频线程，块号从头部的 `write_seq` 续写，见[音频环](#音频环)。再次挂接即换成新的音频环。打不开或不符回 -32005。

| 字段 | 含义 |
| --- | --- |
| `name` | 共享内存名 |
| `size_bytes` | 共享内存的总字节数，不小于 `BLOCKS_OFFSET + block_count × block_stride` |
| `layout_version`、`block_count`、`block_stride`、`block_capacity`、`sample_rate`、`channels`、`sample_format` | 与音频环头部的同名字段相等 |

**`audio.set_target`**：`target` 是联合类型 `AudioTarget`，由主进程的音频配置给出（[音频环的挂接](#音频环的读写规则)）。

| 分支 | 字段 | 含义 |
| --- | --- | --- |
| `window`（`AudioWindowTarget`） | `process_name`、`window_class`、`title` | 窗口识别规则，含义与核对同 `capture.set_target` 的 `window`；采这个窗口所属进程及其子进程的声音，见[目标进程](realtime-core.md#目标进程) |
| `fake`（`FakeAudioTarget`） | `output` | 假音频源，只用于自动测试：假音频输出的共享内存名与布局参数，形式与核对同 `audio_ring.attach`，见[假音频源](realtime-core.md#假音频源) |

- 回应表示目标已核对并交给音频线程，不等音频源建立。找不到窗口或进程已退出不是错误：`core.health.audio` 为 `searching`，实时核定期重新识别（[音频主循环](realtime-core.md#音频主循环)）。
- 再次设定时先停掉旧的音频源。与 `audio_ring.attach` 的先后不限，两者都到了才开始采集。
- 错误：窗口识别规则同 `capture.set_target`，不全回 -32602；`window` 在非 Windows 上回 -32003（unsupported）；假音频输出按 `audio_ring.attach` 的规则核对，不符回 -32005。被拒绝的请求不改变当前的目标。

**控制产出者**：`Producer` 由身份 `id` 与代次 `generation` 组成。代次由主进程分配，在一个实时核的生命期内严格递增；实时核记住当前代次与已作废的最高代次。

- 心跳或动作块的代次不大于已作废的代次，或小于当前代次：拒绝。代次等于当前代次而身份不同：拒绝。
- 代次大于当前代次：先释放全部输入（`producer_changed`），再以它为当前代次。没有当前产出者时（启动后、当前代次被作废后）直接以它为当前代次，不需要释放。以动作块切换的新代次还没有心跳，心跳到达之前它的动作不发（权威检查的第二个条件）；主进程在一个代次的第一块之前先发一次心跳（[提交动作块](plugin-kernel.md#提交动作块)）。
- `producer.heartbeat` 的 `seq` 在同一代次内严格递增，不递增的心跳不算数；新代次的 `seq` 从它的第一次心跳算起。心跳超时即释放全部输入（`heartbeat_timeout`）并待命：产出者仍是当前的，同一代次 `seq` 递增的心跳到达后重新计时。
- `producer.revoke` 作废 `generation` 及更早的全部代次，之后这些代次的心跳与动作块都被拒绝；当前代次在其中时先释放全部输入（`generation_revoked`），此后没有当前产出者。实时核在释放完成后才回应，回应即这些代次的输入已释放的确认。

**`action.submit`** 的 params 见[动作块](#动作块)。params 解析不了（未知字段、类型或范围不对）回参数无效（-32602）；能解析的按动作块的规则校验，result 是联合类型 `ActionSubmitResult`：

| 分支 | 字段 | 含义 |
| --- | --- | --- |
| `accepted`（`Accepted`） | 无 | 已接受，交给调度线程按时间执行；回应时一步都还没执行 |
| `rejected`（`Rejected`） | `reason` | 按以下顺序核对：`stopping`（实时核正在停止）、`stale_generation`（代次早于当前或已作废）、`producer_mismatch`（代次相同、身份不同）、`invalid`（内容不合[动作块](#动作块)的规则）、`stale_seq`（序号不大于同一代次已接受的）、`expired`（有效截止时刻不晚于现在）。被拒绝的动作块不改变任何状态，其中任何动作都不执行 |
| | `message` | 给人看的说明，只用于日志与诊断 |

**`action.receipt`**：报告一块动作中一次性动作的结果，每个动作在一块中只报一次。每执行一步发一条，报这一步的动作；被取代时、释放结束整块时另发一条，报其余未执行的动作。`producer` 与 `block_seq` 指明动作块，`outcomes` 是联合类型 `ActionOutcome` 的列表：

| 分支 | 字段 | 含义 |
| --- | --- | --- |
| `executed`（`Executed`） | `id`、`at_ns` | 已交给输入后端及其时刻；不代表游戏中的效果已发生 |
| `dropped`（`Dropped`） | `id`、`reason` | 没有执行：`superseded`（新的动作块已取代本块未执行的部分）、`duplicate`（同一身份在本代次已执行过）、`released`（到时实时核已释放输入，原因见 `input.released`）、`geometry_changed`（带坐标的动作：基准帧之后客户区的尺寸或 DPI 变了，或基准帧的几何已查不到，见[坐标与几何](#坐标与几何)） |

**`input.released`**：实时核每次释放全部输入后发出，不论释放前是否持有输入。核连接断开时已无法发出，只记日志（以假后端运行时测试设备也能看到，见[测试设备连接](#测试设备连接)）；stdin 读到 EOF 时连接若还在就发出，随即以 0 退出（[生命线与 panic](realtime-core.md#生命线与-panic)）。

| 字段 | 含义 |
| --- | --- |
| `reason` | 见下表 |
| `producer` | 释放时的当前控制产出者；没有时为 `null` |
| `released_at_ns` | 释放完成的时刻 |

| 原因 | 何时 |
| --- | --- |
| `core_start` | 启动时先释放 |
| `generation_revoked` | 当前代次被 `producer.revoke` 作废 |
| `producer_changed` | 出现更高的代次，切换前先释放 |
| `heartbeat_timeout` | 当前产出者的心跳超时；权威检查时这一代次没有有效心跳（超时后待命，或还没有心跳）也是它 |
| `deadline_expired` | 到截止时刻没有新的动作块 |
| `frame_stale` | 最新写入帧的帧龄超过动作允许的年龄，或本实例还没有写过帧 |
| `lease_expired` | 输入许可过了到期时刻，没有续约 |
| `lease_revoked` | 输入许可被吊销，或许可源换发了更大的令牌 |
| `lease_source_lost` | 与许可源的连接断开（G0 是测试设备连接） |
| `no_lease` | 没有输入许可：从未授予，或没有许可源（没有输入后端时即如此） |
| `stop` | 收到 `core.stop` |
| `core_connection_lost` | 生命线：核连接断开。主进程收不到，见日志与测试设备 |
| `stdin_closed` | 生命线：stdin 读到 EOF 或读取失败 |

权威检查不成立时的原因按[权威规则](../architecture.md#权威)三个条件的顺序取第一个不成立的：许可（`lease_*`、`no_lease`）、产出者与心跳（`heartbeat_timeout`、`producer_changed`）、帧龄（`frame_stale`）。

**`core.health`**

| 字段 | 含义 |
| --- | --- |
| `now_ns` | 发出时实时核的时钟读数。实时核先读它再读其余字段，所以其余字段反映的状态不早于这一刻 |
| `capture` | `idle`（还没有挂接帧环或设定采集目标）、`searching`（没有采集源：找不到目标窗口或采集源已失效，正在定期重新识别；没有帧）、`capturing`（有采集源；最小化等可恢复的情形下暂时没有新帧）。转换见[采集主循环](realtime-core.md#采集主循环) |
| `inputs_held` | 当前是否持有输入 |
| `producer` | 当前控制产出者；没有时为 `null` |
| `latest_frame` | 本实例最近写进帧环的一帧（`LatestFrame`）：`frame_number` 为它在帧环中的帧号，`capture_ns` 为采集时刻；本实例还没写过帧时为 `null`。它不前进即帧龄在增长，见[帧龄的依据](realtime-core.md#采集主循环) |
| `audio` | 音频的采集状态，取值同 `capture`：`idle`（还没有挂接音频环或设定音频目标）、`searching`（没有音频源：找不到目标窗口、目标进程已退出或音频源已失效，正在定期重新识别）、`capturing`（有音频源）。转换见[音频主循环](realtime-core.md#音频主循环) |
| `latest_audio` | 本实例最近写进音频环的一块（`LatestAudio`）：`block_number` 为块号，`capture_ns` 为它第一个采样点的采集时刻；本实例还没写过块时为 `null`。只用于诊断与测试，实时核的任何决定都不依据它 |
| `lease` | 输入许可的状态（`LeaseStatus`）：`not_granted`、`valid`、`expired`、`revoked`、`source_lost`，含义见[输入许可](#输入许可)。只报状态，不报令牌；只用于诊断与测试，主进程不据它做任何决定 |

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
- **持续状态**（按住的键、轴）每块完整给出，缺项按释放处理，不沿用旧值。同一步中同一键、鼠标键或轴只能出现一次，轴的取值在 [-1, 1]。
- **一次性动作**（点击、短按、滚动、相对位移）按身份在同一代次内去重：已执行过的身份回执 `duplicate`，重传不重复执行、不延长持续时间；被取代或因释放而没执行的不算执行过。块内身份不得重复。
- 新接受的动作块从它的起始时刻起取代上一块未执行的部分（回执 `superseded`），上一块此前的步照常执行；上一块的截止时刻早于新块的起始时刻时，上一块的持续状态在它的截止时刻抬起（不算释放全部，不发 `input.released`）。到达时时刻已过的步立即执行；截止时刻之后不再执行任何步。
- 到截止时刻没有新的动作块：释放全部输入（`deadline_expired`），这一块结束。
- 权威检查不成立而释放时，这一块随之结束：其余动作回执 `released`，之后的步不再执行，要继续须提交新的动作块。
- 不合以上规则、步的时刻晚于截止时刻、`start_ns + offset_ns` 溢出或没有步的动作块整块拒绝（`invalid`）。
- 剧情游戏的动作块只含一次点击或短按，没有持续状态；即时游戏的动作块是按固定频率排列的完整控制序列。

执行的时机、权威检查与持键账目见[动作块的执行](realtime-core.md#动作块的执行)。

### 坐标与几何

- 带坐标的动作（`click`、`scroll`）的坐标按基准帧的客户区解释。实时核在执行它之前核对：基准帧之后客户区的尺寸或 DPI 变了，这个动作不执行，回执 `dropped(geometry_changed)`；块里其余不带坐标的动作照常。这不是权威检查的失败，不释放输入。
- 只移动窗口（客户区原点变了、尺寸与 DPI 不变）不影响客户区坐标，照常执行；真实后端注入时按当时的客户区原点换算（G2）。
- 实时核记下本实例在当前帧环写出的帧的几何（尺寸与 DPI）的变化点，至多 16 次。基准帧的几何查不到时按变了处理：基准帧早于本实例在当前帧环写出的第一帧（旧实例写的帧、挂接之前的帧）、晚于最近写入的一帧，或早于记下的最早一次变化。实时核重启或再次挂接帧环之后，以旧帧为基准帧的坐标动作因此不执行，产出者须按新实例写出的帧重新决定。
- 帧环的槽头部不带 DPI；几何由实时核在写帧时从采集源取得，主进程看不到 DPI。

## 输入许可

许可的规则取自 Bellis D-31、D-34（[技术基线](../plan/tech-baseline.md)的继承表）：令牌只增不减，实时核只认等于当前令牌且未过期的许可。许可由许可源直接交给实时核，不经主进程（[总体架构](../architecture.md#进程与连接)）。

| 许可源 | 阶段 | 连接 |
| --- | --- | --- |
| 测试许可源，只用于测试 | G0 | [测试设备连接](#测试设备连接)，只能与假输入后端一起启用 |
| 独立运行的操作员入口与测试许可 | G2 | 形式另行登记批准，这里不定 |
| Bellis 安全核的 `game.input` 许可 | G3 | 控制连接（[游戏桥](game-bridge.md#连接)），消息是 Bellis 的 `lease.renew`，契约的源在 Bellis |

实时核持有的许可是一个状态机：

| 事件 | 结果 |
| --- | --- |
| 授予：令牌大于当前令牌 | 有效，到期时刻为给出的值。此前有有效的许可时先释放它负责的全部输入（`lease_revoked`） |
| 授予：令牌等于有效许可的令牌 | 续约，到期时刻换成给出的值（也可以更早） |
| 授予：令牌小于当前令牌、已被吊销、等于已过期的令牌，或到期时刻不晚于现在 | 拒绝（-32004），许可不变：过期的令牌不能续约，须换发更大的令牌 |
| 到了到期时刻 | 过期，释放全部输入（`lease_expired`）。调度线程按到期时刻定时，不等下一个动作 |
| 吊销：令牌不小于有效许可的令牌 | 吊销，释放全部输入（`lease_revoked`）；此后这个令牌及更早的令牌都不能再授予 |
| 与许可源的连接断开 | 此后没有许可，释放全部输入（`lease_source_lost`） |
| 没有许可源（没有输入后端） | 从无许可（`no_lease`），不发任何输入 |

## 测试设备连接

只用于测试。实时核以假输入后端（`--input-backend fake`）启动时，另作为 `bellis-channel/1` 客户端连到测试一方的服务端；生产中没有这条连接。实现见[假输入后端与测试设备连接](realtime-core.md#假输入后端与测试设备连接)，测试一方是 `game_runtime.core_link.fake_device.FakeDevice`。

- **为什么不经主进程**：主进程只能让输入停下，不能让输入继续，许可不能来自主进程；主进程崩溃时测试一方仍能看到实时核是否发出了抬起与释放全部，这是“主进程崩溃即释放”唯一可观察的办法；拓扑与生产时“许可来自另一方（Bellis 控制连接）”一致。
- **凭据**：像核连接一样由环境变量交付，另用一组名字：`GAME_RUNTIME_TEST_DEVICE_PATH`（地址）、`GAME_RUNTIME_TEST_DEVICE_KEY`（base64url 密钥）；角色与核连接相同，为 `game-runtime.core`。主进程一侧由 `CoreLinkOptions.test_device` 给出（只用于测试）。
- **顺序**：实时核先连上测试设备并开始处理它的请求，再释放全部输入（`core_start`，测试设备随即收到 `release_all(core_start)`），再连接主进程。连不上测试设备即以 1 退出。连接断开后不重连：此后没有许可，不发任何输入。
- **门禁**：`--input-backend` 在 G0 只有 `fake` 一个取值，其它取值（真实后端）unsupported；给了测试设备的凭据却没有 `--input-backend fake`、或 `--input-backend fake` 却没有凭据，都以参数不合法（2）退出。测试许可源只在假后端的代码里构造，没有路径能让它与其它后端一起出现。
- 这些消息不是游戏桥控制连接的契约，也不冒用它的消息名（那份契约的源在 Bellis，GD-03）。

| 方法 | 方向 | 类型 | params → result | 用途 |
| --- | --- | --- | --- | --- |
| `test_lease.grant` | 测试一方 → 实时核 | 请求 | `FakeLeaseGrantParams` → `Empty` | 授予或续约许可：`token`、`expires_ns`（与帧环同一时钟）。不被接受时回 -32004 |
| `test_lease.revoke` | 测试一方 → 实时核 | 请求 | `FakeLeaseRevokeParams` → `Empty` | 吊销 `token` 及更早的令牌；释放完成后才回应 |
| `fake_input.event` | 实时核 → 测试一方 | 通知 | `FakeInputEventParams` | 假输入后端的一个事件：`seq`（实例内从 1 起逐个加一）、`at_ns`（交给假后端的时刻）、`event` |

`event` 是联合类型 `FakeInputEvent`：

| 分支 | 字段 | 何时 |
| --- | --- | --- |
| `key_down`、`key_up` | `scan_code` | 持续状态里的键按下、抬起 |
| `mouse_down`、`mouse_up` | `button` | 持续状态里的鼠标键按下、抬起 |
| `axis_set`、`axis_release` | `axis`，`axis_set` 另有 `value` | 轴设为某个取值（含改变取值）、释放 |
| `click`、`key_press`、`scroll`、`mouse_move` | 同动作块里的同名动作，含 `id` | 执行一次性动作 |
| `release_all` | `reason`（释放的原因，取值同 `input.released`） | 释放全部输入：此前按住的每一项都已逐一发出抬起，这一条随后发出；账目为空时也发 |

事件经发送队列写给测试一方，与核连接上的回执、`input.released` 走两条连接，先后不定。实时核退出前至多等 500 ms 把队列里的事件写完。

## 帧环与音频环

两者都是主进程创建的共享内存环形缓冲，实时核连接并写入（非提权一方创建全部进程间对象，GD-02）。

| 环 | 每项内容 |
| --- | --- |
| 帧环 | 帧（BGRA8，客户区）、帧号、采集时刻（QPC）、窗口与 DPI 变换版本 |
| 音频环 | 块：游戏进程的一段连续 PCM（F32，各声道交错）、块号、第一个采样点的采集时刻（QPC，与帧环同一时钟）、不连续标志 |

头部是二进制布局，以 Rust 的 `#[repr(C)]` 结构为源（`core_protocol::frame_ring`、`core_protocol::audio_ring`），由 Rust 导出字段偏移表 `protocol/frame-ring-layout.json`、`protocol/audio-ring-layout.json`；Python 的 `core_protocol.frame_ring`、`core_protocol.audio_ring` 手写同一布局，由测试与偏移表逐项核对；偏移表不一致时 `uv run check` 失败。

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

| 实现 | 位置 | 做什么 |
| --- | --- | --- |
| 主进程（Python） | `runtime/src/game_runtime/frame_ring/` | 创建并写好头部、给出 `frame_ring.attach` 的参数、读最新一帧、算帧龄、销毁；由 `CoreLink` 持有并挂接（见下文） |
| 实时核（Rust） | `core/crates/frame-ring` | 打开与核对、写帧；其中的读取方只供[假采集源](realtime-core.md#假采集源)与测试使用。实时核的采集线程是唯一的写方，见[采集主循环](realtime-core.md#采集主循环) |

#### 读写规则

单写者多读者的 seqlock。写方只有实时核一个：主进程同一时刻只把帧环挂接给一个实时核，实时核重启时等旧进程退出后再挂接新的。主进程一侧的实现见下文“挂接”。

- **写一帧**：把槽的 `seq` 置为下一个奇数（偶数加一，崩溃后遗留的奇数加二），随后一道释放栅栏；写槽头部其余字段与像素，像素去掉行尾的填充，槽的行跨度为 `width × 4`；把 `seq` 加一成为偶数（释放）；最后把 `write_seq` 置为该帧号（释放）。比最大尺寸大或本身不合规则（帧号为 0、宽或高为 0、行跨度小于 `width × 4`、像素不足）的帧不写，帧环不受影响。
- **读最新一帧**：读 `write_seq`（获取）得到帧号，为 0 即还没有帧；读该帧槽的 `seq`（获取），为奇数即撕裂；复制槽头部，`frame_number` 不是所求帧号即撕裂；槽头部不合布局（尺寸超过最大值、行跨度小于 `width × 4` 或像素放不进槽、像素格式不是 BGRA8）时重读 `seq`，变了算撕裂，没变即报帧环损坏；复制像素，一道获取栅栏后重读 `seq`，与先前不等即撕裂。撕裂就从读 `write_seq` 起重试，有上限（主进程缺省 8 次），都撕裂时这次读取失败，稍后再读。
- **帧号**：帧号 = 续写基数 + 采集源的帧号。挂接时续写基数取头部的 `write_seq`，所以实时核重启后帧号续写、不回退；采集源的帧号不再增大（新建了采集源，帧号从 1 重新数起）时，续写基数改为已写出的最大帧号。同一采集源相邻两帧的帧号之差减一仍是其间丢弃的帧数（[采集源接口](realtime-core.md#采集源接口)）。客户区原点与 DPI 不进帧环。

**挂接。** 主进程一侧由 `CoreLink`（`runtime/src/game_runtime/core_link/`）实现：

- **配置**：`CoreLinkOptions.capture` 是 `CaptureConfig`（`core_link/capture.py`）：`target` 为 `WindowRule`（`process_name` 加 `window_class` 或 `title`，都不能为空串）或只用于测试的假显示面（`FakeDisplay.target`）；另有帧环的最大尺寸（缺省 1920×1080）与槽数（缺省 3）。窗口比帧环大时帧写不进帧环，实时核记录错误。没有配置时不建帧环，也不设定采集目标。
- **顺序**：第一次 `start` 创建帧环；每个实例收到 `core_start` 之后先发 `frame_ring.attach`，再发 `capture.set_target`，然后才开始发心跳。任一请求被拒绝即按停止顺序终止该实例，`start` 报 `CoreStartError`，帧环保留到 `stop`。
- **单写方**：`CoreLink` 同一时刻只有一个实例，上一个实例的进程退出之后才能再 `start`，所以挂接新实例时旧的写方已不在。实时核崩溃或被杀时帧环保留，重启后挂接同一个帧环，帧号续写。
- **读帧**：宿主代码经 `CoreLink.frames` 读帧、算帧龄；帧环归 `CoreLink` 所有，宿主不关闭它。插件经服务读帧等用到时再做。
- **销毁**：`stop` 在实时核退出后销毁帧环，强制终止后仍未确认退出时保留。

#### 内存序与平台假设

- **实时核**对共享内存的每一次访问都是原子操作：`seq` 与 `write_seq` 用 Release/Acquire，其余字段与像素用 Relaxed，像素按对齐情况拆成 8 字节与单字节的访问。读写双方都在 Rust 里时（同进程的并发测试、假采集源）也没有数据竞争。
- **主进程**没有原子操作：`write_seq` 与 `seq` 经 ctypes 的 `c_uint64` 字段读写，CPython 以 8 字节的 memcpy 实现，假设它在 x86-64 上编译为单条访存。这两个字段 8 字节对齐；x86-64 上对齐的 8 字节访存是原子的，读不与更早的读重排、写不与更早的写重排（x86-TSO），所以 Python 一方不需要显式的栅栏。只用于测试的 Python 写入方（[假显示面](realtime-core.md#假采集源)）依赖同一假设。
- 生产平台只有 Windows x64，检查用的 Linux 容器是 x86-64；其它架构上主进程创建帧环即报错。

#### 创建、命名与销毁

主进程创建共享内存并写好头部（`write_seq` 为 0），以 `frame_ring.attach` 把参数交给实时核。实时核打开同名共享内存，核对参数本身合布局规则（布局版本、至少 2 个槽、槽跨度对齐且放得下最大的帧、`size_bytes` 放得下全部槽）、共享内存不小于 `size_bytes`、头部与参数逐项相等，任何一项不成立都以 -32002 回错。

| 项 | Windows | 其它平台 |
| --- | --- | --- |
| 对象 | 以页面文件为后备的命名文件映射 | POSIX 共享内存 |
| 名字 | `Local\bellis-frames-<128 位随机数的 base32>`（小写、无填充），在本会话的命名空间里 | `/bellis-frames-<同上>` |
| 创建 | ctypes 调 `CreateFileMappingW`，DACL 同本机通道的管道，为 `D:P(A;;GA;;;<当前用户 SID>)`；同名对象已存在即失败，不换名重试 | `shm_open(O_CREAT \| O_EXCL)`；权限位 0600 只是缺省值，不作安全判断；已存在即失败 |
| 实时核打开 | `OpenFileMappingW` 与 `MapViewOfFile`（读写），映射后即关闭句柄 | `shm_open(O_RDWR)`，`fstat` 核对大小，`mmap(MAP_SHARED)` |
| 销毁 | 主进程解除映射、关闭句柄；实时核也关闭后对象消失 | 主进程解除映射并 `shm_unlink`；实时核已有的映射在解除前仍有效 |

- `name` 字段就是交给 `OpenFileMappingW` 或 `shm_open` 的字符串，不再加工。
- Python 标准库的 `mmap` 与 `multiprocessing.shared_memory` 设不了安全属性，所以 Windows 上用 ctypes 创建，视图仍由 `mmap` 按名字打开，借它管理映射的生命期。POSIX 上不用 `multiprocessing.shared_memory`：它在 Python 3.12 里会把共享内存登记到另起的 resource tracker 进程。
- 销毁由主进程负责，在实时核停止之后（`CoreLink.stop`，见[读写规则](#读写规则)）。主进程崩溃时，Windows 上对象随进程的句柄关闭而消失；POSIX 上名字留到重启，生产平台不受影响。
- 跨完整性级别（非提权主进程创建、提权实时核打开与写入）的可达性经 [GS-03](../plan/spikes.md) 实测成立（2026-10-31）。这些设置只减少暴露，同一用户下的恶意进程不在威胁模型内（Bellis D-12）。

#### 帧龄

帧龄 = 现在 − 采集时刻，现在取 `time.perf_counter_ns()`，与采集时刻同一时钟（[时钟](realtime-core.md#时钟)）；负值按 0，因为 WGC 的采集时刻可能比帧交出的时刻晚几毫秒。主进程以 `frame_age_ns` 计算。

#### 帧环的测试

| 层 | 内容 |
| --- | --- |
| Rust 单元 | 测试自建共享内存并写好头部：参数与头部逐项核对、共享内存缺失或太小、写读往返与去掉行尾填充、拒绝过大与不合规则的帧、帧号的空缺保留与重启续写及新采集源、崩溃遗留的奇数 `seq`、撕裂与损坏的槽、写方高速写读方并发读、假采集源 |
| Python | 创建与命名、读的重试与放弃（`seq` 为奇数、拷贝期间 `seq` 变化、槽里是别的帧）、损坏的槽、帧龄、销毁、同名已存在、Windows 上的 DACL |
| 互通 | pytest 编译并拉起 Rust 示例 `ring_interop`：Python 创建、Rust 写、Python 读，重启后帧号续写；头部与参数不符时 Rust 拒绝；Rust 高速写时 Python 并发读、Python 高速写假显示面时 Rust 的假采集源并发读，读到的帧都完整一致；合成游戏的帧经假采集源进入帧环 |
| 经实时核 | 真实的 `realtime-core` 上的挂接、设定目标、错误码，以及 `CoreLink` 的挂接顺序、重启续写与销毁，见[画面采集](realtime-core.md#画面采集)一节的验证 |

Windows 本机跑命名文件映射，Linux 容器跑 POSIX 共享内存。

### 音频环

共享内存的起点是环头部，从 `BLOCKS_OFFSET` 起是 `block_count` 个跨度为 `block_stride` 的块槽；每个块槽的起点是块头部，从 `BLOCK_SAMPLES_OFFSET` 起是样本值。一块是一段连续的采样点，**采样点**指同一时刻各声道的样本值，F32 时占 `channels × 4` 字节。整数与样本值按本机字节序，结构没有隐式填充，Windows 与 Linux 上布局相同。内存序、平台假设与共享内存的创建、命名、销毁都与帧环相同（[内存序与平台假设](#内存序与平台假设)、[创建、命名与销毁](#创建命名与销毁)），名字中的用途为 `audio`（如 `Local\bellis-audio-<随机数>`），参数不符回 -32005。

| 常量 | 值 | 含义 |
| --- | --- | --- |
| `MAGIC` | 小端读出的 `b"BGRAUDIO"` | 环头部的标识 |
| `LAYOUT_VERSION` | 1 | 头部、块头部或这些常量变化时加一 |
| `BLOCKS_OFFSET` | 64 | 块槽 0 的偏移 |
| `BLOCK_SAMPLES_OFFSET` | 64 | 样本值相对块槽起点的偏移 |
| `BLOCK_ALIGN` | 64 | 块槽跨度的对齐 |
| `SAMPLE_FORMAT_F32` | 1 | 每个样本值是 32 位 IEEE 浮点，各声道交错 |
| `BYTES_PER_VALUE` | 4 | 一个 F32 样本值的字节数 |
| `MAX_CHANNELS` | 2 | 声道数的上限 |
| `MIN_SAMPLE_RATE`、`MAX_SAMPLE_RATE` | 8000、192000 | 采样率（Hz）的范围 |
| `FLAG_DISCONTINUITY` | 1 | 块标志：这一块与上一块之间不连续 |
| `KNOWN_FLAGS` | 1 | 已定义的全部块标志，其余位须为 0 |

环头部 `AudioRingHeader`（48 字节）：主进程创建时写入除 `write_seq` 外的全部字段，此后不变。

| 偏移 | 字段 | 类型 | 含义 |
| --- | --- | --- | --- |
| 0 | `magic` | u64 | `MAGIC` |
| 8 | `layout_version` | u32 | `LAYOUT_VERSION` |
| 12 | `block_count` | u32 | 块槽数，至少 2 |
| 16 | `block_stride` | u64 | 相邻两块槽起点之间的字节数，是 `BLOCK_ALIGN` 的倍数，不小于 `BLOCK_SAMPLES_OFFSET + block_capacity × channels × 4` |
| 24 | `block_capacity` | u32 | 一块最多的采样点数，至少 1 |
| 28 | `sample_rate` | u32 | 采样率（Hz），在 `MIN_SAMPLE_RATE` 到 `MAX_SAMPLE_RATE` 之间 |
| 32 | `channels` | u32 | 声道数，1 到 `MAX_CHANNELS` |
| 36 | `sample_format` | u32 | `SAMPLE_FORMAT_F32` |
| 40 | `write_seq` | 原子 u64 | 最新一块的块号，0 表示还没有块；只由实时核写 |

块头部 `AudioBlockHeader`（32 字节）：只由实时核写。

| 偏移 | 字段 | 类型 | 含义 |
| --- | --- | --- | --- |
| 0 | `seq` | 原子 u64 | seqlock 序号：奇数表示正在写，偶数表示稳定 |
| 8 | `block_number` | u64 | 块号，从 1 起在本音频环内每块加一；块槽号为 `(block_number − 1) mod block_count` |
| 16 | `capture_ns` | u64 | 第一个采样点的采集时刻，与核协议的 `_ns` 时刻同一时钟；第 i 个采样点的采集时刻为 `capture_ns + i × 10⁹ / sample_rate` |
| 24 | `sample_count` | u32 | 采样点数，1 到 `block_capacity`；样本值占 `sample_count × channels × 4` 字节 |
| 28 | `flags` | u32 | 块标志，目前只有 `FLAG_DISCONTINUITY`，其余位为 0 |

| 实现 | 位置 | 做什么 |
| --- | --- | --- |
| 主进程（Python） | `runtime/src/game_runtime/audio_ring/` | 创建并写好头部、给出 `audio_ring.attach` 的参数、读最近一段、销毁；由 `CoreLink` 持有并挂接（见下文） |
| 实时核（Rust） | `core/crates/audio-ring` | 打开与核对、写块；其中的读取方只供[假音频源](realtime-core.md#假音频源)与测试使用。实时核的音频线程是唯一的写方，见[音频主循环](realtime-core.md#音频主循环) |

#### 音频环的读写规则

单写者多读者的 seqlock，规则与帧环相同，以块代替帧：

- **写一块**：把块槽的 `seq` 置为下一个奇数（偶数加一，崩溃后遗留的奇数加二），随后一道释放栅栏；写块头部其余字段与样本值；把 `seq` 加一成为偶数（释放）；最后把 `write_seq` 置为该块号（释放）。
- **块号**：挂接时从头部的 `write_seq` 续写，每块加一，所以实时核重启后块号不回退。
- **拆包**：音频源交出的一个包（[音频源接口](realtime-core.md#音频源接口)）超过 `block_capacity` 时拆成连续的几块，后面各块的采集时刻按采样率从包的采集时刻推算（向下取整到纳秒），不连续标志只在第一块上。采样点数为 0 或字节数不是 `sample_count × channels × 4` 的包不写，音频环不受影响。
- **不连续标志**：这一块与上一块之间丢了声音或时间轴断开：本实例挂接音频环、建立新的音频源或丢弃了写不进的包之后的第一块、WASAPI 报告数据不连续、[假音频源](realtime-core.md#假音频源)读得太慢而跳过了块。读方据此重置依赖连续声音的状态。
- **读一块**（块号 n）：读块槽的 `seq`（获取），为奇数即撕裂；复制块头部，`block_number` 不是 n 即这一块已被覆盖；块头部不合布局（`sample_count` 为 0 或超过 `block_capacity`、`flags` 有未定义的位）时重读 `seq`，变了算撕裂，没变即报音频环损坏；复制样本值，一道获取栅栏后重读 `seq`，与先前不等即撕裂。
- **读最近一段**：读 `write_seq`（获取）得到最新的块号 W，为 0 即还没有块；从 W 起往前逐块读，直到读到的总时长不少于所求、遇到块号不大于调用方给的 `after` 的块、块号早于 `W − block_count + 1`，或某一块撕裂或已被覆盖。W 本身撕裂或已被覆盖（写方在读的期间绕了整整一圈）时从读 `write_seq` 起重试，有上限（主进程缺省 8 次），都失败时这次读取失败；更早的块撕裂或已被覆盖只说明写方已追上它，返回比它新的那些块。

**挂接与读取。** 主进程一侧由 `CoreLink`（`runtime/src/game_runtime/core_link/`）实现，单写方、重启与销毁的规则与帧环相同（[读写规则](#读写规则)中的“挂接”）：

- **配置**：`CoreLinkOptions.audio` 是 `AudioConfig`（`core_link/audio.py`）：`target` 为 `WindowRule`（与画面采集同一条窗口识别规则）或只用于测试的假音频输出（`FakeAudioOutput.target`）；格式缺省为 48000 Hz、2 声道；块容量缺省 960 个采样点（48 kHz 时 20 ms，放得下一个 10 ms 的包），块槽数缺省 400（包为 10 ms 时约 4 秒）。没有配置时不建音频环，也不设定音频目标。
- **顺序**：第一次 `start` 创建音频环；每个实例挂接帧环与设定采集目标之后（配置了采集时），发 `audio_ring.attach`，再发 `audio.set_target`，然后才开始发心跳。任一请求被拒绝即按停止顺序终止该实例，`start` 报 `CoreStartError`，音频环保留到 `stop`。
- **读取**：`read_recent(duration_ns, *, after=0)` 按上面的规则读最近一段，返回 `AudioRead`：`sample_rate`、`channels` 与按块号从小到大的 `blocks`；每个 `AudioBlock` 有 `block_number`、`capture_ns`、`sample_count`、`discontinuity` 与 `samples`（F32 交错的字节）。持续读取的一方把上次取到的最大块号作为 `after`；返回的第一块的块号大于 `after + 1` 即其间的块已被覆盖。宿主代码经 `CoreLink.audio`（`AudioRing`）读；插件经 `ctx.audio` 读（权限 `audio.read`，[注册](plugin-kernel.md#注册)），宿主以 `CoreLink.audio_reader` 交给内核，没有音频环时（没有配置音频，或第一次 `start` 之前、`stop` 之后）它返回 `None`。
- **销毁**：`stop` 在实时核退出后销毁音频环，强制终止后仍未确认退出时保留。

#### 音频环的测试

| 层 | 内容 |
| --- | --- |
| Rust 单元 | 测试自建共享内存并写好头部：参数与头部逐项核对、共享内存缺失或太小、写读往返、拆包与推算的采集时刻、拒绝不合规则的包、块号续写、崩溃遗留的奇数 `seq`、撕裂、损坏与已被覆盖的块、写方高速写读方并发读、假音频源（只交出打开之后写入的块、按块号逐块交出、跳过被覆盖的块时标不连续、损坏的块） |
| Python | 创建与命名、读最近一段（时长、`after`、被覆盖的旧块、撕裂的重试与放弃、损坏的块）、假音频输出、销毁 |
| 互通 | pytest 编译并拉起 Rust 示例 `audio_ring_interop`：Python 创建、Rust 写、Python 读，重启后块号续写；头部与参数不符时 Rust 拒绝；Rust 高速写时 Python 并发读、Python 高速写假音频输出时 Rust 的假音频源并发读，读到的块都完整一致、按块号连续 |
| 经实时核 | 真实的 `realtime-core` 上的挂接、设定音频目标、错误码、假音频输出与音频环格式不同时不写块、`core.health` 的音频状态，以及 `CoreLink` 的挂接顺序、重启续写与销毁，见[音频的验证](realtime-core.md#音频的验证) |

Windows 本机跑命名文件映射，Linux 容器跑 POSIX 共享内存。

## 生成与核对

| 生成物（`protocol/` 下） | 来源 |
| --- | --- |
| `schema/core-protocol.schema.json` | schemars 从 Rust 类型生成；`x-methods`、`x-test-device-methods`、`x-protocol-version`、`x-error-codes` 来自 `core_protocol::methods` 与协议版本常量 |
| `frame-ring-layout.json`、`audio-ring-layout.json` | Rust 以 `std::mem::offset_of!` 导出的帧环与音频环的常量与各结构的大小、对齐、字段偏移、大小与类型 |
| `fixtures/<方法名>/params[.<用例>].json`、`result[.<用例>].json` | Rust 从类型的值序列化出的示例消息，两组的每个方法的 params 与 result 至少一条，每个联合类型的每个分支至少出现一次 |
| `src/core_protocol/models.py` | datamodel-code-generator 从 Schema 生成的 Pydantic 模型 |
| `src/core_protocol/methods.py` | 从 Schema 的 `x-methods` 与 `x-test-device-methods` 生成的两组方法目录（`METHODS`、`TEST_DEVICE_METHODS`）、协议版本与自定义错误码 |

- 重新生成用 `uv run generate`：先运行 Rust 的 `core-protocol-export` 写出 Schema、偏移表与示例消息，再运行 datamodel-code-generator，两份 Python 文件经仓库的 ruff 配置修正与格式化，然后写回 `protocol/`，删除不再生成的示例消息。改了 `core/crates/core-protocol` 后运行它，生成物与源放在同一个提交里。
- `uv run check` 的生成物核对把全部生成物重新生成到临时目录，与入库的逐字节比较，缺少、多余或内容不同都失败，并提示运行 `uv run generate`。
- 生成是确定的：不含时间戳与路径，键按声明顺序，换行一律 LF，Windows 与 Linux 上的结果逐字节相同。
- 联合类型：schemars 把每个分支内联为带 `type` 常量的对象；分支结构体以 `#[schemars(title = …)]` 写明自己的名字，生成的 Pydantic 类与 Rust 同名；导出时给以 `type` 为标签的 `oneOf` 加上 `discriminator`，datamodel-code-generator 据此生成按 `type` 选分支的 Pydantic 判别联合。整数补上 Rust 类型的上下界，`additionalProperties: false` 生成为 `extra="forbid"`。
- 测试：Rust 与 Python 各自解析每条示例消息并往返序列化，结果与原值相等；在每个对象节点加一个未知字段都被拒绝；`type` 标签选出联合类型的分支，未知标签被拒绝。Python 的帧环与音频环布局与各自的偏移表逐项核对；Rust 侧核对偏移表列出了每个字段且没有填充。

## 验收

| 阶段 | 内容 |
| --- | --- |
| G0 | 生成物过期与偏移表不一致时检查失败；本机通道未认证即拒绝，篡改、重放、超长帧与超时都被拒绝；Python 与 Rust 互通，Windows 真机跑命名管道，Linux 容器跑 UDS；帧环由主进程创建、实时核写入、主进程读出，头部与参数不符时拒绝，并发读写不交出撕裂的帧；动作块的校验、调度与权威检查在假输入后端上经测试设备连接验证（[验证](realtime-core.md#动作块的验证)） |
| G1 | 音频环由主进程创建、实时核写入、主进程读出，头部与参数不符时拒绝，并发读写不交出撕裂的块；假音频源的声音经实时核进入音频环（[音频采集](realtime-core.md#音频采集)的验证）；WASAPI 进程回环的真机核对见同一节 |
| G3 | 与 Bellis 宿主真机互通 |

## 技术路线

已批准：GD-01、GD-02、GR-03、GR-28；继承 Bellis D-12、D-21、R-05。
