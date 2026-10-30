"""Python 与 Rust 互通：pytest 编译并拉起 `frame-ring` 的示例 `ring_interop`（命令见
`core/crates/frame-ring/examples/ring_interop.rs`）。

- Python 创建帧环，Rust 写入方挂接、写帧，Python 读到的与写入的一致；实时核重启后帧号续写。
- 头部与参数不符时 Rust 一方拒绝。
- 并发：一方高速写、另一方并发读，读到的帧要么完整一致、要么报撕裂。两个方向都测。
- 假采集源：合成游戏的帧经假显示面进入 Rust 的假采集源，再写进帧环，主进程读到的与
  `SyntheticGame.frame()` 一致；点击改变画面后帧环里的帧随之变化。

Windows 上跑命名文件映射，Linux 容器里跑 POSIX 共享内存。
"""

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO

import pytest

from core_protocol import models
from game_runtime.frame_ring import FrameRing, FrameRingTornError, RingFrame
from game_runtime.frame_ring.fake import FakeDisplay
from synthetic_game import (
    Click,
    EffectKind,
    GameConfig,
    ManualClock,
    MouseButton,
    SceneType,
    SyntheticGame,
)

CORE = Path(__file__).resolve().parents[3] / "core"
LINE_TIMEOUT = 10.0


@pytest.fixture(scope="session")
def ring_interop() -> Path:
    """编译示例并返回可执行文件的路径；尊重 CARGO_TARGET_DIR（由 cargo 自己读取）。"""
    cargo = shutil.which("cargo")
    assert cargo is not None, "PATH 上没有 cargo"
    argv = [
        cargo,
        "build",
        "--locked",
        "-p",
        "frame-ring",
        "--example",
        "ring_interop",
        "--message-format=json",
    ]
    with subprocess.Popen(
        argv,
        cwd=CORE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    ) as process:
        stdout, stderr = process.communicate(timeout=600)
    assert process.returncode == 0, stderr
    for line in stdout.splitlines():
        message = json.loads(line)
        if (
            message.get("reason") == "compiler-artifact"
            and message["target"]["name"] == "ring_interop"
            and message.get("executable")
        ):
            return Path(message["executable"])
    raise AssertionError("cargo 没有给出 ring_interop 的路径")


class Tool:
    """一个运行中的 `ring_interop`：在后台线程里逐行收集标准输出与标准错误。"""

    def __init__(self, binary: Path, *args: str) -> None:
        self.process = subprocess.Popen(
            [os.fspath(binary), *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stderr: list[str] = []
        stdout, stderr = self.process.stdout, self.process.stderr
        assert stdout is not None
        assert stderr is not None
        self._readers = [
            threading.Thread(target=self._pump, args=(stdout,)),
            threading.Thread(target=self._drain, args=(stderr,)),
        ]
        for reader in self._readers:
            reader.start()

    def _pump(self, stream: IO[str]) -> None:
        with stream:
            for line in stream:
                self._lines.put(line.rstrip("\n"))
        self._lines.put(None)

    def _drain(self, stream: IO[str]) -> None:
        with stream:
            self._stderr.extend(stream)

    def _close_stdin(self) -> None:
        stdin = self.process.stdin
        if stdin is not None and not stdin.closed:
            stdin.close()

    def line(self, timeout: float = LINE_TIMEOUT) -> str:
        """下一行输出；超时或程序已结束时测试失败。"""
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(f"{timeout} 秒内没有输出") from None
        assert line is not None, f"程序已结束：{''.join(self._stderr)}"
        return line

    def finish(self, timeout: float = 60.0) -> tuple[int, list[str], str]:
        """关闭标准输入，等程序退出；返回退出码、其余的输出行与标准错误。"""
        self._close_stdin()
        returncode = self.process.wait(timeout)
        lines: list[str] = []
        while (line := self._lines.get(timeout=timeout)) is not None:
            lines.append(line)
        for reader in self._readers:
            reader.join(timeout)
        return returncode, lines, "".join(self._stderr)

    def kill(self) -> None:
        """还在运行就杀掉，并关闭全部管道。"""
        if self.process.poll() is None:
            self.process.kill()
        self._close_stdin()
        self.process.wait()
        for reader in self._readers:
            reader.join()


StartTool = Callable[..., Tool]


@pytest.fixture
def start_tool(ring_interop: Path) -> Iterator[StartTool]:
    """拉起 `ring_interop`；测试结束时还在运行的一律杀掉。"""
    tools: list[Tool] = []

    def start(*args: str) -> Tool:
        tool = Tool(ring_interop, *args)
        tools.append(tool)
        return tool

    yield start
    for tool in tools:
        tool.kill()


def ring_json(ring: FrameRing) -> str:
    """`frame_ring.attach` 的 params。"""
    return ring.attach_params().model_dump_json()


def target_json(display: FakeDisplay) -> str:
    """`capture.set_target` 的 params，`fake` 分支。"""
    params = models.CaptureSetTargetParams(target=models.CaptureTarget(root=display.target))
    return params.model_dump_json()


def pattern(number: int, max_width: int, max_height: int) -> tuple[int, int, int, int, bytes]:
    """第 `number` 帧的图案：宽、高、采集时刻、变换版本与每个像素的值。

    与 `ring_interop.rs` 的 `pattern` 相同：读方只凭元数据就能核对整帧，撕裂的帧一定能被发现。
    """
    word = ((number * 0x9E3779B1) & 0xFFFFFFFF).to_bytes(4, "little")
    return (
        max_width - number % 7,
        max_height - number % 5,
        number * 1000 + 7,
        number // 5 + 1,
        word,
    )


def check_pattern(frame: RingFrame, base: int, max_width: int, max_height: int) -> None:
    """帧环里的第 `frame_number` 帧是采集源的第 `frame_number - base` 帧，写入方去掉了行尾填充。"""
    width, height, capture_ns, transform_version, word = pattern(
        frame.frame_number - base, max_width, max_height
    )
    got = (frame.width, frame.height, frame.row_stride, frame.capture_ns, frame.transform_version)
    assert got == (width, height, width * 4, capture_ns, transform_version), frame.frame_number
    assert frame.pixels == word * (width * height), f"frame {frame.frame_number} is torn"


def test_python_reads_what_rust_writes_and_numbers_resume_after_a_restart(
    start_tool: StartTool,
) -> None:
    with FrameRing.create(16, 12) as ring:
        params = ring_json(ring)
        assert start_tool("attach", params).finish() == (0, ["ok 0"], "")

        assert start_tool("write", params, "5").finish() == (0, ["ok 5"], "")
        frame = ring.read_latest()
        assert frame is not None
        assert frame.frame_number == 5
        check_pattern(frame, 0, 16, 12)

        # 实时核重启：新的写入方从头部的 write_seq 续写，采集源的帧号从 1 重新数起。
        assert start_tool("write", params, "3").finish() == (0, ["ok 8"], "")
        frame = ring.read_latest()
        assert frame is not None
        assert frame.frame_number == 8
        check_pattern(frame, 5, 16, 12)
        assert start_tool("attach", params).finish() == (0, ["ok 8"], "")


def changed(params: str, **fields: object) -> str:
    return json.dumps({**json.loads(params), **fields})


def test_rust_refuses_a_ring_that_does_not_match_the_parameters(start_tool: StartTool) -> None:
    # 另一个帧环更大：共享内存放得下参数要的大小，于是头部不符。
    with FrameRing.create(16, 12) as ring, FrameRing.create(32, 24) as other:
        params = ring_json(ring)
        cases = {
            "max_width": (changed(params, max_width=15), "header max_width is 16"),
            "slot_count": (changed(params, slot_count=2), "header slot_count is 3"),
            "another ring": (changed(params, name=other.name), "header slot_stride"),
            "missing": (changed(params, name=ring.name + "x"), "cannot open shared memory"),
            "too small": (
                changed(params, size_bytes=ring.size_bytes + (1 << 20)),
                "cannot open shared memory",
            ),
            "layout version": (changed(params, layout_version=2), "invalid frame ring parameters"),
        }
        for case, (bad, message) in cases.items():
            returncode, lines, _ = start_tool("attach", bad).finish()
            assert returncode == 1, case
            assert len(lines) == 1, case
            assert lines[0].startswith("error "), (case, lines)
            assert message in lines[0], (case, lines)
        assert ring.latest_frame_number() == 0


def test_python_never_returns_a_torn_frame_from_a_fast_rust_writer(
    start_tool: StartTool,
) -> None:
    count = 20_000
    with FrameRing.create(128, 96, slot_count=2) as ring:
        tool = start_tool("write", ring_json(ring), str(count))
        frames = torn = 0
        while tool.process.poll() is None:
            try:
                frame = ring.read_latest(max_attempts=1)
            except FrameRingTornError:
                torn += 1
                continue
            if frame is not None:
                check_pattern(frame, 0, 128, 96)
                frames += 1
        assert tool.finish() == (0, [f"ok {count}"], "")
        assert frames > 0, f"the reader never got a frame ({torn} torn reads)"
        last = ring.read_latest()
        assert last is not None
        assert last.frame_number == count


def test_rust_fake_capture_never_returns_a_torn_frame_from_a_fast_python_display(
    start_tool: StartTool,
) -> None:
    # Rust 一方经假采集源读够 100 帧并逐帧核对；这期间 Python 一方不停地写。
    with FakeDisplay.create(64, 48, slot_count=2) as display:
        tool = start_tool("read", target_json(display), "100")
        assert tool.line() == "ready"
        deadline = time.monotonic() + 30
        number = 0
        while tool.process.poll() is None and time.monotonic() < deadline:
            number += 1
            width, height, capture_ns, transform_version, word = pattern(number, 64, 48)
            display.present(
                word * (width * height),
                width,
                height,
                capture_ns=capture_ns,
                transform_version=transform_version,
            )
        returncode, lines, stderr = tool.finish()
        assert returncode == 0, (lines, stderr)
        assert len(lines) == 1
        kind, last = lines[0].split()
        assert kind == "ok"
        assert int(last) <= number


def test_synthetic_game_frames_reach_the_frame_ring_through_fake_capture(
    start_tool: StartTool,
) -> None:
    game = SyntheticGame(GameConfig(320, 180), ManualClock())
    with (
        FrameRing.create(320, 180) as ring,
        FakeDisplay.create(320, 180, slot_count=2) as display,
    ):
        tool = start_tool("pipe", ring_json(ring), target_json(display))

        def show() -> RingFrame:
            """把当前画面写进假显示面，等假采集源把它写进帧环，再从帧环读出来。"""
            frame = game.frame()
            capture_ns = time.perf_counter_ns()
            shown = display.present(
                frame.data,
                frame.width,
                frame.height,
                row_stride=frame.stride,
                capture_ns=capture_ns,
            )
            while True:
                kind, ring_number, source_number = tool.line().split()
                assert kind == "frame"
                if int(source_number) == shown:
                    break
            got = ring.read_latest()
            assert got is not None
            assert got.frame_number == int(ring_number) == shown
            assert (got.width, got.height, got.row_stride) == (320, 180, frame.stride)
            assert got.capture_ns == capture_ns
            assert got.pixels == frame.data
            return got

        menu = show()
        shop = next(target for target in game.truth().targets if target.id == "menu.shop")
        x, y = shop.rect.center
        effect = game.handle(Click(x, y, MouseButton.LEFT))
        assert (effect.kind, effect.target) == (EffectKind.HIT, "menu.shop")
        assert game.truth().scene is SceneType.SHOP

        after = show()
        assert after.frame_number > menu.frame_number
        assert after.pixels != menu.pixels
        assert tool.finish() == (0, ["ok"], "")
