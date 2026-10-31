"""ONNX Runtime 的一层薄封装：只在 CPU 上运行、一个输入一个输出，给出带类型的接口。"""

# onnxruntime 不带完整的类型标注。
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false

from pathlib import Path
from typing import cast

import numpy as np
import onnxruntime as ort
from numpy.typing import NDArray


class OnnxModel:
    """一个一输入一输出的模型，在 CPU 上运行。不是线程安全的约束由调用方保证。"""

    def __init__(self, path: Path, threads: int | None) -> None:
        options = ort.SessionOptions()
        options.log_severity_level = 3
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        if threads is not None:
            options.intra_op_num_threads = threads
            options.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._input: str = self._session.get_inputs()[0].name

    @property
    def output_classes(self) -> int | None:
        """输出最后一维的大小；是动态维时为 None。"""
        size = self._session.get_outputs()[0].shape[-1]
        return size if isinstance(size, int) else None

    def run(self, batch: NDArray[np.float32]) -> NDArray[np.float32]:
        outputs = self._session.run(None, {self._input: batch})
        return np.asarray(cast(NDArray[np.float32], outputs[0]), np.float32)


def runtime_version() -> str:
    """ONNX Runtime 的版本，给报告用。"""
    return str(ort.__version__)
