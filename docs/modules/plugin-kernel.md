# 主进程插件内核

主进程中每项能力都以插件提供：按依赖激活，注册的一切都能撤销，控制产出者停用前先释放输入。概念与用词沿用 [Bellis 插件内核](https://github.com/leafliber/bellis/blob/main/docs/modules/plugin-kernel.md)，实现为自写的最小 Python 版本，只做当前需要的部分（GR-02），代码在 `runtime/src/game_runtime/kernel/`，基于 asyncio。G0 实现。

## 职责

- 加载、激活与停用插件；维护服务目录。
- 管理插件的全部注册，停用时按后注册先撤销的顺序清理。
- 按 profile 组合启用哪些插件、用什么配置。
- 按 profile 授予清单申请的权限，未授予即不激活。
- 执行控制产出者的指定与交接：任何时刻只有一个，停用前先等实时核确认输入已释放。

## 不负责

- **实时核。** 实时核是独立进程，不归插件内核管。
- **授权输入。** 能否输入由 Bellis 的输入许可与实时核决定；插件内核只决定哪个组件是当前控制产出者。
- **选择控制产出者的规则。** 由[模式仲裁](step-control.md#模式仲裁)决定，内核只执行指定与交接。
- **隔离插件。** 主进程没有输入权，安全边界在实时核，所以第三方模型代码可以在主进程内运行；这是与 Bellis（第三方代码只能以 sidecar 接入）的一处不同。

## 清单

插件是 `Plugin` 的子类，类属性 `manifest` 是它的清单；每次激活新建一个实例。清单是 Pydantic 模型，有未知字段即报错：

| 字段 | 格式 | 含义 |
| --- | --- | --- |
| `id` | 小写字母开头，由小写字母、数字与 `-` 组成，可用 `.` 分段，至多 48 字符 | 插件标识，profile 条目的 `plugin` 引用它 |
| `version` | 1–32 字符 | 插件版本，只用于显示与日志 |
| `tier` | `critical` 或 `ordinary` | 关键平面或普通插件，分级见[总体架构](../architecture.md#分层)；申请 `control.submit` 的必须是 `critical` |
| `provides` | `名字@主版本` 的列表，名字不重复，缺省为空 | 激活时必须提供的服务；激活结束时缺任何一项即激活失败 |
| `inject` | `名字@主版本` 的列表，名字不重复，缺省为空 | 必需依赖；任何一项不可用即停在 WAITING |
| `permissions` | 权限的列表，不重复，缺省为空 | 申请的权限，见[权限](#权限) |
| `config` | Pydantic 模型类，缺省为只接受空对象的 `NoConfig` | 配置的 Schema：profile 给的配置先按模型的缺省值补齐缺项，再校验；不合法即停在 FAILED，不重试。模型宜设 `extra="forbid"`，让拼错的字段直接报错 |

## 服务

服务目录按 `名字@主版本` 匹配：同一个名字同一时刻只有一个提供方；名字不在即缺少服务，主版本不同即主版本不兼容，两者都使依赖方停在 WAITING 并说明原因。插件在激活期间提供的服务激活成功后才发布，所以插件不能用自己提供的服务满足自己的依赖。代码里用带类型的 `ServiceToken[T](名字, 主版本)` 取得与提供服务。

当前定义的服务：

| 服务 | 提供方 | 文档 |
| --- | --- | --- |
| `game-adapter@1` | 当前游戏适配包（关键平面） | [游戏适配包](game-adapter.md) |
| `ocr@1`、`scene@1`、`voice-activity@1` | 感知插件 | [感知](perception.md) |
| `vlm@1` | 模型接入插件 | [模型接入](models.md) |
| `narrative@1`、`planner@1` | 剧情记录与规划插件 | [剧情记录与规划](narrative-planner.md) |
| `policy@1` | 策略后端（逐步控制器、以后的连续策略），契约见[策略后端契约](step-control.md#策略后端契约) | [逐步控制](step-control.md) |
| `skills@1` | 技能库（原神阶段） | [技能](step-control.md#技能) |

## 注册

插件只经激活时拿到的 `PluginContext` 接触内核，拿不到内核本身，也拿不到 profile 的接口：

| 接口 | 规则 |
| --- | --- |
| `ctx.service(token)` | 取得 `inject` 中声明的服务；未声明或不可用即抛 `LookupError` |
| `ctx.provide(token, impl)` | 提供 `provides` 中声明的服务；只能在激活期间调用，同一服务只能提供一次 |
| `ctx.register(disposer)` | 登记一项副作用的撤销函数，同步函数或返回可等待对象的函数均可 |
| `ctx.config` | 补齐缺省值并校验过的配置 |
| `ctx.permissions` | 已授予的权限，即清单申请的权限 |
| `ctx.control` | 控制产出者的提交资格，见[控制产出者](#控制产出者)；其它插件为 None |
| `ctx.audio` | 读音频环的接口 `AudioReader`（`read_recent`，见[音频环的挂接与读取](core-protocol.md#音频环的读写规则)）。清单没有申请 `audio.read` 即抛 `PermissionError`；宿主没有给出时抛 `LookupError`。宿主构造内核时以 `Kernel(audio=link.audio_reader)` 给出 |
| `Plugin.quiesce(ctx)` | 插件可覆盖：进入 QUIESCING 时调用，停止新工作、取消在途任务 |

- 一切副作用（任务、定时器、连接、子进程）都经 `ctx.register` 登记。发布的服务由内核登记在最后，停用时最先撤回。
- 停用或激活失败时按后注册先撤销的顺序运行撤销函数。单个撤销函数抛错或超时只记 ERROR 日志，继续撤销其余注册。
- 实例卸载后才登记的撤销函数立即运行，例如激活超时后插件仍在后台运行时。

## 权限

| 权限 | 含义 |
| --- | --- |
| `frames.read` | 读帧环 |
| `audio.read` | 读音频环（`ctx.audio`） |
| `narrative.write` | 写剧情记录 |
| `control.submit` | 向实时核提交动作块，只授予当前控制产出者 |
| `net.egress:<主机>` | 向该主机外发 |
| `secret:<名字>` | 读该凭据 |

- profile 按条目授予（条目的 `grant`），与清单申请的权限逐项精确比对；申请而未授予即停在 WAITING，原因写明缺哪一项。
- `control.submit` 只有被指定为当前控制产出者的条目才算授予，其余授予了的条目停在 WAITING；申请它的插件必须是关键平面。

## 生命周期

| 状态 | 含义 | 离开的条件 |
| --- | --- | --- |
| WAITING | 等条件：依赖、权限、控制产出者的指定与交接 | 全部满足即 LOADING；被停用即 DISPOSED |
| LOADING | 正在激活，控制产出者的提交资格尚未生效 | 成功即 ACTIVE；抛错、超时、缺少声明的服务或服务名已被占用即 FAILED |
| ACTIVE | 运行中，服务已发布 | 被停用或条件不再满足即 QUIESCING |
| QUIESCING | 停止新工作；控制产出者还要等输入已释放的确认 | 完成即 UNLOADING；控制产出者等确认超时或失败时保持 QUIESCING |
| UNLOADING | 按后注册先撤销的顺序撤销注册 | 撤销完，启用中的回 WAITING，被停用的进 DISPOSED |
| DISPOSED | 已卸载：条目被停用，保留条目，不加载实例 | 启用即 WAITING，重试计数清零 |
| FAILED | 激活失败 | 退避到时回 WAITING 再试；重试用尽或配置不合法时停在这里，操作员停用再启用才重来 |

- 全部状态变化都在一个串行的调和任务里进行，每一轮先续完已收到确认的 QUIESCING，再按 profile 逆序停用，最后按 profile 顺序激活。依赖某个提供方服务的条目先于提供方停用；它还在 QUIESCING 时提供方保持 ACTIVE 并说明原因，等它完成再停。
- 操作员与宿主的接口是 `add`（加入 profile 展开后的条目）、`enable`、`disable`、`set_control_producer`、`settled`（等调和结束）与 `shutdown`（停止内核，见下文），插件拿不到。
- 状态或原因每次变化都经 `on_transition` 通知宿主，`status` 与 `snapshot` 给出当前的状态与原因，健康上报以此为来源。

**原因。** 每条原因有机器可读的 `code`、涉及的对象 `subject`（服务、权限或条目）与给人读的 `message`。因条件不再满足而停用时，QUIESCING 的原因就是相应的 WAITING 原因。

| code | 出现在 | 含义 |
| --- | --- | --- |
| `service-missing` | WAITING | 缺少服务 |
| `service-major-mismatch` | WAITING | 服务主版本不兼容 |
| `permission-not-granted` | WAITING | profile 未授予权限 |
| `control-not-designated` | WAITING | 授予了 `control.submit`，但不是当前控制产出者 |
| `control-handover-pending` | WAITING | 已被指定为控制产出者，但上一个尚未完成 QUIESCING |
| `provider-stopping` | QUIESCING | 依赖的提供方正在停用 |
| `disabled` | QUIESCING、DISPOSED | 已停用 |
| `dependent-quiescing` | ACTIVE | 应当停用，但依赖它的条目还在 QUIESCING |
| `config-invalid` | FAILED | 配置不合法，不重试 |
| `activation-failed` | FAILED | 激活失败，等退避后重试 |
| `retries-exhausted` | FAILED | 重试已用尽 |
| `release-timeout` | QUIESCING | 等输入已释放的确认超时，仍在等 |
| `release-failed` | QUIESCING | 等确认失败 |

**退避重试。** 激活失败时先撤销该次激活登记的注册，再进 FAILED；按 `retry_delays` 依次等待后重试，个数即重试次数，用尽后停在 FAILED 并说明原因。激活成功或操作员停用再启用时，连续失败计数清零。

### 时限与缺省值

时限与退避由 `KernelOptions` 配置，单位为秒；时钟可注入，测试用手动推进的时钟，不真睡。

| 选项 | 缺省 | 超时后 |
| --- | --- | --- |
| `activate_timeout` | 10 | 取消激活，按激活失败处理 |
| `quiesce_timeout` | 5 | 插件的 `quiesce` 记录告警后继续停用；提交资格已由内核收回，不影响下文的顺序 |
| `release_timeout` | 5 | 控制产出者保持 QUIESCING 并上报，继续等确认；停止内核时上报后撤销注册继续停止。见下文 |
| `disposer_timeout` | 2 | 取消该撤销函数并记录，继续撤销其余注册 |
| `retry_delays` | 1、2、4 | 共重试 3 次，连同首次共激活 4 次 |

前四项与重试间隔取 Bellis 内核的缺省值（激活、收尾、单个撤销函数的时限与重试间隔），`release_timeout` 取 Bellis 收尾时限的值。

## 控制产出者

清单申请了 `control.submit` 且 profile 授予了它的条目是控制产出者的候选；宿主以 `set_control_producer(条目 id 或 None)` 指定其中一个，G0 由宿主直接指定，以后由模式仲裁决定。

- **单写者。** 任何时刻至多一个控制产出者持有实例（LOADING 到 UNLOADING 之间）。更换时旧的先走完 QUIESCING，新的才激活；旧的还在 QUIESCING 时新的停在 WAITING（`control-handover-pending`）。
- **代次。** 每激活一次控制产出者代次加一，在本次主进程运行内单调递增。插件经 `ctx.control` 拿到提交资格 `ControlGrant`：条目 id、实例 id 与代次。资格只在该实例 ACTIVE 时有效，激活期间尚未生效；提交动作块的路径在发送前调用 `check()`，失效即抛 `ControlRevokedError`。宿主经 `active_control` 读当前有效的资格；插件以资格提交动作块，见下文[提交动作块](#提交动作块)。

**QUIESCING。** 控制产出者无论因停用、依赖消失还是被更换而停用，都依次：

1. **停止提交动作块：** 进入 QUIESCING 的同时提交资格失效。
2. **实时核作废其代次：** 立即调用 `CoreControl.invalidate_generation`；插件的 `quiesce` 同时运行，不推迟这一步。
3. **等到输入已释放的确认：** 等 `CoreControl.wait_released` 返回。作废请求送达失败只记录，仍等确认。
4. **撤销注册：** 确认到达且 `quiesce` 结束后，按后注册先撤销的顺序撤销。

`CoreControl` 是实时核一侧的接口，有三项：上面的 `invalidate_generation` 与 `wait_released`，内核用它们完成 QUIESCING；以及[提交动作块](#提交动作块)的 `submit`，内核只把它交给提交资格，自己不调用。真实实现是核连接的 `CoreLink`（`runtime/src/game_runtime/core_link/`）：`invalidate_generation` 发 `producer.revoke`，请求写出后返回；`wait_released` 在 `producer.revoke` 的回应、新实例的 `input.released(core_start)` 或实时核释放后以 0 退出时完成，细节见[释放](realtime-core.md#释放)。测试也用假实现。

**等确认超时。** 从请求作废起 `release_timeout` 内没有确认时，条目保持 QUIESCING，原因为 `release-timeout`，记录告警并通知宿主；不撤销注册，也不放弃等待，确认到达后再撤销注册并完成停用。等待期间新的控制产出者不激活，它依赖的提供方也不停用。没有确认就撤销注册会违反上面的顺序；此时提交资格已失效，主进程不再提交该代次的动作块，已提交的动作块到有效截止时刻即被实时核释放（[动作块](core-protocol.md#动作块)）。`wait_released` 抛错时同样保持 QUIESCING，原因为 `release-failed`，不自动恢复，由操作员处理；主进程退出时实时核按生命线释放输入。

**停止内核。** 主进程停止时宿主调用 `shutdown`，再按 R-06 停止实时核（[停止](realtime-core.md#停止)）。`shutdown` 让全部条目不再应当运行，在调和任务里按 profile 逆序停用，此后不再激活任何条目，也不再接受 `add`；条目的启用开关不变，DISPOSED 的原因是 `disabled`（“内核已停止”）。控制产出者照常走完 QUIESCING；等确认超时或失败时照常以 `release-timeout` 或 `release-failed` 上报，然后记录告警、撤销注册继续停止，它依赖的提供方随后停用。停止前已卡在 QUIESCING 的控制产出者不再计时，同样撤销注册。这里不再等确认，是因为接下来实时核停止时也会释放全部输入：`core.stop` 先释放再回应；实时核不应答时关 stdin，它读到 EOF 释放后退出；最后才强制终止。

### 提交动作块

控制产出者以 `ctx.control.submit(steps, base_frame=…, valid_for=…, start_delay=0)` 提交一块动作，这就是 `control.submit` 权限的用法。内核创建资格时把 `CoreControl.submit` 交给它，插件拿不到核连接本身：

1. `ControlGrant.submit` 先 `check()`，资格已收回即抛 `ControlRevokedError`，什么也不发；再交给 `CoreControl.submit`。
2. `CoreLink.submit`（`runtime/src/game_runtime/core_link/`）再 `check()` 一次后组装动作块：身份与代次取自资格；起始时刻为 `time.perf_counter_ns()` 加 `start_delay` 秒，有效截止时刻再加 `valid_for` 秒，与实时核同一时钟；`seq` 在主进程运行期间全局递增，因而在每个代次内严格递增；`base_frame` 由产出者给出，是它做决定时看的那一帧在帧环中的帧号（`CoreLink.frames` 读出的 `RingFrame.frame_number`），坐标按这一帧的客户区解释（[坐标与几何](core-protocol.md#坐标与几何)）。这一代次还没发过心跳时先发一次，让实时核在执行第一步之前就知道产出者在。
3. 发出 `action.submit`，等实时核的回应后返回 `Submission`：`result` 是 `Accepted` 或 `Rejected`（原因见[消息集](core-protocol.md#消息集)）；`await outcomes()` 等到每个一次性动作都有了回执，按动作身份给出 `Executed` 或 `Dropped`，被拒绝的块与没有一次性动作的块立即返回空。实时核退出时还没等到的回执以 `ChannelError` 失败。
4. `executed` 只表示已交给输入后端，不代表游戏中的效果已发生；效果由感知从新的帧判断。

接受不等于执行：实时核在每次把输入交给后端之前另行核对输入许可、心跳与帧龄（[动作块的执行](realtime-core.md#动作块的执行)），不成立即不发并释放，主进程一侧由 `input.released` 与回执的 `released` 得知。插件不能放宽这些条件，也拿不到许可。

## profile

profile 与 patch 是 JSON 文件，格式同 Bellis（`packages/protocol` 的 `Profile` 与 `ProfilePatch`），本仓库以 Pydantic 模型校验，有未知字段即报错。游戏与 profile 由操作员选择，Bellis 与插件都不能更换（见[权威](../architecture.md#权威)）：只有主进程的启动入口读取并展开 profile，插件拿不到这部分接口。

| 结构 | 字段 |
| --- | --- |
| profile | `name`（格式同插件 `id`）、`packs`（有序的插件包） |
| 插件包 | `name`（1–48 字符）、`entries`（有序的条目） |
| 条目 | `id`（位置，格式同插件 `id`）、`plugin`（填在这个位置上的插件 `id`）；可选 `disabled`（布尔）、`config`（对象）、`grant`（权限的列表） |
| patch | `name`（1–48 字符）、`ops`（有序的操作） |

| 操作 | 效果 |
| --- | --- |
| `{"op": "replace", "entry": 条目}` | 按 `id` 替换整个条目，不与旧条目合并 |
| `{"op": "insert", "entry": 条目, "after": id}` | 插入新条目；有 `after` 时插在该条目之后，否则追加到末尾 |
| `{"op": "disable", "id": id}`、`{"op": "enable", "id": id}` | 只改开关，配置与授予保留 |

展开时先按包的顺序展开条目，`id` 重复即报错；再依次应用 patch，引用不存在的条目或插入已存在的条目都报错。展开后的条目缺省为启用、配置为空对象、不授予任何权限；被停用的条目保留但不加载实例。

### 与 Bellis 的对应

| Bellis | 本仓库 | 差异 |
| --- | --- | --- |
| 清单 `PluginManifest`（TypeBox） | `Manifest`（Pydantic） | 没有 `process`，因为不做 sidecar；`inject` 不支持可选依赖（`名字@主版本?`）；`config` 是 Pydantic 模型类，不是 JSON Schema |
| `definePlugin` 的 `activate`、`quiesce` | `Plugin` 子类的同名方法 | 上下文没有 `on`、`extensions`、`optional`、`fail`、`signal`、`forced`、`log`；多了 `control` |
| 状态 | 同名的七个状态 | 无 |
| 静止中：吊销执行许可 → 插件收尾并等待物理确认 → 撤销；超时即强制撤销，记 `unknown` 并告警，关键平面急停 | 控制产出者：提交资格失效 → 请求作废代次（插件同时收尾）→ 等确认 → 撤销 | 物理确认由内核向实时核取得，不靠插件的 `quiesce`；超时不强制撤销，保持 QUIESCING 并继续等，输入由实时核按失效规则释放 |
| 与安全核的接缝 `KernelSafetyHooks` | `CoreControl` | 作废代次与等确认两项，另有经提交资格交给控制产出者的 `submit`；没有维护态，因为不做热替换 |
| 实例活动满 60 秒后重试计数清零 | 激活成功即清零 | 没有运行中报告失败的入口 |
| 宿主直接提供的服务 `provideCore`、`provideScoped` | 无 | 用到时再加 |
| profile 与 patch 的格式与展开规则 | 同 | 不检查安全核的名字，因为实时核不是插件、不经加载器；不记 `explicitSwitch`（只用于 Bellis 的审核插件）；条目的 `config` 必须是对象，Bellis 到激活时才报错 |
| `pnpm run profile` 打印生效的条目 | 无 | 用到时再加 |

## 暂不做

普通插件热替换、维护态替换关键平面、事件分发模式（用到哪种加哪种）与 sidecar 代理，需要时再加。

## 验收

| 阶段 | 内容 |
| --- | --- |
| G0 | 依赖缺失时停在 WAITING 并说明原因，恢复后激活；未授予权限即不激活；FAILED 有限次退避重试；控制产出者的 QUIESCING 按顺序完成，等到输入已释放才撤销注册，与真实实时核联调同样成立（按住输入时更换控制产出者，新产出者在实时核释放之后才激活）；提交资格收回后 `submit` 什么也不发；停止内核时按 profile 逆序停用，等确认超时上报后继续 |

## 技术路线

已批准：GD-02、GR-02。
