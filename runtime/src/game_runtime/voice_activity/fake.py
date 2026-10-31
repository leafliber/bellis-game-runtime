"""只用于测试的语音检测模型：不要模型文件，在没有模型的环境（含 Linux 容器）里跑通核心与插件。

生产代码不使用它。按音量判断只在这里出现：真正的判定不按音量（`docs/modules/perception.md` 的
“配音检测”）。
"""

import threading
from collections.abc import Callable, Sequence

import numpy as np

from game_runtime.voice_activity.frontend import Float32Array


class EnergyModel:
    """均方根不低于 `threshold` 的帧概率为 1，否则为 0。记下重置与处理的帧数。"""

    def __init__(
        self,
        *,
        threshold: float = 0.01,
        sample_rate: int = 16_000,
        frame_size: int = 512,
        before: Callable[[], None] | None = None,
    ) -> None:
        self.threshold = threshold
        self._sample_rate = sample_rate
        self._frame_size = frame_size
        self._before = before
        """每帧推理之前调用，测试用来让推理卡住或出错。"""
        self.resets = 0
        self.frames = 0
        self.lock = threading.Lock()

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def frame_size(self) -> int:
        return self._frame_size

    def reset(self) -> None:
        self.resets += 1

    def probability(self, frame: Float32Array) -> float:
        if self._before is not None:
            self._before()
        with self.lock:
            self.frames += 1
        rms = float(np.sqrt(np.mean(np.square(frame, dtype=np.float64))))
        return 1.0 if rms >= self.threshold else 0.0


class ScriptedModel:
    """按顺序给出 `probabilities`，用完后一直给出最后一个；`reset` 不改变进度。"""

    def __init__(
        self, probabilities: Sequence[float], *, sample_rate: int = 16_000, frame_size: int = 512
    ) -> None:
        if not probabilities:
            raise ValueError("至少要一个概率")
        self._probabilities = list(probabilities)
        self._sample_rate = sample_rate
        self._frame_size = frame_size
        self.index = 0
        self.resets = 0

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def frame_size(self) -> int:
        return self._frame_size

    def reset(self) -> None:
        self.resets += 1

    def probability(self, frame: Float32Array) -> float:
        del frame
        value = self._probabilities[min(self.index, len(self._probabilities) - 1)]
        self.index += 1
        return value
