"""真机手动测试：自建测试进程的声音经 WASAPI 进程回环、实时核进入音频环（G1 第 2 步）。

只在 Windows 的交互桌面上运行，标 `manual`，不在 `uv run check` 中；
运行命令见 AGENTS.md 的常用命令，加 `-s` 看打印的实测值。会以很小的音量放出两个正弦波各约 1 秒。

测试拉起两个 `tone_player`（`core/crates/audio-capture/examples/tone_player.rs`）：A 播放 440 Hz，
B 播放 1200 Hz，各开一个窗口。`CoreLink` 以 A 的窗口识别规则设定音频目标，依次核对：

- A 的窗口出现之前实时核在 searching，出现之后进入 capturing；
- A 静音时块照常前进，样本值全为零；只有 B 出声时音频环里仍然没有声音；
- A 出声后音频环里有 440 Hz，没有 B 的 1200 Hz；
- A 退出后回到 searching。

并打印从 A 交出第一个正弦波缓冲到它出现在音频环里的时延，以及块交到主进程时的块龄。
"""

import sys

import pytest

pytestmark = pytest.mark.manual

if sys.platform == "win32":
    import array
    import asyncio
    import json
    import math
    import queue
    import shutil
    import statistics
    import subprocess
    import threading
    import time
    from collections.abc import Callable, Iterator
    from pathlib import Path
    from typing import IO

    from core_protocol.models import CaptureState, CoreHealthParams
    from game_runtime.audio_ring import AudioBlock, AudioRing
    from game_runtime.core_link import AudioConfig, CoreLink, CoreLinkOptions, WindowRule

    CORE = Path(__file__).resolve().parents[3] / "core"
    WAIT = 10.0
    AMPLITUDE = 0.03
    TARGET_HZ = 440
    OTHER_HZ = 1200
    RATE = 48_000

    def build_tone_player() -> Path:
        cargo = shutil.which("cargo")
        assert cargo is not None, "PATH 上没有 cargo"
        argv = [cargo, "build", "--locked", "-p", "audio-capture", "--example", "tone_player"]
        with subprocess.Popen(
            [*argv, "--message-format=json"],
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
                and message["target"]["name"] == "tone_player"
                and message.get("executable")
            ):
                return Path(message["executable"])
        raise AssertionError("cargo 没有给出 tone_player 的路径")

    class Player:
        """一个运行中的 `tone_player`：窗口标题、频率与它报出的时刻。"""

        def __init__(self, binary: Path, title: str, frequency: int) -> None:
            self.title = title
            self.process = subprocess.Popen(
                [
                    str(binary),
                    "--title",
                    title,
                    "--frequency",
                    str(frequency),
                    "--amplitude",
                    str(AMPLITUDE),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                encoding="utf-8",
            )
            self._lines: queue.Queue[str] = queue.Queue()
            stdout = self.process.stdout
            assert stdout is not None
            self._reader = threading.Thread(target=self._pump, args=(stdout,), daemon=True)
            self._reader.start()
            kind, pid = self.line().split()
            assert kind == "ready"
            self.pid = int(pid)

        def _pump(self, stream: IO[str]) -> None:
            with stream:
                for line in stream:
                    self._lines.put(line.strip())

        def line(self) -> str:
            return self._lines.get(timeout=WAIT)

        def send(self, command: str) -> int:
            """`tone` 或 `silence`；返回测试进程交出第一个新缓冲时的时刻。"""
            stdin = self.process.stdin
            assert stdin is not None
            stdin.write(command + "\n")
            stdin.flush()
            kind, at = self.line().split()
            assert kind == command
            return int(at)

        def quit(self, *, kill: bool = False) -> None:
            """关标准输入让它退出（`kill` 时直接杀掉），等它退出、读完它的输出。"""
            if kill and self.process.poll() is None:
                self.process.kill()
            if self.process.stdin is not None and not self.process.stdin.closed:
                self.process.stdin.close()
            self.process.wait(WAIT)
            self._reader.join(WAIT)

    def channel0(blocks: list[AudioBlock]) -> list[float]:
        values = array.array("f")
        for block in blocks:
            values.frombytes(block.samples)
        return list(values[0::2])

    def amplitude_at(values: list[float], frequency: float) -> float:
        """Goertzel：`values` 中频率为 `frequency` 的正弦分量的幅度。"""
        n = len(values)
        coefficient = 2 * math.cos(2 * math.pi * frequency / RATE)
        s1 = s2 = 0.0
        for value in values:
            s1, s2 = value + coefficient * s1 - s2, s1
        power = s1 * s1 + s2 * s2 - coefficient * s1 * s2
        return 2 * math.sqrt(max(power, 0.0)) / n

    class Watcher:
        """每 2 ms 读一次音频环，记下每一块第一次被看到的时刻。"""

        def __init__(self, ring: AudioRing) -> None:
            self.ring = ring
            self.blocks: list[AudioBlock] = []
            self.seen_ns: list[int] = []

        def poll(self) -> None:
            after = self.blocks[-1].block_number if self.blocks else 0
            read = self.ring.read_recent(10**9, after=after)
            now = time.perf_counter_ns()
            for block in read.blocks:
                if self.blocks:
                    assert block.block_number == self.blocks[-1].block_number + 1, "丢了块"
                self.blocks.append(block)
                self.seen_ns.append(now)

        async def collect(self, seconds: float) -> list[AudioBlock]:
            start = len(self.blocks)
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                self.poll()
                await asyncio.sleep(0.002)
            return self.blocks[start:]

        def ages_ms(self) -> list[float]:
            """每块被看到时的块龄：看到的时刻减去这一块最后一个采样点之后的时刻。"""
            return [
                (seen - block.capture_ns - block.sample_count * 1_000_000_000 // RATE) / 1e6
                for block, seen in zip(self.blocks, self.seen_ns, strict=True)
            ]

    async def until[T](probe: Callable[[], T | None], timeout: float = WAIT) -> T:
        deadline = time.monotonic() + timeout
        while (value := probe()) is None:
            assert time.monotonic() < deadline, "等待的条件没有成立"
            await asyncio.sleep(0.01)
        return value

    @pytest.fixture(scope="module")
    def tone_player() -> Path:
        return build_tone_player()

    @pytest.fixture
    def players() -> Iterator[list[Player]]:
        started: list[Player] = []
        yield started
        for player in started:
            player.quit(kill=True)

    def test_process_loopback_captures_only_the_target_process(
        core_exe: Path, tone_player: Path, players: list[Player]
    ) -> None:
        rule = WindowRule(process_name="tone_player.exe", title="Bellis Tone A")

        async def main() -> None:
            health: list[CoreHealthParams] = []
            options = CoreLinkOptions(
                core_exe, health_interval=0.05, audio=AudioConfig(target=rule, sample_rate=RATE)
            )
            link = CoreLink(options, on_health=health.append)
            await link.start()
            try:
                ring = link.audio
                assert ring is not None
                # 窗口出现之前在找。
                await until(
                    lambda: True if health and health[-1].audio is CaptureState.SEARCHING else None
                )
                target = Player(tone_player, "Bellis Tone A", TARGET_HZ)
                players.append(target)
                other = Player(tone_player, "Bellis Tone B", OTHER_HZ)
                players.append(other)
                found_at = time.monotonic()
                await until(lambda: True if health[-1].audio is CaptureState.CAPTURING else None)
                waited = time.monotonic() - found_at
                print(f"\n识别到目标进程 {target.pid}：{waited:.2f} 秒内进入 capturing")

                watcher = Watcher(ring)
                # 两个都静音：块照常前进，样本值全为零。
                silent = await watcher.collect(0.5)
                assert len(silent) >= 30, len(silent)
                assert max(abs(v) for v in channel0(silent)) == 0.0
                sizes = sorted({block.sample_count for block in silent})
                print(f"静音时 0.5 秒内 {len(silent)} 块，每块采样点数 {sizes}")

                # 只有另一个进程出声：音频环里仍然没有声音。
                other.send("tone")
                only_other = await watcher.collect(0.6)
                peak_other = max(abs(v) for v in channel0(only_other[10:]))
                print(f"只有 B 出声时目标的峰值 {peak_other:.6f}")
                assert peak_other == 0.0

                # 目标出声：找出第一个非零的采样点。
                submitted_ns = target.send("tone")
                onset: tuple[int, int] | None = None
                while onset is None:
                    assert time.monotonic() - found_at < 60, "没有采到目标的声音"
                    before = len(watcher.blocks)
                    watcher.poll()
                    for block, seen in zip(
                        watcher.blocks[before:], watcher.seen_ns[before:], strict=True
                    ):
                        values = array.array("f")
                        values.frombytes(block.samples)
                        index = next((i for i, v in enumerate(values[0::2]) if abs(v) > 0), None)
                        if index is not None:
                            onset = (block.capture_ns + index * 1_000_000_000 // RATE, seen)
                            break
                    await asyncio.sleep(0.002)
                captured_ns, seen_ns = onset
                tone = await watcher.collect(1.3)
                values = channel0(tone)[RATE // 10 : RATE // 10 + RATE]
                assert len(values) == RATE
                at_target = amplitude_at(values, TARGET_HZ)
                at_other = amplitude_at(values, OTHER_HZ)
                print(
                    f"目标出声后 1 秒：{TARGET_HZ} Hz 幅度 {at_target:.5f}，"
                    f"{OTHER_HZ} Hz 幅度 {at_other:.2e}（两个测试进程的幅度都是 {AMPLITUDE}）"
                )
                assert AMPLITUDE / 2 < at_target < AMPLITUDE * 2
                assert at_other < AMPLITUDE / 1000
                print(
                    "时延：交出第一个正弦波缓冲 → 它的采集时刻 "
                    f"{(captured_ns - submitted_ns) / 1e6:.1f} ms，"
                    f"采集时刻 → 主进程读到 {(seen_ns - captured_ns) / 1e6:.1f} ms，"
                    f"交出 → 主进程读到 {(seen_ns - submitted_ns) / 1e6:.1f} ms"
                )
                ages = watcher.ages_ms()
                print(
                    f"{len(ages)} 块交到主进程时的块龄：中位 {statistics.median(ages):.1f} ms，"
                    f"最大 {max(ages):.1f} ms"
                )

                other.quit()
                target.quit()
                await until(lambda: True if health[-1].audio is CaptureState.SEARCHING else None)
                print("目标进程退出后回到 searching")
            finally:
                await link.stop()

        asyncio.run(main())
