# Bellis Game Runtime

Bellis 的游戏运行时：负责游戏画面采集、感知（OCR、VLM 等）、VLA 控制策略、唯一的输入代理（Input Broker），以及控制模型的数据与训练。它是独立仓库和独立进程，通过公共契约向 Bellis 宿主提供游戏能力，逐帧控制不经过 Bellis。

## 目标

在已适配的游戏中完全自主游玩，覆盖即时战斗、剧情理解和多样小游戏解谜，最终以 VLA 方式玩原神这类游戏。零样本通关未适配的游戏、直播中在线自动训练并上线，都不在目标内。

当前主验证游戏为《崩坏：星穹铁道》（HSR）。HSR 适配为 **NOT_READY**；随仓可用于离线回归的适配包为 `synthetic-game`。上一款游戏的验证范围已结束。

## 当前状态

独立运行与 HSR 的验收顺序见 [G2 任务卡](docs/plan/G2.md)，与 Bellis 的桥接联调见 [G3 任务卡](docs/plan/G3.md)。总体架构、管理模型、阶段划分与各模块的技术路线都已批准，见[技术基线](docs/plan/tech-baseline.md)；部分细节待[真机探针](docs/plan/spikes.md)实测后确定，需要 Bellis 配合的改动见 [Bellis P3 契约提案](docs/plan/bellis-p3-proposals.md)。

## 文档

从[文档索引](docs/README.md)开始，那里写明了每份文档的分工与阅读顺序。Agent 的工作规则见 [AGENTS.md](AGENTS.md)。
