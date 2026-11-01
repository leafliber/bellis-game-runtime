"""普通插件 `observer`：只读运行把感知插件的结果交给宿主（`ObserveHub`）显示与统计，并做语言核对的
两层（文字系统、固定文字）。

设计见 `docs/modules/offline-tools.md` 的“observer 插件”与“只读运行的语言核对”。只读：不提供服务，
不申请权限，不写任何文件。
"""

import asyncio
import collections
import contextlib
import logging
from collections.abc import Callable
from typing import Any, ClassVar, Final

from game_runtime.adapter import GAME_ADAPTER
from game_runtime.frame_ring import RingFrame
from game_runtime.kernel import (
    Catalog,
    Manifest,
    NoConfig,
    Plugin,
    PluginContext,
    ResolvedEntry,
    Tier,
    catalog,
)
from game_runtime.narrative import NARRATIVE, NarrativeRecord, NarrativeService
from game_runtime.observe.hub import (
    LanguageEvent,
    LanguageResult,
    NarrativeFinal,
    ObserveHub,
    ScriptWatch,
)
from game_runtime.ocr import OCR
from game_runtime.recording import CatalogFactory, ResultsHub, perception_catalog
from game_runtime.scene import SCENE, SceneState, SceneUpdate
from game_runtime.voice_activity import VOICE_ACTIVITY, VoiceActivityService

logger = logging.getLogger(__name__)

FOLLOW_INTERVAL: Final = 0.5
"""再探测一次可选依赖的间隔（秒）。"""


class ObserverPlugin(Plugin[NoConfig]):
    """订阅 `scene@1`，跟随可选的 `voice-activity@1` 与 `narrative@1`，把结果交给 `hub`；以 `ocr@1`
    核对剧情记录里完整的句子的文字系统，在声明了固定文字的画面上核对固定文字。宿主用
    `observer_plugin` 得到绑定了 `ObserveHub` 的子类。"""

    manifest = Manifest(
        id="observer",
        version="1",
        tier=Tier.ORDINARY,
        inject=(
            GAME_ADAPTER.ref,
            SCENE.ref,
            f"{VOICE_ACTIVITY.ref}?",
            f"{NARRATIVE.ref}?",
            f"{OCR.ref}?",
        ),
    )
    hub: ClassVar[ObserveHub | None] = None
    results: ClassVar[ResultsHub | None] = None

    async def activate(self, ctx: PluginContext[NoConfig]) -> None:
        hub = type(self).hub
        if hub is None:
            raise RuntimeError("observer 没有绑定 ObserveHub，宿主须用 observer_plugin")
        adapter = ctx.service(GAME_ADAPTER)
        scene = ctx.service(SCENE)
        watch, script = hub.attach(adapter, scene_problems=scene.problems)
        checker = _LanguageChecker(ctx, hub)
        ctx.register(checker.close)

        def on_record(record: NarrativeRecord) -> None:
            hub.on_record(record)
            _check_script(ctx, hub, script, record)

        follower = _Follower(ctx, hub, type(self).results, on_record)
        follower.probe()
        task = asyncio.get_running_loop().create_task(
            follower.run(), name=f"{ctx.instance_id} 探测"
        )

        async def stop() -> None:
            task.cancel()
            await asyncio.wait((task,))
            follower.release()

        ctx.register(stop)

        def on_scene(update: SceneUpdate) -> None:
            hub.on_scene(update.state)
            screen = watch.observe(update.state)
            if screen is not None:
                checker.submit(update.frame, update.state, screen)

        ctx.register(scene.subscribe(on_scene))

        def final() -> None:
            narrative = follower.narrative
            hub.close(
                None
                if narrative is None
                else NarrativeFinal(
                    narrative.records(), narrative.scopes, narrative.discontinuities
                )
            )

        # 后登记先撤销：先记下剧情记录此刻的各句，再退订、停探测、取消在途的核对。
        ctx.register(final)


class _Follower:
    """跟随可选依赖：当即探测一次，之后每 `FOLLOW_INTERVAL` 秒再探测；出现了就订阅，换了或停用了
    就退订。"""

    def __init__(
        self,
        ctx: PluginContext[NoConfig],
        hub: ObserveHub,
        results: ResultsHub | None,
        on_record: Callable[[NarrativeRecord], None],
    ) -> None:
        self._ctx = ctx
        self._hub = hub
        self._results = results
        self._on_record = on_record
        self.voice: VoiceActivityService | None = None
        self.narrative: NarrativeService | None = None
        self._unsubscribe_voice: Callable[[], None] | None = None
        self._unsubscribe_narrative: list[Callable[[], None]] = []

    def probe(self) -> None:
        voice = self._ctx.optional(VOICE_ACTIVITY)
        if voice is not self.voice:
            if self._unsubscribe_voice is not None:
                self._unsubscribe_voice()
                self._unsubscribe_voice = None
            if voice is not None:
                self._unsubscribe_voice = voice.subscribe(self._hub.on_voice)
            self.voice = voice
            self._hub.follow_voice(voice)
        narrative = self._ctx.optional(NARRATIVE)
        if narrative is not self.narrative:
            for release in self._unsubscribe_narrative:
                release()
            self._unsubscribe_narrative = []
            if narrative is not None:
                self._unsubscribe_narrative.append(narrative.subscribe(self._on_record))
                if self._results is not None:
                    # 不录制时没有 results：由这里交给宿主，停内核之前等剧情记录处理完。
                    self._unsubscribe_narrative.append(self._results.track(narrative))
            self.narrative = narrative
            self._hub.follow_narrative(narrative)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(FOLLOW_INTERVAL)
            self.probe()

    def release(self) -> None:
        if self._unsubscribe_voice is not None:
            self._unsubscribe_voice()
            self._unsubscribe_voice = None
        for release in self._unsubscribe_narrative:
            release()
        self._unsubscribe_narrative = []
        self.voice = self.narrative = None
        self._hub.follow_voice(None)
        self._hub.follow_narrative(None)


def _check_script(
    ctx: PluginContext[NoConfig], hub: ObserveHub, watch: ScriptWatch, record: NarrativeRecord
) -> None:
    """文字系统一层：结束了的完整的句子以 `ocr@1` 的 `check_script` 核对（同步的纯计算）；此刻没有
    `ocr@1` 时这一句不核对、不计。"""
    if not watch.wants(record):
        return
    ocr = ctx.optional(OCR)
    if ocr is None:
        return
    event = watch.add(record, ocr.check_script(record.text))
    if event is not None:
        hub.on_script(event)


class _LanguageChecker:
    """固定文字一层：同一时刻至多一次在途，后来的排队。"""

    def __init__(self, ctx: PluginContext[NoConfig], hub: ObserveHub) -> None:
        self._ctx = ctx
        self._hub = hub
        self._queue: collections.deque[tuple[RingFrame, SceneState, str]] = collections.deque()
        self._task: asyncio.Task[None] | None = None

    def submit(self, frame: RingFrame, state: SceneState, screen: str) -> None:
        self._queue.append((frame, state, screen))
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name=f"{self._ctx.instance_id} 语言核对"
            )

    async def _run(self) -> None:
        while self._queue:
            frame, state, screen = self._queue.popleft()
            ocr = self._ctx.optional(OCR)
            if ocr is None:
                event = LanguageEvent(
                    state.capture_ns,
                    state.frame_number,
                    screen,
                    LanguageResult.ERROR,
                    None,
                    "ocr@1 不可用",
                )
            else:
                try:
                    check = await ocr.check_language(frame, screen)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    event = LanguageEvent(
                        state.capture_ns,
                        state.frame_number,
                        screen,
                        LanguageResult.ERROR,
                        None,
                        f"{type(error).__name__}：{error}",
                    )
                else:
                    result = LanguageResult.MATCH if check.passed else LanguageResult.MISMATCH
                    event = LanguageEvent(
                        state.capture_ns, state.frame_number, screen, result, check
                    )
            self._hub.on_language(event)

    async def close(self) -> None:
        self._queue.clear()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def observer_plugin(hub: ObserveHub, results: ResultsHub | None = None) -> type[ObserverPlugin]:
    """把结果交给 `hub` 的 `observer` 插件类，放进插件目录；`results` 是实时运行的 `ResultsHub`。"""
    return type("BoundObserverPlugin", (ObserverPlugin,), {"hub": hub, "results": results})


OBSERVER_ENTRY: Final = ResolvedEntry(
    id="observer", plugin="observer", config={}, disabled=False, grant=frozenset()
)
"""只读运行的 `observer` 条目，排在感知插件之后。"""


def observe_catalog(hub: ObserveHub, base: CatalogFactory | None = None) -> CatalogFactory:
    """在 `base`（缺省为 `perception_catalog`）的插件目录里加上绑定了 `hub` 的 `observer`。"""

    def factory(results: ResultsHub) -> Catalog:
        plugins: dict[str, type[Plugin[Any]]] = dict((base or perception_catalog)(results))
        plugins.update(catalog(observer_plugin(hub, results)))
        return plugins

    return factory
