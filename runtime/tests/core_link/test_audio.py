"""主进程一侧的音频环与音频目标：`CoreLink` 创建音频环、挂接给真实的 `realtime-core`
并设定音频目标，插件经插件内核的 `ctx.audio` 读。

自动测试用假音频源：测试把声音逐块写进假音频输出（`FakeAudioOutput`），实时核的 `FakeAudioCapture`
取声音写进音频环，主进程经 `CoreLink.audio` 与 `ctx.audio` 读出。真机上经 WASAPI 进程回环的测试见
`test_audio_process.py`（手动）。Windows 上跑命名文件映射，Linux 容器里跑 POSIX 共享内存。
"""

import array
import asyncio
import os
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core_protocol import models
from core_protocol.models import CaptureState, CoreHealthParams
from game_runtime.audio_ring import AudioBlock, AudioReader
from game_runtime.audio_ring.fake import FakeAudioOutput
from game_runtime.core_link import (
    AudioConfig,
    CoreLink,
    CoreLinkOptions,
    CoreStartError,
    StopResult,
    StopStage,
    WindowRule,
)
from game_runtime.kernel import (
    AUDIO_READ,
    Kernel,
    Manifest,
    Plugin,
    PluginContext,
    ResolvedEntry,
    Tier,
    catalog,
)

WAIT = 5.0
RATE = 16_000
BLOCK = 160
"""一块 10 ms。"""


def link_options(exe: Path, audio: AudioConfig) -> CoreLinkOptions:
    return CoreLinkOptions(
        exe,
        heartbeat_interval=0.05,
        heartbeat_timeout=0.3,
        health_interval=0.05,
        audio=audio,
        restart=None,  # 杀掉后由测试自己重启
    )


def fake_config(output: FakeAudioOutput) -> AudioConfig:
    return AudioConfig(
        target=output.target,
        sample_rate=output.sample_rate,
        channels=output.channels,
        block_capacity=output.block_capacity,
        block_count=16,
    )


def kill(pid: int) -> None:
    """强制终止进程：Windows 上 `os.kill` 即 TerminateProcess，其它平台发 SIGKILL。"""
    os.kill(pid, signal.SIGTERM if sys.platform == "win32" else signal.SIGKILL)


async def until[T](probe: Callable[[], T | None], timeout: float = WAIT) -> T:
    """等到 `probe()` 不是 None，返回它。"""
    deadline = time.monotonic() + timeout
    while (value := probe()) is None:
        assert time.monotonic() < deadline, "等待的条件没有成立"
        await asyncio.sleep(0.005)
    return value


def samples(value: float) -> bytes:
    return array.array("f", [value] * BLOCK).tobytes()


async def capturing(link: CoreLink, health: list[CoreHealthParams]) -> None:
    """等到实时核的音频源建立：之后写进假音频输出的声音才会被采到，与真实的音频源一样。"""
    await until(lambda: True if health and health[-1].audio is CaptureState.CAPTURING else None)
    assert link.audio is not None


async def blocks_after(reader: AudioReader, after: int, count: int) -> list[AudioBlock]:
    """等到 `after` 之后有 `count` 块，返回它们。"""

    def probe() -> list[AudioBlock] | None:
        read = reader.read_recent(10**9, after=after)
        assert read is not None
        return list(read.blocks) if len(read.blocks) >= count else None

    return await until(probe)


def test_fake_audio_reaches_the_main_process_and_plugins_through_the_core(
    core_exe: Path,
) -> None:
    readers: list[AudioReader] = []

    class Listener(Plugin[Any]):
        manifest = Manifest(
            id="listener", version="1.0.0", tier=Tier.ORDINARY, permissions=(AUDIO_READ,)
        )

        async def activate(self, ctx: PluginContext[Any]) -> None:
            readers.append(ctx.audio)

    async def main() -> None:
        health: list[CoreHealthParams] = []
        with FakeAudioOutput.create(RATE, 1, block_capacity=BLOCK, block_count=16) as output:
            link = CoreLink(link_options(core_exe, fake_config(output)), on_health=health.append)
            kernel = Kernel(catalog=catalog(Listener), core=link, audio=link.audio_reader)
            # 没有音频环之前读到 None。
            assert link.audio_reader.read_recent(0) is None
            await link.start()
            try:
                kernel.add(
                    [
                        ResolvedEntry(
                            id="listener",
                            plugin="listener",
                            config={},
                            disabled=False,
                            grant=frozenset({AUDIO_READ}),
                        )
                    ]
                )
                await kernel.settled()
                assert len(readers) == 1
                await capturing(link, health)
                sent = [
                    output.play(samples(i / 8), capture_ns=1_000_000 * (i + 1)) for i in range(5)
                ]
                assert sent == [1, 2, 3, 4, 5]
                ring = link.audio
                assert ring is not None
                got = await blocks_after(ring, 0, 5)
                assert [b.block_number for b in got] == [1, 2, 3, 4, 5]
                assert [b.capture_ns for b in got] == [1_000_000 * (i + 1) for i in range(5)]
                assert [b.samples for b in got] == [samples(i / 8) for i in range(5)]
                # 新音频源之后的第一块标不连续，其余连续。
                assert [b.discontinuity for b in got] == [True, False, False, False, False]
                # 插件经 ctx.audio 读到同样的声音。
                via_plugin = readers[0].read_recent(10**9)
                assert via_plugin is not None
                assert via_plugin.blocks == tuple(got)
                assert (via_plugin.sample_rate, via_plugin.channels) == (RATE, 1)
                # 实时核记下的最近一块就是音频环里的最新一块。
                await until(
                    lambda: (
                        True
                        if health[-1].latest_audio
                        == models.LatestAudio(block_number=5, capture_ns=5_000_000)
                        else None
                    )
                )
                # 画面没有配置，仍是 idle。
                assert health[-1].capture is CaptureState.IDLE
            finally:
                await kernel.shutdown()
                await link.stop()
            assert link.audio is None
            assert ring.closed
            assert link.audio_reader.read_recent(0) is None

    asyncio.run(main())


def test_plugins_without_the_permission_cannot_read_audio(core_exe: Path) -> None:
    errors: list[BaseException] = []

    class Nosy(Plugin[Any]):
        manifest = Manifest(id="nosy", version="1.0.0", tier=Tier.ORDINARY)

        async def activate(self, ctx: PluginContext[Any]) -> None:
            try:
                _ = ctx.audio
            except PermissionError as error:
                errors.append(error)

    class Listener(Plugin[Any]):
        manifest = Manifest(
            id="listener", version="1.0.0", tier=Tier.ORDINARY, permissions=(AUDIO_READ,)
        )

        async def activate(self, ctx: PluginContext[Any]) -> None:
            try:
                _ = ctx.audio
            except LookupError as error:
                errors.append(error)

    async def main() -> None:
        with FakeAudioOutput.create(RATE, 1, block_capacity=BLOCK, block_count=16) as output:
            link = CoreLink(link_options(core_exe, fake_config(output)))
            # 宿主没有把音频交给内核。
            kernel = Kernel(catalog=catalog(Nosy, Listener), core=link)
            kernel.add(
                [
                    ResolvedEntry(
                        id=name, plugin=name, config={}, disabled=False, grant=frozenset(grant)
                    )
                    for name, grant in (("nosy", ()), ("listener", (AUDIO_READ,)))
                ]
            )
            await kernel.settled()
            await kernel.shutdown()
        assert [type(error) for error in errors] == [PermissionError, LookupError]

    asyncio.run(main())


def test_a_restarted_core_reattaches_and_block_numbers_continue(core_exe: Path) -> None:
    async def main() -> None:
        health: list[CoreHealthParams] = []
        with FakeAudioOutput.create(RATE, 1, block_capacity=BLOCK, block_count=16) as output:
            link = CoreLink(link_options(core_exe, fake_config(output)), on_health=health.append)
            await link.start()
            try:
                ring = link.audio
                assert ring is not None
                await capturing(link, health)
                for i in range(3):
                    output.play(samples(i))
                first = await blocks_after(ring, 0, 3)
                # 实时核崩溃：音频环留着，不再有写方。
                pid = link.pid
                assert pid is not None
                kill(pid)
                await until(lambda: True if not link.running else None)
                assert link.audio is ring
                assert not ring.closed
                health.clear()

                # 新实例挂接同一个音频环，块号接着写，不回退；它的第一块标不连续。
                await link.start()
                assert link.audio is ring
                await capturing(link, health)
                for i in range(3, 5):
                    output.play(samples(i))
                second = await blocks_after(ring, first[-1].block_number, 2)
                assert [b.block_number for b in second] == [4, 5]
                assert [b.discontinuity for b in second] == [True, False]
                assert [b.samples for b in second] == [samples(3), samples(4)]
            finally:
                await link.stop()
            assert link.audio is None
            assert ring.closed

    asyncio.run(main())


def test_start_fails_when_the_core_refuses_the_audio_target(core_exe: Path) -> None:
    async def main() -> None:
        with FakeAudioOutput.create(RATE, 1, block_capacity=BLOCK, block_count=16) as output:
            # 参数里的块槽数与假音频输出不符：实时核以 -32005 拒绝。
            mismatched = output.target.output.model_copy(update={"block_count": 8})
            config = fake_config(output).model_copy(
                update={"target": models.FakeAudioTarget(output=mismatched)}
            )
            link = CoreLink(link_options(core_exe, config))
            with pytest.raises(CoreStartError, match="没有接受音频环或音频目标") as failed:
                await link.start()
            assert "header block_count is 16" in str(failed.value)
            assert not link.running
            # 音频环保留到 stop。
            assert link.audio is not None
            assert await link.stop() == StopResult(StopStage.ALREADY_EXITED, 0)
            assert link.audio is None

    asyncio.run(main())


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 上支持窗口目标，见手动测试")
def test_audio_window_targets_are_unsupported_off_windows(core_exe: Path) -> None:
    async def main() -> None:
        rule = WindowRule(process_name="game.exe", title="Game")
        link = CoreLink(link_options(core_exe, AudioConfig(target=rule)))
        with pytest.raises(CoreStartError, match="only available on Windows"):
            await link.start()
        await link.stop()

    asyncio.run(main())


# 配置


def test_audio_config_reads_a_window_rule_and_checks_the_format() -> None:
    config = AudioConfig.model_validate(
        {"target": {"type": "window", "process_name": "game.exe", "title": "Game"}}
    )
    assert (config.sample_rate, config.channels, config.block_capacity, config.block_count) == (
        48_000,
        2,
        960,
        400,
    )
    assert config.set_target_params().model_dump(mode="json") == {
        "target": {
            "type": "window",
            "process_name": "game.exe",
            "window_class": None,
            "title": "Game",
        }
    }
    for bad in (
        {"sample_rate": 7_999},
        {"sample_rate": 192_001},
        {"channels": 3},
        {"block_capacity": 0},
        {"block_count": 1},
        {"unknown": 1},
    ):
        with pytest.raises(ValidationError):
            AudioConfig.model_validate({"target": config.target, **bad})
