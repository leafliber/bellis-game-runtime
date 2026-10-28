# Bellis P3 契约提案

Game Runtime 需要 Bellis 配合的改动，在 Bellis 游戏桥契约冻结（P3 开工前）时提出。契约以 Bellis `packages/protocol` 为源（GD-03），下列条目须在 Bellis 仓库审核并合入后，本仓库才更新锁定的生成物。

本文件只写要 Bellis 改什么、为什么；Game Runtime 侧的行为写在“本仓库”一栏所指的文档。命名沿用 [Bellis 游戏桥](https://github.com/leafliber/bellis/blob/main/docs/modules/game-bridge.md)的约定：命令用 `对象.动作`，事件用 `对象.过去式`。字段名是建议，以 Bellis 冻结的为准。

## 契约提案

| 编号 | 提案 | 要 Bellis 做的 | 本仓库 | 涉及 Bellis |
| --- | --- | --- | --- | --- |
| P-01 | 实时核的控制连接 | 接受实时核以角色 `game-runtime.core` 直连宿主，这条连接只承载 `lease.renew`、`input.stop`、`input.released` 与实时核健康，其它消息一律拒绝；令牌与到期时间直接给实时核；主进程以 `game-runtime.main` 走语义连接；控制连接不在时，游戏桥插件不申请也不续约 `game.input` | GR-05，[连接](../modules/game-bridge.md#连接) | 游戏桥、安全核 |
| P-02 | 决策点的候选 | `decision_point.opened` 增加 `candidates`：每项含选项编号、排名、理由与所引的剧情（只截短句）。有计时的决策点到余量线仍未 `decision.resolve` 时，Game Runtime 执行排第一的候选并上报实际结果（Bellis D-07） | GR-14，[决策点](../modules/game-bridge.md#决策点) | 游戏桥、决策模型 |
| P-03 | 局面摘要的字段 | `situation.observed` 含画面类型、简短摘要、当前这一句与可交互的东西，不含整段剧情日志；Bellis 写入 Iris 的游戏事实只能是摘要，不能是原文（配信指南第 7 条） | GR-14，[规划](../modules/narrative-planner.md#规划) | 游戏桥、记忆 |
| P-04 | 坏结局、卡住与重读 | 画面类型增加“坏结局”与“卡住”，带失败来源（自选、投票或超时默认）；坏结局后由决策模型解说并以 `goal.submit` 给出继续的目标；候选中失败过的选项带“已试过”标记与结果摘要，是否重开投票或排除由 Bellis 决定；局面摘要带“重读”标记 | GR-15，[坏结局与重来](../modules/narrative-planner.md#坏结局与重来) | 游戏桥、决策模型、投票 |
| P-05 | 有计时的决策点优先 | 决策模型目前同一时间只有一个在途调用，新时机排队。提议：有计时的 `decision_point.opened` 作为最高优先的时机，可取消尚未开口的在途调用；这类调用的提示词只含候选、局面摘要与截止时间，不召回记忆；调用截止时间取 Game Runtime 给出的余量线 | GR-14，[决策点](../modules/game-bridge.md#决策点) | 决策模型 |
| P-06 | 看点触发解说 | 决策模型的“游戏决策点或重要局面变化”时机定义为：决策点打开、坏结局与卡住，以及 `situation.observed` 带有看点标记时；普通的局面更新只更新上下文，不单独触发调用。契约增加看点标记字段：类型、一句摘要、重要程度（高、中）、发生时刻。部分标记至多滞后一个批量间隔（缺省 45 秒），Bellis 可按发生时刻判断是否已过时 | GR-20，[看点与暂缓推进](../modules/game-bridge.md#看点与暂缓推进) | 决策模型 |
| P-07 | 暂缓推进信号 | Bellis 播出来源为“游戏关键反应”（Bellis D-33）的句子，或决策模型标为游戏解说的回应时，由发声仲裁以确定性代码发 `pacing.hold`（编号、原因、到期时间，到期时间必填），播完发 `pacing.release`，不经决策模型。契约增加这两条命令，以及 `situation.observed` 中的配音忙闲（忙、闲或未知），供 Bellis 选择等游戏配音结束再开口 | GR-20，[看点与暂缓推进](../modules/game-bridge.md#看点与暂缓推进) | 游戏桥、发声 |
| P-08 | 开播预检纳入游戏的前置条件 | 握手与健康上报中带游戏适配包声明的操作员前置条件及是否已确认；Bellis 开播预检中“Game Runtime 就绪”一项要求全部已确认，否则不允许进入 M1–M3 | GR-21，[开播前置条件](../modules/game-bridge.md#开播前置条件) | 游戏桥、运维（开播预检） |

## Bellis 侧的其他遗留

这些不是契约改动，但同样须在 Bellis 侧处理：

- **R-17 的 GLM 接入点**：决策模型会收到游戏内容，须在 T1 前核对经 Agent Plan `/api/plan/v3` 调用的 GLM 是否走已启用安全沙箱的接入点，否则换模型（[合规 C-2](../compliance.md#c-2-的依据与条件)）。
- **合规文档**：Bellis `docs/compliance.md` 中《魔女审判》一行仍是转引，可按官方[配信・二次創作ガイドライン](https://manosaba.com/guidelines)（2026-10-21 版）更新，要点见本仓库[合规](../compliance.md#魔法少女的魔女审判)。
