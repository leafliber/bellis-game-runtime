# 继承的控制相关外部参考

摘自 Bellis main `docs/future/spec/d-sources.md`（提交 204df03），编号沿用原文，供 `spec/` 下的 `[Sn]` 引用对照。

| 编号 | 参考 | 本次使用与核验范围 |
|---|---|---|
| S35 | [RFC8785 JSON Canonicalization Scheme](https://www.rfc-editor.org/rfc/rfc8785) | 本版读取RFC8785；采用对象的JCS摘要仍需实现阶段互操作测试。 |
| S36 | [Reinforcement Learning for Real-Time Vision-Language-Action Policies — 2609.18207v1](https://arxiv.org/html/2609.18207v1) | 2026-09-18核对固定版本HTML的方法与限制：慢候选与基于当前观察的有界编辑、价值选择分离。未复现模型，未测本机或游戏；Bellis对象/门禁为项目设计。 |
| S37 | [Decoupling Vision, Language, and Action for Efficient Multi-Task Robot Policies — 2609.18374v2](https://arxiv.org/html/2609.18374v2) | 2026-09-18核对固定版本HTML的方法与限制：独立视觉/语言编码条件化动作头，复用未变指令表示；结论限定已评估训练任务。未复现模型，未测本机或游戏；Bellis对象/门禁为项目设计。 |
| S38 | [Rollback the World, Keep the Reflection: Rollback-Induced Reflection for Long-Horizon LLM Agents — 2609.18304v2](https://arxiv.org/html/2609.18304v2) | 2026-09-18核对固定版本HTML的方法与限制：when/where/what恢复决策；世界恢复能力来自评测环境。未复现模型，未测本机或游戏；Bellis对象/门禁为项目设计。 |
| S39 | [RankGround: Efficient High-Resolution GUI Grounding via Lightweight Reranker-Guided Crop Selection — 2609.18690v1](https://arxiv.org/html/2609.18690v1) | 2026-09-18核对固定版本HTML的方法与限制：区域重排后定位；跨crop置信度、动态/多语言迁移有限制，非必然快于整图。未复现模型，未测本机或游戏；Bellis对象/门禁为项目设计。 |
