"""GS-06 配音检测的离线评测（`docs/plan/spikes.md` 的 GS-06），不进运行时。

用与插件 `voice-activity@1` 相同的核心与参数：从纯配音的录音按能量得出真值，与背景按增益离线混合，
逐块喂给核心，统计开始与结束的判定延迟、漏判与多判。口径见 `docs/modules/perception.md` 的
“GS-06 的评测”；命令见 `uv run gs06 --help`。
"""
