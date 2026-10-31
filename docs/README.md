# 文档索引

建议按顺序阅读：[README](../README.md) → 总体架构 → 当前阶段任务卡 → 任务卡列出的模块文档。

## 分工

每类内容只写在一处，其它地方链接过去。

| 文档 | 写什么 | 不写什么 |
| --- | --- | --- |
| [总体架构](architecture.md) | 跨模块的结构：术语、分层、权威、进程与连接、失效与释放、控制的组织、模型调用、契约、扩展分类、仓库目录 | 单个模块内部怎么做 |
| [modules/](modules/) | 一个模块的设计：职责与不负责、接口、规则、失败与恢复、各阶段的验收、所依据的路线 | 选型的理由与审核过程；某款游戏特有的参数 |
| [games/](games/) | 一款游戏特有的适配：版本与语言、必需的快路径、白名单与风险、待普查项 | 通用机制 |
| [合规](compliance.md) | 条款要点、维护者的合规确认（C-x）及其条件 | 实现方式 |
| [技术基线](plan/tech-baseline.md) | 登记处：每个决策与路线选了什么、为什么、何时批准，指向写细节的文档；待审核的候选 | 设计细节 |
| [路线图](plan/roadmap.md) | 阶段、每个阶段用到的路线与探针、出口 | 模块设计 |
| 任务卡（如 [G1](plan/G1.md)） | 当前阶段的目标、步骤与出口 | 进度、测试计数、本机路径 |
| [真机探针](plan/spikes.md) | 需要在 Windows 真机上验证的假设、方法与影响 | 结果（写入不入库的报告） |
| [Bellis P3 契约提案](plan/bellis-p3-proposals.md) | 要 Bellis 改什么、为什么 | Game Runtime 侧的行为 |
| [inherited/](inherited/README.md) | 从 Bellis v0.8 移交的旧设计，只读参考 | 不作为规范，不直接引用 |

Agent 的工作规则只在 [AGENTS.md](../AGENTS.md)。

## 模块

| 模块 | 层 | 首次实现 |
| --- | --- | --- |
| [实时核](modules/realtime-core.md) | 实时核 | G0（采集、假输入后端），G1 音频，G2 真实输入 |
| [核协议与本机通道](modules/core-protocol.md) | 主进程与实时核之间 | G0 |
| [主进程插件内核](modules/plugin-kernel.md) | 主进程内核 | G0 |
| [感知](modules/perception.md) | 普通插件 | G1；G2 画面状态；界面元素检测在第二款游戏 |
| [模型接入](modules/models.md) | 普通插件 | G2 |
| [剧情记录与规划](modules/narrative-planner.md) | 普通插件 | G1 剧情记录，G2 规划 |
| [逐步控制](modules/step-control.md) | 关键平面（控制产出者） | G2；技能库在原神阶段 |
| [游戏适配包](modules/game-adapter.md) | 关键平面 | G1 声明格式与必需部分，G2 快路径 |
| [游戏桥（Game Runtime 侧）](modules/game-bridge.md) | 主进程与实时核对 Bellis | G3；独立运行在 G0–G2 |
| [离线工具](modules/offline-tools.md) | 不在直播进程 | G0 合成游戏，G1 录制，G2 回放基准 |

## 游戏

| 游戏 | 状态 |
| --- | --- |
| [《魔法少女的魔女审判》](games/witch-trial.md) | 首个里程碑，G1 起适配 |
