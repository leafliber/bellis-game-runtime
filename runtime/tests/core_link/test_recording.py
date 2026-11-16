"""录制与 M0 回放经真实实时核的联调：合成游戏的帧写进假显示面、声音写进假音频输出，实时核的假采集源
与假音频源写进帧环与音频环，定序帧源接纳的帧交给感知插件并录下，声音按块录下；回放同一段录制，
`scene@1` 的逐帧状态、`voice-activity@1` 的变化与喂入标记、剧情记录的句子与播片段都与实时运行逐条
相同，录下的像素与样本值与写进去的逐一相同。另核对中途停止（像 Ctrl+C）时收尾完整、不留半截文件。

OCR 用按脚本回答的识别器，配音检测用按音量判断的假模型，都不要模型文件；Windows 上跑命名文件映射，
Linux 容器里跑 POSIX 共享内存。真机经 WGC 的录制见 `test_record_window.py`（手动）。
"""

import asyncio
import contextlib
import dataclasses
import functools
import json
import shutil
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import IO, cast

import numpy as np
import pytest

from game_runtime.adapter import GAMES_DIR, Adapter, load_adapter
from game_runtime.audio_ring.fake import FakeAudioOutput
from game_runtime.core_link import AudioConfig, CaptureConfig, CoreLinkOptions, StopStage
from game_runtime.frame_ring.fake import FakeDisplay
from game_runtime.frames import AdmissionPolicy
from game_runtime.kernel import Kernel
from game_runtime.ocr import Image, RecognizedLine
from game_runtime.ocr import plugin as ocr_plugin
from game_runtime.ocr.fake import ScriptedRecognizer, scripted_line
from game_runtime.recording import (
    NARRATIVE_FINAL_FILE,
    RESULT_FILES,
    LiveOptions,
    LiveSession,
    Recording,
    SessionResult,
    WriterOptions,
    adapter_sha256,
    perception_catalog,
    replay,
)
from game_runtime.recording import session as session_module
from game_runtime.recording.cli import record
from game_runtime.recording.layout import (
    FRAMES_DIR,
    LIVE_DIR,
    META_FILE,
    REPLAY_DIR,
    ToolInfo,
    read_meta,
)
from game_runtime.scene import store_template
from game_runtime.voice_activity import SpeechModel, VoiceActivityPlugin
from game_runtime.voice_activity.fake import EnergyModel
from synthetic_game import Click, GameConfig, ManualClock, SceneType, SyntheticGame
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

    async def read_on(self) -> None:
        """点对话框推进（补全或下一句），每次之后显示 0.1 秒，直到离开对话。"""
        while self.game.truth().scene is SceneType.DIALOGUE:
            self.click("dialogue.box")
            await self.show(0.1)

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
                # 读完开场对话进入选项，敲门回应，读完插曲进入播片；看完 4 秒的播片，回到选项后再
                # 显示 2 秒，scene@1 在最后一个动帧之后 1.5 秒判出播片结束。
                await player.read_on()
                player.click("choice.1")
                await player.show(0.3)
                await player.read_on()
                assert player.game.truth().scene is SceneType.CUTSCENE
                while player.game.truth().scene is SceneType.CUTSCENE:
                    await player.show(0.1)
                await player.show(2.0)
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
    narrative = (live / "narrative.jsonl").read_text(encoding="utf-8").splitlines()
    assert any('"complete"' in line for line in narrative)
    assert (live / "narrative" / "synthetic-game.sqlite3").is_file()
    # 播片：开始与结束各一行插在句子的变化之间，播片中读到的字幕带播片段编号；最后的输出里有这个
    # 播片段，已结束。
    cutscenes = [json.loads(line) for line in narrative if '"type":"cutscene"' in line]
    assert [(c["index"], c["closed_frame"] is not None) for c in cutscenes] == [
        (1, False),
        (1, True),
    ]
    assert any('"cutscene":1,' in line for line in narrative if '"type"' not in line)
    final = (live / "narrative-final.jsonl").read_text(encoding="utf-8").splitlines()
    [ended] = [json.loads(line) for line in final if '"type":"cutscene"' in line]
    assert ended == cutscenes[-1]
    assert ended["sentences"]
    # 手动时钟每显示一帧走 0.02 秒，墙钟上的一帧不一定正好 20 ms：4 秒的播片按采集时刻大约 3–6 秒。
    assert 2e9 < ended["end_ns"] - ended["first_ns"] < 8e9

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


def hold_flush(
    session: LiveSession, file: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[asyncio.Event, threading.Event]:
    """卡住真实文件的 flush，等测试方放行。"""
    writer = session._writer  # pyright: ignore[reportPrivateUsage]
    assert writer is not None
    stream = cast("IO[str] | IO[bytes]", getattr(writer, file))
    flush = stream.flush
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    def block() -> None:
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(8):
            raise TimeoutError("录制的 flush 未获放行")
        flush()

    monkeypatch.setattr(stream, "flush", block)
    return entered, release


async def cancel_while_waiting[T](task: asyncio.Task[T]) -> None:
    for _ in range(2):
        task.cancel()
        await asyncio.wait((task,), timeout=0.05)
        assert not task.done()


def hold_activation(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    activating = asyncio.Event()
    settled = Kernel.settled

    async def block_start(kernel: Kernel) -> None:
        if not activating.is_set():
            activating.set()
            await asyncio.Event().wait()
        await settled(kernel)

    monkeypatch.setattr(Kernel, "settled", block_start)
    return activating


@pytest.mark.parametrize("media", ["audio", "frame"])
def test_cancelling_stop_waits_for_in_flight_writes_and_finishes(
    core_exe: Path,
    synthetic: Adapter,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    media: str,
) -> None:
    async def main() -> None:
        with (
            FakeDisplay.create(WIDTH, HEIGHT, slot_count=2) as display,
            FakeAudioOutput.create(RATE, CHANNELS, block_capacity=BLOCK, block_count=64) as output,
        ):
            live = options(core_exe, synthetic, data_dir, display, output)
            session = LiveSession(
                dataclasses.replace(live, perception=False, writer=WriterOptions(threads=1))
            )
            await session.start()
            file = "_audio_samples" if media == "audio" else "_frames_index"
            entered, release = hold_flush(session, file, monkeypatch)
            stop: asyncio.Task[SessionResult] | None = None
            showing: asyncio.Task[None] | None = None
            close_started = asyncio.Event()
            close = session.frames.close

            async def report_close() -> None:
                close_started.set()
                await close()

            monkeypatch.setattr(session.frames, "close", report_close)
            audio = session.audio_task
            assert audio is not None
            path = session.path
            try:
                if media == "audio":
                    output.play(tone(1, loud=True))
                else:
                    showing = asyncio.create_task(Player(display, output).show(4.0))
                await asyncio.wait_for(entered.wait(), 4)
                stop = asyncio.create_task(session.stop("seconds"))
                if media == "audio":
                    await until(lambda: audio.cancelling() or None, timeout=4)
                    assert not audio.done()
                else:
                    await asyncio.wait_for(close_started.wait(), 4)
                    assert not session.frames.closed
                await cancel_while_waiting(stop)
                assert session.link.running
                assert session.link.frames is not None
                assert session.link.audio is not None
                assert read_meta(path).state == "recording"
            finally:
                release.set()
                if showing is not None:
                    showing.cancel()
                    await asyncio.gather(showing, return_exceptions=True)
                if stop is not None:
                    (outcome,) = await asyncio.gather(stop, return_exceptions=True)
                else:
                    outcome = None
                stopped = not session.link.running
                if not stopped:
                    await session.stop("test-cleanup")
            assert isinstance(outcome, asyncio.CancelledError)
            assert stopped
            assert session.link.frames is None
            assert session.link.audio is None
            assert audio.done()
            recording = Recording.open(path)
            assert (recording.meta.state, recording.meta.stop_reason) == ("complete", "seconds")
            assert len(recording.audio if media == "audio" else recording.frames) > 0

    asyncio.run(main())


def test_repeated_start_cancellation_finishes_abort_with_audio_in_flight(
    core_exe: Path, synthetic: Adapter, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def main() -> None:
        with (
            FakeDisplay.create(WIDTH, HEIGHT, slot_count=2) as display,
            FakeAudioOutput.create(RATE, CHANNELS, block_capacity=BLOCK, block_count=64) as output,
        ):
            session = LiveSession(
                dataclasses.replace(
                    options(core_exe, synthetic, data_dir, display, output), perception=False
                )
            )
            activating = hold_activation(monkeypatch)
            start = asyncio.create_task(session.start())
            release: threading.Event | None = None
            try:
                await asyncio.wait_for(activating.wait(), 4)
                entered, release = hold_flush(session, "_audio_samples", monkeypatch)
                audio = session.audio_task
                assert audio is not None
                path = session.path
                output.play(tone(1, loud=True))
                await asyncio.wait_for(entered.wait(), 4)
                start.cancel()
                await until(lambda: audio.cancelling() or None, timeout=4)
                await cancel_while_waiting(start)
                assert not audio.done()
                assert session.link.running
                assert read_meta(path).state == "recording"
            finally:
                if release is not None:
                    release.set()
                if not start.cancelling():
                    start.cancel()
                (outcome,) = await asyncio.gather(start, return_exceptions=True)
                stopped = not session.link.running
                if not stopped:
                    await session.stop("test-cleanup")
            assert isinstance(outcome, asyncio.CancelledError)
            assert stopped
            assert session.link.frames is None
            assert session.link.audio is None
            assert audio.done()
            assert not path.exists()

    asyncio.run(main())


@pytest.mark.parametrize("case", ["empty", "deletion-denied", "unindexed-frame"])
def test_cancelled_start_preserves_the_cause_when_final_metadata_replace_fails(
    core_exe: Path,
    synthetic: Adapter,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: str,
) -> None:
    async def main() -> None:
        with (
            FakeDisplay.create(WIDTH, HEIGHT, slot_count=2) as display,
            FakeAudioOutput.create(RATE, CHANNELS, block_capacity=BLOCK, block_count=64) as output,
        ):
            live = options(core_exe, synthetic, data_dir, display, output)
            session = LiveSession(
                dataclasses.replace(live, perception=False, writer=WriterOptions(threads=1))
            )
            activating = hold_activation(monkeypatch)
            start = asyncio.create_task(session.start())
            showing: asyncio.Task[None] | None = None
            try:
                await asyncio.wait_for(activating.wait(), 4)
                writer = session._writer  # pyright: ignore[reportPrivateUsage]
                assert writer is not None
                path = writer.path
                meta_error = PermissionError("禁止替换结束元数据")
                remove_error = PermissionError("禁止删除录制目录")
                replace = Path.replace
                removals: list[Path] = []

                def fail_replace(source: Path, target: str | Path) -> Path:
                    if Path(target) == path / META_FILE:
                        raise meta_error
                    return replace(source, target)

                def delete(target: Path) -> None:
                    removals.append(target)
                    if case == "deletion-denied":
                        raise remove_error
                    shutil.rmtree(target)

                monkeypatch.setattr(Path, "replace", fail_replace)
                monkeypatch.setattr(session_module, "shutil", SimpleNamespace(rmtree=delete))
                if case == "unindexed-frame":
                    # 启动还在等激活，让定序帧源接纳一帧，像素文件写好后索引才失败。
                    def fail_index(_line: str) -> int:
                        raise OSError("禁止写入帧索引")

                    index = writer._frames_index  # pyright: ignore[reportPrivateUsage]
                    monkeypatch.setattr(index, "write", fail_index)
                    session.frames.start()
                    showing = asyncio.create_task(Player(display, output).show(4.0))
                    await until(lambda: session.frames.closed or None, timeout=4)
                    assert session.frames.stats.admitted > 0
                    assert writer.meta.frames.count == 0
                    assert any((path / FRAMES_DIR).glob("*.png"))
                start.cancel("中止插件激活")
                with pytest.raises(asyncio.CancelledError, match="中止插件激活"):
                    await start
                assert not session.link.running
                assert session.link.frames is None
                assert session.link.audio is None
                assert any(log.exc_info and log.exc_info[1] is meta_error for log in caplog.records)
                if case == "deletion-denied":
                    failure = next(
                        log
                        for log in caplog.records
                        if log.exc_info and log.exc_info[1] is remove_error
                    )
                    assert str(path) in failure.getMessage()
                    assert read_meta(path).state == "recording"
                if case == "unindexed-frame":
                    assert removals == []
                    assert any((path / FRAMES_DIR).glob("*.png"))
                else:
                    assert removals == [path]
                    assert path.exists() == (case == "deletion-denied")
            finally:
                if showing is not None:
                    showing.cancel()
                    await asyncio.gather(showing, return_exceptions=True)
                if not start.done():
                    start.cancel()
                    await asyncio.gather(start, return_exceptions=True)
                if session.link.running:
                    await session.link.stop()

    asyncio.run(main())


def test_cancelling_core_start_releases_the_rings_before_recording_is_created(
    core_exe: Path, synthetic: Adapter, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def main() -> None:
        with (
            FakeDisplay.create(WIDTH, HEIGHT, slot_count=2) as display,
            FakeAudioOutput.create(RATE, CHANNELS, block_capacity=BLOCK, block_count=64) as output,
        ):
            session = LiveSession(options(core_exe, synthetic, data_dir, display, output))
            ready = asyncio.Event()
            start_core = session.link.start

            async def pause_before_return() -> None:
                await start_core()
                ready.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(session.link, "start", pause_before_return)
            task = asyncio.create_task(session.start())
            try:
                await asyncio.wait_for(ready.wait(), 4)
                assert session.link.running
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert not session.link.running
                assert session.link.frames is None
                assert session.link.audio is None
                assert session.recording_path is None
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if session.link.running:
                    await session.link.stop()

    asyncio.run(main())
