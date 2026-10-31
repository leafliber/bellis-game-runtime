"""`voice-activity@1` 的模型制品：钉住的来源、版本与摘要。

设计见 `docs/modules/perception.md` 的“语音检测模型”。

这里列出的是 GS-06 要评测的候选，不是定论；型号、版本与许可在 GS-06 后写回技术基线 GR-08。

模型作者 Silero（GitHub 上的 snakers4/silero-vad）在仓库里直接发布 ONNX 文件，发布页不另附文件；
它的 pip 包也是把同一个文件打包进去。取标签 v6.2 上的 `silero_vad.onnx`（这个文件在提交 bfdc019
“add v6.2 model” 加入，标签 v6.2.1 到 v6.2.3 中是同一个文件，摘要相同）。
"""

from typing import Final

from game_runtime.artifacts import Artifact

SILERO_VAD: Final = Artifact(
    name="silero-vad",
    version="v6.2",
    file="silero_vad.onnx",
    sha256="1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3",
    size=2327524,
    url=(
        "https://raw.githubusercontent.com/snakers4/silero-vad/v6.2/"
        "src/silero_vad/data/silero_vad.onnx"
    ),
    source=(
        "模型作者 Silero 的 GitHub 仓库 snakers4/silero-vad，标签 v6.2（提交 be95df9）的 "
        "src/silero_vad/data/silero_vad.onnx；8 kHz 与 16 kHz 两用的 ONNX 模型"
    ),
    license="MIT（snakers4/silero-vad）",
)
"""Silero VAD v6.2 的 ONNX 模型。"""


def all_artifacts() -> tuple[Artifact, ...]:
    """`voice-activity@1` 用到的全部制品。"""
    return (SILERO_VAD,)
