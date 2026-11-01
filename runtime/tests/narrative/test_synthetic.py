"""在合成游戏上按真值逐句核对剧情记录：`game-adapter`、`scene`、`ocr` 与 `narrative` 在内核中，帧由
合成游戏渲染、经帧源逐帧交给 `scene@1`。

真值来自合成游戏的状态机：哪一句（测试自己按推进记下）、说话人、已显示的文字与是否显示完。`Play`
按模块文档的规则从真值算出应有的记录（含按采集时刻的 OCR 节流），与 `narrative@1` 的记录逐句比较；
名字栏与对白文字的 OCR 用按真值作答的识别器（`TruthRecognizer`），标 `ocr_model` 的一项用真实模型。
除专门测节流的一项外都关掉节流（`ocr_interval` 为 0），每个对白框帧都读。
"""

import asyncio
import collections
import dataclasses
import hashlib
import itertools
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import pytest

from core_protocol import models
from game_runtime.adapter import (
    ADAPTER_FILE,
    GAME_ADAPTER,
    GAMES_DIR,
    Adapter,
    GameAdapterPlugin,
    PixelRect,
    load_adapter,
)
from game_runtime.artifacts import ArtifactError
from game_runtime.frame_ring import RingFrame
from game_runtime.kernel import (
    FRAMES_READ,
    ControlGrant,
    Kernel,
    KernelOptions,
    Manifest,
    NoConfig,
    Plugin,
    PluginContext,
    ResolvedEntry,
    State,
    Submission,
    Tier,
    catalog,
)
from game_runtime.local_data import local_data_dir
from game_runtime.narrative import (
    NARRATIVE,
    NARRATIVE_WRITE,
    Completeness,
    Discontinuity,
    NarrativeConfig,
    NarrativePlugin,
    NarrativeRecord,
    NarrativeService,
    Scope,
    TrackerParams,
    compare_key,
    read_discontinuities,
    read_records,
    read_scopes,
    store_path,
)
from game_runtime.ocr import (
    DEFAULT_MODEL_SET,
    MODEL_SETS,
    OCR,
    EngineOptions,
    Image,
    OcrEngine,
    OcrPlugin,
    OcrService,
    RecognizedLine,
    Recognizer,
    crop_bgr,
)
from game_runtime.ocr import plugin as ocr_plugin
from game_runtime.ocr.models import ModelSet
from game_runtime.scene import (
    SCENE,
    ScenePlugin,
    SceneService,
    SceneState,
    SceneUpdate,
    SignatureJudge,
    Stability,
    bgra_view,
)
from game_runtime.scene import Outcome as SignatureOutcome
from synthetic_game import Click, GameConfig, ManualClock, SceneType, SyntheticGame, Truth
from synthetic_game.story import NODES, UI, Dialogue, Line

FRAME: Final = 1 / 30
WAIT: Final = 30.0
HIGH: Final = 0.97
LOW: Final = 0.6
PARAMS: Final = TrackerParams()
"""插件配置的缺省值。"""


class NoCore:
    """这里的插件都不是控制产出者，内核不会调用实时核。"""

    async def invalidate_generation(self, grant: ControlGrant) -> None:
        raise AssertionError("不应调用")

    async def wait_released(self, grant: ControlGrant) -> None:
        raise AssertionError("不应调用")

    async def submit(
        self,
        grant: ControlGrant,
        steps: Sequence[models.ControlStep],
        *,
        base_frame: int,
        valid_for: float,
        start_delay: float = 0.0,
    ) -> Submission:
        raise AssertionError("不应调用")


def image_key(image: Image) -> bytes:
    return hashlib.blake2b(repr(image.shape).encode() + image.tobytes(), digest_size=16).digest()


class TruthRecognizer:
    """按真值作答的识别器：`teach` 记下一个 ROI 裁出的图像应读出的文字，`recognize` 按图像查。

    `low` 里的文字以低置信度给出，`failing` 里的文字识别出错；`gate` 设下时识别前先等它。
    """

    def __init__(self) -> None:
        self.answers: dict[bytes, str] = {}
        self.low: set[str] = set()
        self.failing: set[str] = set()
        self.misses = 0
        self.calls = 0
        self.gate: threading.Event | None = None
        self.waiting = threading.Event()

    def teach(self, frame: RingFrame, rect: PixelRect, text: str) -> None:
        key = image_key(crop_bgr(frame, rect))
        known = self.answers.setdefault(key, text)
        assert known == text, f"同样的像素教了两种文字：{known!r} 与 {text!r}"

    def recognize(self, image: Image) -> list[RecognizedLine]:
        self.calls += 1
        if self.gate is not None:
            self.waiting.set()
            assert self.gate.wait(WAIT)
        text = self.answers.get(image_key(image))
        if text is None:
            self.misses += 1
            raise LookupError("没有教过这张图")
        if text in self.failing:
            raise RuntimeError(f"识别“{text}”出错")
        if not text:
            return []
        box = PixelRect(0, 0, image.shape[1], image.shape[0])
        return [RecognizedLine(text, box, LOW if text in self.low else HIGH)]


class Shared:
    """替身插件与测试之间共享的东西，每个测试重置。"""

    recognizer: Recognizer = TruthRecognizer()
    scene: SceneService | None = None
    narrative: NarrativeService | None = None


@pytest.fixture(autouse=True)
def _reset() -> None:
    Shared.recognizer = TruthRecognizer()
    Shared.scene = None
    Shared.narrative = None


class FakeOcr(Plugin[NoConfig]):
    """以 `Shared.recognizer` 提供真正的 `OcrService`。"""

    manifest = Manifest(
        id="fake-ocr",
        version="1",
        tier=Tier.ORDINARY,
        provides=(OCR.ref,),
        inject=(GAME_ADAPTER.ref,),
    )

    async def activate(self, ctx: PluginContext[NoConfig]) -> None:
        service = OcrService(ctx.service(GAME_ADAPTER), Shared.recognizer, model="truth")
        ctx.register(service.aclose)
        ctx.provide(OCR, service)


class Probe(Plugin[NoConfig]):
    """依赖 `scene@1` 与 `narrative@1` 的探针插件，把服务交给测试。"""

    manifest = Manifest(
        id="probe", version="1", tier=Tier.ORDINARY, inject=(SCENE.ref, NARRATIVE.ref)
    )

    async def activate(self, ctx: PluginContext[NoConfig]) -> None:
        Shared.scene = ctx.service(SCENE)
        Shared.narrative = ctx.service(NARRATIVE)


class FeedSource:
    """单个读者的帧源：读到的帧之前的都丢掉，内存里只留还没读的帧。"""

    def __init__(self) -> None:
        self._frames: collections.deque[RingFrame] = collections.deque()
        self._closed = False
        self._event = asyncio.Event()

    def push(self, frame: RingFrame) -> None:
        self._frames.append(frame)
        self._event.set()

    def close(self) -> None:
        self._closed = True
        self._event.set()

    async def next_frame(self, after: int) -> RingFrame | None:
        while True:
            while self._frames and self._frames[0].frame_number <= after:
                self._frames.popleft()
            if self._frames:
                return self._frames[0]
            if self._closed:
                return None
            self._event.clear()
            await self._event.wait()


async def until[T](probe: Callable[[], T | None], timeout: float = WAIT) -> T:
    deadline = time.monotonic() + timeout
    while (value := probe()) is None:
        assert time.monotonic() < deadline, "等待的条件没有成立"
        await asyncio.sleep(0.002)
    return value


# 真值与应有的记录


def speaker_of(node: str, text: str) -> str:
    dialogue = NODES[node]
    assert isinstance(dialogue, Dialogue)
    return next(line.speaker for line in dialogue.lines if line.text == text)


@dataclass
class Expected:
    """按真值算出的一句：规则同模块文档，只是“哪一句”取自真值而不是 OCR。"""

    serial: int
    speaker: str
    scope: int
    first_frame: int
    first_ns: int
    full: str
    text: str = ""
    revision: int = 0
    completeness: Completeness = Completeness.PARTIAL
    last_frame: int = 0
    last_ns: int = 0
    revision_frame: int = 0
    complete_frame: int | None = None
    closed_frame: int | None = None
    same: int = 0
    changed_ns: int = 0
    texts: list[str] = field(default_factory=list[str])
    """显示过的各个文字，按顺序。"""


def row(item: Expected | NarrativeRecord) -> tuple[Any, ...]:
    return (
        item.speaker,
        item.text,
        item.revision,
        item.completeness,
        item.scope,
        item.first_frame,
        item.first_ns,
        item.last_frame,
        item.last_ns,
        item.revision_frame,
        item.complete_frame,
        item.closed_frame,
    )


class Play:
    """驱动合成游戏：每次观察渲染一帧、教给识别器、放进帧源，并按真值更新应有的记录。"""

    def __init__(
        self,
        frames: FeedSource,
        adapter: Adapter,
        recognizer: TruthRecognizer | None,
        *,
        marker: bool = True,
        params: TrackerParams = PARAMS,
        interval_ns: int = 0,
        size: tuple[int, int] = (1280, 720),
        elsewhere: Callable[[Truth], dict[str, str]] | None = None,
    ) -> None:
        self.frames = frames
        self.adapter = adapter
        self.recognizer = recognizer
        self.marker = marker
        self.params = params
        self.interval_ns = interval_ns
        """节流：两次读至少相隔这么久（采集时刻）。"""
        self.last_read: int | None = None
        self.elsewhere = elsewhere
        """不是对话（或有弹窗）的帧上，对白框的两个 ROI 应读出的文字；不写即不教。"""
        self.clock = ManualClock()
        self.game = SyntheticGame(GameConfig(*size), self.clock)
        self.number = 0
        self.serial = 0
        self.expected: list[Expected] = []
        self.current: Expected | None = None
        self.scope = 0
        self.scope_pending = False
        self.pace: Callable[[int], Awaitable[None]] | None = None

    # 观察

    async def observe(self, seconds: float = FRAME) -> None:
        self.clock.advance(seconds)
        rendered = self.game.frame()
        self.number += 1
        capture_ns = round(self.clock.now() * 1e9)
        frame = RingFrame(
            self.number,
            capture_ns,
            1,
            rendered.width,
            rendered.height,
            rendered.stride,
            rendered.data,
        )
        truth = self.game.truth()
        if self.recognizer is not None:
            self._teach(frame, truth)
        self.frames.push(frame)
        self._expect(truth, capture_ns)
        if self.pace is not None:
            await self.pace(self.number)

    def _teach(self, frame: RingFrame, truth: Truth) -> None:
        assert self.recognizer is not None
        size = (frame.width, frame.height)
        if truth.scene is SceneType.DIALOGUE and truth.overlay is None:
            speaker = speaker_of(truth.node, truth.text)
            self.recognizer.teach(frame, self.adapter.roi("speaker").to_pixels(*size), speaker)
            text_rect = self.adapter.roi("dialogue").to_pixels(*size)
            self.recognizer.teach(frame, text_rect, truth.visible_text)
        elif self.elsewhere is not None:
            for roi, text in self.elsewhere(truth).items():
                self.recognizer.teach(frame, self.adapter.roi(roi).to_pixels(*size), text)
        # 画面签名的固定文字项（标题菜单的“开始”、坏结局的“重来”）：ROI 里的按钮文字。
        for signature in self.adapter.signatures:
            for item in signature.texts:
                rect = self.adapter.roi(item.roi).to_pixels(*size)
                labels = [
                    t.label
                    for t in truth.targets
                    if t.label
                    and rect.left <= t.rect.center[0] < rect.right
                    and rect.top <= t.rect.center[1] < rect.bottom
                ]
                self.recognizer.teach(frame, rect, "".join(labels))

    def _expect(self, truth: Truth, capture_ns: int) -> None:
        number = self.number
        if truth.overlay is not None:
            return  # 弹窗压暗画面：不命中签名，不改变记录
        if truth.scene is not SceneType.DIALOGUE:
            self._close(number)
            if truth.scene is SceneType.BAD_ENDING:
                self.scope_pending = True
            return
        if (
            self.interval_ns
            and self.last_read is not None
            and capture_ns - self.last_read < self.interval_ns
        ):
            return  # 节流：相隔不够，这一帧不读
        self.last_read = capture_ns
        current = self.current
        if current is not None and current.serial != self.serial:
            self._close(number)
            current = None
        visible = truth.visible_text
        if not visible:
            self._close(number)
            return
        if current is None:
            if self.scope == 0 or self.scope_pending:
                self.scope += 1
                self.scope_pending = False
            speaker = speaker_of(truth.node, truth.text)
            current = Expected(self.serial, speaker, self.scope, number, capture_ns, truth.text)
            self.expected.append(current)
            self.current = current
        if visible != current.text:
            current.text = visible
            current.texts.append(visible)
            current.revision += 1
            current.revision_frame = number
            current.same = 1
            current.changed_ns = capture_ns
            current.completeness = Completeness.PARTIAL
            current.complete_frame = None
        else:
            current.same += 1
        current.last_frame, current.last_ns = number, capture_ns
        if current.completeness is not Completeness.COMPLETE:
            marker = self.marker and visible == truth.text
            stable = (
                current.same >= self.params.complete_frames
                and capture_ns - current.changed_ns >= self.params.complete_after_ns
            )
            if marker or stable:
                current.completeness = Completeness.COMPLETE
                current.complete_frame = number

    def _close(self, number: int) -> None:
        current = self.current
        if current is None:
            return
        self.current = None
        current.closed_frame = number
        if current.completeness is not Completeness.COMPLETE:
            current.completeness = Completeness.GAP

    # 操作

    async def watch(self, count: int = 1, seconds: float = FRAME) -> None:
        for _ in range(count):
            await self.observe(seconds)

    def press(self, target: str) -> None:
        """点击一个目标，不观察。只有点击还没显示完的一句（补全）仍是同一句。"""
        truth = self.game.truth()
        rect = next(t.rect for t in truth.targets if t.id == target)
        reveal = target == "dialogue.box" and truth.visible_text != truth.text
        effect = self.game.handle(Click(*rect.center))
        assert effect.target == target
        if not reveal:
            self.serial += 1

    async def click(self, target: str) -> None:
        self.press(target)
        await self.observe()

    async def type_out(self) -> None:
        """逐帧看着这一句显示完，再多看两帧。"""
        while (truth := self.game.truth()).visible_text != truth.text:
            await self.observe()
        await self.watch(2)

    async def read(self) -> None:
        await self.type_out()
        await self.click("dialogue.box")

    async def drain(self) -> None:
        """等 scene 处理完已放进帧源的帧，再等剧情记录处理完它们。"""
        scene, narrative = Shared.scene, Shared.narrative
        assert scene is not None
        assert narrative is not None
        await until(lambda: _processed(scene, self.number))
        await narrative.wait_idle()

    async def skip(self) -> None:
        """两帧之间点两下：补全并推进，补全的那一刻没有被采到。"""
        self.press("dialogue.box")
        self.press("dialogue.box")
        await self.observe()


type Script = Callable[[Play], Awaitable[None]]


async def full_story(play: Play) -> None:
    """标题菜单；开场三句（看完、补全、未读完被推进）；两次敲门（同一句重复出现）；回去睡觉的坏结局；
    重来后读到地下与计时选择、等到超时的坏结局；再重来，停在第一句的逐字显示中。"""
    await play.watch(3)
    await play.click("menu.start")
    await play.type_out()  # 开场第一句：看完，继续标记出现
    await play.click("dialogue.box")
    await play.watch(4)
    await play.click("dialogue.box")  # 第二句：显示到一半时补全
    await play.watch(2)
    await play.click("dialogue.box")
    await play.watch(5)
    await play.skip()  # 第三句：没显示完就被推进，进入选项
    await play.watch(2)
    for _ in range(2):  # 敲门回应两次：同样的文字，两句
        await play.click("choice.1")
        await play.read()
        await play.watch(2)
    await play.click("choice.2")  # 回去睡觉：坏结局
    await play.watch(2)
    await play.click("ending.retry")  # 新的剧情范围
    await play.watch(1, 1.0)  # 第一句一下子显示完
    await play.watch(2)
    await play.click("dialogue.box")
    await play.watch(3)
    await play.skip()  # 第二句没显示完就被推进
    await play.read()
    await play.click("choice.0")  # 地下
    await play.read()
    await play.read()  # 读完弹出获得物品的弹窗
    await play.watch(3)
    await play.click("popup.ok")
    while play.game.truth().node != "late":  # 计时选择：等到超时
        await play.watch(1, 0.5)
    await play.watch(2)
    await play.click("ending.retry")  # 又一个剧情范围
    await play.watch(5)  # 停在逐字显示中：这一句保持部分


@dataclass
class RunResult:
    records: tuple[NarrativeRecord, ...]
    scopes: tuple[Scope, ...]
    discontinuities: tuple[Discontinuity, ...]
    expected: list[Expected]
    session: int
    stored: list[NarrativeRecord]
    stored_scopes: list[Scope]


def entry(
    entry_id: str, plugin: str, config: dict[str, Any] | None = None, grant: Sequence[str] = ()
) -> ResolvedEntry:
    return ResolvedEntry(
        id=entry_id, plugin=plugin, config=config or {}, disabled=False, grant=frozenset(grant)
    )


@dataclass
class Rig:
    """一次运行的设定：适配包、本地数据目录、OCR 与插件配置。"""

    data_dir: Path
    games_dir: Path = GAMES_DIR
    real_ocr: bool = False
    marker: bool = True
    ocr_interval: float = 0.0
    """`narrative@1` 的节流；除专门测它的一项外关掉。"""
    narrative: dict[str, Any] = field(default_factory=dict[str, Any])
    pace_narrative: bool = False
    elsewhere: Callable[[Truth], dict[str, str]] | None = None

    @property
    def adapter(self) -> Adapter:
        return load_adapter(self.games_dir / "synthetic-game")

    def profile(self) -> list[ResolvedEntry]:
        game = {"game": "synthetic-game", "games_dir": str(self.games_dir)}
        data = str(self.data_dir)
        return [
            entry("game", "game-adapter", game),
            entry("ocr", "ocr" if self.real_ocr else "fake-ocr"),
            entry("scene", "scene", {"data_dir": data}, [FRAMES_READ]),
            entry("narrative", "narrative", {"data_dir": data, **self.config}, [NARRATIVE_WRITE]),
            entry("probe", "probe"),
        ]

    @property
    def config(self) -> dict[str, Any]:
        return {"ocr_interval": self.ocr_interval, **self.narrative}

    async def run(self, script: Script) -> RunResult:
        frames = FeedSource()
        kernel = Kernel(
            catalog=catalog(
                GameAdapterPlugin, OcrPlugin, FakeOcr, ScenePlugin, NarrativePlugin, Probe
            ),
            core=NoCore(),
            options=KernelOptions(retry_delays=(), activate_timeout=WAIT),
            frames=frames,
        )
        kernel.add(self.profile())
        await kernel.settled()
        assert kernel.status("narrative").state is State.ACTIVE, kernel.status("narrative")
        scene, narrative = Shared.scene, Shared.narrative
        assert scene is not None
        assert narrative is not None
        recognizer = Shared.recognizer
        config = NarrativeConfig.model_validate(self.config)
        play = Play(
            frames,
            self.adapter,
            None if self.real_ocr or not isinstance(recognizer, TruthRecognizer) else recognizer,
            marker=self.marker,
            params=config.tracker_params(),
            interval_ns=config.ocr_interval_ns,
            elsewhere=self.elsewhere,
        )

        async def pace(number: int) -> None:
            # 不让帧源与剧情记录的积压无限增长：scene 落后至多 8 帧。
            await until(lambda: _processed(scene, number - 8))
            if self.pace_narrative:
                await until(lambda: True if narrative.backlog <= 8 else None)

        play.pace = pace
        await script(play)
        await play.drain()
        records = narrative.records()
        scopes = narrative.scopes
        discontinuities = narrative.discontinuities
        session = narrative.session
        await kernel.shutdown()
        path = store_path(self.data_dir, "synthetic-game")
        return RunResult(
            records,
            scopes,
            discontinuities,
            play.expected,
            session,
            read_records(path, session),
            read_scopes(path, session),
        )


def _processed(scene: SceneService, number: int) -> bool | None:
    latest = scene.latest
    return True if number <= 0 or (latest and latest.state.frame_number >= number) else None


def check(outcome: RunResult) -> None:
    """记录与按真值算出的逐句相同，存储里的与内存里的相同。"""
    assert [row(r) for r in outcome.records] == [row(e) for e in outcome.expected]
    assert outcome.stored == list(outcome.records)
    assert outcome.stored_scopes == list(outcome.scopes)
    assert [r.occurrence for r in outcome.records] == list(range(1, len(outcome.records) + 1))
    assert all(r.session == outcome.session for r in outcome.records)


# 按真值逐句核对


def test_the_whole_story_matches_the_truth(data_dir: Path) -> None:
    outcome = asyncio.run(Rig(data_dir).run(full_story))

    check(outcome)
    recognizer = Shared.recognizer
    assert isinstance(recognizer, TruthRecognizer)
    assert recognizer.misses == 0
    records = outcome.records
    intro = NODES["intro"]
    knock = NODES["knock"]
    assert isinstance(intro, Dialogue)
    assert isinstance(knock, Dialogue)
    assert [(r.speaker, r.completeness, r.scope) for r in records] == [
        ("旁白", Completeness.COMPLETE, 1),  # 看完
        ("阿遥", Completeness.COMPLETE, 1),  # 补全
        ("阿遥", Completeness.GAP, 1),  # 未读完即被推进
        ("旁白", Completeness.COMPLETE, 1),  # 敲门回应
        ("旁白", Completeness.COMPLETE, 1),  # 再一次
        ("旁白", Completeness.COMPLETE, 2),  # 重来后，一下子显示完
        ("阿遥", Completeness.GAP, 2),
        ("阿遥", Completeness.COMPLETE, 2),
        ("旁白", Completeness.COMPLETE, 2),  # 地下
        ("阿遥", Completeness.COMPLETE, 2),
        ("旁白", Completeness.PARTIAL, 3),  # 停在逐字显示中
    ]
    first, revealed, skipped, knocked, again, instant, *_, last = records
    # 逐字显示：每多显示一个字修订号加一；看完的一句文字就是全文。
    assert first.text == intro.lines[0].text
    assert first.revision == len(outcome.expected[0].texts) >= len(first.text) - 2
    assert first.complete_frame == first.revision_frame
    # 补全：显示到一半的文字直接变成全文，仍是同一句。
    assert revealed.text == intro.lines[1].text
    assert revealed.revision >= 2
    # 缺口：文字只是这一句的前一部分。
    assert intro.lines[2].text.startswith(skipped.text)
    assert skipped.text != intro.lines[2].text
    assert skipped.complete_frame is None
    assert skipped.closed_frame is not None
    # 同一句重复出现：两句，出现编号不同，都完整。
    assert knocked.text == again.text == knock.lines[0].text
    assert knocked.occurrence != again.occurrence
    assert instant.revision == 1
    assert last.closed_frame is None
    assert [(s.scope, s.after_screen) for s in outcome.scopes] == [
        (1, None),
        (2, "bad-ending"),
        (3, "bad-ending"),
    ]
    assert outcome.discontinuities == ()


def test_the_same_frames_give_the_same_records(data_dir: Path) -> None:
    """同样的帧序列跑两次（回放），记录除运行号外逐句相同。"""
    first = asyncio.run(Rig(data_dir).run(full_story))
    second = asyncio.run(Rig(data_dir).run(full_story))

    assert second.session > first.session
    assert [dataclasses.replace(r, session=0) for r in first.records] == [
        dataclasses.replace(r, session=0) for r in second.records
    ]
    assert [dataclasses.replace(s, session=0) for s in first.scopes] == [
        dataclasses.replace(s, session=0) for s in second.scopes
    ]


def test_reading_is_throttled_by_capture_time(data_dir: Path) -> None:
    """缺省的节流：同一 ROI 两次读至少相隔 0.25 秒（采集时刻），其间的帧跳过、不计入连续帧数。记录
    与按同样规则从真值算出的逐句相同，识别少得多；同样的帧跑两次得到同样的记录（回放）。"""
    unthrottled = asyncio.run(Rig(data_dir).run(full_story))
    every_frame = Shared.recognizer
    assert isinstance(every_frame, TruthRecognizer)

    Shared.recognizer = TruthRecognizer()
    rig = Rig(data_dir, ocr_interval=NarrativeConfig().ocr_interval)
    throttled = asyncio.run(rig.run(full_story))
    recognizer = Shared.recognizer
    assert isinstance(recognizer, TruthRecognizer)

    check(throttled)
    assert recognizer.misses == 0
    assert recognizer.calls * 3 < every_frame.calls
    assert sum(r.revision for r in throttled.records) < sum(r.revision for r in unthrottled.records)
    # 读过的帧（每句的首帧、最近一次变化的帧与最后出现的帧）的采集时刻都相隔不短于 0.25 秒。
    read = sorted(
        {e.first_ns for e in throttled.expected}
        | {e.changed_ns for e in throttled.expected}
        | {e.last_ns for e in throttled.expected}
    )
    assert all(b - a >= 250_000_000 for a, b in itertools.pairwise(read))

    Shared.recognizer = TruthRecognizer()
    again = asyncio.run(rig.run(full_story))
    assert [dataclasses.replace(r, session=0) for r in again.records] == [
        dataclasses.replace(r, session=0) for r in throttled.records
    ]


def _without_screen(root: Path, *, keep: bool = False) -> Path:
    """合成游戏的适配包，对白框不写 `screen`（通用缺省）；`keep` 时另留着写了 `screen` 的一项。"""
    games = root / "games" / "synthetic-game"
    games.mkdir(parents=True)
    source = (GAMES_DIR / "synthetic-game" / ADAPTER_FILE).read_text(encoding="utf-8")
    box = (
        '[[dialogue_boxes]]\nscreen = "dialogue"\nspeaker = "speaker"\ntext = "dialogue"\n'
        'continue_signature = "dialogue-continue"\n'
    )
    assert source.count(box) == 1
    generic = box.replace('screen = "dialogue"\n', "")
    (games / ADAPTER_FILE).write_text(
        source.replace(box, (box + "\n" if keep else "") + generic), encoding="utf-8"
    )
    return root / "games"


def _menu_help_in_the_box(truth: Truth) -> dict[str, str]:
    """标题菜单上，对白文字的 ROI 盖住了“说明”按钮的下半：假定 OCR 读出它；别处读不出文字。"""
    if truth.scene is SceneType.MENU and truth.overlay is None:
        return {"speaker": "", "dialogue": UI.menu_help}
    return {"speaker": "", "dialogue": ""}


@pytest.mark.parametrize("keep", [False, True], ids=["generic only", "with a dialogue box"])
def test_a_box_without_a_screen_reads_every_frame_that_is_not_a_transition(
    data_dir: Path, root: Path, keep: bool
) -> None:
    """不写 `screen` 的对白框：在每个不是过场的帧上读，判别结果不限。对白照样逐句记下；别的画面
    落在 ROI 里的文字也记成句子（标题菜单的“说明”）；离开对白时由读到的空文字结束这一句，过场的帧
    不读；坏结局照样开始新的范围。与写了 `screen` 的一项并存时，对话画面按那一项读。"""
    states: dict[int, SceneState] = {}
    marks: dict[str, int] = {}

    async def script(play: Play) -> None:
        scene = Shared.scene
        assert scene is not None

        def remember(update: SceneUpdate) -> None:
            states[update.state.frame_number] = update.state

        scene.subscribe(remember)
        await play.watch(3)
        await play.click("menu.start")
        await play.read()
        await play.read()
        await play.watch(3)
        await play.skip()  # 第三句没显示完就被推进，进入选项
        marks["choice"] = play.number
        await play.watch(4)
        await play.click("choice.2")  # 回去睡觉：坏结局
        await play.watch(3)
        await play.click("ending.retry")
        await play.watch(1, 1.0)
        await play.watch(2)

    games = _without_screen(root, keep=keep)
    rig = Rig(data_dir, games_dir=games, elsewhere=_menu_help_in_the_box)
    outcome = asyncio.run(rig.run(script))

    intro = NODES["intro"]
    assert isinstance(intro, Dialogue)
    records = outcome.records
    assert [r.text for r in records[:3]] == [UI.menu_help, intro.lines[0].text, intro.lines[1].text]
    assert intro.lines[2].text.startswith(records[3].text)
    assert [r.completeness for r in records[:4]] == [
        Completeness.GAP,  # 菜单上的“说明”：没等到完整就进了对话
        Completeness.COMPLETE,
        Completeness.COMPLETE,
        Completeness.GAP,
    ]
    dialogue_screen = "dialogue" if keep else None
    assert [r.screen for r in records[:4]] == [None, *[dialogue_screen] * 3]
    # 第三句由选项画面上第一个不是过场的帧结束：过场的帧不读，不结束它。
    closed = records[3].closed_frame
    assert closed is not None
    first = marks["choice"]
    assert closed == min(
        n for n, s in states.items() if n >= first and s.stability is not Stability.TRANSITION
    )
    # 重来之后的一句在新的范围里。
    assert [(s.scope, s.after_screen) for s in outcome.scopes] == [(1, None), (2, "bad-ending")]
    assert records[-1].scope == 2
    assert records[-1].text == intro.lines[0].text
    assert outcome.stored == list(records)


def test_the_same_text_after_the_box_clears_is_a_new_sentence(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两句一样的台词接连出现：推进后对白框先清空（或重新开始逐字显示），记为两句。"""
    line = Line("旁白", "门那边安静了一会儿，又敲了三下。")
    monkeypatch.setitem(NODES, "knock", Dialogue(lines=(line, line), next="door"))

    async def script(play: Play) -> None:
        await play.click("menu.start")
        for _ in range(3):
            await play.read()
        await play.click("choice.1")
        await play.read()
        await play.read()
        await play.watch(2)

    outcome = asyncio.run(Rig(data_dir).run(script))

    check(outcome)
    repeated = [r for r in outcome.records if r.text == line.text]
    assert len(repeated) == 2
    assert all(r.completeness is Completeness.COMPLETE for r in repeated)
    closed = repeated[0].closed_frame
    assert closed is not None
    assert repeated[1].first_frame in (closed, closed + 1)


def test_without_a_continue_marker_lines_complete_by_staying_unchanged(
    data_dir: Path, root: Path
) -> None:
    """适配包不声明继续标记时，文字连续 3 帧不变且过了 0.5 秒才算完整。"""
    games = root / "games" / "synthetic-game"
    games.mkdir(parents=True)
    source = (GAMES_DIR / "synthetic-game" / ADAPTER_FILE).read_text(encoding="utf-8")
    marker_line = 'continue_signature = "dialogue-continue"\n'
    assert source.count(marker_line) == 1
    (games / ADAPTER_FILE).write_text(source.replace(marker_line, ""), encoding="utf-8")

    async def script(play: Play) -> None:
        await play.click("menu.start")
        await play.type_out()
        await play.watch(20)  # 显示完后再过约 0.7 秒
        await play.click("dialogue.box")
        await play.type_out()  # 显示完了，但只看了两帧：还不算完整
        await play.click("dialogue.box")
        await play.watch(3)
        await play.skip()
        await play.watch(2)

    outcome = asyncio.run(Rig(data_dir, games_dir=root / "games", marker=False).run(script))

    check(outcome)
    assert [r.completeness for r in outcome.records] == [
        Completeness.COMPLETE,
        Completeness.GAP,
        Completeness.GAP,
    ]
    first = outcome.records[0]
    assert first.complete_frame is not None
    assert first.complete_frame > first.revision_frame + 2


def test_low_confidence_lines_are_marked(data_dir: Path) -> None:
    recognizer = TruthRecognizer()
    recognizer.low.add("阿遥")
    Shared.recognizer = recognizer

    async def script(play: Play) -> None:
        await play.click("menu.start")
        for _ in range(2):
            await play.read()

    outcome = asyncio.run(Rig(data_dir).run(script))

    check(outcome)
    narrator, ayao, *_ = outcome.records
    assert narrator.low_confidence == ()
    assert [(line.roi, line.text) for line in ayao.low_confidence] == [("speaker", "阿遥")]
    assert ayao.low_confidence[0].confidence == pytest.approx(LOW)
    assert outcome.stored[1].low_confidence == ayao.low_confidence


def test_an_ocr_error_leaves_that_frame_out(
    data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    recognizer = TruthRecognizer()
    intro = NODES["intro"]
    assert isinstance(intro, Dialogue)
    recognizer.failing.add(intro.lines[0].text[:3])
    Shared.recognizer = recognizer

    async def script(play: Play) -> None:
        await play.click("menu.start")
        await play.read()

    outcome = asyncio.run(Rig(data_dir).run(script))

    [record] = outcome.records
    [expected] = outcome.expected
    assert record.text == expected.text
    assert record.completeness is Completeness.COMPLETE
    assert record.revision == expected.revision - 1  # 出错的那一帧不算
    warnings = [r for r in caplog.records if "OCR 出错" in r.getMessage()]
    assert len(warnings) == 1


def test_an_overflowing_backlog_is_recorded_as_a_discontinuity(
    data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """OCR 卡住时积压到上限，之后到来的更新丢弃到积压降到一半；当前句记为缺口。"""
    recognizer = TruthRecognizer()
    gate = threading.Event()
    recognizer.gate = gate
    Shared.recognizer = recognizer

    async def script(play: Play) -> None:
        await play.click("menu.start")
        await play.watch(19)
        # 20 帧都交给了 scene，剧情记录还卡在第一帧的 OCR 上；放开后处理完。
        scene, narrative = Shared.scene, Shared.narrative
        assert scene is not None
        assert narrative is not None
        await until(lambda: _processed(scene, play.number))
        await asyncio.to_thread(recognizer.waiting.wait, WAIT)
        # 排满的 4 个，多半另有一个在处理（第一帧在第二帧到来之前就已取走）。
        assert narrative.backlog in (4, 5)
        gate.set()
        await narrative.wait_idle()
        assert narrative.backlog == 0
        assert narrative.processed_frame == 20

    with caplog.at_level(logging.ERROR, logger="game_runtime.narrative.plugin"):
        outcome = asyncio.run(Rig(data_dir, narrative={"max_backlog": 4}).run(script))

    [gap] = outcome.discontinuities
    assert gap.last_frame == 20
    assert gap.count == gap.last_frame - gap.first_frame + 1
    assert gap.first_frame >= 3
    [record] = outcome.records
    assert record.completeness is Completeness.GAP
    assert record.closed_frame == gap.first_frame
    path = store_path(data_dir, "synthetic-game")
    assert read_discontinuities(path, outcome.session) == [gap]
    assert any("积压达到上限" in r.getMessage() for r in caplog.records)
    assert any("丢弃了帧" in r.getMessage() for r in caplog.records)


def test_subscribers_get_every_change_in_order(
    data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    received: list[NarrativeRecord] = []

    def broken(record: NarrativeRecord) -> None:
        raise RuntimeError("监听者出错")

    async def script(play: Play) -> None:
        narrative = Shared.narrative
        assert narrative is not None
        assert (narrative.latest, narrative.records(), narrative.processed_frame) == (None, (), 0)
        stop = narrative.subscribe(received.append)
        stop_broken = narrative.subscribe(broken)
        await play.click("menu.start")
        await play.read()
        await play.watch(3)
        await play.drain()
        stop_broken()
        stop()
        stop()  # 可以重复调用
        await play.read()

    outcome = asyncio.run(Rig(data_dir).run(script))

    first, second, *_ = outcome.records
    # 第一句：每次修订（最后一次修订的那一帧继续标记出现，同时判为完整）、结束各通知一次，按发生的
    # 顺序；第二句在结束第一句的同一帧之后新建。
    mine = [r for r in received if r.occurrence == 1]
    assert [r.revision for r in mine[:-1]] == list(range(1, first.revision + 1))
    assert [r.completeness for r in mine[:-2]] == [Completeness.PARTIAL] * (first.revision - 1)
    assert mine[-2].completeness is Completeness.COMPLETE
    assert mine[-2].closed_frame is None
    assert mine[-1] == first
    later = [r for r in received if r.occurrence == 2]
    assert received.index(mine[-1]) < received.index(later[0])
    # 取消订阅之后的变化（第二句显示完、结束）不再收到。
    assert second.closed
    assert all(r.completeness is Completeness.PARTIAL for r in later)
    assert max(r.revision for r in later) < second.revision
    # 抛错的监听者每次都只记录，不影响别的监听者。
    errors = [r for r in caplog.records if "监听者处理第" in r.getMessage()]
    assert len(errors) == len(received)


def test_stopping_keeps_the_open_sentence_as_it_was(data_dir: Path) -> None:
    """停用时还没结束的一句保持当时的完整度，最后出现的帧号写进存储。"""

    async def script(play: Play) -> None:
        await play.click("menu.start")
        await play.type_out()
        await play.watch(10)

    outcome = asyncio.run(Rig(data_dir).run(script))

    check(outcome)
    [record] = outcome.stored
    assert record.completeness is Completeness.COMPLETE
    assert record.closed_frame is None
    assert record.last_frame == outcome.expected[0].last_frame > record.complete_frame + 10  # pyright: ignore[reportOptionalOperand]


# 继续标记的签名


@pytest.mark.parametrize("size", [(1280, 720), (2560, 1440), (1920, 1080), (640, 360)])
def test_the_continue_signature_hits_only_when_a_line_is_finished(
    data_dir: Path, size: tuple[int, int]
) -> None:
    adapter = load_adapter(GAMES_DIR / "synthetic-game")
    judge = SignatureJudge.load(adapter, data_dir)
    clock = ManualClock()
    game = SyntheticGame(GameConfig(*size), clock)
    rect = next(t.rect for t in game.truth().targets if t.id == "menu.start")
    game.handle(Click(*rect.center))
    seen: list[tuple[bool, bool]] = []
    for step in range(25):
        clock.advance(FRAME)
        rendered = game.frame()
        frame = RingFrame(
            step + 1, 0, 1, rendered.width, rendered.height, rendered.stride, rendered.data
        )
        judgement = judge.judge(frame, bgra_view(frame), None)
        [marker] = [s for s in judgement.signatures if s.id == "dialogue-continue"]
        truth = game.truth()
        assert judgement.screen == "dialogue"
        seen.append((marker.outcome is SignatureOutcome.MATCH, truth.visible_text == truth.text))
    assert all(hit == finished for hit, finished in seen)
    assert (True, True) in seen
    assert (False, False) in seen


# 真实 OCR


@pytest.fixture(scope="module")
def real_engine() -> OcrEngine:
    try:
        return OcrEngine.load(MODEL_SETS[DEFAULT_MODEL_SET], local_data_dir(), EngineOptions())
    except ArtifactError as error:
        pytest.skip(f"OCR 模型不可用：{error}")


@pytest.mark.ocr_model
def test_the_whole_story_with_real_ocr(
    data_dir: Path, real_engine: OcrEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实模型读名字栏与对白：每句的说话人、文字（归一后）、完整度、范围与帧号引用都与真值相同；
    修订号随 OCR 读出的不同文字而定，与真值的差至多为 2。同样的帧跑两次得到同样的记录。"""

    def load(model_set: ModelSet, data_dir: Path, options: EngineOptions) -> Recognizer:
        return real_engine

    monkeypatch.setattr(ocr_plugin, "load_engine", load)
    rig = Rig(data_dir, real_ocr=True, pace_narrative=True)
    outcome = asyncio.run(rig.run(full_story))

    def strict(item: Expected | NarrativeRecord) -> tuple[Any, ...]:
        speaker, text, _revision, *rest = row(item)
        return (compare_key(speaker or ""), compare_key(text), *rest)

    report = [
        f"{r.occurrence:2d} {r.completeness.value:8s} 修订 {r.revision:2d}（真值 {e.revision:2d}）"
        f" {r.speaker}：{r.text}" + (f"  ← 真值 {e.text}" if strict(r) != strict(e) else "")
        for r, e in zip(outcome.records, outcome.expected, strict=False)
    ]
    print("\n".join(report))
    assert [strict(r) for r in outcome.records] == [strict(e) for e in outcome.expected]
    assert all(
        abs(r.revision - e.revision) <= 2
        for r, e in zip(outcome.records, outcome.expected, strict=True)
    )
    assert outcome.stored == list(outcome.records)

    again = asyncio.run(rig.run(full_story))
    assert [dataclasses.replace(r, session=0) for r in again.records] == [
        dataclasses.replace(r, session=0) for r in outcome.records
    ]
