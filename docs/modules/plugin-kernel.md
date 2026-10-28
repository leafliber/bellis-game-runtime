# 主进程插件内核

主进程中每项能力都以插件提供：按依赖激活，注册的一切都能撤销，控制产出者停用前先释放输入。概念与用词沿用 [Bellis 插件内核](https://github.com/leafliber/bellis/blob/main/docs/modules/plugin-kernel.md)，实现为自写的最小 Python 版本，只做当前需要的部分（GR-02）。G0 实现。

## 职责

- 加载、激活与停用插件；维护服务目录。
- 管理插件的全部注册，停用时按后注册先撤销的顺序清理。
- 按 profile 组合启用哪些插件、用什么配置。
- 按清单授予权限，未授予即不激活。

## 不负责

- **实时核。** 实时核是独立进程，不归插件内核管。
- **授权输入。** 能否输入由 Bellis 的输入许可与实时核决定；插件内核只决定哪个组件是当前控制产出者。
- **隔离插件。** 主进程没有输入权，安全边界在实时核，所以第三方模型代码可以在主进程内运行；这是与 Bellis（第三方代码只能以 sidecar 接入）的一处不同。

## 清单

| 字段 | 内容 |
| --- | --- |
| `id`、`version` | 插件身份 |
| `tier` | 关键平面或普通插件，分级见[总体架构](../architecture.md#分层) |
| `provides`、`inject` | 提供与依赖的服务，按 `名字@主版本` 匹配 |
| `permissions` | 申请的权限，见下文 |
| `config` | Pydantic 模型，先补缺省值再校验 |

## 服务

按 `名字@主版本` 匹配，匹配不上停在 WAITING 并说明原因。当前定义的服务：

| 服务 | 提供方 | 文档 |
| --- | --- | --- |
| `game-adapter@1` | 当前游戏适配包（关键平面） | [游戏适配包](game-adapter.md) |
| `ocr@1`、`scene@1`、`voice-activity@1` | 感知插件 | [感知](perception.md) |
| `vlm@1` | 模型接入插件 | [模型接入](models.md) |
| `narrative@1`、`planner@1` | 剧情记录与规划插件 | [剧情记录与规划](narrative-planner.md) |
| `policy@1` | 策略后端（逐步控制器、以后的连续策略） | [逐步控制](step-control.md) |

## 权限

| 权限 | 含义 |
| --- | --- |
| `frames.read` | 读帧环 |
| `audio.read` | 读音频环 |
| `narrative.write` | 写剧情记录 |
| `control.submit` | 向实时核提交动作块，只授予当前控制产出者 |
| `net.egress:<主机>` | 向该主机外发 |
| `secret:<名字>` | 读该凭据 |

未授予即停在 WAITING 并说明原因。

## 生命周期

WAITING、LOADING、ACTIVE、QUIESCING、卸载；FAILED 后有限次退避重试。

**控制产出者的 QUIESCING** 依次为：停止提交动作块 → 实时核作废其代次 → 等到输入已释放的确认 → 撤销注册。更换控制产出者（包括[模式仲裁](step-control.md#模式仲裁)的切换）都走这一顺序。

## profile

profile 与 patch 的格式同 Bellis。游戏与 profile 由操作员选择，Bellis 与插件都不能更换（见[权威](../architecture.md#权威)）。

## 暂不做

普通插件热替换、维护态替换关键平面、事件分发模式（用到哪种加哪种）与 sidecar 代理，需要时再加。

## 验收

| 阶段 | 内容 |
| --- | --- |
| G0 | 依赖缺失时停在 WAITING 并说明原因，恢复后激活；未授予权限即不激活；FAILED 有限次退避重试；控制产出者的 QUIESCING 按顺序完成，等到输入已释放才撤销注册 |

## 技术路线

已批准：GD-02、GR-02。
