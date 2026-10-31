"""Silero VAD 的 ONNX 模型经 ONNX Runtime 在 CPU 上运行。

型号是 GS-06 评测的候选，不是定论（GR-08）。接口同 Silero VAD v5 起的 `silero_vad.onnx`：输入
`input`（[1, 64 + 512]：上一帧末尾的 64 个采样点接这一帧的 512 个采样点，16 kHz）、`state`
（[2, 1, 128] 的递归状态）与 `sr`（int64 标量），输出 `output`（[1, 1] 的语音概率）与 `stateN`
（新的递归状态）。设计见 `docs/modules/perception.md` 的“语音检测模型”。
"""

# onnxruntime 不带完整的类型标注。
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false

from pathlib import Path
from typing import Any, Final

import numpy as np
import onnxruntime as ort

from game_runtime.artifacts import Artifact, verify_artifact
from game_runtime.voice_activity.frontend import Float32Array
from game_runtime.voice_activity.models import SILERO_VAD

SAMPLE_RATE: Final = 16_000
FRAME_SIZE: Final = 512
"""一帧 512 个采样点，16 kHz 时 32 ms。"""
CONTEXT_SIZE: Final = 64
"""每帧前面接上一帧末尾的这么多个采样点。"""
STATE_SHAPE: Final = (2, 1, 128)
INPUTS: Final = frozenset({"input", "state", "sr"})
OUTPUTS: Final = ("output", "stateN")
THREADS: Final = 1
"""算子内线程数。每帧的计算量很小，多线程几乎不快，还占游戏与 OCR 的核心。"""


class ModelNotFoundError(FileNotFoundError):
    """模型文件不存在。"""


class ModelInterfaceError(ValueError):
    """模型文件的输入与输出不是 Silero VAD 的接口。"""


class SileroVad:
    """Silero VAD 模型，实现 `SpeechModel`。

    单线程推理（算子内与算子间各 1 个线程，顺序执行），同一台机器上同样的输入给出同样的概率。
    不是线程安全的。
    """

    def __init__(self, path: Path) -> None:
        if not path.is_file():
            raise ModelNotFoundError(f"语音检测模型不存在：{path}")
        options = ort.SessionOptions()
        options.log_severity_level = 3
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.intra_op_num_threads = THREADS
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(
            str(path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        inputs = frozenset(str(item.name) for item in session.get_inputs())
        outputs = frozenset(str(item.name) for item in session.get_outputs())
        if inputs != INPUTS or not outputs >= frozenset(OUTPUTS):
            raise ModelInterfaceError(
                f"{path} 不是 Silero VAD 的接口：输入 {sorted(inputs)}，输出 {sorted(outputs)}"
            )
        self.path = path
        self._session: Any = session
        self._rate = np.array(SAMPLE_RATE, dtype=np.int64)
        self._state: Float32Array = np.zeros(STATE_SHAPE, dtype=np.float32)
        self._context: Float32Array = np.zeros((1, CONTEXT_SIZE), dtype=np.float32)

    @classmethod
    def load(cls, data_dir: Path, artifact: Artifact = SILERO_VAD) -> "SileroVad":
        """核对本地数据目录里的制品（大小与 SHA-256）后加载；没有或不符时抛 `ArtifactError`。"""
        return cls(verify_artifact(artifact, data_dir))

    @property
    def sample_rate(self) -> int:
        return SAMPLE_RATE

    @property
    def frame_size(self) -> int:
        return FRAME_SIZE

    def reset(self) -> None:
        self._state = np.zeros(STATE_SHAPE, dtype=np.float32)
        self._context = np.zeros((1, CONTEXT_SIZE), dtype=np.float32)

    def probability(self, frame: Float32Array) -> float:
        if frame.shape != (FRAME_SIZE,):
            raise ValueError(f"一帧须为 {FRAME_SIZE} 个采样点：{frame.shape}")
        x = np.concatenate((self._context, frame.astype(np.float32)[None, :]), axis=1)
        output, state = self._session.run(
            list(OUTPUTS), {"input": x, "state": self._state, "sr": self._rate}
        )
        self._state = np.asarray(state, dtype=np.float32)
        self._context = x[:, -CONTEXT_SIZE:]
        return float(np.asarray(output, dtype=np.float32)[0, 0])


def runtime_version() -> str:
    """ONNX Runtime 的版本，给报告用。"""
    return str(ort.__version__)
