"""录制测试的共用夹具：临时的本地数据目录、合成游戏的适配包与它渲染出的帧序列，以及新建写入方。"""

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from game_runtime.adapter import GAMES_DIR, Adapter, load_adapter
from game_runtime.frame_ring import RingFrame
from game_runtime.recording import RecordingWriter, WriterOptions, adapter_sha256
from game_runtime.recording.layout import AdmissionInfo, GameInfo, Resolution, ToolInfo
from synthetic_game import Click, GameConfig, ManualClock, SyntheticGame

WIDTH, HEIGHT = 640, 360
MS = 1_000_000

type NewWriter = Callable[..., RecordingWriter]


@pytest.fixture(scope="session")
def synthetic() -> Adapter:
    return load_adapter(GAMES_DIR / "synthetic-game")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """测试的临时目录。Windows 上加扩展长度前缀：录制与模板的路径较长，临时目录较深而系统没有开
    长路径支持时会超过 MAX_PATH。"""
    if sys.platform == "win32":
        return Path("\\\\?\\" + str(tmp_path.resolve()))
    return tmp_path


@pytest.fixture(scope="session")
def game(synthetic: Adapter) -> GameInfo:
    return GameInfo(
        id=synthetic.id,
        name=synthetic.name,
        version=synthetic.version,
        adapter_sha256=adapter_sha256(synthetic.id),
        resolution=Resolution(width=synthetic.resolution.width, height=synthetic.resolution.height),
    )


@pytest.fixture
def new_writer(root: Path, game: GameInfo) -> NewWriter:
    """`new_writer(name=None, audio=(采样率, 声道数) 或 None, **WriterOptions 的字段)`：
    在临时的本地数据目录里新建一段录制。"""

    def create(
        name: str | None = None, audio: tuple[int, int] | None = None, **options: float
    ) -> RecordingWriter:
        return RecordingWriter.create(
            root / "data" / "recordings" / game.id,
            game=game,
            name=name,
            admission=AdmissionInfo(max_rate=10.0, idle_interval=0.25, backlog=32),
            audio_format=audio,
            tool=ToolInfo(commit=None, dirty=None),
            options=WriterOptions(**options),  # pyright: ignore[reportArgumentType]
        )

    return create


@pytest.fixture(scope="session")
def frames() -> list[RingFrame]:
    """合成游戏 640×360 的一段画面：标题菜单，进入对话后逐字显示，每 50 ms 一帧，帧号有空缺。"""
    clock = ManualClock()
    game = SyntheticGame(GameConfig(WIDTH, HEIGHT), clock)
    result = [RingFrame(3, 0, 1, WIDTH, HEIGHT, WIDTH * 4, game.frame().data)]
    start = next(t.rect for t in game.truth().targets if t.id == "menu.start")
    game.handle(Click(*start.center))
    for step in range(1, 15):
        clock.advance(0.05)
        number = 3 + 2 * step
        data = game.frame().data
        result.append(RingFrame(number, step * 50 * MS, 1, WIDTH, HEIGHT, WIDTH * 4, data))
    return result
