"""配音检测的核心：按块号顺序逐块喂入，返回状态的变化。

纯逻辑，不读墙钟，不碰音频环：输出只取决于喂入的块（块号、采集时刻、采样点数、不连续标志、
样本值与格式）、中断（原因与时刻）与模型。实时运行（`plugin.VoiceRunner`）与回放、GS-06 的评测
用同一个核心。设计见 `docs/modules/perception.md` 的“配音检测”。

不是线程安全的：同一时刻只能有一个线程调用。
"""

from collections.abc import Callable
from typing import Protocol

from game_runtime.audio_ring import AudioBlock
from game_runtime.voice_activity.frontend import Float32Array, Frontend
from game_runtime.voice_activity.state import (
    Hysteresis,
    UnknownReason,
    VoiceChange,
    VoiceParams,
    VoiceStatus,
)


class SpeechModel(Protocol):
    """语音检测模型：有状态，按顺序逐帧给出这一帧是语音的概率。"""

    @property
    def sample_rate(self) -> int:
        """模型要的采样率（Hz）。"""
        ...

    @property
    def frame_size(self) -> int:
        """一帧的采样点数。"""
        ...

    def reset(self) -> None:
        """清掉模型的状态：新的一段声音从这里开始。"""
        ...

    def probability(self, frame: Float32Array) -> float:
        """处理下一帧（单声道、`frame_size` 个采样点），返回它是语音的概率。"""
        ...


type FrameObserver = Callable[[float, int, int], None]
"""每处理一帧调用一次：概率、帧开头与末尾的采集时刻。供评测记录概率。"""


class VoiceActivityDetector:
    """配音检测的核心：混音、重采样、分帧、模型推理与迟滞。

    一段是一串块号相连、没有不连续标志、格式不变的块。段断开（不连续标志、块号不是上一块加一、
    格式变了）或喂入中断时，丢掉重采样器、帧缓冲与模型状态，清掉迟滞的累计，状态转为未知；
    之后的块开始新的一段。
    """

    def __init__(
        self,
        model: SpeechModel,
        params: VoiceParams | None = None,
        *,
        on_frame: FrameObserver | None = None,
    ) -> None:
        params = params or VoiceParams()
        self._model = model
        self._params = params
        self._on_frame = on_frame
        rate, size = model.sample_rate, model.frame_size
        self._hysteresis = Hysteresis(
            onset=params.onset,
            offset=params.offset,
            speech_frames=params.frames(params.min_speech, size, rate),
            silence_frames=params.frames(params.min_silence, size, rate),
        )
        self._frontend: Frontend | None = None
        self._last_block: int | None = None
        self._processed: int | None = None

    @property
    def params(self) -> VoiceParams:
        return self._params

    @property
    def status(self) -> VoiceStatus:
        h = self._hysteresis
        return VoiceStatus(h.state, h.reason, h.since_ns, self._processed)

    def feed(self, block: AudioBlock, *, sample_rate: int, channels: int) -> list[VoiceChange]:
        """喂入下一块（`sample_rate` Hz、`channels` 声道），返回因此发生的变化。"""
        changes: list[VoiceChange] = []
        frontend = self._frontend
        if frontend is not None and self._last_block is not None:
            reason: UnknownReason | None = None
            if block.discontinuity or (sample_rate, channels) != (
                frontend.sample_rate,
                frontend.channels,
            ):
                reason = UnknownReason.DISCONTINUITY
            elif block.block_number != self._last_block + 1:
                reason = UnknownReason.GAP
            if reason is not None:
                changes.extend(self._reset(reason, block.capture_ns))
                frontend = None
        if frontend is None:
            frontend = Frontend(
                sample_rate,
                channels,
                rate=self._model.sample_rate,
                frame_size=self._model.frame_size,
            )
            self._frontend = frontend
            self._model.reset()
        self._last_block = block.block_number
        for frame in frontend.push(block):
            probability = self._model.probability(frame.samples)
            self._processed = frame.end_ns
            if self._on_frame is not None:
                self._on_frame(probability, frame.start_ns, frame.end_ns)
            change = self._hysteresis.push(probability, frame.start_ns, frame.end_ns)
            if change is not None:
                changes.append(change)
        return changes

    def interrupt(self, reason: UnknownReason, at_ns: int) -> list[VoiceChange]:
        """在块之间断开：外壳发现没有音频环、块号不前进或出错时调用，`at_ns` 为发现的时刻。"""
        return self._reset(reason, at_ns)

    def _reset(self, reason: UnknownReason, at_ns: int) -> list[VoiceChange]:
        self._frontend = None
        self._last_block = None
        change = self._hysteresis.reset(reason, at_ns)
        return [] if change is None else [change]
