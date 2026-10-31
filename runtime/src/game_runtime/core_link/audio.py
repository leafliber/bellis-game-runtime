"""音频配置：音频环的格式与大小，以及音频目标。

音频目标是与画面采集同一条窗口识别规则（G1 起来自游戏适配包）：实时核采这个窗口所属进程及其子进程的
声音。挂接顺序与单写方规则见 `docs/modules/core-protocol.md` 的“音频环”，目标进程见
`docs/modules/realtime-core.md` 的“目标进程”。
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from core_protocol import models
from core_protocol.audio_ring import MAX_CHANNELS, MAX_SAMPLE_RATE, MIN_SAMPLE_RATE
from game_runtime.core_link.capture import WindowRule

DEFAULT_SAMPLE_RATE = 48_000
"""音频环缺省的采样率（Hz）。"""
DEFAULT_CHANNELS = 2
"""音频环缺省的声道数。"""
DEFAULT_BLOCK_CAPACITY = 960
"""一块缺省最多的采样点数：48 kHz 时 20 ms，放得下 WASAPI 一个 10 ms 的包。"""
DEFAULT_BLOCK_COUNT = 400
"""缺省的块槽数：包为 10 ms 时约 4 秒。"""

type _U32 = Annotated[int, Field(le=0xFFFFFFFF)]


class AudioConfig(BaseModel):
    """音频环的格式、大小与音频目标。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: Annotated[WindowRule | models.FakeAudioTarget, Field(discriminator="type")]
    """音频目标：窗口识别规则（采窗口所属进程及其子进程的声音），或只用于自动测试的假音频输出
    （`FakeAudioOutput.target`）。"""
    sample_rate: Annotated[int, Field(ge=MIN_SAMPLE_RATE, le=MAX_SAMPLE_RATE)] = DEFAULT_SAMPLE_RATE
    channels: Annotated[int, Field(ge=1, le=MAX_CHANNELS)] = DEFAULT_CHANNELS
    block_capacity: Annotated[_U32, Field(ge=1)] = DEFAULT_BLOCK_CAPACITY
    block_count: Annotated[_U32, Field(ge=2)] = DEFAULT_BLOCK_COUNT

    def set_target_params(self) -> models.AudioSetTargetParams:
        """`audio.set_target` 的 params。"""
        target = self.target.audio_target() if isinstance(self.target, WindowRule) else self.target
        return models.AudioSetTargetParams(target=models.AudioTarget(root=target))
