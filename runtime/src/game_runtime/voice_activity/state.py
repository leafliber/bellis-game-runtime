"""配音忙闲的状态、变化与迟滞。只处理逐帧的语音概率，不碰声音与模型。

设计见 `docs/modules/perception.md` 的“配音检测”。
"""

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

DEFAULT_ONSET: Final = 0.5
DEFAULT_OFFSET: Final = 0.35
DEFAULT_MIN_SPEECH: Final = 0.1
DEFAULT_MIN_SILENCE: Final = 0.5


class VoiceState(StrEnum):
    BUSY = "busy"
    """配音正在播放。"""
    IDLE = "idle"
    """没有配音。"""
    UNKNOWN = "unknown"
    """不知道：原因见 `UnknownReason`。"""


class UnknownReason(StrEnum):
    """为什么是未知。"""

    STARTING = "starting"
    """启动以来还没有足够的声音作判定。"""
    NO_AUDIO = "no-audio"
    """没有音频环。"""
    STALLED = "stalled"
    """块号不前进：最新一块的末尾早于现在超过 `stall_timeout`。"""
    DISCONTINUITY = "discontinuity"
    """块带不连续标志，或格式变了。"""
    GAP = "gap"
    """块号不是上一块加一：其间的块已被覆盖，或换了音频环。"""
    ERROR = "error"
    """检测出错，已停止检测。"""


@dataclass(frozen=True, slots=True)
class VoiceChange:
    """状态或未知的原因的一次变化。时刻都是采集时刻（与 `time.perf_counter_ns()` 同一时钟）。"""

    state: VoiceState
    at_ns: int
    """判定时刻：忙与闲为做出判定的那一帧末尾的采集时刻；未知为引起它的那一块的采集时刻，
    或中断的时刻。"""
    since_ns: int
    """估计的转换时刻：忙为连续语音帧中第一帧开头的采集时刻，闲为连续非语音帧中第一帧开头的采集时刻；
    未知同 `at_ns`。"""
    reason: UnknownReason | None = None
    """未知的原因；忙与闲时为 None。"""


@dataclass(frozen=True, slots=True)
class VoiceStatus:
    """当前的状态。"""

    state: VoiceState
    reason: UnknownReason | None
    """未知的原因；忙与闲时为 None。"""
    since_ns: int | None
    """当前状态的 `since_ns`；启动以来没有变化过时为 None。"""
    processed_ns: int | None
    """已处理到的声音：最近处理的一帧末尾的采集时刻；还没有处理过声音时为 None。
    判定只覆盖到这一刻。"""


INITIAL_STATUS: Final = VoiceStatus(VoiceState.UNKNOWN, UnknownReason.STARTING, None, None)


@dataclass(frozen=True, slots=True)
class VoiceParams:
    """迟滞的参数，时长以秒计。"""

    onset: float = DEFAULT_ONSET
    """语音帧：概率不低于它。"""
    offset: float = DEFAULT_OFFSET
    """非语音帧：概率低于它。介于两者之间的帧打断两种累计，不改变状态。"""
    min_speech: float = DEFAULT_MIN_SPEECH
    """连续的语音帧累计到这么长即转为忙。"""
    min_silence: float = DEFAULT_MIN_SILENCE
    """连续的非语音帧累计到这么长即转为闲。"""

    def __post_init__(self) -> None:
        if not 0.0 < self.offset <= self.onset <= 1.0:
            raise ValueError(f"须 0 < offset ≤ onset ≤ 1：offset {self.offset}，onset {self.onset}")
        for name, value in (("min_speech", self.min_speech), ("min_silence", self.min_silence)):
            if not (math.isfinite(value) and value > 0.0):
                raise ValueError(f"{name} 须为正数：{value}")

    def frames(self, seconds: float, frame_size: int, sample_rate: int) -> int:
        """累计到 `seconds` 秒要几帧：帧数乘以帧长不短于它，至少 1 帧。"""
        samples = round(seconds * sample_rate)
        return max(1, -(-samples // frame_size))


class Hysteresis:
    """逐帧的迟滞：语音帧连续 `speech_frames` 帧即转为忙，非语音帧连续 `silence_frames` 帧即转为闲。

    未知时同样按这两条转为忙或闲。`reset` 清掉累计并转为未知。
    """

    def __init__(
        self, *, onset: float, offset: float, speech_frames: int, silence_frames: int
    ) -> None:
        if not 0.0 < offset <= onset <= 1.0:
            raise ValueError(f"须 0 < offset ≤ onset ≤ 1：offset {offset}，onset {onset}")
        if speech_frames < 1 or silence_frames < 1:
            raise ValueError(f"帧数须为正数：{speech_frames}、{silence_frames}")
        self._onset = onset
        self._offset = offset
        self._speech_frames = speech_frames
        self._silence_frames = silence_frames
        self._state = VoiceState.UNKNOWN
        self._reason: UnknownReason | None = UnknownReason.STARTING
        self._since: int | None = None
        self._speech_run = 0
        self._speech_start = 0
        self._silence_run = 0
        self._silence_start = 0

    @property
    def state(self) -> VoiceState:
        return self._state

    @property
    def reason(self) -> UnknownReason | None:
        return self._reason

    @property
    def since_ns(self) -> int | None:
        return self._since

    def push(self, probability: float, start_ns: int, end_ns: int) -> VoiceChange | None:
        """处理一帧：这一帧是语音的概率与它开头、末尾的采集时刻。状态变了即返回变化。"""
        if probability >= self._onset:
            if self._speech_run == 0:
                self._speech_start = start_ns
            self._speech_run += 1
            self._silence_run = 0
        elif probability < self._offset:
            if self._silence_run == 0:
                self._silence_start = start_ns
            self._silence_run += 1
            self._speech_run = 0
        else:
            # 中间带（含 NaN）：打断两种累计。
            self._speech_run = 0
            self._silence_run = 0
        if self._state is not VoiceState.BUSY and self._speech_run >= self._speech_frames:
            return self._change(VoiceState.BUSY, None, end_ns, self._speech_start)
        if self._state is not VoiceState.IDLE and self._silence_run >= self._silence_frames:
            return self._change(VoiceState.IDLE, None, end_ns, self._silence_start)
        return None

    def reset(self, reason: UnknownReason, at_ns: int) -> VoiceChange | None:
        """清掉累计并转为未知；已是同一原因的未知时不算变化。"""
        self._speech_run = 0
        self._silence_run = 0
        if self._state is VoiceState.UNKNOWN and self._reason is reason:
            return None
        return self._change(VoiceState.UNKNOWN, reason, at_ns, at_ns)

    def _change(
        self, state: VoiceState, reason: UnknownReason | None, at_ns: int, since_ns: int
    ) -> VoiceChange:
        self._state = state
        self._reason = reason
        self._since = since_ns
        return VoiceChange(state, at_ns, since_ns, reason)
