# 游戏桥（Game Runtime 侧）

Game Runtime 与 Bellis 之间的连接、消息与行为。Bellis 侧的游戏桥插件见 [Bellis 游戏桥](https://github.com/leafliber/bellis/blob/main/docs/modules/game-bridge.md)；需要 Bellis 配合的契约改动见 [Bellis P3 契约提案](../plan/bellis-p3-proposals.md)。G3 与 Bellis P3 同步实现；G0–G2 以独立运行开发与验收。

## 职责

- 维护语义连接与控制连接，完成认证与握手。
- 把局面、决策点与候选、已发生的动作与健康报给 Bellis。
- 执行 Bellis 的目标、决定、停止与暂缓推进。
- 按 GR-38 维护授权任务预算与动作尝试。
- 不接 Bellis 时，提供独立运行的操作员入口。

## 不负责

- **选择与解说。** 由 Bellis 决策模型负责。
- **长期记忆。** Game Runtime 不直连 Iris；需要写入的游戏事实交给 Bellis，经其记忆插件与隐私网关写入，且只能是摘要。

## 连接

（GR-05）两条连接都以 Bellis 宿主为服务端，走 `bellis-channel/1`（[本机通道](core-protocol.md#本机通道)）。

| 连接 | 客户端 | 承载 |
| --- | --- | --- |
| 语义连接 | 主进程，角色 `game-runtime.main` | 目标、局面、决策点与候选、暂缓推进、已发生的动作、健康 |
| 控制连接 | 实时核，角色 `game-runtime.core` | 只有输入许可续约、停止、输入已释放与实时核健康，其它消息一律拒绝 |

- 实时核直接从 Bellis 取得令牌与到期时间并自行核对，许可不经主进程转发；主进程只能让输入停下，不能让输入继续。
- 控制连接不在时，Bellis 游戏桥插件不申请也不续约 `game.input`。
- 主进程与实时核都从 SecretStore 读取 Bellis 本次会话的连接信息（Bellis R-23）。

### Bellis 启动入口

`uv run supervise <适配包 id> --profile <文件> --bellis --real-input` 选择 Bellis 许可源。操作员核对适配包前置条件后，以可重复的 `--confirm-precondition <id>` 确认本次运行的对应条目。启动器读取本次宿主会话，并为主进程与实时核生成同一个 `runtimeId`。

普通插件 `game-bridge` 注册语义 RPC 和上报任务。兼容的 `game.describe` 完成之后接受命令。断链时撤掉语义授权并让控制产出者走失效释放，同一宿主会话重连后等待新授权。凭据消失或换代时结束本次运行。

### 实时核控制连接

- 启动实时核时，主进程以 `--bellis-control <runtimeId>` 选择 Bellis，并设置 `BELLIS_SESSION_ID` 为自己读到的 Bellis 会话号。同一次 Runtime 内的实时核重启继续使用相同的 `runtimeId`，供 Bellis 匹配语义与控制连接。实时核独立读取 Windows 凭据 `session.bellis`，其值是 UTF-16LE 编码的 `SessionInfo`，字段为 `v`、`sessionId`、`channelPath`、`channelKey`、`launcherPid`、`startedAt`。这里的地址与密钥只在凭据管理器交付。操作员许可源和 Bellis 只选一个。[Bellis 会话契约](https://github.com/leafliber/bellis/blob/main/packages/protocol/src/process.ts)
- 实时核先发起本机通道认证，再以原始 JSON 接收 `game.describe`，应答该 Runtime 的会话号、主版本与系统单调时刻。写出兼容应答后才接收许可并以核健康周期上报 `health.reported`。续约把 Bellis 给出的绝对截止换算为纳秒，过期或失效的令牌回 `-32004`。断线或凭据变化时释放全部输入并锁存 `lease_source_lost`，在本实时核实例内保持停止。
- 控制连接每帧发送有 200 ms 的写入时限。未能写完时关闭连接，按许可源失效释放。真实后端换令在核验旧许可的释放后复查会话信息，再确认新许可。[输入许可](core-protocol.md#输入许可)
- `input.stop` 吊销截至 `throughToken` 的许可。当前有效令牌高于该上界时只更新撤销水位。其余情形创建新的释放编号并尝试释放，随后应答 `{}`，即使输入此前已经释放。此次 `input.released` 以 `throughToken` 关联，零对应 `null`。其它释放使用产生该释放时的相关许可令牌。
- 释放事实保留对应释放请求完成的单调时刻。实时核按本实例的 `release_id` 匹配物理核验，限时等候后上报一次。只有对应核验成功才报 `verified`，最终仍有残留或补发被拒报 `residual`，其余结果及假后端报 `unverified`。[核内释放与核验](core-protocol.md#消息集)
- 进程联调以 `GAME_RUNTIME_TEST_BELLIS_SESSION_FILE` 提供临时 `SessionInfo`，仅在显式选用 Bellis、假后端与测试设备时可用。该测试设备只接收假输入事件。

## 契约

- 以 Bellis `packages/protocol`（TypeBox，Bellis D-21）为唯一的源（GD-03）。本仓库在 `vendor/bellis-protocol/` 锁定某个 Bellis 提交的生成物（JSON Schema 与 Pydantic 模型），附来源提交与摘要；`uv run check` 核对锁定副本未被改动。
- 改动先在 Bellis 合入，本仓库再更新锁定的生成物；两边的检查各自校验。
- 锁定清单为 `vendor/bellis-protocol/manifest.json`：记录上游仓库、完整提交号、每个副本的源路径与 SHA-256。Schema、Pydantic 模型与控制连接 fixture 均取自同一个已合入提交的 Git blob，保留原始字节；本仓库的格式化工具不改动副本。更新时一起替换这三份文件与清单。`uv run check` 离线核对文件集合、来源路径与摘要；它不联网验证提交是否已合入，更新者须在复制前确认。
- 两仓联调可在隔离验证分支锁定 Bellis 已冻结但尚未合入提交的候选生成物。仍取同一完整提交的原始 Git blob，按相同路径与摘要规则核对，在联调 PR 或报告注明候选提交和未合入状态。正式锁定仍按前两条执行。
- Python 接收端先用锁定模型提供的 `parse_game_bridge_json` 解码原始 JSON，再校验信封、载荷、角色与方向；不能先用普通 JSON 解码丢失 `-0`、小数或指数形式的整数字段信息。模型和 fixture 只是契约，不能代替实时核对有效许可、帧与输入策略的核验。
- 握手比较主版本，不兼容时 Bellis 的游戏桥插件停在 WAITING 并说明原因。
- 控制连接的 Rust 类型在 `core/crates/game-bridge-protocol`。typify 0.8.0 生成的 `GameLeaseRenewParams` 未保留 `token` 和 `expiresAtMs` 的上界，现用手写类型。完整检查的 pytest 项逐条核对锁定夹具，从 Schema 派生字段边界样本，并把 Rust 和冻结 Pydantic 模型各自编码的消息送给对端校验。
- Rust 解码入口按控制角色拆成 Bellis 请求、实时核通知和实时核应答，逐字段从原始 JSON 读取。应答由调用方提供同一连接的在途请求，编号匹配后按请求方法验证。版本不同的 `game.describe` 应答只读取公共主版本，`error.data` 保留任意 JSON。
- `MonotonicMs` 把实时核的系统单调纳秒向下取整为协议毫秒，并将绝对截止时刻乘回纳秒。离线互通检查用 Node 的 `process.hrtime.bigint()` 核对时钟域。握手顺序、运行会话、到期和释放由控制连接与输入代理处理。[Bellis 时间与标识](https://github.com/leafliber/bellis/blob/4274d54cce2da97a29fad1e7e19e31d60e3c95c5/docs/modules/game-bridge.md#时间与标识)

## 消息

命名沿用 Bellis：命令用 `对象.动作`，事件用 `对象.过去式`。字段以 Bellis 冻结的契约为准。

| 方向 | 消息 | Game Runtime 的处理 |
| --- | --- | --- |
| 下行 | `goal.submit` | 按本次会话接纳有绝对截止的目标，语言目标和约束交给 [planner](narrative-planner.md#规划) |
| 下行 | `decision.resolve` | 在新帧上重新定位并核对实例后执行（[决策点](step-control.md#决策点)） |
| 下行 | `lease.renew`、`input.stop` | 只走控制连接，由实时核处理 |
| 下行 | `pacing.hold`、`pacing.release` | 见下文 |
| 上行 | `situation.observed` | 局面摘要（[规划](narrative-planner.md#规划)），画面类型取自[通用分类](perception.md#画面状态)，带看点标记与配音忙闲 |
| 上行 | `decision_point.opened` | 选项、是否计时与截止时间，附候选 |
| 上行 | `action.committed` | 与动作编号匹配的实时核已执行回执加上新帧效果核验 |
| 上行 | `input.released` | 只走控制连接 |
| 上行 | `health.reported` | 运行模式、目标与决策点、前置条件、首个已核验动作时间、帧龄、队列深度、风险状态与最近拒绝 |

## 目标与执行事实

- 接纳时绑定 `goalId`、认证连接代次、绝对截止和 `traceparent`。同一 `goalId` 只接纳一次。目标题述和约束只作为规划数据。
- 动作块受当前目标与决策的绝对截止约束。语义目标到期时即失去授权，动作块最多有效到到期前一纳秒。提交时捕获目标编号、动作编号、选项和决定来源。仅在匹配的 `Executed` 回执与新帧效果都核验后，才按捕获的身份上报 `action.committed`。
- 物理未决按[动作尝试与未决结案](#动作尝试与未决结案)处理。授权任务、回读与结案使用协商后的 Bellis 语义契约。

## 授权任务与动作尝试

（GR-38）授权任务、持久账目与未决结案的交付状态为 PENDING。GR-38 真实自主输入须在 Bellis 源契约冻结、Runtime 锁定副本更新且 [P-09 与 P-10](../plan/bellis-p3-proposals.md#p-09-未决状态与结案) 双端验收通过后启用。v1 用于既有路径与假输入对照。

### 授权任务

由操作员入口经 Bellis 授权开启任务。Bellis 持久记录任务授权与被控对象绑定。Runtime 游戏桥用本地 SQLite 按固定被控对象和任务编号记录预算及原尝试，planner 与控制器共用。一个任务包含子目标、投票和 P-04 恢复目标。任务开启时锁定单调时钟截止及配置批准的调用、输入与无进展上限。

规划、选择排序与策略调用发起前扣减调用次数，输入按[动作尝试](#动作尝试与未决结案)记账。迟到、取消与失败照样计入。无进展时长从开任务或上一次确证效果、子目标完成起算，暂停、等票、刷新、重连与换 `goalId` 都继续累计。P-04 只能用原任务剩余额度，命令与决策的更早期限继续生效。耗尽预算时拒绝后续可执行目标。仍获授权的目标可在原时限内核验已有完成证据，需要进一步输入或调用时按 P-04 报卡住。已试选项和 GR-37 风险计数沿各自规则。

同一主进程重连时回读原任务与尝试。主进程重启后结束旧任务的自动执行，保留原消耗、尝试和结案记录。开启新任务须通过旧尝试和物理释放门禁。

### 动作尝试与未决结案

每个被控对象串行接纳尝试。可能写出核请求前，游戏桥在同一持久事务登记原任务、Runtime、会话、目标、动作、决策实例、基帧、预期效果、尝试编号与期限，并扣减一次输入额度。`CoreLink` 写请求前按核实例、产出者代次、块序号及动作号登记回执关联。持久写入后再核对帧、原授权与提交截止，通过才写一次核请求。存储不可用时停发。

游戏桥持有回执队列与只读核验的生命周期。暂停、接管和释放立即撤权，作废未送出的提议。旧回执仍按原关联收取，核验按原基帧、期限、窗口、变换和捕获连续性进行。真人接管、切窗等打断因果证据时，缺少可靠证据的原尝试进未决。经核验的执行事实按原连接与身份规则发出，断连后的结果留在持久记录。

在改写授权或提交输入前检查该对象的原尝试。处理中和未决时拒绝新可执行目标、决策与输入，暂停、撤权与只读观察照常处理。能够证明未执行、收到执行回执且核验效果，或确证符合重发规则的普通无效果时，关闭原尝试。选择无效果、回执超时、结果不明、可能提交后崩溃或记录不全均转为未决，跨目标、连接与重启维持隔离。

普通 P-04 卡住与物理未决分别投影。操作员核对外部状态和输入释放，按 [P-09](../plan/bellis-p3-proposals.md#p-09-未决状态与结案) 办理结案。Runtime 只确认同一对象与原尝试的已持久结案。Bellis 回读匹配结案后再受理新授权。结案记录独立于 `action.committed`。

## 决策点

- 无计时的选项：Bellis 决定直接自选或开观众投票；Game Runtime 停在该决策点，直到收到 `decision.resolve`。
- 有计时的选项：Bellis 在截止前自选；到余量线仍未回复，Game Runtime 执行排第一的候选，并上报实际结果（Bellis D-07）。
- `waiting_vote` 暂存当前自主目标，并绑定当前无计时实例。投票目标只授权该选择。确认选项效果后，先按投票目标上报事实，再恢复仍在原截止内、属于原连接代次的自主目标和规划。过期、暂停、新目标替换、断连或结果不明时保持对应的当前状态。需新授权时由 Bellis 再发 `goal.submit`。
- 候选更新沿用已打开的实例编号、题目、选项和追踪上下文。倒计时只可收紧，已接纳决定或已关闭的实例不再更新。
- 坏结局、卡住与重读的上报见[坏结局与重来](narrative-planner.md#坏结局与重来)。

## 看点与暂缓推进

（GR-20）两边都由确定性代码处理，不额外调用 LLM。

**看点标记**随 `situation.observed` 上报：

- 类型：章节与审判的开始与结束、新证据、新证词、矛盾或疑点、反转、坏结局、卡住。
- 每条含类型、一句摘要、重要程度（高、中）与发生时刻。同一件事只报一次，等到局面帧的采集时刻达到看点发生时刻再上报。摘要只含短句。
- 由快路径、planner 的批量更新（至多滞后一个间隔）或坏结局处理给出（[看点标记](narrative-planner.md#看点标记)）。
- 标记只是数据，是否解说与频率由 Bellis 决定。

**暂缓推进**：

- Bellis 发 `pacing.hold`（编号、原因、到期时间，到期时间必填）与 `pacing.release`；多个 hold 以最晚到期为准。
- hold 有效时逐步控制器不执行推进类与导航类动作；选择类动作、计时决策与余量线照常。
- 第一个有效 hold 开始的暂缓窗口最多 30 秒。窗口内的多个 hold 使用较晚的到期时刻并受同一上限约束，收到 release 可提前到期。恢复后仍要核对原授权。
- hold 走语义连接，只能让动作暂停，不产生输入，不影响输入许可。

**配音忙闲**（忙、闲或未知，[感知](perception.md#配音检测)）随 `situation.observed` 上报。只使用覆盖当前观测帧且仍新鲜的配音判定。

## 开播前置条件

游戏适配包声明须由操作员确认的前置条件（[动作风险](step-control.md#动作风险)的第四道防线）。Game Runtime 在握手与健康上报中给出条目与是否已确认；Bellis 开播预检要求全部已确认才允许进入 M1–M3（提案 P-08）。

主进程的 `health.reported.ready` 要求逐步控制服务持有内核授予的有效控制产出资格、实时核健康且正在采集、会话前置条件已确认、[风险观测](#风险观测与恢复)可用。实时核最新采集帧和主进程最近一次有效判别帧各自从采集时刻算起，帧龄都须在 `max_frame_age` 内。服务可在首个输入租约前就绪，启动后须先完成一次有效判别。

画面像素变化后，判别新帧期间仅保留上次有效判别的采集时刻供健康检查。该帧超过帧龄上限时，健康检查返回 false。帧源错误、积压超限、坐标变换或帧格式变化、判别出错、不可判或停用都使该时刻失效。接纳可执行的目标和提交新动作仍需要当前有效且新鲜的已判别帧，并须通过各自的授权检查。

### 风险观测与恢复

（GR-37）`health.reported.riskScore` 实时读取活动逐步控制器的[同画面拒绝计数](step-control.md#动作风险)，取 `min(计数, max_rejections) / max_rejections`，沿用计数与清零条件。控制器停用或控制产出资格失效时，`ready` 为 false，报告 `risk-unsupported`，拒绝可执行目标和新动作。v1 要求缺值时仍编码 `riskScore: 0.0`。

达到拒绝上限时停止推进，按局面和失败摘要上报卡住。P-04 后续目标受[同一任务的预算](#授权任务)和[未决门禁](#动作尝试与未决结案)约束。独立风险阈值锁存为 unsupported，`riskLimitExceeded` 按 v1 必填字段编码为 false。人工接管与交还仍走[监督模式](#监督模式)。

## 监督模式

| Bellis 状态或事件 | Game Runtime 的行为 |
| --- | --- |
| M0 仿真 | 回放本地录制的帧（[M0 回放](offline-tools.md#m0-回放)），输入走假后端；录制只存本地；[离线回放基准](offline-tools.md#离线回放基准)也在这一模式下运行 |
| M1、M2 | 真实输入，按输入许可执行（初值约 500ms、每 250ms 内续约，Bellis D-34） |
| 安全停止、维护态 | 不授予输入许可，实时核不发出任何输入；感知可以继续只读运行 |
| 急停 | Bellis 吊销许可并下发停止；实时核释放全部输入后上报已释放 |
| 真人接管（上报） | 实时核本地撤权、释放全部输入并上报；Bellis 切到人工模式；交还控制只能由操作员执行（[接管与急停](realtime-core.md#接管与急停)） |
| 本地急停热键（上报） | 实时核撤权、释放全部输入并上报；Bellis 进入急停与人工接管 |
| 当前许可的释放上报为 `residual` 或 `unverified` | Bellis 切到人工模式；交还控制只能由操作员执行 |

## 独立运行

不接 Bellis 的独立运行用于开发与受监督的真机测试（G0–G2），不用于直播；决策点由操作员给出结果。G0 只用测试用的本地许可源驱动假输入后端（[测试设备连接](core-protocol.md#测试设备连接)）；G2 起输入许可来自操作员控制台，只在测试许可下使用（GR-36）。

### 模型游玩入口

先在另一个终端运行 [操作员控制台](#操作员控制台)，再运行 `uv run supervise <适配包 id> --profile profiles/supervised.example.json`。默认运行不向游戏窗口注入，Windows 上确认要测试真实输入时再加 `--real-input`。每项前置条件、“不直播”确认、`start` 和 `goal` 都由在场操作员在控制台输入。

启动器按参数顺序应用可重复的 `--patch <文件>`，使用同一内核按 [profile](plugin-kernel.md#profile) 加载服务。命令行选定的游戏同时用于操作员鉴别、实时核的输入策略和 `game-adapter` 条目。模型控制需要 `vlm@1` 和 `planner@1` 都可用。方舟凭据沿用 [模型接入](models.md#允许清单)的本机条目和 profile 授权。剧情记录和配音检测可以在 profile 中停用或省略。启用了配音检测并授予 `audio.read` 时，从同一个实时核采集游戏进程的音频。可用 `--core <路径>` 指定已经构建的实时核，`--data-dir <绝对路径>` 指定缺省的本地数据目录。

### 操作员控制台

操作员在另一个终端以 `uv run operate <适配包 id>` 启动它（`game_runtime.operator`），`--max-minutes` 改会话的时长上限。拓扑与 G3 的 Bellis 宿主相同（[连接](#连接)）：控制台是 `bellis-channel/1` 的服务端，一个地址与密钥，按角色区分两条连接。

| 连接 | 客户端 | 承载 |
| --- | --- | --- |
| 操作员连接 | 主进程，角色 `game-runtime.main` | 会话状态、当前目标、决策点与已提交的动作，见[操作员连接的消息](#操作员连接的消息) |
| 操作员许可连接 | 实时核，角色 `game-runtime.core` | 只有输入许可的授予、续约、吊销与输入已释放，见[核协议](core-protocol.md#操作员许可连接) |

- 每种角色同时只接受一个连接，同角色的第二个连接与其它角色的连接都被拒绝；前一个断开后可以再连（实时核重启后的新实例）。
- **连接信息**：启动时新生成通道地址与密钥，写入 SecretStore 的 `operator-session` 条目（服务名 `bellis-game-runtime`），格式同 Bellis 的 `SessionInfo`（`v`、`sessionId`、`channelPath`、`channelKey`、`launcherPid`、`startedAt`，`launcherPid` 是控制台的进程号），已有的覆盖；退出时只在条目仍是自己写的（`sessionId` 相同）时删除，已被后开的控制台覆盖的不动。主进程读取它，连上操作员连接，再把同一地址与密钥经环境变量交给实时核。SecretStore 在 Windows 上是凭据管理器，命名与编码同 Bellis 的 `SecretStore`；其它平台只有测试用的内存实现，真实存储 unsupported。
- 控制台用终端交互，按行读命令，不加界面依赖：

| 命令 | 作用 |
| --- | --- |
| `y`、`n` | 回答当前的确认项 |
| `start`、`s` | 开始或继续：倒数 3 秒后换发新令牌并开始续约，见[许可](#许可) |
| `pause`、`p` | 暂停：吊销当前令牌；倒数中即取消倒数 |
| `goal <文字>`、`goal` | 设定或清除当前目标 |
| `<序号>` | 给出当前决策点（最近打开、还没关闭的那个）的选择：选项的 `index`，每个决策点只给一次 |
| `end`、`q` | 结束会话并退出；Ctrl+C 与 stdin 读到 EOF 同样 |

会话不论怎样结束（命令、确认项答 `n`、到时），控制台都随即退出：吊销令牌、断开两条连接、删除 SecretStore 里自己的连接信息。

### 许可

- 控制台持有只增的令牌。每次“开始”换发更大的令牌，以“现在 + 500 ms”授予，此后每 250 ms 续约（Bellis D-34 的初值）；“暂停”与会话结束时吊销当前令牌。到期时刻用与帧环同一的单调时钟（[约定](core-protocol.md#约定)）。
- **倒数**：“开始”先倒数 3 秒（`ConsoleOptions.start_delay`），倒数结束才进入 `running` 并授予，操作员在倒数中把游戏窗口切到前台。授予之后，操作员的任何键鼠输入都算真人接管（[接管与急停](realtime-core.md#接管与急停)），实时核从不把游戏切到前台。倒数中 `operator.session` 的状态不变；`pause`、`end` 或会话结束即取消倒数，倒数中再按 `start` 不受理。时长上限从第一次倒数结束算起。`start_delay` 为 0 时不倒数，立即授予（只用于测试）。
- 实时核没连上时不授予。运行中实时核重新连上（GR-35 重启后的新实例），控制台换发新令牌授予它，不再倒数：新实例先释放全部输入再接受动作（[启动与生命线](realtime-core.md#启动与生命线)），不算交还控制。
- 实时核每次释放全部输入都在操作员许可连接上发 `input.released`。原因是实时核本地撤权的那几种（真人接管 `takeover`、急停 `emergency_stop`、注入被拦截 `injection_blocked`、钩子失效 `hook_lost`）时，控制台转为暂停并吊销令牌：交还控制只能由操作员在控制台上按“开始”。实时核报来的残留按键（`input.residual`）照样显示并记进会话记录。
- 实时核拒绝授予或续约时（-32004，例如控制台卡住超过有效期：过期的令牌不能续约），控制台同样转为暂停并吊销，不自动换发令牌。
- 控制台退出或崩溃即连接断开，实时核释放全部输入（`lease_source_lost`）。

### 测试会话

测试许可即控制台的一次测试会话。状态依次为 `waiting`（等待确认与开始）、`running` 与 `paused`（可来回切换）、`ended`（不能再开始）。

- **确认**：启动后控制台逐条列出适配包的[前置条件](#开播前置条件)，最后一条是“本次是受监督的测试，不直播”，操作员逐条以 `y` 确认；有一条答 `n` 即结束会话。全部确认后才能开始。`operator.session` 的 `confirmed` 只列适配包的前置条件，不含最后一条。
- **时长上限**：从第一次“开始”算起，缺省 60 分钟。到时吊销令牌，会话结束。
- **会话记录**：每次会话一个 JSON Lines 文件，在本地数据目录的 `operator/sessions/<适配包 id>/` 下，以控制台启动的时刻（UTC）命名，同一秒里已有时加 `-2` 起的序号。逐行记下：会话的开始与结束（原因与各项计数），每项确认，开始的倒数与倒数的取消，开始、暂停（原因）与继续，授予实时核的每个新令牌与被拒绝的授予，当前目标的设定与清除，主进程与实时核的连上与断开，被拒绝的连接，收到的 `input.released`（原因）与 `input.residual`，决策点的打开、选择与关闭，主进程报来的动作。不记密钥。记录是运行数据，不进版本库。
- **门禁**：真实输入后端只能与操作员许可连接或 Bellis 控制连接一起选出，没有自己给自己许可的路径（[操作员许可连接](core-protocol.md#操作员许可连接)）。

“不直播”在本机无法可靠判定，是操作员确认加会话记录的程序性约束（GR-36）；不做推流检测。

### 操作员连接的消息

两端都是 Python，消息类型是 `game_runtime.operator.messages` 的 Pydantic 模型；约定同核协议（[约定](core-protocol.md#约定)）：JSON-RPC 2.0，结构体拒绝未知字段，`_ns` 结尾的时刻与帧环同一时钟。方法名以 `operator.` 起头，是本仓库自有的，不冒用游戏桥的消息名（契约的源在 Bellis，GD-03）。

| 方法 | 方向 | 类型 | 字段 |
| --- | --- | --- | --- |
| `operator.session` | 控制台 → 主进程 | 通知 | 连上后立即发一次，此后每次变化都发。字段为 `adapter`、`state`、`confirmed`、`goal`、`goal_revision`、`ends_at_ns`。每条目标指令使 `goal_revision` 递增，包括重发同一目标或清除目标 |
| `operator.decision_opened` | 主进程 → 控制台 | 通知 | `id`（本会话内唯一）、`prompt`、`options`（每项 `index` 与 `text`）、`candidates`（按推荐顺序的 `index`）、`deadline_ns`（无计时时为 `null`） |
| `operator.decision_updated` | 主进程 → 控制台 | 通知 | 已打开的 `id`、当前 `candidates` 与 `deadline_ns`。控制台校验候选属于原选项且截止时刻只会缩短 |
| `operator.decision_resolved` | 控制台 → 主进程 | 通知 | `id`、`index`：操作员的选择 |
| `operator.decision_closed` | 主进程 → 控制台 | 通知 | `id`、`index`（已核验的选项）、`by`（`operator`、`default`、`withdrawn` 或 `unknown`）、`executed_at_ns`（实时核确认交给输入后端的时刻）。撤回或结果未知时 `index` 为 `null`，缺少核的已执行回执时 `executed_at_ns` 为 `null` |
| `operator.action` | 主进程 → 控制台 | 通知 | `category`（`advance`、`navigate`、`select`）、`summary`：已提交的一个动作，计入会话记录 |

- 主进程核对 `adapter` 与自己加载的适配包相同，不同即断开并报错。
- 主进程只在 `state` 为 `running` 且自己适配包的前置条件全部在 `confirmed` 里时进入自动游玩（[动作风险](step-control.md#动作风险)的第四道防线）；其它状态下不提议动作。
- 操作员连接断开即按会话结束处理：主进程停止自动游玩。控制台一侧，主进程断开时它打开、还没关闭的决策点随之作废。

## 验收

| 阶段 | 内容 |
| --- | --- |
| G3 | 同 Bellis P3：私有测试直播间中完整推进一段剧情；无计时选项的投票或自选、有计时选项的自选都正确执行；许可过期即释放；急停后全部输入释放的时延实测；与 Bellis 宿主真机互通 |
| 原神阶段 | 按探索与剧情 → 解谜 → 战斗分步扩展契约与验收 |

## 技术路线

已批准：GD-03、GR-05、GR-20、GR-28、GR-36、GR-37、GR-38；继承 Bellis D-07、D-27、D-31、D-34、R-23。Bellis 侧见 [P3 契约提案](../plan/bellis-p3-proposals.md)。
