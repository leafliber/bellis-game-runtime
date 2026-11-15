# 模型接入

主进程通过 `game_runtime.models` 的 OpenAI 兼容客户端调用语言与视觉模型。普通插件 `vlm` 提供 `vlm@1`，供通用视觉智能体和 `planner@1` 取用（GR-11、GR-18）。调用时机见[总体架构](../architecture.md#模型调用)。

## 职责

- 按用途选择端点、型号与参数，发出请求，校验结构化输出。
- 外发前核对允许清单，裁剪缩小图像，换算返回的坐标。
- 按用途记录调用的型号、用量、时延与失败。

## 不负责

- **决定何时调用。** 由各调用方决定（[逐步控制](step-control.md)、[剧情记录与规划](narrative-planner.md#调用时机)）。
- **采信结果。** 模型输出只是候选；坐标须经[定位与吸附](step-control.md#定位与吸附)核验才可执行。

## 客户端与服务

- `VLM` 是 `ServiceToken[ModelClient]`。插件依赖 `game-adapter@1`，从上下文取得当前适配包与权限。`close()` 停接调用、取消任务并关闭连接，最多等 1 秒核对收尾。仍有工作在途时以 `cleanup-pending` 报告，保留其占用，后续 `close()` 可再次核对。
- `await client.complete(purpose, instruction=…, data=…, output=…, images=…)` 返回 `ModelResult`，含已校验的 `value`、逐图对应的 `transforms` 与调用 `metrics`。`instruction` 与 Pydantic 的 `output` 类型由调用方代码提供。`data` 是 JSON 数据，游戏文字只放在这里，画面只以 `ModelImage` 传入。客户端把指令放在系统消息，把引用的数据与图像放在用户消息。
- 使用 HTTPS 的 Chat Completions SSE，通过 Python 标准库直连，关闭后即丢弃连接。SSE 的 `[DONE]` 与完整的 HTTP 消息边界都到达后才交付结果。请求使用 `json_object` 或 `json_schema`，两种模式都要求单个 JSON 对象，并由调用方的 Pydantic 模型严格校验。拒绝重复键、额外字段、非有限数字和未正常结束的输出。思考内容用后即丢弃，只记录 token 数。
- 调用有覆盖凭据读取、本机准备、DNS、连接、收发和输出校验的截止时间。到期或调用方取消时关闭连接并立即结束等待，作废此次结果。尚未开始的线程工作被跳过。已开始的阻塞操作只能自行收尾，收尾前继续占用该用途的并发名额。线程工作另按用途在整个进程内计数，旧实例未完成的工作也计入新实例的上限。每个用途还限制过去 60 秒发起的网络尝试，达到上限时立即报错。每次接纳的调用最多发送一次请求。
- 失败以 `ModelCallError` 的 `reason` 报告，调用方取消仍为 `CancelledError`。智能体用途的本地小型 VLM 降级为 unsupported，待 GS-07 确定可用型号与运行配置。

## 按用途配置

| 用途 | 调用方 | 要求 |
| --- | --- | --- |
| `agent`（智能体） | 通用视觉智能体选下一步动作 | 一张图，看重定位准确率与时延，思考关闭或最低 |
| `reasoning`（推理） | planner 的审判推理与决策候选 | 文本，开启思考 |
| `summary`（摘要） | planner 的批量更新 | 文本，看重费用，思考关闭 |
| `keyframe`（关键帧） | planner 在关键帧上的多模态调用 | 一至八张图，见[游戏笔记](narrative-planner.md#游戏笔记) |
| `calibration`（标定） | 离线工具生成快路径签名候选 | 一张图，见[快路径](game-adapter.md#快路径) |

profile 的 `config` 由 `VlmConfig` 校验，每个用途有独立的一组字段。

| 字段 | 含义与缺省 |
| --- | --- |
| `vendor`、`model` | 厂商与完整型号 ID。缺省为方舟的 `glm-5-3-flash-260828`，实际选择须通过允许清单 |
| `endpoint` | 完整 Chat Completions 地址。方舟主机 `ark.cn-beijing.volces.com` 的 `/api/plan/v3/chat/completions`（Agent Plan，缺省）或 `/api/v3/chat/completions`（按量） |
| `secret` | Game Runtime `SecretStore` 的条目名，缺省 `ark` |
| `thinking`、`reasoning_effort` | 思考开关与档位。缺省只发 `reasoning_effort: low`，沿用 Bellis R-17 的 GLM 参数。设为 `null` 即省略相应字段 |
| `response_format` | `json_object`（缺省）或 `json_schema`。JSON Schema 由 `output` 生成 |
| `timeout_s`、`max_tokens` | 单次时限与输出 token 上限，缺省 30 秒与 4096 token |
| `concurrency`、`requests_per_minute` | 按用途的在途与速率上限，缺省 1 与 30 |
| `image` | 视觉用途的 `max_long_edge`（缺省 1024）、`roi`（缺省整个客户区）与 `coordinates`（`pixels` 或 `normalized`，缺省 `pixels`） |

缺省型号用于 GS-07 对照。更换型号、时限与图像规格由维护者调整配置。

### 图像与坐标

`ModelImage` 接收客户区的 `RingFrame`，可指定一个客户区物理像素矩形进一步裁剪。矩形须在帧内并完全落在用途配置的 ROI 内。ROI 未声明、尚未标定或换算后为空时拒绝调用。先裁剪，再按宽高比缩小至最长边上限，保持至少一像素，不放大，去掉 alpha 后编码为 PNG。请求体上限为 16 MiB，响应体上限为 4 MiB，含分块标记与尾部字段。

`ImageTransform` 保留原帧号、窗口变换版本、裁剪矩形及发送尺寸。`client_point(x, y)` 以发送图像为原点，像素点须在 `[0, 宽) × [0, 高)`，归一化点须在 `[0, 1]`。客户端按收到的数值的最短十进制表示和实际发送宽、高精确换算，向下取整后加裁剪原点，归一化边界的 1 取最后一个像素。`client_rect(left, top, right, bottom)` 允许矩形右边与下边落在图像边界，返回覆盖该区域的最小半开物理像素矩形。越界、非有限坐标及空矩形均拒绝。

## 允许清单

发送游戏画面与文本受[合规确认 C-2](../compliance.md#合规确认) 约束，由客户端强制执行：

- 只能选游戏适配包合规声明中允许的厂商与型号，按厂商与请求 `model` 字段的完整值核对，与用途无关；清单外一律拒绝，配置与配置页面都不能越过。方舟上豆包系列写型号 ID；第三方模型（如 GLM）写已确认启用安全沙箱的那个接入点 ID，安全沙箱在方舟上按接入点启用，C-2 的确认也是对接入点做的。本仓库文档里的 `glm-5-3-flash` 是简写，指 Bellis R-17 所用的 `glm-5-3-flash-260828`。
- 适配包合规声明的 `models` 每项写 `vendor` 与 `model`，方舟的厂商标识为 `volcengine-ark`。每一项都须在 C-2 的范围内，校验适配包时核对：方舟上以 `doubao-` 开头的型号 ID，以及接入点 `glm-5-3-flash-260828`。这个范围在代码里只有一份（`game_runtime.adapter` 的 `c2_allows`），随 C-2 修改。
- 端点必须匹配已批准厂商的 HTTPS 主机与 Chat Completions 路径。客户端按 `net.egress:<主机>` 和 `secret:<条目名>` 逐项核对，之后才从 Windows 的 `bellis-game-runtime` 凭据服务读取条目。先解析批准的主机，再对解析出的地址建连，TLS 仍核验证书与批准的主机名。直连拒绝重定向，关闭 TLS 会话密钥日志，包括环境变量 `SSLKEYLOGFILE` 所指定的文件。Game Runtime 的方舟 API Key 由维护者单独创建并录入。其它平台的真实 SecretStore 为 unsupported。
- `VlmPlugin` 清单声明缺省配置所需权限。使用其它凭据名时，宿主以 `vlm_plugin_for(VlmConfig)` 为已选 profile 构造清单，再由内核按该条目的 `grant` 核对。插件激活时再检查配置所需权限是否已声明。激活阶段无网络请求，也不读取凭据值。
- 只发游戏窗口的画面，按图像规格裁剪缩小后再发，不带任何观众信息。
- 不作为示例图或少样本示例发送已存的游戏截图（C-1）；已存样本用于评测见 C-3。
- 向不在允许清单内的模型只能发非游戏的测试图像（GS-07）。

## 遥测

`model.call` 按用途记录配置的厂商和型号、服务端报告的 token（含思考）、首段可见正文与完整调用的耗时以及失败类别。HTTP 失败可带数字状态码。缺失的用量与首字时延留空。日志与错误说明仅使用这些字段和固定原因，排除凭据、提示词、图像、模型正文和服务端错误正文。供 GS-07 与[离线回放基准](offline-tools.md#离线回放基准)使用。

## 验收

| 阶段 | 内容 |
| --- | --- |
| G2 | 清单与权限拒绝、图像裁剪与坐标还原、取消与超时、调用上限和结构化输出用本地假服务校验。真实方舟的可用性、各用途的时延、费用与定位结果及本地降级由 GS-07 实测，状态 PENDING |

## 技术路线

已批准：GR-11、GR-18；合规 C-1、C-2。探针：GS-07。
