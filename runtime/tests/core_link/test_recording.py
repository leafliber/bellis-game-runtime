"""录制与 M0 回放经真实实时核的联调：合成游戏的帧写进假显示面、声音写进假音频输出，实时核的假采集源
与假音频源写进帧环与音频环，定序帧源接纳的帧交给感知插件并录下，声音按块录下；回放同一段录制，
`scene@1` 的逐帧状态、`voice-activity@1` 的变化与喂入标记都与实时运行逐条相同，录下的像素与样本值与
写进去的逐一相同。另核对中途停止（像 Ctrl+C）时收尾完整、不留半截文件。

OCR 用按脚本回答的识别器，配音检测用按音量判断的假模型，都不要模型文件；Windows 上跑命名文件映射，
Linux 容器里跑 POSIX 共享内存。真机经 WGC 的录制见 `test_record_window.py`（手动）。
"""

import asyncio
import contextlib
import functools
import sys
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from game_runtime.adapter import GAMES_DIR, Adapter, load_adapter
from game_runtime.audio_ring.fake import FakeAudioOutput
from game_runtime.core_link import AudioConfig, CaptureConfig, CoreLinkOptions, StopStage
from game_runtime.frame_ring.fake import FakeDisplay
from game_runtime.frames import AdmissionPolicy
from game_runtime.ocr import Image, RecognizedLine
from game_runtime.ocr import plugin as ocr_plugin
from game_runtime.ocr.fake import ScriptedRecognizer, scripted_line
from game_runtime.recording import (
    NARRATIVE_FINAL_FILE,
    RESULT_FILES,
    LiveOptions,
    LiveSession,
    Recording,
    adapter_sha256,
    perception_catalog,
    replay,
)
from game_runtime.recording.cli import record
from game_runtime.recording.layout import LIVE_DIR, REPLAY_DIR, ToolInfo, read_meta
from game_runtime.scene import store_template
from game_runtime.voice_activity import SpeechModel, VoiceActivityPlugin
from game_runtime.voice_activity.fake import EnergyModel
from synthetic_game import Click, GameConfig, ManualClock, SyntheticGame
from synthetic_game.anchors import anchor_templates

WIDTH, HEIGHT = 640, 360
RATE, CHANNELS, BLOCK = 48_000, 2, 480
"""10 ms 一块的立体声。"""


def read(image: Image) -> list[RecognizedLine]:
    """OCR 的替身：按像素给出一行确定的文字，逐字显示时随之变化，同样的像素总是同样的文字。"""
    return [scripted_line(f"第{int(image.sum(dtype=np.int64)) % 9973}句")]


class FakeVoice(VoiceActivityPlugin):
    """同一个插件，模型换成按音量判断的假模型。"""

    def load_model(self, data_dir: Path) -> SpeechModel:
        del data_dir
        return EnergyModel()


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时的本地数据目录，里面是合成游戏的锚点模板；OCR 换成按脚本回答的识别器。"""
    root = Path("\\\\?\\" + str(tmp_path.resolve())) if sys.platform == "win32" else tmp_path
    directory = root / "data"
    for template in anchor_templates():
        store_template(directory, "synthetic-game", template.png)
    monkeypatch.setattr(ocr_plugin, "load_engine", lambda *args: ScriptedRecognizer(answer=read))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    return directory


@pytest.fixture(scope="module")
def synthetic() -> Adapter:
    return load_adapter(GAMES_DIR / "synthetic-game")


def options(
    core_exe: Path,
    synthetic: Adapter,
    data_dir: Path,
    display: FakeDisplay,
    output: FakeAudioOutput,
) -> LiveOptions:
    return LiveOptions(
        link=CoreLinkOptions(
            core_exe, heartbeat_interval=0.05, heartbeat_timeout=0.3, health_interval=0.05
        ),
        adapter=synthetic,
        adapter_sha256=adapter_sha256(synthetic.id),
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
        name="link-test",
        policy=AdmissionPolicy(max_rate=50.0, idle_interval=0.1),
        plugins=functools.partial(perception_catalog, voice=FakeVoice),
    )


def tone(number: int, loud: bool) -> bytes:
    t = (np.arange(BLOCK) + number * BLOCK) / RATE
    mono = 0.4 * np.sin(2 * np.pi * 300 * t) if loud else np.zeros(BLOCK)
    return np.repeat(mono, CHANNELS).astype(np.float32).tobytes()


class Player:
    """测试一侧的“游戏”：按墙钟显示合成游戏的帧、发出声音，记下写进去的像素与样本值。"""

    def __init__(self, display: FakeDisplay, output: FakeAudioOutput) -> None:
        self.display = display
        self.output = output
        self.clock = ManualClock()
        self.game = SyntheticGame(GameConfig(WIDTH, HEIGHT), self.clock)
        self.pixels: dict[int, bytes] = {}
        """采集时刻 → 显示的像素。"""
        self.samples: dict[int, bytes] = {}
        """采集时刻 → 发出的样本值。"""

    async def show(self, seconds: float) -> None:
        """每 20 ms 显示一帧，一直显示 `seconds` 秒；画面按手动时钟逐字显示。"""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.clock.advance(0.02)
            frame = self.game.frame()
            capture = time.perf_counter_ns()
            self.display.present(frame.data, WIDTH, HEIGHT, capture_ns=capture)
            self.pixels[capture] = frame.data
            await asyncio.sleep(0.02)

    def click(self, target: str) -> None:
        rect = next(t.rect for t in self.game.truth().targets if t.id == target)
        self.game.handle(Click(*rect.center))

    async def sound(self, pattern: list[tuple[float, bool]]) -> None:
        """依次发出（时长，有声与否）的声音，每 10 ms 一块；之后不再发出，块号不前进。

        按墙钟定时发出，采集时刻正好相隔一块的时长。不用 `asyncio.sleep(0.01)` 定时：Windows 上
        事件循环的时钟精度约 15.6 ms，更短的定时器会立即到期。
        """
        start = time.perf_counter_ns()
        number = 0
        for seconds, loud in pattern:
            for _ in range(round(seconds * 100)):
                capture = start + number * 10_000_000
                while time.perf_counter_ns() < capture:
                    await asyncio.sleep(0.005)
                number += 1
                data = tone(number, loud)
                self.output.play(data, capture_ns=capture)
                self.samples[capture] = data


async def until[T](probe: Callable[[], T | None], timeout: float = 10.0) -> T:
    deadline = time.monotonic() + timeout
    while (value := probe()) is None:
        assert time.monotonic() < deadline, "等待的条件没有成立"
        await asyncio.sleep(0.01)
    return value


def leftovers(directory: Path) -> list[Path]:
    return [path for path in directory.rglob("*") if path.name.endswith(".tmp")]


def test_a_live_recording_replays_to_the_same_results(
    core_exe: Path, synthetic: Adapter, data_dir: Path
) -> None:
    async def main() -> tuple[Path, Player]:
        with (
            FakeDisplay.create(WIDTH, HEIGHT, slot_count=2) as display,
            FakeAudioOutput.create(RATE, CHANNELS, block_capacity=BLOCK, block_count=64) as output,
        ):
            player = Player(display, output)
            session = LiveSession(options(core_exe, synthetic, data_dir, display, output))
            await session.start()
            try:
                # 有声 0.3 秒、静音 0.6 秒，之后不再发出声音（块号不前进）；画面先是标题菜单，
                # 再进入对话逐字显示。
                sound = asyncio.create_task(player.sound([(0.3, True), (0.6, False)]))
                await player.show(0.4)
                player.click("menu.start")
                await player.show(1.6)
                await sound
            finally:
                result = await session.stop("seconds")
            assert result.drained
            assert result.core.stage is StopStage.SHUTDOWN
            assert result.meta is not None
            assert result.path is not None
            assert result.meta.state == "complete"
            assert result.meta.reader_overflows == 0
            return result.path, player

    path, player = asyncio.run(main())
    recording = Recording.open(path)
    assert len(recording.frames) > 10
    # 录下的像素就是显示过的像素。
    for index, entry in enumerate(recording.frames):
        assert recording.frame(index).pixels == player.pixels[entry.capture_ns]
    # 录下的样本值就是发出过的样本值，块号连续。
    blocks = recording.blocks(0, len(recording.audio))
    assert [b.block_number for b in blocks] == list(range(1, len(blocks) + 1))
    assert {b.capture_ns: b.samples for b in blocks} == player.samples
    live = path / LIVE_DIR
    voice = (live / "voice.jsonl").read_text(encoding="utf-8")
    assert '"busy"' in voice
    assert '"stalled"' in voice  # 不再发出声音之后按墙钟发现的中断
    assert '"interrupt"' in (live / "voice-feed.jsonl").read_text(encoding="utf-8")
    # 剧情记录：对白画面上逐句记录，SQLite 在录制目录的 live/ 里，与实时运行的剧情记录分开。
    assert '"complete"' in (live / "narrative.jsonl").read_text(encoding="utf-8")
    assert (live / "narrative" / "synthetic-game.sqlite3").is_file()

    async def again() -> None:
        plugins = functools.partial(perception_catalog, voice=FakeVoice)
        result = await replay(recording, data_dir=data_dir, plugins=plugins)
        assert set(result.comparisons) == set(RESULT_FILES)
        assert result.comparisons[NARRATIVE_FINAL_FILE].same > 0
        for name, comparison in result.comparisons.items():
            assert comparison.identical, (name, comparison)
            assert comparison.same > 0, name
        assert not result.adapter_changed

    asyncio.run(again())
    assert (path / REPLAY_DIR / "scene.jsonl").read_bytes() == (live / "scene.jsonl").read_bytes()


def test_interrupting_the_recording_finishes_it_cleanly(
    core_exe: Path, synthetic: Adapter, data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def main() -> None:
        with (
            FakeDisplay.create(WIDTH, HEIGHT, slot_count=2) as display,
            FakeAudioOutput.create(RATE, CHANNELS, block_capacity=BLOCK, block_count=64) as output,
        ):
            player = Player(display, output)
            live = options(core_exe, synthetic, data_dir, display, output)
            recording = asyncio.create_task(record(live, quota=10**9))
            player.click("menu.start")
            showing = asyncio.create_task(player.show(5.0))
            sound = asyncio.create_task(player.sound([(5.0, True)]))
            root = data_dir / "recordings" / "synthetic-game"
            await until(lambda: True if any(root.glob("*/frames/*.png")) else None)
            await asyncio.sleep(0.5)
            # 像 Ctrl+C：主任务被取消，画面与声音还在来。
            recording.cancel()
            code = await recording
            assert code == 0
            for task in (showing, sound):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    asyncio.run(main())
    out = capsys.readouterr().out
    assert "收到 Ctrl+C" in out
    assert "实时核：shutdown" in out
    [path] = (data_dir / "recordings" / "synthetic-game").iterdir()
    meta = read_meta(path)
    assert (meta.state, meta.stop_reason) == ("complete", "interrupt")
    assert leftovers(path) == []
    recording = Recording.open(path)
    assert len(recording.frames) == meta.frames.count > 0
    for index in range(len(recording.frames)):
        recording.frame(index)
    assert len(recording.audio) == recording.meta.audio.blocks > 0  # pyright: ignore[reportOptionalMemberAccess]
