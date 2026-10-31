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

from game_runtime.adapter import GAME_ADAPTER, GAMES_DIR, Adapter, GameAdapterPlugin, load_adapter
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
    Discontinuity,
    LowConfidenceLine,
    NarrativeListener,
    NarrativeRecord,
    Scope,
)
from game_runtime.observe import (
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
    VoiceEvent,
    banner,
    clock,
    observer_plugin,
    summary,
)
from game_runtime.observe.display import Recording
from game_runtime.ocr import OCR, OcrService
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
) -> NarrativeRecord:
    return NarrativeRecord(
        session=1,
        occurrence=occurrence,
        scope=1,
        screen="dialogue",
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


# 语言核对的时机


def test_language_is_checked_on_the_first_stable_frame_of_each_appearance(
    synthetic: Adapter,
) -> None:
    watch = LanguageWatch(synthetic)
    assert watch.problem is None
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


def test_language_check_of_the_witch_trial_is_not_calibrated(witch_trial: Adapter) -> None:
    watch = LanguageWatch(witch_trial)
    assert watch.problem is not None
    assert "语言核对的固定文字尚未标定" in watch.problem
    assert watch.screens == ()
    assert watch.observe(state(1, Verdict.SCREEN, "dialogue")) is None


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
            result = await service.check_language(blank(5), "title-menu")
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


def blank(number: int) -> RingFrame:
    return RingFrame(
        number, number * SECOND // 10, 1, WIDTH, HEIGHT, WIDTH * 4, bytes(WIDTH * 4 * HEIGHT)
    )


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
        "  剧情记录：4 句，完整 2、部分 1、缺口 1；剧情范围 2，中断 1",
        "  画面判别（4 帧，4.0 秒）：对话（dialogue） 75%（3 帧）、不命中 25%（1 帧）",
        "  配音（10.0 秒）：忙 20%、闲 30%、未知（stalled） 40%、未知（starting） 10%",
        "  语言核对：标题菜单（title-menu） 1 次，出错 1",
    ]


def test_summary_without_narrative_voice_or_checks(witch_trial: Adapter) -> None:
    hub, _, formatter = make_hub(witch_trial)
    hub.end_ns = SECOND
    lines = summary(formatter, narrative_problem=narrative_problem(witch_trial))
    assert lines[1].startswith("  剧情记录：不运行，适配包 witch-trial 的")
    assert lines[2] == "  画面判别：没有处理过任何帧"
    assert lines[3] == "  配音：voice-activity@1 没有激活"
    assert lines[4] == "  语言核对：尚未标定，没有核对"


def test_banner_lists_plugins_and_what_is_not_calibrated(witch_trial: Adapter) -> None:
    _hub, _, formatter = make_hub(witch_trial)
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
        witch_trial,
        frame_size=(2560, 1440),
        recording=None,
        plugins=plugins,
        narrative_problem=narrative_problem(witch_trial),
    )
    assert lines[0] == "只读运行 witch-trial（魔法少女的魔女审判 0.1.0）：不发任何输入，不连 Bellis"
    assert lines[1] == "帧环 2560×1440；不录制"
    assert lines[2] == "插件：game active、ocr failed"
    assert lines[3] == "  ocr failed（activation-failed：模型制品不在）"
    assert lines[4].startswith("剧情记录：不运行，适配包 witch-trial 的")
    assert lines[5].startswith("尚未标定：ROI speaker（名字栏）")
    assert lines[6].startswith("语言核对：尚未标定，不核对（适配包 witch-trial 的语言核对")
    assert lines[-1] == "按回车或 Ctrl+C 停止。"
    recording = Recording(GAMES_DIR / "x", 50 * 10**9, 1_200_000)
    with_recording = banner(
        formatter,
        witch_trial,
        frame_size=(2560, 1440),
        recording=recording,
        plugins=plugins,
        narrative_problem=None,
    )
    assert (
        with_recording[1]
        == f"帧环 2560×1440；录制到 {recording.path}（配额 50.00 GB，已用 1.2 MB）"
    )


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
        self._listeners: list[NarrativeListener] = []

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
    def discontinuities(self) -> tuple[Discontinuity, ...]:
        return ()

    def subscribe(self, listener: NarrativeListener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

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
        # 标题菜单的第一个已稳定帧：做一次语言核对。
        scene = Shared.scene
        assert scene is not None
        menu = state(2, Verdict.SCREEN, "title-menu")
        scene.publish(SceneUpdate(blank(2), menu))
        scene.publish(SceneUpdate(blank(3), state(3, Verdict.SCREEN, "title-menu")))
        await until(lambda: hub.language_results or None)
        await kernel.shutdown()

    asyncio.run(main())
    kinds = [type(e) for e in collected.events if not isinstance(e, PluginEvent)]
    assert kinds == [VoiceEvent, LineEvent, SceneEvent, LanguageEvent]
    [language] = [e for e in collected.events if isinstance(e, LanguageEvent)]
    assert language.result is LanguageResult.MATCH
    assert language.frame_number == 2
    # 停用时记下了剧情记录此刻的各句，退订后不再跟随。
    assert hub.final is not None
    assert [r.text for r in hub.final.records] == ["雨停了。"]
    assert hub.end_ns is not None
    assert hub.voice is None
    assert hub.narrative is None
    assert results.narrative is None
