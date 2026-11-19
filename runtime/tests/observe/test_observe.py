"""只读运行的事件、显示与统计，以及 `observer` 插件在内核中的行为；不经实时核。

规则见 `docs/modules/offline-tools.md` 的“只读运行”。经实时核与假采集源的联调见
`runtime/tests/core_link/test_observe.py`。
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import ClassVar

import pytest

from game_runtime.adapter import (
    GAME_ADAPTER,
    GAMES_DIR,
    Adapter,
    GameAdapterPlugin,
    Language,
    load_adapter,
)
from game_runtime.frame_ring import RingFrame
from game_runtime.kernel import (
    EntryStatus,
    Kernel,
    KernelOptions,
    Manifest,
    NoConfig,
    Plugin,
    PluginContext,
    Reason,
    ReasonCode,
    ResolvedEntry,
    State,
    Tier,
    Transition,
    catalog,
)
from game_runtime.narrative import (
    NARRATIVE,
    Completeness,
    Cutscene,
    CutsceneListener,
    Discontinuity,
    LowConfidenceLine,
    NarrativeListener,
    NarrativeRecord,
    Scope,
)
from game_runtime.observe import (
    CutsceneEvent,
    Event,
    Formatter,
    LanguageEvent,
    LanguageResult,
    LanguageWatch,
    LineEvent,
    LineKind,
    NarrativeFinal,
    ObserveHub,
    PluginEvent,
    SceneEvent,
    ScriptEvent,
    ScriptResult,
    ScriptWatch,
    VoiceEvent,
    banner,
    clock,
    observer_plugin,
    summary,
)
from game_runtime.observe.display import Recording
from game_runtime.ocr import OCR, OcrService, Script, check_script
from game_runtime.ocr.fake import ScriptedRecognizer, scripted_line
from game_runtime.recording import NoControl, ResultsHub, narrative_problem
from game_runtime.scene import (
    SCENE,
    Outcome,
    SceneHub,
    SceneState,
    SceneUpdate,
    SignatureResult,
    Stability,
    Verdict,
)
from game_runtime.voice_activity import (
    VOICE_ACTIVITY,
    UnknownReason,
    VoiceChange,
    VoiceHub,
    VoiceState,
    VoiceStatus,
)

SECOND = 1_000_000_000
WIDTH, HEIGHT = 320, 180


@pytest.fixture(scope="module")
def synthetic() -> Adapter:
    return load_adapter(GAMES_DIR / "synthetic-game")


@pytest.fixture(scope="module")
def witch_trial() -> Adapter:
    return load_adapter(GAMES_DIR / "witch-trial")


@pytest.fixture(scope="module")
def uncalibrated(witch_trial: Adapter) -> Adapter:
    """《魔女审判》去掉对白框的标定（第 9 步之前的样子）：剧情记录不运行。"""
    names = {"speaker", "dialogue", "continue-marker", "hud-border"}
    rois = {
        name: roi.model_copy(update={"rect": None}) if name in names else roi
        for name, roi in witch_trial.rois.items()
    }
    signatures = tuple(
        s.model_copy(update={"anchors": ()}) if s.id == "dialogue-continue" else s
        for s in witch_trial.signatures
    )
    return witch_trial.model_copy(update={"rois": rois, "signatures": signatures})


def state(
    frame: int,
    verdict: Verdict,
    screen: str | None = None,
    *,
    stability: Stability = Stability.STABLE,
    at: int | None = None,
    signatures: tuple[SignatureResult, ...] = (),
    matched: tuple[str, ...] = (),
    detail: str | None = None,
) -> SceneState:
    return SceneState(
        frame_number=frame,
        capture_ns=frame * SECOND // 10 if at is None else at,
        transform_version=1,
        width=WIDTH,
        height=HEIGHT,
        stability=stability,
        stable_ns=0,
        changed_fraction=0.0,
        playing=False,
        playing_since_ns=None,
        playing_ended_ns=None,
        verdict=verdict,
        screen=screen,
        matched=matched,
        signatures=signatures,
        detail=detail,
    )


def signature(sig: str, screen: str) -> SignatureResult:
    return SignatureResult(sig, screen, 0, Outcome.MATCH, None, ())


def record(
    occurrence: int,
    text: str,
    completeness: Completeness = Completeness.PARTIAL,
    revision: int = 1,
    *,
    speaker: str | None = "阿遥",
    closed: int | None = None,
    low: tuple[LowConfidenceLine, ...] = (),
    at: int = 0,
    screen: str | None = "dialogue",
    cutscene: int | None = None,
) -> NarrativeRecord:
    return NarrativeRecord(
        session=1,
        occurrence=occurrence,
        scope=1,
        screen=screen,
        cutscene=cutscene,
        speaker=speaker,
        text=text,
        revision=revision,
        completeness=completeness,
        first_frame=1,
        first_ns=at,
        last_frame=2,
        last_ns=at,
        revision_frame=2,
        complete_frame=2 if completeness is Completeness.COMPLETE else None,
        closed_frame=closed,
        low_confidence=low,
    )


@dataclass
class Collected:
    events: list[Event] = field(default_factory=list[Event])

    def __call__(self, event: Event) -> None:
        self.events.append(event)


def make_hub(adapter: Adapter) -> tuple[ObserveHub, Collected, Formatter]:
    collected = Collected()
    hub = ObserveHub(collected, origin_ns=0)
    hub.attach(adapter, scene_problems=())
    return hub, collected, Formatter(hub)


def test_clock() -> None:
    assert clock(0) == "00:00.0"
    assert clock(83_440_000_000) == "01:23.4"
    assert clock(3_723_400_000_000) == "1:02:03.4"
    assert clock(-500_000_000) == "-00:00.5"


# 语言核对：文字系统一层

CHINESE = ["雨停了，钟楼的指针停在七点。", "你听见了吗？地下传来敲门声。", "门后也许有路。"]
"""合成的中文句子。"""
OTHERS = {
    "japanese": "これはテストです。",
    "korean": "안녕하세요, 반갑습니다.",
    "latin": "The rain has stopped.",
}
"""合成的其它文字系统的句子。"""


def closed(
    occurrence: int, text: str, completeness: Completeness = Completeness.COMPLETE
) -> NarrativeRecord:
    return record(occurrence, text, completeness, closed=occurrence + 1, at=occurrence * SECOND)


def feed_script(watch: ScriptWatch, records: list[NarrativeRecord]) -> list[ScriptEvent]:
    """像 `observer` 那样：要核对的句子以 `check_script` 核对，收集结论。"""
    events: list[ScriptEvent] = []
    for item in records:
        if watch.wants(item):
            event = watch.add(item, check_script(item.text, watch.tag))
            if event is not None:
                events.append(event)
    return events


def test_three_chinese_sentences_match_the_chinese_version(synthetic: Adapter) -> None:
    watch = ScriptWatch(synthetic)
    assert (watch.tag, watch.script, watch.problem) == ("zh-Hans", Script.HAN, None)
    records = [
        record(1, "雨停", at=SECOND),  # 还在显示：不核对
        closed(1, CHINESE[0]),
        closed(2, "……"),  # 没有可数的字母：无从判断，不计
        closed(3, "门后", Completeness.GAP),  # 缺口：不核对
        closed(4, CHINESE[1]),
        closed(5, CHINESE[2]),
        closed(6, OTHERS["latin"]),  # 有了结论之后不再核对
    ]

    [event] = feed_script(watch, records)

    assert event.result is ScriptResult.MATCH
    assert [r.occurrence for r, _ in event.checks] == [1, 4, 5]
    assert all(check.matches for _, check in event.checks)
    assert (watch.result, watch.undecided, len(watch.checks)) == (ScriptResult.MATCH, 1, 3)
    assert event.at_ns == 5 * SECOND


@pytest.mark.parametrize("script", sorted(OTHERS))
def test_another_script_does_not_match_the_chinese_version(synthetic: Adapter, script: str) -> None:
    """合成的日文、韩文与拉丁字母的句子：任一句不符即不符，不等满 3 句。"""
    watch = ScriptWatch(synthetic)

    [event] = feed_script(
        watch, [closed(1, CHINESE[0]), closed(2, OTHERS[script]), closed(3, CHINESE[1])]
    )

    assert event.result is ScriptResult.MISMATCH
    mismatch, check = event.checks[-1]
    assert (mismatch.occurrence, mismatch.text, check.matches) == (2, OTHERS[script], False)
    assert watch.checks == list(event.checks)


def test_an_unsupported_script_is_not_checked(synthetic: Adapter) -> None:
    adapter = synthetic.model_copy(update={"language": Language(tag="ru", fixed_texts=())})
    watch = ScriptWatch(adapter)

    assert watch.script is None
    assert watch.problem is not None
    assert "语言 ru 的文字系统不受支持" in watch.problem
    assert not watch.wants(closed(1, CHINESE[0]))


def test_script_results_are_shown_and_logged(
    synthetic: Adapter, caplog: pytest.LogCaptureFixture
) -> None:
    hub, collected, formatter = make_hub(synthetic)
    caplog.set_level(logging.INFO, logger="game_runtime.observe")
    match = ScriptWatch(synthetic)
    mismatch = ScriptWatch(synthetic)
    for event in feed_script(match, [closed(n + 1, text) for n, text in enumerate(CHINESE)]):
        hub.on_script(event)
    for event in feed_script(mismatch, [closed(1, CHINESE[0]), closed(2, OTHERS["japanese"])]):
        hub.on_script(event)

    assert [formatter.event(e) for e in collected.events] == [
        "[00:03.0] 语言核对 文字系统相符（zh-Hans，汉字）：#1 100%、#2 100%、#3 100%",
        "[00:02.0] 语言核对 文字系统不符（zh-Hans，汉字）：#2「これはテストです。」汉字占 0%",
    ]
    levels = [(r.levelno, r.getMessage()) for r in caplog.records if "文字系统" in r.getMessage()]
    assert levels == [
        (logging.INFO, "语言核对 文字系统：相符（3 句）"),
        (logging.WARNING, "语言核对 文字系统：不符：#2“これはテストです。”汉字占 0%"),
    ]


# 语言核对：固定文字一层的时机


def test_language_is_checked_on_the_first_stable_frame_of_each_appearance(
    synthetic: Adapter,
) -> None:
    watch = LanguageWatch(synthetic)
    assert watch.screens == ("title-menu",)
    assert len(watch.items) == 3
    menu, dialogue = "title-menu", "dialogue"
    sequence = [
        (state(1, Verdict.SCREEN, menu, stability=Stability.CHANGING), None),
        (state(2, Verdict.SCREEN, menu), menu),  # 第一个已稳定帧
        (state(3, Verdict.SCREEN, menu), None),  # 同一次出现只核对一次
        (state(4, Verdict.NONE), None),  # 弹窗：不算离开
        (state(5, Verdict.SCREEN, menu), None),
        (state(6, Verdict.SCREEN, dialogue), None),  # 判出另一个画面：离开
        (state(7, Verdict.SCREEN, menu, stability=Stability.TRANSITION), None),
        (state(8, Verdict.UNDETERMINED), None),
        (state(9, Verdict.SCREEN, menu), menu),  # 再次出现，再核对
    ]
    for scene, expected in sequence:
        assert watch.observe(scene) == expected, scene.frame_number


def test_the_witch_trial_checks_only_the_script(witch_trial: Adapter) -> None:
    """《魔女审判》没有声明固定文字：不做固定文字一层，文字系统一层照常（不用标定）。"""
    watch = LanguageWatch(witch_trial)
    assert watch.items == ()
    assert watch.screens == ()
    assert watch.observe(state(1, Verdict.SCREEN, "dialogue")) is None
    script = ScriptWatch(witch_trial)
    assert (script.script, script.problem) == (Script.HAN, None)
    assert script.wants(closed(1, CHINESE[0]))


# 画面


def test_scene_is_shown_only_when_the_verdict_changes(synthetic: Adapter) -> None:
    hub, collected, formatter = make_hub(synthetic)
    ambiguous = (signature("choice", "choice"), signature("timed-choice", "timed-choice"))
    for scene in [
        state(1, Verdict.NONE, stability=Stability.TRANSITION),
        state(2, Verdict.NONE, detail="别的说明"),
        state(3, Verdict.SCREEN, "title-menu", stability=Stability.CHANGING),
        state(4, Verdict.SCREEN, "title-menu"),
        state(5, Verdict.AMBIGUOUS, signatures=ambiguous, matched=("choice", "timed-choice")),
        state(6, Verdict.AMBIGUOUS, signatures=ambiguous, matched=("timed-choice", "choice")),
        state(7, Verdict.UNDETERMINED, detail="签名 title-menu：固定文字 menu-start 不可判"),
        state(8, Verdict.DISABLED, detail="分辨率不符"),
        state(9, Verdict.SCREEN, "dialogue", stability=Stability.CHANGING),
    ]:
        hub.on_scene(scene)
    events = [e for e in collected.events if isinstance(e, SceneEvent)]
    assert [e.frame_number for e in events] == [1, 3, 5, 7, 8, 9]
    assert [formatter.event(e) for e in events] == [
        "[00:00.1] 画面 不命中，过场，帧 1",
        "[00:00.3] 画面 标题菜单（title-menu），变化中，帧 3",
        "[00:00.5] 画面 歧义：选项（choice）、计时选择（timed-choice），已稳定，帧 5",
        "[00:00.7] 画面 不可判，已稳定，帧 7：签名 title-menu：固定文字 menu-start 不可判",
        "[00:00.8] 画面 停用，已稳定，帧 8：分辨率不符",
        "[00:00.9] 画面 对话（dialogue），变化中，帧 9",
    ]
    assert hub.scene is not None
    assert hub.scene.frame_number == 9


# 剧情记录


def test_sentences_are_shown_when_they_appear_complete_are_revised_and_close(
    synthetic: Adapter,
) -> None:
    hub, collected, formatter = make_hub(synthetic)
    low = (LowConfidenceLine("speaker", "阿谣", 0.62),)
    for change in [
        record(1, "门后", speaker=None, at=SECOND),
        record(1, "门后也许", revision=2, speaker=None),  # 逐字显示中途：不显示
        record(1, "门后也许有路。", Completeness.COMPLETE, 3, at=2 * SECOND),
        record(
            1, "门后也许有路。", Completeness.COMPLETE, 3, speaker="阿谣", low=low, at=2 * SECOND
        ),
        record(1, "门后也许有路，也", Completeness.PARTIAL, 4, at=3 * SECOND),
        record(1, "门后也许有路，也许。", Completeness.PARTIAL, 5),
        record(1, "门后也许有路，也许。", Completeness.COMPLETE, 5, at=4 * SECOND),
        record(1, "门后也许有路，也许。", Completeness.COMPLETE, 5, closed=9, at=4 * SECOND),
        record(2, "你听", at=5 * SECOND),
        record(2, "你听见了", revision=2),
        record(2, "你听见了", Completeness.GAP, 2, closed=12, at=6 * SECOND),
        record(3, "雨停了。", Completeness.COMPLETE, 1, speaker="旁白", closed=14, at=7 * SECOND),
    ]:
        hub.on_record(change)
    events = [e for e in collected.events if isinstance(e, LineEvent)]
    assert [(e.record.occurrence, e.kind) for e in events] == [
        (1, LineKind.APPEAR),
        (1, LineKind.COMPLETE),
        (1, LineKind.REVISE),
        (1, LineKind.REVISE),
        (1, LineKind.COMPLETE),
        (1, LineKind.CLOSE),
        (2, LineKind.APPEAR),
        (2, LineKind.CLOSE),
        (3, LineKind.APPEAR),
        (3, LineKind.CLOSE),
    ]
    assert [formatter.event(e) for e in events] == [
        "[00:01.0] 剧情 #1 出现 「门后」（部分，修订 1）",
        "[00:02.0] 剧情 #1 完整 阿遥「门后也许有路。」（修订 3）",
        "[00:02.0] 剧情 #1 修订 阿谣「门后也许有路。」（完整，修订 3）"
        " 低置信度：名字栏「阿谣」0.62",
        "[00:03.0] 剧情 #1 修订 阿遥「门后也许有路，也」（部分，修订 4）",
        "[00:04.0] 剧情 #1 完整 阿遥「门后也许有路，也许。」（修订 5）",
        "[00:04.0] 剧情 #1 结束（完整）",
        "[00:05.0] 剧情 #2 出现 阿遥「你听」（部分，修订 1）",
        "[00:06.0] 剧情 #2 结束 阿遥「你听见了」（缺口，修订 2）",
        "[00:07.0] 剧情 #3 出现 旁白「雨停了。」（完整，修订 1）",
        "[00:07.0] 剧情 #3 结束（完整）",
    ]
    assert [r.completeness for r in hub.records()] == [
        Completeness.COMPLETE,
        Completeness.GAP,
        Completeness.COMPLETE,
    ]


def cutscene(
    index: int,
    *,
    first: int,
    last: int,
    end: int | None = None,
    closed: int | None = None,
    sentences: tuple[int, ...] = (),
) -> Cutscene:
    """第 `index` 个播片段：帧号与采集时刻相同（毫秒为单位的帧号 × 0.1 秒）。"""
    return Cutscene(
        session=1,
        index=index,
        scope=1,
        first_frame=first,
        first_ns=first * SECOND // 10,
        last_frame=last,
        last_ns=last * SECOND // 10,
        end_ns=end,
        closed_frame=closed,
        sentences=sentences,
    )


def test_cutscenes_are_shown_when_they_start_and_end_and_subtitles_name_them(
    synthetic: Adapter,
) -> None:
    hub, collected, formatter = make_hub(synthetic)
    hub.on_record(record(1, "门后也许有路。", Completeness.COMPLETE, closed=20, at=SECOND))
    hub.on_cutscene(cutscene(1, first=20, last=20))
    hub.on_record(record(2, "雨夜里。", speaker=None, cutscene=1, at=25 * SECOND // 10))
    hub.on_record(
        record(
            2, "雨夜里。", Completeness.COMPLETE, speaker=None, cutscene=1, closed=61, at=6 * SECOND
        )
    )
    ended = cutscene(1, first=20, last=74, end=6 * SECOND, closed=75, sentences=(2,))
    hub.on_cutscene(ended)

    assert [formatter.event(e) for e in collected.events] == [
        "[00:01.0] 剧情 #1 出现 阿遥「门后也许有路。」（完整，修订 1）",
        "[00:01.0] 剧情 #1 结束（完整）",
        "[00:02.0] 播片 1 开始，帧 20",
        "[00:02.5] 剧情 #2 出现 播片 1 「雨夜里。」（部分，修订 1）",
        "[00:06.0] 剧情 #2 结束 播片 1（完整）",
        "[00:06.0] 播片 1 结束：4.0 秒，1 句，帧 75",
    ]
    assert [e.ended for e in collected.events if isinstance(e, CutsceneEvent)] == [False, True]
    assert hub.cutscenes() == (ended,)
    # 结束时刻随中断丢了：时长算到最后一个在播片中的帧，那一行的时刻也是它。
    lost = cutscene(2, first=100, last=130, closed=131)
    hub.on_cutscene(lost)
    assert formatter.event(collected.events[-1]) == "[00:13.0] 播片 2 结束：3.0 秒，0 句，帧 131"
    assert hub.cutscenes() == (ended, lost)


# 语言核对的显示与日志


def test_language_results_are_shown_and_logged(
    synthetic: Adapter, caplog: pytest.LogCaptureFixture
) -> None:
    async def check(texts: list[str]) -> LanguageEvent:
        service = OcrService(
            synthetic,
            ScriptedRecognizer(replies=[[scripted_line(t)] for t in texts]),
            model="scripted",
        )
        try:
            result = await service.check_language(language_frame(5), "title-menu")
        finally:
            await service.aclose()
        outcome = LanguageResult.MATCH if result.passed else LanguageResult.MISMATCH
        return LanguageEvent(SECOND, 5, "title-menu", outcome, result)

    hub, collected, formatter = make_hub(synthetic)
    caplog.set_level(logging.INFO, logger="game_runtime.observe")
    hub.on_language(asyncio.run(check(["开始", "商店", "说明"])))
    hub.on_language(asyncio.run(check(["开始", "商城", "说明"])))
    hub.on_language(
        LanguageEvent(SECOND, 6, "title-menu", LanguageResult.ERROR, None, "ocr@1 不可用")
    )
    assert [formatter.event(e) for e in collected.events] == [
        "[00:01.0] 语言核对 标题菜单（title-menu）相符（zh-Hans）："
        "menu-start「开始」、menu-shop「商店」、menu-help「说明」，帧 5",
        "[00:01.0] 语言核对 标题菜单（title-menu）不符（zh-Hans）："
        "menu-shop 应为「商店」，读出「商城」，帧 5",
        "[00:01.0] 语言核对 标题菜单（title-menu）出错：ocr@1 不可用，帧 6",
    ]
    levels = [(r.levelno, r.getMessage()) for r in caplog.records if "语言核对" in r.getMessage()]
    assert levels == [
        (logging.INFO, "语言核对 title-menu：相符"),
        (logging.WARNING, "语言核对 title-menu：不符：menu-shop 应为“商店”，读出“商城”"),
        (logging.WARNING, "语言核对 title-menu：出错：ocr@1 不可用"),
    ]
    assert dict(hub.language_results["title-menu"]) == {
        LanguageResult.MATCH: 1,
        LanguageResult.MISMATCH: 1,
        LanguageResult.ERROR: 1,
    }


def language_frame(number: int) -> RingFrame:
    """给各菜单 ROI 不同像素，避免在相同空白裁片上脚本返回不同文字。"""
    pixels = bytes(
        channel
        for y in range(HEIGHT)
        for x in range(WIDTH)
        for channel in (x % 256, y % 256, 0, 255)
    )
    return RingFrame(number, number * SECOND // 10, 1, WIDTH, HEIGHT, WIDTH * 4, pixels)


# 摘要


def test_summary_counts_sentences_and_shares_of_time(synthetic: Adapter) -> None:
    hub, _, formatter = make_hub(synthetic)
    for scene in [
        state(1, Verdict.SCREEN, "dialogue", at=0),
        state(2, Verdict.SCREEN, "dialogue", at=SECOND),
        state(3, Verdict.NONE, at=3 * SECOND),
        state(4, Verdict.SCREEN, "dialogue", at=4 * SECOND),
    ]:
        hub.on_scene(scene)
    hub.follow_voice(VoiceHub())
    for change in [
        VoiceChange(VoiceState.BUSY, SECOND, SECOND // 2),
        VoiceChange(VoiceState.IDLE, 3 * SECOND, 3 * SECOND),
        VoiceChange(VoiceState.UNKNOWN, 6 * SECOND, 6 * SECOND, UnknownReason.STALLED),
    ]:
        hub.on_voice(change)
    hub.follow_narrative(None)
    hub.narrative_seen = True
    hub.on_language(LanguageEvent(0, 1, "title-menu", LanguageResult.ERROR, None, "x"))
    hub.close(
        NarrativeFinal(
            records=(
                record(1, "一", Completeness.COMPLETE, closed=3),
                record(2, "二", Completeness.GAP, closed=5),
                record(3, "三", Completeness.COMPLETE, closed=7),
                record(4, "四"),
            ),
            scopes=(Scope(1, 1, None, 1), Scope(1, 2, "bad-ending", 9)),
            # 一段放完了 4 秒；另一段停止时还在放，算到最后一个在播片中的帧，1.5 秒。
            cutscenes=(
                cutscene(1, first=20, last=75, end=6 * SECOND, closed=76),
                cutscene(2, first=80, last=95),
            ),
            discontinuities=(Discontinuity(1, 10, 20, 11),),
        )
    )
    shares = hub.scene_shares()
    assert [(s.key, s.frames, s.ns) for s in shares] == [
        ("dialogue", 3, 3 * SECOND),
        ("none", 1, SECOND),
    ]
    voice = hub.voice_shares(10 * SECOND)
    assert [(s.key, s.ns) for s in voice] == [
        ("busy", 2 * SECOND),
        ("idle", 3 * SECOND),
        ("unknown:stalled", 4 * SECOND),
        ("unknown:starting", SECOND),
    ]
    hub.end_ns = 10 * SECOND
    assert summary(formatter, narrative_problem=None) == [
        "摘要（00:10.0）：",
        "  剧情记录：4 句，完整 2、部分 1、缺口 1；剧情范围 2，中断 1；播片段 2，共 5.5 秒",
        "  画面判别（4 帧，4.0 秒）：对话（dialogue） 75%（3 帧）、不命中 25%（1 帧）",
        "  配音（10.0 秒）：忙 20%、闲 30%、未知（stalled） 40%、未知（starting） 10%",
        "  语言核对（文字系统）：没有完整的句子",
        "  语言核对（固定文字）：标题菜单（title-menu） 1 次，出错 1",
    ]


@pytest.mark.parametrize(
    ("records", "expected"),
    [
        ([], "没有完整的句子"),
        (["……"], "没有判断了的完整句子；另有 1 句无从判断，不计"),
        (CHINESE[:2], "判断了 2 句，都相符，不到 3 句"),
        ([*CHINESE[:1], "……", *CHINESE[1:]], "相符（#1、#3、#4）；另有 1 句无从判断，不计"),
        ([CHINESE[0], OTHERS["korean"]], "不符（#2「안녕하세요, 반갑습니다.」汉字占 0%）"),
    ],
)
def test_the_summary_gives_the_script_result(
    synthetic: Adapter, records: list[str], expected: str
) -> None:
    hub, _, formatter = make_hub(synthetic)
    assert hub.script is not None
    feed_script(hub.script, [closed(n + 1, text) for n, text in enumerate(records)])
    hub.end_ns = SECOND

    lines = summary(formatter, narrative_problem=None)

    assert f"  语言核对（文字系统）：{expected}" in lines


def test_summary_without_narrative_voice_or_checks(uncalibrated: Adapter) -> None:
    hub, _, formatter = make_hub(uncalibrated)
    hub.end_ns = SECOND
    lines = summary(formatter, narrative_problem=narrative_problem(uncalibrated))
    assert lines[1].startswith("  剧情记录：不运行，适配包 witch-trial 的")
    assert lines[2] == "  画面判别：没有处理过任何帧"
    assert lines[3] == "  配音：voice-activity@1 没有激活"
    assert lines[4] == "  语言核对（文字系统）：不核对，不运行剧情记录"
    assert lines[5] == "  语言核对（固定文字）：适配包没有声明，没有核对"


def test_banner_lists_plugins_and_what_is_not_calibrated(uncalibrated: Adapter) -> None:
    _hub, _, formatter = make_hub(uncalibrated)
    plugins = [
        EntryStatus("game", "game-adapter", "1", Tier.CRITICAL, State.ACTIVE, (), "i1", True, 0),
        EntryStatus(
            "ocr",
            "ocr",
            "1",
            Tier.ORDINARY,
            State.FAILED,
            (Reason(ReasonCode.ACTIVATION_FAILED, "ocr", "模型制品不在"),),
            None,
            True,
            1,
        ),
    ]
    lines = banner(
        formatter,
        uncalibrated,
        frame_size=(2560, 1440),
        recording=None,
        plugins=plugins,
        narrative_problem=narrative_problem(uncalibrated),
    )
    assert lines[0] == "只读运行 witch-trial（魔法少女的魔女审判 0.1.0）：不发任何输入，不连 Bellis"
    assert lines[1] == "帧环 2560×1440；不录制"
    assert lines[2] == "插件：game active、ocr failed"
    assert lines[3] == "  ocr failed（activation-failed：模型制品不在）"
    assert lines[4].startswith("剧情记录：不运行，适配包 witch-trial 的")
    assert lines[5].startswith("尚未标定：ROI speaker（名字栏）")
    assert "固定文字" not in lines[5]
    assert "阅读节奏" not in lines[5]
    assert lines[6] == "语言核对（文字系统）：不核对，不运行剧情记录"
    assert lines[7] == "语言核对（固定文字）：适配包没有声明，不核对"
    assert lines[-1] == "按回车或 Ctrl+C 停止。"
    recording = Recording(GAMES_DIR / "x", 50 * 10**9, 1_200_000)
    with_recording = banner(
        formatter,
        uncalibrated,
        frame_size=(2560, 1440),
        recording=recording,
        plugins=plugins,
        narrative_problem=None,
    )
    assert (
        with_recording[1]
        == f"帧环 2560×1440；录制到 {recording.path}（配额 50.00 GB，已用 1.2 MB）"
    )
    assert "语言核对（文字系统）：zh-Hans 按汉字，核对剧情记录里前 3 句完整的句子" in with_recording


def test_plugin_changes_are_shown_without_loading(synthetic: Adapter) -> None:
    hub, collected, formatter = make_hub(synthetic)
    reason = Reason(ReasonCode.SERVICE_MISSING, "ocr@1", "缺少服务 ocr@1")
    hub.transition(Transition("narrative", State.DISPOSED, State.WAITING, (reason,)))
    hub.transition(Transition("narrative", State.WAITING, State.LOADING, ()))
    hub.transition(Transition("narrative", State.LOADING, State.ACTIVE, ()))
    events = [e for e in collected.events if isinstance(e, PluginEvent)]
    assert [(e.entry, e.state) for e in events] == [
        ("narrative", State.WAITING),
        ("narrative", State.ACTIVE),
    ]
    assert formatter.event(events[0]).endswith(
        "插件 narrative waiting（service-missing：缺少服务 ocr@1）"
    )


# observer 插件


class StubNarrative:
    """最小的 `narrative@1`：测试直接发布记录。"""

    def __init__(self) -> None:
        self.items: list[NarrativeRecord] = []
        self.played: list[Cutscene] = []
        self._listeners: list[NarrativeListener] = []
        self._cutscene_listeners: list[CutsceneListener] = []

    @property
    def session(self) -> int:
        return 1

    @property
    def latest(self) -> NarrativeRecord | None:
        return self.items[-1] if self.items else None

    def records(self, after: int = 0) -> tuple[NarrativeRecord, ...]:
        return tuple(self.items[after:])

    @property
    def scopes(self) -> tuple[Scope, ...]:
        return (Scope(1, 1, None, 1),)

    @property
    def cutscenes(self) -> tuple[Cutscene, ...]:
        return tuple(self.played)

    @property
    def discontinuities(self) -> tuple[Discontinuity, ...]:
        return ()

    def subscribe(self, listener: NarrativeListener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def subscribe_cutscenes(self, listener: CutsceneListener) -> Callable[[], None]:
        listeners = self._cutscene_listeners
        listeners.append(listener)
        return lambda: listeners.remove(listener) if listener in listeners else None

    def play(self, item: Cutscene) -> None:
        self.played[item.index - 1 :] = [item]
        for listener in list(self._cutscene_listeners):
            listener(item)

    @property
    def processed_frame(self) -> int:
        return 0

    @property
    def backlog(self) -> int:
        return 0

    async def wait_idle(self) -> None:
        return None

    def publish(self, item: NarrativeRecord) -> None:
        self.items.append(item)
        for listener in list(self._listeners):
            listener(item)


class Shared:
    scene: SceneHub | None = None
    voice: VoiceHub | None = None
    voice_attempts = 0
    narrative: StubNarrative | None = None
    recognizer = ScriptedRecognizer()


@pytest.fixture(autouse=True)
def _reset() -> None:
    Shared.scene = Shared.voice = Shared.narrative = None
    Shared.voice_attempts = 0
    Shared.recognizer = ScriptedRecognizer(
        replies=[[scripted_line("开始")], [scripted_line("商店")], [scripted_line("说明")]]
    )


class FakeScene(Plugin[NoConfig]):
    manifest: ClassVar = Manifest(
        id="fake-scene", version="1", tier=Tier.ORDINARY, provides=(SCENE.ref,)
    )

    async def activate(self, ctx: PluginContext[NoConfig]) -> None:
        Shared.scene = SceneHub(problems=("模板缺失",), uncalibrated=())
        ctx.provide(SCENE, Shared.scene)


class FlakyVoice(Plugin[NoConfig]):
    """第一次激活失败，退避重试后才提供 `voice-activity@1`。"""

    manifest: ClassVar = Manifest(
        id="flaky-voice", version="1", tier=Tier.ORDINARY, provides=(VOICE_ACTIVITY.ref,)
    )

    async def activate(self, ctx: PluginContext[NoConfig]) -> None:
        Shared.voice_attempts += 1
        if Shared.voice_attempts == 1:
            raise RuntimeError("第一次激活失败")
        Shared.voice = VoiceHub()
        ctx.provide(VOICE_ACTIVITY, Shared.voice)


class FakeNarrative(Plugin[NoConfig]):
    manifest: ClassVar = Manifest(
        id="fake-narrative", version="1", tier=Tier.ORDINARY, provides=(NARRATIVE.ref,)
    )

    async def activate(self, ctx: PluginContext[NoConfig]) -> None:
        Shared.narrative = StubNarrative()
        ctx.provide(NARRATIVE, Shared.narrative)


class FakeOcr(Plugin[NoConfig]):
    manifest: ClassVar = Manifest(
        id="fake-ocr",
        version="1",
        tier=Tier.ORDINARY,
        provides=(OCR.ref,),
        inject=(GAME_ADAPTER.ref,),
    )

    async def activate(self, ctx: PluginContext[NoConfig]) -> None:
        service = OcrService(ctx.service(GAME_ADAPTER), Shared.recognizer, model="scripted")
        ctx.register(service.aclose)
        ctx.provide(OCR, service)


def entry(entry_id: str, plugin: str, config: dict[str, str] | None = None) -> ResolvedEntry:
    return ResolvedEntry(
        id=entry_id, plugin=plugin, config=config or {}, disabled=False, grant=frozenset()
    )


async def until[T](probe: Callable[[], T | None], timeout: float = 5.0) -> T:
    deadline = time.monotonic() + timeout
    while (value := probe()) is None:
        assert time.monotonic() < deadline, "等待的条件没有成立"
        await asyncio.sleep(0.01)
    return value


def test_observer_follows_services_and_checks_the_language() -> None:
    collected = Collected()
    hub = ObserveHub(collected)
    results = ResultsHub()

    async def main() -> None:
        kernel = Kernel(
            catalog=catalog(
                GameAdapterPlugin,
                FakeOcr,
                FakeScene,
                FlakyVoice,
                FakeNarrative,
                observer_plugin(hub, results),
            ),
            core=NoControl(),
            options=KernelOptions(retry_delays=(0.1,)),
        )
        kernel.add(
            [
                entry("game", "game-adapter", {"game": "synthetic-game"}),
                entry("ocr", "fake-ocr"),
                entry("scene", "fake-scene"),
                entry("voice", "flaky-voice"),
                entry("narrative", "fake-narrative"),
                entry("observer", "observer"),
            ]
        )
        await kernel.settled()
        assert kernel.status("observer").state is State.ACTIVE
        assert kernel.status("voice").state is State.FAILED
        assert hub.voice is None
        assert hub.scene_problems == ("模板缺失",)
        narrative = Shared.narrative
        assert narrative is not None
        assert hub.narrative is narrative
        assert results.narrative is narrative  # 不录制时由 observer 交给宿主
        # 退避重试后才激活的 voice-activity@1 也被订阅。
        followed = await until(lambda: hub.voice)
        voice = Shared.voice
        assert voice is not None
        assert followed is voice
        assert hub.voice_seen
        voice.publish(
            [VoiceChange(VoiceState.BUSY, 10, 5)], VoiceStatus(VoiceState.BUSY, None, 5, 10)
        )
        assert hub.voice_status is not None
        assert hub.voice_status.state is VoiceState.BUSY
        narrative.publish(record(1, "雨停了。", Completeness.COMPLETE))
        # 播片段的开始与结束也交给宿主显示。
        narrative.play(cutscene(1, first=5, last=5))
        narrative.play(cutscene(1, first=5, last=40, end=4 * SECOND, closed=41))
        # 结束的完整句子经 ocr@1 的 check_script 核对文字系统：3 句中文即相符。
        for occurrence, text in enumerate(CHINESE, start=2):
            narrative.publish(closed(occurrence, text))
        # 标题菜单的第一个已稳定帧：做一次语言核对。
        scene = Shared.scene
        assert scene is not None
        menu = state(2, Verdict.SCREEN, "title-menu")
        scene.publish(SceneUpdate(language_frame(2), menu))
        scene.publish(SceneUpdate(language_frame(3), state(3, Verdict.SCREEN, "title-menu")))
        await until(lambda: hub.language_results or None)
        await kernel.shutdown()

    asyncio.run(main())
    kinds = [type(e) for e in collected.events if not isinstance(e, PluginEvent)]
    # 第一句出现；播片段开始、结束；之后三句各出现、结束一次，第三句结束时文字系统相符。
    assert kinds == [
        VoiceEvent,
        LineEvent,
        *[CutsceneEvent] * 2,
        *[LineEvent] * 6,
        ScriptEvent,
        SceneEvent,
        LanguageEvent,
    ]
    [script] = [e for e in collected.events if isinstance(e, ScriptEvent)]
    assert script.result is ScriptResult.MATCH
    [language] = [e for e in collected.events if isinstance(e, LanguageEvent)]
    assert language.result is LanguageResult.MATCH
    assert language.frame_number == 2
    # 停用时记下了剧情记录此刻的各句，退订后不再跟随。
    assert hub.final is not None
    assert [r.text for r in hub.final.records] == ["雨停了。", *CHINESE]
    assert [c.closed_frame for c in hub.final.cutscenes] == [41]
    assert hub.end_ns is not None
    assert hub.voice is None
    assert hub.narrative is None
    assert results.narrative is None
