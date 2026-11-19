"""一帧的判别：变化与稳定加画面签名，得出 `SceneState`。

输出见 `docs/modules/perception.md` 的“输出”。结果只取决于依次处理的帧与当时 OCR 是否可用，
时刻取帧的采集时刻，不读墙钟；实时运行与回放按同样的顺序处理同样的帧即得到同样的结果。
"""

from dataclasses import dataclass, field

from game_runtime.adapter import ScreenType
from game_runtime.frame_ring import RingFrame
from game_runtime.frames import check_frame
from game_runtime.scene.change import ChangeDetector, ChangeParams, Stability, bgra_view
from game_runtime.scene.signatures import (
    SignatureJudge,
    SignatureResult,
    TextReader,
    Verdict,
)
from game_runtime.scene.state import ScreenClassification, ScreenSource, ScreenState


@dataclass(frozen=True, slots=True)
class SceneState:
    """一帧的判别结果。"""

    frame_number: int
    capture_ns: int
    transform_version: int
    width: int
    height: int
    stability: Stability
    stable_ns: int
    """稳定时长，纳秒。"""
    changed_fraction: float
    """与上一处理帧相比变化的格子比例。"""
    playing: bool
    """是否在播片中。"""
    playing_since_ns: int | None
    """在播片中时，这段播片的开始时刻。"""
    playing_ended_ns: int | None
    """本帧起不再在播片中时，刚结束的这段播片的结束时刻。"""
    verdict: Verdict
    screen: str | None
    """`verdict` 为 `SCREEN` 时的画面 `id`。"""
    matched: tuple[str, ...]
    """最高档中命中的签名 `id`（画面与歧义时）。"""
    signatures: tuple[SignatureResult, ...]
    """每个参与判别的签名的结果；停用时为空。"""
    detail: str | None
    """停用时的分辨率、不可判时是哪些签名、歧义时是哪些画面。"""


@dataclass(frozen=True, slots=True)
class SceneUpdate:
    """处理过的一帧、逐帧判别与控制使用的画面状态。"""

    frame: RingFrame
    state: SceneState
    screen_state: ScreenState = field(default_factory=ScreenState)
    observation_scope: object = field(
        default_factory=object, repr=False, compare=False, kw_only=True
    )
    """连续未变的画面共用；由 scene@1 发布时设置。"""
    capture_scope: object | None = field(default=None, repr=False, compare=False, kw_only=True)
    """同一段连续采集共用；由 scene@1 发布时设置。"""

    @property
    def analyzed_capture_ns(self) -> int | None:
        """签名可判时返回此帧的采集时刻。"""
        if self.state.verdict in (Verdict.UNDETERMINED, Verdict.DISABLED):
            return None
        return self.frame.capture_ns


class SceneAnalyzer:
    """逐帧判别。不是线程安全的：同一时刻只在一个工作线程里调用 `analyze`。"""

    def __init__(self, judge: SignatureJudge, params: ChangeParams | None = None) -> None:
        self.judge = judge
        self._change = ChangeDetector(params)

    def analyze(self, frame: RingFrame, reader: TextReader | None) -> SceneState:
        """判别一帧；`reader` 为 None 即 OCR 不可用。"""
        check_frame(frame)
        bgra = bgra_view(frame)
        judgement = self.judge.judge(frame, bgra, reader)
        interface = judgement.verdict in (Verdict.SCREEN, Verdict.AMBIGUOUS)
        change = self._change.update(bgra, frame.capture_ns, interface=interface)
        return SceneState(
            frame_number=frame.frame_number,
            capture_ns=frame.capture_ns,
            transform_version=frame.transform_version,
            width=frame.width,
            height=frame.height,
            stability=change.stability,
            stable_ns=change.stable_ns,
            changed_fraction=change.changed_fraction,
            playing=change.playing,
            playing_since_ns=change.playing_since_ns,
            playing_ended_ns=change.playing_ended_ns,
            verdict=judgement.verdict,
            screen=judgement.screen,
            matched=judgement.matched,
            signatures=judgement.signatures,
            detail=judgement.detail,
        )

    def analyze_update(self, frame: RingFrame, reader: TextReader | None) -> SceneUpdate:
        state = self.analyze(frame, reader)
        if state.verdict is Verdict.SCREEN and state.screen is not None:
            screen_type = self.judge.adapter.generic_type(state.screen)
            screen = ScreenState(ScreenClassification(screen_type, ScreenSource.SIGNATURE, 1.0))
        elif state.playing:
            screen = ScreenState(ScreenClassification(ScreenType.CUTSCENE, None, 1.0))
        else:
            screen = ScreenState(uncertain_signature=state.verdict is not Verdict.NONE)
        return SceneUpdate(frame, state, screen)
