"""只读运行经真实实时核的联调：合成游戏的帧写进假显示面、声音写进假音频输出，实时核的假采集源与
假音频源写进帧环与音频环，`uv run observe` 的运行循环拉起感知插件与 `observer`，核对显示的事件
序列、语言核对、停止的顺序与摘要；加 `--record` 时录制完整。

OCR 用按真值作答的识别器（`TruthOcr`：合成游戏每显示一帧，就记下各 ROI 裁出的图像应读出的文字），
配音检测用按音量判断的假模型，都不要模型文件；剧情记录按缺省的节流读对白框；Windows 上跑命名文件
映射，Linux 容器里跑 POSIX 共享内存。真机经 WGC 的只读运行见 `test_observe_window.py`（手动）。
"""

import asyncio
import contextlib
import functools
import hashlib
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from game_runtime.adapter import ADAPTER_FILE, GAMES_DIR, Adapter, PixelRect, load_adapter
from game_runtime.audio_ring.fake import FakeAudioOutput
from game_runtime.core_link import AudioConfig, CaptureConfig, CoreLink, CoreLinkOptions
from game_runtime.frame_ring.fake import FakeDisplay
from game_runtime.frames import AdmissionPolicy, SequencedFrameSource
from game_runtime.kernel import Kernel
from game_runtime.narrative import Completeness, read_records, store_path
from game_runtime.observe import (
    LanguageEvent,
    LanguageResult,
    LineEvent,
    LineKind,
    SceneEvent,
    ScriptEvent,
    ScriptResult,
    VoiceEvent,
)
from game_runtime.observe.cli import ObserveMonitor, observe
from game_runtime.ocr import Image, RecognizedLine, crop_bgr
from game_runtime.ocr import plugin as ocr_plugin
from game_runtime.recording import (
    LiveOptions,
    Recording,
    ResultsHub,
    adapter_sha256,
    perception_catalog,
)
from game_runtime.recording.layout import LIVE_DIR, ToolInfo, read_meta
from game_runtime.scene import Verdict, store_template
from game_runtime.voice_activity import SpeechModel, VoiceActivityPlugin, VoiceState
from game_runtime.voice_activity.fake import EnergyModel
from synthetic_game import Click, GameConfig, Key, ManualClock, Press, SceneType, SyntheticGame
from synthetic_game.anchors import anchor_templates
from synthetic_game.story import NODES, UI, Dialogue

WIDTH, HEIGHT = 640, 360
RATE, CHANNELS, BLOCK = 48_000, 2, 480
"""10 ms 一块的立体声。"""


def _intro() -> Dialogue:
    node = NODES["intro"]
    assert isinstance(node, Dialogue)
    return node


INTRO = _intro()
"""开场对话：标题菜单的“开始”进入它。"""


class FakeVoice(VoiceActivityPlugin):
    """同一个插件，模型换成按音量判断的假模型。"""

    def load_model(self, data_dir: Path) -> SpeechModel:
        del data_dir
        return EnergyModel()


@dataclass(frozen=True)
class _Pixels:
    """`crop_bgr` 所需的帧：BGRA8，行间无填充。"""

    width: int
    height: int
    pixels: bytes
    frame_number: int = 1
    transform_version: int = 1

    @property
    def row_stride(self) -> int:
        return self.width * 4


def _key(image: Image) -> bytes:
    return hashlib.blake2b(repr(image.shape).encode() + image.tobytes(), digest_size=16).digest()


class TruthOcr:
    """按真值作答的识别器：`learn` 记下合成游戏显示的一帧里各 ROI 应读出的文字（标题菜单的按钮、
    对话的名字与已显示的台词、坏结局的“重来”），`recognize` 按裁出的图像查；没记过的图像读不出
    文字。"""

    def __init__(self, adapter: Adapter) -> None:
        self.adapter = adapter
        self.answers: dict[bytes, str] = {}

    def learn(self, game: SyntheticGame, pixels: bytes) -> None:
        truth = game.truth()
        texts: dict[str, str] = {}
        if truth.overlay is None:
            if truth.scene is SceneType.MENU:
                texts = {
                    "menu-start": UI.menu_start,
                    "menu-shop": UI.menu_shop,
                    "menu-help": UI.menu_help,
                }
            elif truth.scene is SceneType.DIALOGUE:
                node = NODES[truth.node]
                assert isinstance(node, Dialogue)
                line = next(line for line in node.lines if line.text == truth.text)
                texts = {"speaker": line.speaker or "", "dialogue": truth.visible_text}
            elif truth.scene is SceneType.BAD_ENDING:
                texts = {"ending-retry": UI.retry}
        frame = _Pixels(WIDTH, HEIGHT, pixels)
        for roi, text in texts.items():
            rect = self.adapter.roi(roi).to_pixels(WIDTH, HEIGHT)
            self.answers[_key(crop_bgr(frame, rect))] = text

    def recognize(self, image: Image) -> list[RecognizedLine]:
        text = self.answers.get(_key(image))
        if not text:
            return []
        return [RecognizedLine(text, PixelRect(0, 0, image.shape[1], image.shape[0]), 0.95)]


@pytest.fixture(scope="module")
def synthetic() -> Adapter:
    return load_adapter(GAMES_DIR / "synthetic-game")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return Path("\\\\?\\" + str(tmp_path.resolve())) if sys.platform == "win32" else tmp_path


@pytest.fixture
def data_dir(root: Path) -> Path:
    """临时的本地数据目录，里面是合成游戏的锚点模板。"""
    directory = root / "data"
    for template in anchor_templates():
        store_template(directory, "synthetic-game", template.png)
    return directory


@pytest.fixture
def ocr(synthetic: Adapter, monkeypatch: pytest.MonkeyPatch) -> TruthOcr:
    """`ocr@1` 的识别器换成按真值作答的。"""
    recognizer = TruthOcr(synthetic)
    monkeypatch.setattr(ocr_plugin, "load_engine", lambda *args: recognizer)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    return recognizer


def tone(number: int, loud: bool) -> bytes:
    t = (np.arange(BLOCK) + number * BLOCK) / RATE
    mono = 0.4 * np.sin(2 * np.pi * 300 * t) if loud else np.zeros(BLOCK)
    return np.repeat(mono, CHANNELS).astype(np.float32).tobytes()


class Player:
    """测试一侧的“游戏”：每 20 ms 显示一帧合成游戏的画面（手动时钟同步推进），并发出声音。"""

    def __init__(self, display: FakeDisplay, output: FakeAudioOutput, ocr: TruthOcr) -> None:
        self.display = display
        self.output = output
        self.ocr = ocr
        self.clock = ManualClock()
        self.game = SyntheticGame(GameConfig(WIDTH, HEIGHT), self.clock)

    async def show(self) -> None:
        while True:
            self.clock.advance(0.02)
            frame = self.game.frame()
            self.ocr.learn(self.game, frame.data)
            self.display.present(frame.data, WIDTH, HEIGHT, capture_ns=time.perf_counter_ns())
            await asyncio.sleep(0.02)

    def click(self, target: str) -> None:
        rect = next(t.rect for t in self.game.truth().targets if t.id == target)
        self.game.handle(Click(*rect.center))

    def advance(self) -> None:
        self.game.handle(Press(Key.ENTER))

    async def sound(self, pattern: list[tuple[float, bool]]) -> None:
        """依次发出（时长，有声与否）的声音，每 10 ms 一块，采集时刻正好相隔一块的时长；之后不再
        发出。"""
        start = time.perf_counter_ns()
        number = 0
        for seconds, loud in pattern:
            for _ in range(round(seconds * 100)):
                capture = start + number * 10_000_000
                while time.perf_counter_ns() < capture:
                    await asyncio.sleep(0.005)
                number += 1
                self.output.play(tone(number, loud), capture_ns=capture)


async def until[T](probe: Callable[[], T | None], timeout: float = 15.0) -> T:
    deadline = time.monotonic() + timeout
    while (value := probe()) is None:
        assert time.monotonic() < deadline, "等待的条件没有成立"
        await asyncio.sleep(0.01)
    return value


def options(
    core_exe: Path,
    adapter: Adapter,
    data_dir: Path,
    display: FakeDisplay,
    output: FakeAudioOutput,
    *,
    record: bool = False,
    games_dir: Path | None = None,
) -> LiveOptions:
    return LiveOptions(
        link=CoreLinkOptions(
            core_exe, heartbeat_interval=0.05, heartbeat_timeout=0.3, health_interval=0.05
        ),
        adapter=adapter,
        adapter_sha256=adapter_sha256(adapter.id, games_dir),
        capture=CaptureConfig(target=display.target, max_width=WIDTH, max_height=HEIGHT),
        audio=AudioConfig(
            target=output.target,
            sample_rate=RATE,
            channels=CHANNELS,
            block_capacity=BLOCK,
            block_count=64,
        ),
        data_dir=data_dir,
        tool=ToolInfo(commit=None, dirty=None),
        games_dir=games_dir,
        name="observe-test" if record else None,
        policy=AdmissionPolicy(max_rate=50.0, idle_interval=0.1),
        plugins=functools.partial(perception_catalog, voice=FakeVoice),
        record=record,
    )


def complete(monitor: ObserveMonitor, occurrence: int) -> LineEvent | None:
    for event in monitor.events:
        if (
            isinstance(event, LineEvent)
            and event.record.occurrence == occurrence
            and event.kind is LineKind.COMPLETE
        ):
            return event
    return None


def languages(monitor: ObserveMonitor) -> list[LanguageEvent]:
    return [e for e in monitor.events if isinstance(e, LanguageEvent)]


def scripts(monitor: ObserveMonitor) -> list[ScriptEvent]:
    return [e for e in monitor.events if isinstance(e, ScriptEvent)]


async def play(
    live: LiveOptions,
    player: Player,
    monitor: ObserveMonitor,
    *,
    quota: int | None = None,
    dialogue: bool = True,
) -> int:
    """显示标题菜单直到做完固定文字的核对；`dialogue` 时再进入开场对话，读完三句（其间发出 0.4 秒
    的声音），推进到选项，文字系统一层随之相符；然后像 Ctrl+C 那样取消主任务，返回退出码。"""
    showing = asyncio.create_task(player.show())
    observing = asyncio.create_task(
        observe(live, quota=quota, status_interval=0.5, monitor=monitor)
    )
    try:
        await until(lambda: monitor.session)
        await until(lambda: languages(monitor) or None)
        if dialogue:
            player.click("menu.start")
            sound = asyncio.create_task(player.sound([(0.4, True), (0.8, False)]))
            await until(lambda: complete(monitor, 1))
            player.advance()
            await until(lambda: complete(monitor, 2))
            player.advance()
            await until(lambda: complete(monitor, 3))
            player.advance()
            await until(lambda: scripts(monitor) or None)
            await sound
        await asyncio.sleep(0.3)
        observing.cancel()
        return await observing
    finally:
        for task in (showing, observing):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.fixture
def stops(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """记下收尾的各步按什么顺序发生。"""
    order: list[str] = []

    def wrap(owner: type, name: str, label: str) -> None:
        original = getattr(owner, name)

        @functools.wraps(original)
        async def wrapper(self: object, *args: object, **kwargs: object) -> object:
            order.append(label)
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(owner, name, wrapper)

    wrap(SequencedFrameSource, "close", "close frames")
    wrap(SequencedFrameSource, "wait_ended", "wait readers")
    wrap(ResultsHub, "drain", "drain narrative")
    wrap(Kernel, "shutdown", "stop kernel")
    wrap(CoreLink, "stop", "stop core")
    return order


def test_observing_the_synthetic_game_shows_its_events_and_a_summary(
    core_exe: Path,
    synthetic: Adapter,
    data_dir: Path,
    ocr: TruthOcr,
    stops: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    lines: list[str] = []
    monitor = ObserveMonitor(lines.append)

    async def main() -> int:
        with (
            FakeDisplay.create(WIDTH, HEIGHT, slot_count=2) as display,
            FakeAudioOutput.create(RATE, CHANNELS, block_capacity=BLOCK, block_count=64) as output,
        ):
            player = Player(display, output, ocr)
            return await play(
                options(core_exe, synthetic, data_dir, display, output), player, monitor
            )

    assert asyncio.run(main()) == 0
    out = capsys.readouterr().out
    # 开始的几行。
    assert lines[0] == "只读运行 synthetic-game（合成游戏 0.1.0）：不发任何输入，不连 Bellis"
    assert lines[1] == "帧环 640×360；不录制"
    assert lines[2] == (
        "插件：game active、ocr active、scene active、voice active、narrative active、"
        "observer active"
    )
    assert "语言核对（文字系统）：zh-Hans 按汉字，核对剧情记录里前 3 句完整的句子" in lines
    assert "语言核对（固定文字）：标题菜单（title-menu）每次出现时核对，共 3 项" in lines
    # 事件的顺序：标题菜单 → 语言核对相符 → 对话 → 第一句出现、完整、结束 → 第二句 ……
    scenes = [e for e in monitor.events if isinstance(e, SceneEvent)]
    screens = [e.screen for e in scenes if e.verdict is Verdict.SCREEN]
    assert screens[:2] == ["title-menu", "dialogue"]
    [language] = languages(monitor)
    assert language.result is LanguageResult.MATCH
    assert language.check is not None
    assert [o.result.text for o in language.check.outcomes] == ["开始", "商店", "说明"]
    order = [
        e
        for e in monitor.events
        if isinstance(e, LanguageEvent)
        or (isinstance(e, SceneEvent) and e.screen == "dialogue")
        or isinstance(e, LineEvent)
    ]
    assert isinstance(order[0], LanguageEvent)
    assert isinstance(order[1], SceneEvent)
    sentences = [(e.record.occurrence, e.kind) for e in order[2:] if isinstance(e, LineEvent)]
    assert sentences[:9] == [
        (1, LineKind.APPEAR),
        (1, LineKind.COMPLETE),
        (1, LineKind.CLOSE),
        (2, LineKind.APPEAR),
        (2, LineKind.COMPLETE),
        (2, LineKind.CLOSE),
        (3, LineKind.APPEAR),
        (3, LineKind.COMPLETE),
        (3, LineKind.CLOSE),
    ]
    # 文字系统一层：前三句完整的中文句子都相符，在第三句结束之后显示。
    [script] = scripts(monitor)
    assert script.result is ScriptResult.MATCH
    assert [(r.occurrence, r.text) for r, _ in script.checks] == [
        (n, line.text) for n, line in enumerate(INTRO.lines, start=1)
    ]
    shown = monitor.formatter.event(script)
    assert shown.endswith("语言核对 文字系统相符（zh-Hans，汉字）：#1 100%、#2 100%、#3 100%")
    assert shown in lines
    first, second = (complete(monitor, n) for n in (1, 2))
    assert first is not None
    assert second is not None
    assert (first.record.speaker, first.record.text) == (
        INTRO.lines[0].speaker,
        INTRO.lines[0].text,
    )
    assert (second.record.speaker, second.record.text) == (
        INTRO.lines[1].speaker,
        INTRO.lines[1].text,
    )
    displayed = monitor.formatter.event(first)
    assert displayed.endswith(
        f"剧情 #1 完整 旁白「{INTRO.lines[0].text}」（修订 {first.record.revision}）"
    )
    assert displayed in lines
    voices = [e.change.state for e in monitor.events if isinstance(e, VoiceEvent)]
    assert VoiceState.BUSY in voices
    assert VoiceState.IDLE in voices[voices.index(VoiceState.BUSY) :]
    assert any(line.startswith("[") and " 积压 帧 " in line for line in lines)  # 状态行
    # 停止：按顺序收尾，最后是摘要与实时核停在哪一步。
    assert "收到 Ctrl+C，正在收尾（再按一次即强制退出）……" in out
    assert stops == ["close frames", "wait readers", "drain narrative", "stop kernel", "stop core"]
    summary = lines[lines.index(next(x for x in lines if x.startswith("摘要（"))) :]
    records = monitor.hub.records()
    counts = {c: sum(1 for r in records if r.completeness is c) for c in Completeness}
    assert summary[1] == (
        f"  剧情记录：{len(records)} 句，完整 {counts[Completeness.COMPLETE]}、"
        f"部分 {counts[Completeness.PARTIAL]}、缺口 {counts[Completeness.GAP]}；剧情范围 1，中断 0"
    )
    assert len(records) >= 3
    assert counts[Completeness.COMPLETE] >= 3
    assert summary[2].startswith("  画面判别（")
    assert "标题菜单（title-menu）" in summary[2]
    assert "对话（dialogue）" in summary[2]
    assert summary[3].startswith("  配音（")
    assert "忙 " in summary[3]
    assert summary[4] == "  语言核对（文字系统）：相符（#1、#2、#3）"
    assert summary[5] == "  语言核对（固定文字）：标题菜单（title-menu） 1 次，相符 1"
    assert "已停止（interrupt）" in out
    assert "实时核：shutdown，退出码 0" in out
    # 不录制：不建录制目录；剧情记录写进本地数据目录，就是实时运行的剧情记录。
    assert not (data_dir / "recordings").exists()
    stored = read_records(store_path(data_dir, "synthetic-game"), records[0].session)
    assert [r.text for r in stored] == [r.text for r in records]


def test_a_changed_fixed_text_is_reported_as_a_mismatch(
    core_exe: Path, root: Path, data_dir: Path, ocr: TruthOcr
) -> None:
    """合成的不符样本：适配包里把“说明”改成“帮助”，画面上仍是“说明”。"""
    games = root / "games"
    (games / "synthetic-game").mkdir(parents=True)
    source = (GAMES_DIR / "synthetic-game" / ADAPTER_FILE).read_text(encoding="utf-8")
    assert source.count('text = "说明"') == 1
    (games / "synthetic-game" / ADAPTER_FILE).write_text(
        source.replace('text = "说明"', 'text = "帮助"'), encoding="utf-8", newline="\n"
    )
    changed = load_adapter(games / "synthetic-game")
    ocr.adapter = changed
    lines: list[str] = []
    monitor = ObserveMonitor(lines.append)

    async def main() -> int:
        with (
            FakeDisplay.create(WIDTH, HEIGHT, slot_count=2) as display,
            FakeAudioOutput.create(RATE, CHANNELS, block_capacity=BLOCK, block_count=64) as output,
        ):
            player = Player(display, output, ocr)
            live = options(core_exe, changed, data_dir, display, output, games_dir=games)
            return await play(live, player, monitor, dialogue=False)

    assert asyncio.run(main()) == 0
    [language] = languages(monitor)
    assert language.result is LanguageResult.MISMATCH
    assert language.check is not None
    assert [(o.item.roi, o.item.text, o.result.text) for o in language.check.mismatches] == [
        ("menu-help", "帮助", "说明")
    ]
    assert any(
        line.endswith(
            "语言核对 标题菜单（title-menu）不符（zh-Hans）：menu-help 应为「帮助」，"
            f"读出「说明」，帧 {language.frame_number}"
        )
        for line in lines
    )
    assert "  语言核对（固定文字）：标题菜单（title-menu） 1 次，不符 1" in lines
    assert "  语言核对（文字系统）：没有完整的句子" in lines


def test_observing_with_record_finishes_the_recording(
    core_exe: Path,
    synthetic: Adapter,
    data_dir: Path,
    ocr: TruthOcr,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lines: list[str] = []
    monitor = ObserveMonitor(lines.append)

    async def main() -> int:
        with (
            FakeDisplay.create(WIDTH, HEIGHT, slot_count=2) as display,
            FakeAudioOutput.create(RATE, CHANNELS, block_capacity=BLOCK, block_count=64) as output,
        ):
            player = Player(display, output, ocr)
            live = options(core_exe, synthetic, data_dir, display, output, record=True)
            return await play(live, player, monitor, quota=10**9)

    assert asyncio.run(main()) == 0
    out = capsys.readouterr().out
    [path] = (data_dir / "recordings" / "synthetic-game").iterdir()
    assert lines[1].startswith(f"帧环 640×360；录制到 {path}（配额 1.00 GB，已用 ")
    assert "收到 Ctrl+C，正在收尾（再按一次即强制退出，录制将不完整）……" in out
    assert f"录制目录：{path}" in out
    meta = read_meta(path)
    assert (meta.state, meta.stop_reason) == ("complete", "interrupt")
    recording = Recording.open(path)
    assert len(recording.frames) == meta.frames.count > 0
    # 录制时剧情记录与其它结果写进 live/，与只读运行显示的是同一组句子。
    live = path / LIVE_DIR
    assert (live / "narrative-final.jsonl").is_file()
    records = monitor.hub.records()
    stored = read_records(store_path(live, "synthetic-game"), records[0].session)
    assert [r.text for r in stored] == [r.text for r in records]
    assert not store_path(data_dir, "synthetic-game").exists()
    assert "  语言核对（文字系统）：相符（#1、#2、#3）" in lines
    assert "  语言核对（固定文字）：标题菜单（title-menu） 1 次，相符 1" in lines
