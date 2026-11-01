"""只读运行的终端输出：每个事件、开始、状态行与摘要写成哪几行。

写法见 `docs/modules/offline-tools.md` 的“显示”与“停止与摘要”。只是格式化，不做判断：显示哪些事件由
`ObserveHub` 决定。
"""

import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from game_runtime.adapter import Adapter
from game_runtime.kernel import EntryStatus, Reason, State
from game_runtime.narrative import Completeness, NarrativeRecord
from game_runtime.observe.hub import (
    CutsceneEvent,
    Event,
    LanguageEvent,
    LanguageResult,
    LanguageWatch,
    LineEvent,
    LineKind,
    ObserveHub,
    PluginEvent,
    SceneEvent,
    ScriptEvent,
    ScriptResult,
    ScriptWatch,
    Share,
    VoiceEvent,
)
from game_runtime.ocr import SCRIPT_SENTENCES, ScriptCheck
from game_runtime.recording import SessionStatus
from game_runtime.scene import Stability, Verdict
from game_runtime.voice_activity import VoiceState, VoiceStatus

VERDICTS: Final = {
    Verdict.SCREEN: "画面",
    Verdict.AMBIGUOUS: "歧义",
    Verdict.NONE: "不命中",
    Verdict.UNDETERMINED: "不可判",
    Verdict.DISABLED: "停用",
}
STABILITY: Final = {
    Stability.STABLE: "已稳定",
    Stability.CHANGING: "变化中",
    Stability.TRANSITION: "过场",
}
VOICE: Final = {VoiceState.BUSY: "忙", VoiceState.IDLE: "闲", VoiceState.UNKNOWN: "未知"}
COMPLETENESS: Final = {
    Completeness.PARTIAL: "部分",
    Completeness.COMPLETE: "完整",
    Completeness.GAP: "缺口",
}
LINE_KINDS: Final = {
    LineKind.APPEAR: "出现",
    LineKind.COMPLETE: "完整",
    LineKind.REVISE: "修订",
    LineKind.CLOSE: "结束",
}
LANGUAGE: Final = {
    LanguageResult.MATCH: "相符",
    LanguageResult.MISMATCH: "不符",
    LanguageResult.ERROR: "出错",
}
SCRIPT: Final = {ScriptResult.MATCH: "相符", ScriptResult.MISMATCH: "不符"}
GB: Final = 10**9


def clock(ns: int) -> str:
    """一段时长写成 `mm:ss.s`，满一小时为 `h:mm:ss.s`，负的带负号。"""
    sign = "-" if ns < 0 else ""
    seconds, tenth = divmod(round(abs(ns) / 1e8), 10)
    minutes, second = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    if hours:
        return f"{sign}{hours}:{minute:02}:{second:02}.{tenth}"
    return f"{sign}{minute:02}:{second:02}.{tenth}"


def size(count: int) -> str:
    """字节数写成 GB，不到 1 GB 时写成 MB（都按 10⁹、10⁶）。"""
    return f"{count / GB:.2f} GB" if count >= GB else f"{count / 1e6:.1f} MB"


def reasons(items: Sequence[Reason]) -> str:
    return "；".join(f"{r.code.value}：{r.message}" for r in items)


def percent(check: ScriptCheck) -> str:
    """属于这个文字系统的比例，写成百分数。"""
    return "—" if check.ratio is None else f"{check.ratio * 100:.0f}%"


def plugin_state(status: EntryStatus) -> str:
    """`ocr failed（activation-failed：……）`。"""
    text = f"{status.id} {status.state.value}"
    return f"{text}（{reasons(status.reasons)}）" if status.reasons else text


class Formatter:
    """把事件写成一行。适配包取自 `hub`（`observer` 激活之后才有，之前按 `id` 写画面）。"""

    def __init__(self, hub: ObserveHub) -> None:
        self.hub = hub

    @property
    def adapter(self) -> Adapter | None:
        return self.hub.adapter

    def stamp(self, ns: int) -> str:
        return f"[{clock(ns - self.hub.origin_ns)}]"

    def screen(self, screen: str | None) -> str:
        """画面写成“标题（id）”。"""
        if screen is None:
            return "—"
        adapter = self.adapter
        if adapter is not None:
            for declared in adapter.screens:
                if declared.id == screen:
                    return f"{declared.title}（{screen}）"
        return screen

    def roi(self, roi: str) -> str:
        adapter = self.adapter
        declared = None if adapter is None else adapter.rois.get(roi)
        return roi if declared is None else declared.title

    def event(self, event: Event) -> str:
        match event:
            case SceneEvent():
                return self.scene(event)
            case VoiceEvent():
                return self.voice(event)
            case LineEvent():
                return self.line(event)
            case CutsceneEvent():
                return self.cutscene(event)
            case LanguageEvent():
                return self.language(event)
            case ScriptEvent():
                return self.script(event)
            case PluginEvent():
                return self.plugin(event)

    def scene(self, event: SceneEvent) -> str:
        if event.verdict is Verdict.SCREEN:
            what = self.screen(event.screen)
        elif event.verdict is Verdict.AMBIGUOUS:
            what = "歧义：" + "、".join(self.screen(s) for s in event.screens)
        else:
            what = VERDICTS[event.verdict]
        line = f"{self.stamp(event.at_ns)} 画面 {what}，{STABILITY[event.stability]}，"
        line += f"帧 {event.frame_number}"
        if event.detail and event.verdict in (Verdict.UNDETERMINED, Verdict.DISABLED):
            line += f"：{event.detail}"
        return line

    def voice(self, event: VoiceEvent) -> str:
        change = event.change
        head = f"{self.stamp(change.at_ns)} 配音 {VOICE[change.state]}"
        if change.state is VoiceState.UNKNOWN:
            reason = "" if change.reason is None else change.reason.value
            return f"{head}（{reason}）"
        return f"{head}（自 {clock(change.since_ns - self.hub.origin_ns)}）"

    def line(self, event: LineEvent) -> str:
        record = event.record
        head = f"{self.stamp(event.at_ns)} 剧情 #{record.occurrence} {LINE_KINDS[event.kind]}"
        adapter = self.adapter
        if adapter is not None and len(adapter.dialogue_boxes) > 1:
            head += " " + ("不限画面" if record.screen is None else self.screen(record.screen))
        if record.cutscene is not None:
            head += f" 播片 {record.cutscene}"
        completeness = COMPLETENESS[record.completeness]
        if (
            event.kind is LineKind.CLOSE
            and record.completeness is Completeness.COMPLETE
            and record.text == event.shown_text
        ):
            return f"{head}（{completeness}）"
        text = self._sentence(record)
        if event.kind is LineKind.COMPLETE:
            tail = f"（修订 {record.revision}）"
        else:
            tail = f"（{completeness}，修订 {record.revision}）"
        return f"{head} {text}{tail}{self._low_confidence(record)}"

    @staticmethod
    def _sentence(record: NarrativeRecord) -> str:
        return f"{record.speaker or ''}「{record.text}」"

    def _low_confidence(self, record: NarrativeRecord) -> str:
        if not record.low_confidence:
            return ""
        items = "、".join(
            f"{self.roi(line.roi)}「{line.text}」{line.confidence:.2f}"
            for line in record.low_confidence
        )
        return f" 低置信度：{items}"

    def cutscene(self, event: CutsceneEvent) -> str:
        """开始：判出播片的帧；结束：时长、其中的句数与不再在播片中的那一帧。"""
        cutscene = event.cutscene
        head = f"{self.stamp(event.at_ns)} 播片 {cutscene.index}"
        if not event.ended:
            return f"{head} 开始，帧 {cutscene.first_frame}"
        seconds = cutscene.duration_ns / 1e9
        sentences = len(cutscene.sentences)
        return f"{head} 结束：{seconds:.1f} 秒，{sentences} 句，帧 {cutscene.closed_frame}"

    def language(self, event: LanguageEvent) -> str:
        head = f"{self.stamp(event.at_ns)} 语言核对 {self.screen(event.screen)}"
        head += LANGUAGE[event.result]
        frame = f"，帧 {event.frame_number}"
        check = event.check
        if check is None:
            return f"{head}：{event.error}{frame}"
        if event.result is LanguageResult.MATCH:
            items = "、".join(f"{o.item.roi}「{o.result.text}」" for o in check.outcomes)
        else:
            items = "；".join(
                f"{o.item.roi} 应为「{o.item.text}」，读出「{o.result.text}」"
                for o in check.mismatches
            )
        return f"{head}（{check.tag}）：{items}{frame}"

    def script(self, event: ScriptEvent) -> str:
        record, check = event.checks[-1]
        head = f"{self.stamp(event.at_ns)} 语言核对 文字系统{SCRIPT[event.result]}"
        head += f"（{check.tag}，{check.script.label}）："
        if event.result is ScriptResult.MATCH:
            return head + "、".join(f"#{r.occurrence} {percent(c)}" for r, c in event.checks)
        return (
            head + f"#{record.occurrence}「{record.text}」{check.script.label}占 {percent(check)}"
        )

    def plugin(self, event: PluginEvent) -> str:
        line = f"{self.stamp(time.perf_counter_ns())} 插件 {event.entry} {event.state.value}"
        return f"{line}（{reasons(event.reasons)}）" if event.reasons else line


# 开始、状态行与摘要


@dataclass(frozen=True, slots=True)
class Recording:
    """开始时显示的录制：目录、配额与已用。"""

    path: Path
    quota: int
    used: int


def banner(
    formatter: Formatter,
    adapter: Adapter,
    *,
    frame_size: tuple[int, int],
    recording: Recording | None,
    plugins: Sequence[EntryStatus],
    narrative_problem: str | None,
) -> list[str]:
    """开始时的几行。"""
    hub = formatter.hub
    lines = [
        f"只读运行 {adapter.id}（{adapter.name} {adapter.version}）：不发任何输入，不连 Bellis",
        f"帧环 {frame_size[0]}×{frame_size[1]}；"
        + (
            "不录制"
            if recording is None
            else f"录制到 {recording.path}（配额 {size(recording.quota)}，已用 "
            f"{size(recording.used)}）"
        ),
        "插件：" + "、".join(f"{s.id} {s.state.value}" for s in plugins),
    ]
    lines += [f"  {plugin_state(s)}" for s in plugins if s.state is not State.ACTIVE]
    if narrative_problem is not None:
        lines.append(f"剧情记录：不运行，{narrative_problem}")
    uncalibrated = adapter.uncalibrated()
    if uncalibrated:
        lines.append("尚未标定：" + "、".join(uncalibrated))
    lines += [f"scene@1：{problem}" for problem in hub.scene_problems]
    watch, script = hub.language, hub.script
    if watch is None or script is None:
        lines.append("语言核对：observer 没有激活，不核对")
    else:
        if script.script is None:
            lines.append(f"语言核对（文字系统）：不核对，{script.problem}")
        elif narrative_problem is not None:
            lines.append("语言核对（文字系统）：不核对，不运行剧情记录")
        else:
            lines.append(
                f"语言核对（文字系统）：{script.tag} 按{script.script.label}，"
                f"核对剧情记录里前 {SCRIPT_SENTENCES} 句完整的句子"
            )
        if not watch.items:
            lines.append("语言核对（固定文字）：适配包没有声明，不核对")
        else:
            screens = "、".join(formatter.screen(s) for s in watch.screens)
            lines.append(f"语言核对（固定文字）：{screens}每次出现时核对，共 {len(watch.items)} 项")
    lines.append("按回车或 Ctrl+C 停止。")
    return lines


def _current(formatter: Formatter) -> str:
    state = formatter.hub.scene
    if state is None:
        return "—"
    if state.verdict is Verdict.SCREEN and state.screen is not None:
        adapter = formatter.adapter
        title = state.screen
        if adapter is not None:
            title = next((s.title for s in adapter.screens if s.id == state.screen), title)
        what = title
    else:
        what = VERDICTS[state.verdict]
    return f"{what} {STABILITY[state.stability]}"


def _voice_now(status: VoiceStatus | None) -> str:
    if status is None:
        return "—"
    if status.state is VoiceState.UNKNOWN and status.reason is not None:
        return f"未知（{status.reason.value}）"
    return VOICE[status.state]


def status_line(
    formatter: Formatter,
    previous: tuple[float, SessionStatus],
    current: tuple[float, SessionStatus],
    *,
    queued: int,
    recording_size: int | None,
) -> str:
    """一行状态；`queued` 是定序帧源里 `scene@1` 还没读的帧数，`recording_size` 是录制的体积
    （不录制为 None）。"""
    (then, before), (now, status) = previous, current
    hub = formatter.hub
    elapsed = max(now - then, 1e-9)
    admitted = (status.admitted - before.admitted) / elapsed
    ring = (status.ring_latest - before.ring_latest) / elapsed
    capture = "—" if status.capture is None else status.capture.value
    audio = "—" if status.audio is None else status.audio.value
    narrative = hub.narrative
    backlog = "—" if narrative is None else str(narrative.backlog)
    sentences = "—" if narrative is None else f"{len(narrative.records())} 句"
    line = (
        f"{formatter.stamp(time.perf_counter_ns())} 采集 画面 {capture}、声音 {audio} · "
        f"接纳 {admitted:.1f} 帧/秒（帧环 {ring:.1f}）· 积压 帧 {queued}、剧情 {backlog} · "
        f"超限 {status.overflows} · 当前画面 {_current(formatter)} · "
        f"配音 {_voice_now(hub.voice_status)} · 剧情 {sentences}"
    )
    if recording_size is not None:
        line += f" · 录制 {status.frames} 帧、{status.audio_blocks} 块、{size(recording_size)}"
    return line


def _percent(part: int, total: int) -> str:
    return f"{part / total * 100:.0f}%" if total > 0 else "—"


def _voice_label(key: str) -> str:
    if key.startswith("unknown:"):
        return f"未知（{key.removeprefix('unknown:')}）"
    return VOICE[VoiceState(key)]


def summary(formatter: Formatter, *, narrative_problem: str | None) -> list[str]:
    """结束时的摘要：剧情记录、画面判别的分布、配音状态的时长占比与语言核对。"""
    hub = formatter.hub
    end = hub.end_ns or time.perf_counter_ns()
    lines = [f"摘要（{clock(end - hub.origin_ns)}）："]
    records = hub.records()
    if narrative_problem is not None:
        lines.append(f"  剧情记录：不运行，{narrative_problem}")
    elif not hub.narrative_seen:
        lines.append("  剧情记录：narrative@1 没有激活")
    else:
        counts = {c: sum(1 for r in records if r.completeness is c) for c in Completeness}
        final = hub.final
        scopes = "—" if final is None else str(len(final.scopes))
        gaps = "—" if final is None else str(len(final.discontinuities))
        cutscenes = hub.cutscenes()
        played = sum(c.duration_ns for c in cutscenes) / 1e9
        lines.append(
            f"  剧情记录：{len(records)} 句，完整 {counts[Completeness.COMPLETE]}、"
            f"部分 {counts[Completeness.PARTIAL]}、缺口 {counts[Completeness.GAP]}；"
            f"剧情范围 {scopes}，中断 {gaps}；播片段 {len(cutscenes)}，共 {played:.1f} 秒"
        )
    shares = hub.scene_shares()
    frames = sum(s.frames for s in shares)
    total = sum(s.ns for s in shares)
    if not shares:
        lines.append("  画面判别：没有处理过任何帧")
    else:
        items = "、".join(
            f"{_scene_label(formatter, s)} {_percent(s.ns, total)}（{s.frames} 帧）" for s in shares
        )
        lines.append(f"  画面判别（{frames} 帧，{total / 1e9:.1f} 秒）：{items}")
    if not hub.voice_seen:
        lines.append("  配音：voice-activity@1 没有激活")
    else:
        voice = hub.voice_shares(end)
        span = sum(s.ns for s in voice)
        items = "、".join(f"{_voice_label(s.key)} {_percent(s.ns, span)}" for s in voice)
        lines.append(f"  配音（{span / 1e9:.1f} 秒）：{items}")
    lines += ["  " + line for line in _language_summary(formatter, narrative_problem)]
    return lines


def _scene_label(formatter: Formatter, share: Share) -> str:
    try:
        return VERDICTS[Verdict(share.key)]
    except ValueError:
        return formatter.screen(share.key)


def _language_summary(formatter: Formatter, narrative_problem: str | None) -> list[str]:
    hub = formatter.hub
    if hub.language is None or hub.script is None:
        return ["语言核对：observer 没有激活，没有核对"]
    return [
        "语言核对（文字系统）：" + _script_summary(hub.script, narrative_problem),
        "语言核对（固定文字）：" + _fixed_summary(formatter, hub.language),
    ]


def _script_summary(watch: ScriptWatch, narrative_problem: str | None) -> str:
    if watch.script is None:
        return f"不核对，{watch.problem}"
    if narrative_problem is not None:
        return "不核对，不运行剧情记录"
    tail = f"；另有 {watch.undecided} 句无从判断，不计" if watch.undecided else ""
    if watch.result is ScriptResult.MATCH:
        return "相符（" + "、".join(f"#{r.occurrence}" for r, _ in watch.checks) + "）" + tail
    if watch.result is ScriptResult.MISMATCH:
        record, check = watch.checks[-1]
        label = check.script.label
        return f"不符（#{record.occurrence}「{record.text}」{label}占 {percent(check)}）{tail}"
    if not watch.checks:
        return ("没有判断了的完整句子" if watch.undecided else "没有完整的句子") + tail
    return f"判断了 {len(watch.checks)} 句，都相符，不到 {SCRIPT_SENTENCES} 句{tail}"


def _fixed_summary(formatter: Formatter, watch: LanguageWatch) -> str:
    hub = formatter.hub
    if not watch.items:
        return "适配包没有声明，没有核对"
    if not hub.language_results:
        screens = "、".join(formatter.screen(s) for s in watch.screens)
        return f"没有核对过（{screens}没有稳定出现）"
    parts: list[str] = []
    for screen, counter in hub.language_results.items():
        counts = "、".join(
            f"{LANGUAGE[result]} {counter[result]}" for result in LanguageResult if counter[result]
        )
        parts.append(f"{formatter.screen(screen)} {counter.total()} 次，{counts}")
    return "；".join(parts)
