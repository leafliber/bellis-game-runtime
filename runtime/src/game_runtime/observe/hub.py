"""只读运行的事件与统计：`observer` 插件把收到的结果交给 `ObserveHub`，它决定哪些结果成为显示的
事件，交给输出，并为状态行与摘要计数。

规则见 `docs/modules/offline-tools.md` 的“只读运行”。事件只取决于依次收到的结果，不读墙钟；时刻一律
是采集时刻（与 `time.perf_counter_ns()` 同一时钟）。只在事件循环的线程里使用。
"""

import collections
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from game_runtime.adapter import Adapter
from game_runtime.kernel import Reason, State, Transition
from game_runtime.narrative import (
    Completeness,
    Cutscene,
    Discontinuity,
    NarrativeRecord,
    NarrativeService,
    Scope,
)
from game_runtime.ocr import SCRIPT_SENTENCES, LanguageCheck, Script, ScriptCheck, script_of
from game_runtime.ocr.script import supported_languages
from game_runtime.scene import SceneState, Stability, Verdict
from game_runtime.voice_activity import (
    INITIAL_STATUS,
    UnknownReason,
    VoiceActivityService,
    VoiceChange,
    VoiceState,
    VoiceStatus,
)

logger = logging.getLogger(__name__)


# 事件


@dataclass(frozen=True, slots=True)
class SceneEvent:
    """`scene@1` 的判别结果变了：结果、画面或歧义的各画面。"""

    at_ns: int
    frame_number: int
    verdict: Verdict
    screen: str | None
    """`verdict` 为画面时的画面。"""
    screens: tuple[str, ...]
    """歧义时最高档命中的各画面，按名字排序；其余为空。"""
    stability: Stability
    detail: str | None


@dataclass(frozen=True, slots=True)
class VoiceEvent:
    change: VoiceChange


class LineKind(StrEnum):
    """一句显示一行的几处。"""

    APPEAR = "appear"
    """第一次读到。"""
    COMPLETE = "complete"
    """判为完整。"""
    REVISE = "revise"
    """判为完整并显示之后又被修订：回到部分，或文字、说话人变了。"""
    CLOSE = "close"
    """结束。"""


@dataclass(frozen=True, slots=True)
class LineEvent:
    kind: LineKind
    record: NarrativeRecord
    """这一刻这一句的状态。"""
    shown_text: str | None
    """上一次显示的文字；第一次显示时为 None。"""

    @property
    def at_ns(self) -> int:
        return self.record.first_ns if self.kind is LineKind.APPEAR else self.record.last_ns


@dataclass(frozen=True, slots=True)
class CutsceneEvent:
    """剧情记录的一个播片段开始或结束。"""

    cutscene: Cutscene
    """这一刻这个播片段的状态；结束时含其中的各句。"""

    @property
    def ended(self) -> bool:
        return self.cutscene.closed

    @property
    def at_ns(self) -> int:
        """开始时为开始时刻；结束时为结束时刻，结束时刻随中断丢了时为最后一个在播片中的帧。"""
        cutscene = self.cutscene
        if not cutscene.closed:
            return cutscene.first_ns
        return cutscene.last_ns if cutscene.end_ns is None else cutscene.end_ns


class LanguageResult(StrEnum):
    MATCH = "match"
    """各项都相符。"""
    MISMATCH = "mismatch"
    """有不符的项。"""
    ERROR = "error"
    """OCR 出错或此刻不可用，没有核对。"""


@dataclass(frozen=True, slots=True)
class LanguageEvent:
    """固定文字一层的一次核对的结果。"""

    at_ns: int
    frame_number: int
    screen: str
    result: LanguageResult
    check: LanguageCheck | None
    """核对的各项；出错时为 None。"""
    error: str | None = None


class ScriptResult(StrEnum):
    MATCH = "match"
    """前几句判断了的完整句子都相符。"""
    MISMATCH = "mismatch"
    """有一句不符。"""


@dataclass(frozen=True, slots=True)
class ScriptEvent:
    """文字系统一层的结论：相符或不符。"""

    result: ScriptResult
    checks: tuple[tuple[NarrativeRecord, ScriptCheck], ...]
    """判断了的各句与核对，按结束的顺序；不符时最后一项是不符的那一句。"""

    @property
    def at_ns(self) -> int:
        return self.checks[-1][0].last_ns


@dataclass(frozen=True, slots=True)
class PluginEvent:
    """插件内核的一个条目变了状态或原因。"""

    entry: str
    state: State
    reasons: tuple[Reason, ...]


type Event = (
    SceneEvent | VoiceEvent | LineEvent | CutsceneEvent | LanguageEvent | ScriptEvent | PluginEvent
)
type EventSink = Callable[[Event], None]


# 语言核对的时机


class ScriptWatch:
    """文字系统一层：剧情记录每结束一句完整的句子，核对它的文字；无从判断的句子不计。前
    `SCRIPT_SENTENCES` 句判断了的都相符即相符，任一句不符即不符；有了结论之后不再核对。"""

    def __init__(self, adapter: Adapter) -> None:
        self.tag = adapter.language.tag
        self.script: Script | None = script_of(self.tag)
        self.problem: str | None = (
            None
            if self.script is not None
            else f"语言 {self.tag} 的文字系统不受支持（支持 {supported_languages()}）"
        )
        """文字系统不受支持时的原因；否则为 None。"""
        self.checks: list[tuple[NarrativeRecord, ScriptCheck]] = []
        """判断了的各句与核对。"""
        self.undecided = 0
        """无从判断（没有可数的字母）而不计的句数。"""
        self.result: ScriptResult | None = None

    def wants(self, record: NarrativeRecord) -> bool:
        """这一次变化是不是要核对的：结束了的完整的句子，还没有结论。"""
        return (
            self.script is not None
            and self.result is None
            and record.closed
            and record.completeness is Completeness.COMPLETE
        )

    def add(self, record: NarrativeRecord, check: ScriptCheck) -> ScriptEvent | None:
        """记下一句的核对；有了结论时返回它。"""
        if self.result is not None:
            return None
        if check.matches is None:
            self.undecided += 1
            return None
        self.checks.append((record, check))
        if not check.matches:
            self.result = ScriptResult.MISMATCH
        elif len(self.checks) >= SCRIPT_SENTENCES:
            self.result = ScriptResult.MATCH
        else:
            return None
        return ScriptEvent(self.result, tuple(self.checks))


class LanguageWatch:
    """固定文字一层何时核对：声明了固定文字的画面每次出现，在它的第一个已稳定帧上核对一次。

    一次出现从判出这个画面起，到判出另一个画面止；其间的不命中、不可判、歧义与停用不算离开。
    固定文字为空时不核对。
    """

    def __init__(self, adapter: Adapter) -> None:
        self.items = adapter.language.fixed_texts
        self.screens: tuple[str, ...] = tuple(dict.fromkeys(item.screen for item in self.items))
        """声明了固定文字的画面，按声明的顺序。"""
        self._appearance: str | None = None
        self._checked = False

    def observe(self, state: SceneState) -> str | None:
        """处理一帧的判别结果；这一帧要核对时返回它的画面。"""
        if state.verdict is not Verdict.SCREEN or state.screen is None:
            return None
        if state.screen != self._appearance:
            self._appearance, self._checked = state.screen, False
        if (
            self._checked
            or state.screen not in self.screens
            or state.stability is not Stability.STABLE
        ):
            return None
        self._checked = True
        return state.screen


# 统计


@dataclass(slots=True)
class _Tally:
    frames: int = 0
    ns: int = 0


@dataclass(frozen=True, slots=True)
class Share:
    """摘要里的一项：名字、帧数（配音为 0）与时长。"""

    key: str
    frames: int
    ns: int


@dataclass(slots=True)
class _Shown:
    """一句上一次显示时的样子。"""

    completeness: Completeness
    text: str
    speaker: str | None
    closed: bool


def scene_key(state: SceneState) -> str:
    """画面判别的分布里的一项：画面为画面的 `id`，其余为结果的取值。"""
    if state.verdict is Verdict.SCREEN and state.screen is not None:
        return state.screen
    return state.verdict.value


def _ambiguous_screens(state: SceneState) -> tuple[str, ...]:
    if state.verdict is not Verdict.AMBIGUOUS:
        return ()
    matched = set(state.matched)
    return tuple(sorted({s.screen for s in state.signatures if s.id in matched}))


@dataclass(frozen=True, slots=True)
class NarrativeFinal:
    """停用时剧情记录的各句、范围、播片段与中断。"""

    records: tuple[NarrativeRecord, ...]
    scopes: tuple[Scope, ...]
    cutscenes: tuple[Cutscene, ...]
    discontinuities: tuple[Discontinuity, ...]


@dataclass(slots=True)
class _Voice:
    """配音状态的时间线：从 `start_ns` 起的 `INITIAL_STATUS`，之后每条变化。"""

    start_ns: int
    changes: list[VoiceChange] = field(default_factory=list[VoiceChange])


class ObserveHub:
    """宿主与 `observer` 插件之间：插件交来结果，这里生成事件交给 `sink`、记下最新的状态并计数。"""

    def __init__(self, sink: EventSink, *, origin_ns: int | None = None) -> None:
        self.sink = sink
        self.origin_ns = time.perf_counter_ns() if origin_ns is None else origin_ns
        """显示的时刻从这里起算：命令开始的一刻（采集时钟）。"""
        self.adapter: Adapter | None = None
        self.language: LanguageWatch | None = None
        """固定文字一层；`observer` 激活之前为 None。"""
        self.script: ScriptWatch | None = None
        """文字系统一层；`observer` 激活之前为 None。"""
        self.scene_problems: tuple[str, ...] = ()
        self.scene: SceneState | None = None
        """最近一帧的判别结果。"""
        self.voice: VoiceActivityService | None = None
        self.voice_seen = False
        """运行中订阅过 `voice-activity@1`。"""
        self.narrative: NarrativeService | None = None
        self.narrative_seen = False
        """运行中订阅过 `narrative@1`。"""
        self.final: NarrativeFinal | None = None
        self.end_ns: int | None = None
        """`observer` 停用的时刻；还在运行时为 None。"""
        self.language_results: dict[str, collections.Counter[LanguageResult]] = {}
        self._scene_key: tuple[Verdict, str | None, tuple[str, ...]] | None = None
        self._scene_tally: dict[str, _Tally] = {}
        self._scene_order: list[str] = []
        self._voice = _Voice(self.origin_ns)
        self._shown: dict[int, _Shown] = {}
        self._records: dict[int, NarrativeRecord] = {}
        self._cutscenes: dict[int, Cutscene] = {}

    # 插件一侧

    def attach(
        self, adapter: Adapter, *, scene_problems: tuple[str, ...]
    ) -> tuple[LanguageWatch, ScriptWatch]:
        """`observer` 激活时调用；返回这次运行的固定文字一层的时机与文字系统一层。"""
        self.adapter = adapter
        self.scene_problems = scene_problems
        self.language = LanguageWatch(adapter)
        self.script = ScriptWatch(adapter)
        if self.script.problem is not None:
            logger.warning("语言核对的文字系统一层：%s，不核对", self.script.problem)
        if not self.language.items:
            logger.info("语言核对：适配包没有声明固定文字，只做文字系统一层")
        return self.language, self.script

    def on_scene(self, state: SceneState) -> None:
        previous = self.scene
        if previous is not None:
            tally = self._scene_tally[scene_key(previous)]
            tally.ns += max(0, state.capture_ns - previous.capture_ns)
        key = scene_key(state)
        if key not in self._scene_tally:
            self._scene_tally[key] = _Tally()
            self._scene_order.append(key)
        self._scene_tally[key].frames += 1
        self.scene = state
        screens = _ambiguous_screens(state)
        identity = (state.verdict, state.screen, screens)
        if identity != self._scene_key:
            self._scene_key = identity
            self.sink(
                SceneEvent(
                    state.capture_ns,
                    state.frame_number,
                    state.verdict,
                    state.screen,
                    screens,
                    state.stability,
                    state.detail,
                )
            )

    def on_voice(self, change: VoiceChange) -> None:
        self._voice.changes.append(change)
        self.sink(VoiceEvent(change))

    def on_record(self, record: NarrativeRecord) -> None:
        """剧情记录的一次变化：按“出现、完整、修订、结束”决定显示哪几行。"""
        self._records[record.occurrence] = record
        shown = self._shown.get(record.occurrence)
        if shown is None:
            self._emit(LineKind.APPEAR, record, None)
            if record.closed:
                self._emit(LineKind.CLOSE, record, record.text)
            return
        if record.closed:
            if not shown.closed:
                self._emit(LineKind.CLOSE, record, shown.text)
            return
        if record.completeness is Completeness.COMPLETE:
            if shown.completeness is not Completeness.COMPLETE:
                self._emit(LineKind.COMPLETE, record, shown.text)
            elif (record.text, record.speaker) != (shown.text, shown.speaker):
                self._emit(LineKind.REVISE, record, shown.text)
        elif shown.completeness is Completeness.COMPLETE:
            self._emit(LineKind.REVISE, record, shown.text)

    def _emit(self, kind: LineKind, record: NarrativeRecord, shown_text: str | None) -> None:
        self._shown[record.occurrence] = _Shown(
            record.completeness, record.text, record.speaker, record.closed
        )
        self.sink(LineEvent(kind, record, shown_text))

    def on_cutscene(self, cutscene: Cutscene) -> None:
        """剧情记录的一个播片段开始或结束：各显示一行。"""
        self._cutscenes[cutscene.index] = cutscene
        self.sink(CutsceneEvent(cutscene))

    def on_language(self, event: LanguageEvent) -> None:
        self.language_results.setdefault(event.screen, collections.Counter())[event.result] += 1
        if event.result is LanguageResult.MATCH:
            logger.info("语言核对 %s：相符", event.screen)
        elif event.result is LanguageResult.MISMATCH and event.check is not None:
            items = "；".join(
                f"{o.item.roi} 应为“{o.item.text}”，读出“{o.result.text}”"
                for o in event.check.mismatches
            )
            logger.warning("语言核对 %s：不符：%s", event.screen, items)
        else:
            logger.warning("语言核对 %s：出错：%s", event.screen, event.error)
        self.sink(event)

    def on_script(self, event: ScriptEvent) -> None:
        if event.result is ScriptResult.MATCH:
            logger.info("语言核对 文字系统：相符（%d 句）", len(event.checks))
        else:
            record, check = event.checks[-1]
            ratio = 0.0 if check.ratio is None else check.ratio
            logger.warning(
                "语言核对 文字系统：不符：#%d“%s”%s占 %.0f%%",
                record.occurrence,
                record.text,
                check.script.label,
                ratio * 100,
            )
        self.sink(event)

    def follow_voice(self, voice: VoiceActivityService | None) -> None:
        self.voice = voice
        self.voice_seen = self.voice_seen or voice is not None

    def follow_narrative(self, narrative: NarrativeService | None) -> None:
        self.narrative = narrative
        self.narrative_seen = self.narrative_seen or narrative is not None

    def close(self, final: NarrativeFinal | None) -> None:
        """`observer` 停用时调用。"""
        self.final = final
        self.end_ns = time.perf_counter_ns()

    # 宿主一侧

    def transition(self, change: Transition) -> None:
        """插件内核的 `on_transition`：进入 LOADING 不显示。"""
        if change.after is State.LOADING:
            return
        self.sink(PluginEvent(change.entry_id, change.after, change.reasons))

    @property
    def voice_status(self) -> VoiceStatus | None:
        """配音此刻的状态；没有 `voice-activity@1` 时为 None。"""
        return None if self.voice is None else self.voice.status

    def records(self) -> tuple[NarrativeRecord, ...]:
        """各句最后的状态：停用时记下的，或还在运行时收到的，按出现编号。"""
        if self.final is not None:
            return self.final.records
        return tuple(self._records[key] for key in sorted(self._records))

    def cutscenes(self) -> tuple[Cutscene, ...]:
        """各播片段最后的状态：停用时记下的，或还在运行时收到的（开始与结束时的），按编号。"""
        if self.final is not None:
            return self.final.cutscenes
        return tuple(self._cutscenes[key] for key in sorted(self._cutscenes))

    def scene_shares(self) -> list[Share]:
        """画面判别的分布，按第一次出现的顺序；每帧的时长算到下一帧为止。"""
        return [
            Share(key, self._scene_tally[key].frames, self._scene_tally[key].ns)
            for key in self._scene_order
        ]

    def voice_shares(self, end_ns: int | None = None) -> list[Share]:
        """配音状态的时长：从命令开始到 `end_ns`（缺省为停用的时刻或现在）。忙、闲各一项，未知按
        原因各一项（`unknown:<原因>`），按忙、闲、未知的顺序，没出现的不列。"""
        end = end_ns or self.end_ns or time.perf_counter_ns()
        start = self._voice.start_ns
        spans: dict[str, int] = collections.defaultdict(int)
        status: tuple[VoiceState, UnknownReason | None] = (
            INITIAL_STATUS.state,
            INITIAL_STATUS.reason,
        )
        at = start
        for change in self._voice.changes:
            moment = min(max(change.at_ns, start), end)
            spans[_voice_key(*status)] += moment - at
            status, at = (change.state, change.reason), moment
        spans[_voice_key(*status)] += max(0, end - at)
        order = {"busy": 0, "idle": 1}
        keys = sorted((k for k, v in spans.items() if v > 0), key=lambda k: (order.get(k, 2), k))
        return [Share(key, 0, spans[key]) for key in keys]


def _voice_key(state: VoiceState, reason: UnknownReason | None) -> str:
    if state is VoiceState.UNKNOWN:
        return f"unknown:{reason.value if reason is not None else ''}"
    return state.value
