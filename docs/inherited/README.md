# 从 Bellis 移交的游戏控制设计

2026-10-14，Bellis 决定从零重写，并把游戏控制相关设计移交给本仓库。本目录原文照录 Bellis main 分支（提交 204df03）中 `docs/future/` 的相关内容，没有修订。

这些材料是 Game Runtime 重新设计的起点，不是现行规范：

- 它们按 Bellis v0.8 的旧约束写成，章节号和交叉引用保持原样，引用的其它章节可能不在本仓库。
- 契约 JSON 是完整复制的冻结登记，其中也含 Bellis 侧条目，只作字段与状态机的参考。
- 新设计吸收其中有用的部分后，应在本仓库的正式文档中重写，不直接引用本目录作为规范依据。

| 位置 | 内容 |
| --- | --- |
| [spec/13-game-integration.md](spec/13-game-integration.md) | 游戏集成模式、Bridge 能力面、本地输入契约、剧情记录与阅读节奏、目标定位 |
| [spec/14-control-protocol.md](spec/14-control-protocol.md) | 四层控制语义、ControlCapability、控制模式、ControlFrame、部分提交 |
| [spec/15-control-loop-policy.md](spec/15-control-loop-policy.md) | 本地控制循环、continuous_local、PolicyBackend、策略更新、分频计算 |
| [spec/10-updates-recovery.md](spec/10-updates-recovery.md) | 检查点、恢复票据、结构性更新与恢复层级 |
| [spec/23-offline-learning.md](spec/23-offline-learning.md) | 离线学习、示范数据治理、制品批准 |
| [spec/selected-sections.md](spec/selected-sections.md) | 其它章节中与控制相关的小节（决策 D01–D24、C0–C5 路线、控制实验矩阵、相关场景走查等） |
| [spec/sources-control.md](spec/sources-control.md) | 上述小节引用的控制相关论文 |
| [contracts/](contracts/) | 冻结的契约登记与控制相关 fixture |
| [plan/spikes-control.md](plan/spikes-control.md) | Windows 输入接管与采集的技术探针 |

更早的完整历史保存在 Bellis 的归档包 `bellis-history-2026-10-14.bundle` 中。
