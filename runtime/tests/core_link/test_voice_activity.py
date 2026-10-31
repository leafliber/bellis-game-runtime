"""`voice-activity@1` 经假音频源、真实的 `realtime-core` 与音频环联调：测试把声音写进假音频输出，
实时核写进音频环，插件经 `ctx.audio` 读出并判断忙闲。

模型用按音量判断的假模型（`EnergyModel`），不要模型文件；音频环用缺省的 48 kHz 立体声，插件自己降到
16 kHz 单声道。Windows 上跑命名文件映射，Linux 容器里跑 POSIX 共享内存。
"""

import asyncio
import os
import signal
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import ClassVar

import numpy as np

from core_protocol.models import CaptureState, CoreHealthParams
from game_runtime.audio_ring.fake import FakeAudioOutput
from game_runtime.core_link import AudioConfig, CoreLink, CoreLinkOptions
from game_runtime.kernel import (
    AUDIO_READ,
    Kernel,
    Manifest,
    NoConfig,
    Plugin,
    PluginContext,
    ResolvedEntry,
    Tier,
    catalog,
)
from game_runtime.voice_activity import (
    VOICE_ACTIVITY,
    SpeechModel,
    UnknownReason,
    VoiceActivityPlugin,
    VoiceActivityService,
    VoiceChange,
    VoiceState,
)
from game_runtime.voice_activity.fake import EnergyModel

WAIT = 5.0
RATE = 48_000
BLOCK = 480
BLOCK_NS = 10_000_000
BLOCKS = 400


class FakeModelPlugin(VoiceActivityPlugin):
    def load_model(self, data_dir: Path) -> SpeechModel:
        del data_dir
        return EnergyModel()


class Probe(Plugin[NoConfig]):
    service: ClassVar[VoiceActivityService | None] = None
    manifest: ClassVar = Manifest(
        id="voice-probe", version="1", tier=Tier.ORDINARY, inject=(VOICE_ACTIVITY.ref,)
    )

    async def activate(self, ctx: PluginContext[NoConfig]) -> None:
        Probe.service = ctx.service(VOICE_ACTIVITY)


def kill(pid: int) -> None:
    """强制终止进程：Windows 上 `os.kill` 即 TerminateProcess，其它平台发 SIGKILL。"""
    os.kill(pid, signal.SIGTERM if sys.platform == "win32" else signal.SIGKILL)


async def until[T](probe: Callable[[], T | None], timeout: float = WAIT) -> T:
    deadline = time.monotonic() + timeout
    while (value := probe()) is None:
        assert time.monotonic() < deadline, "等待的条件没有成立"
        await asyncio.sleep(0.005)
    return value


def chunks(seconds: float, amplitude: float) -> list[bytes]:
    t = np.arange(round(seconds * RATE)) / RATE
    mono = amplitude * np.sin(2 * np.pi * 300 * t)
    samples = np.stack([mono, mono], axis=1).astype(np.float32)
    return [samples[i : i + BLOCK].tobytes() for i in range(0, len(samples), BLOCK)]


def play(output: FakeAudioOutput, parts: Sequence[bytes]) -> None:
    """依次写进假音频输出，采集时刻从现在起每块加 10 ms。"""
    start = time.perf_counter_ns()
    for i, part in enumerate(parts):
        output.play(part, capture_ns=start + i * BLOCK_NS)


def test_voice_activity_follows_the_sound_through_the_core(core_exe: Path) -> None:
    async def main() -> None:
        health: list[CoreHealthParams] = []
        with FakeAudioOutput.create(
            RATE, 2, block_capacity=BLOCK * 2, block_count=BLOCKS
        ) as output:
            audio = AudioConfig(
                target=output.target,
                block_capacity=output.block_capacity,
                block_count=BLOCKS,
            )
            options = CoreLinkOptions(
                core_exe,
                heartbeat_interval=0.05,
                heartbeat_timeout=0.3,
                health_interval=0.05,
                audio=audio,
            )
            link = CoreLink(options, on_health=health.append)
            kernel = Kernel(
                catalog=catalog(FakeModelPlugin, Probe), core=link, audio=link.audio_reader
            )
            await link.start()
            try:
                kernel.add(
                    [
                        ResolvedEntry(
                            id="voice",
                            plugin="voice-activity",
                            config={"poll_interval": 0.01, "stall_timeout": 0.3},
                            disabled=False,
                            grant=frozenset({AUDIO_READ}),
                        ),
                        ResolvedEntry(
                            id="probe",
                            plugin="voice-probe",
                            config={},
                            disabled=False,
                            grant=frozenset(),
                        ),
                    ]
                )
                await kernel.settled()
                voice = Probe.service
                assert voice is not None
                received: list[VoiceChange] = []
                voice.subscribe(received.append)
                await until(
                    lambda: True if health and health[-1].audio is CaptureState.CAPTURING else None
                )

                def state_is(state: VoiceState, reason: UnknownReason | None = None) -> bool | None:
                    status = voice.status
                    return True if (status.state, status.reason) == (state, reason) else None

                play(output, chunks(0.7, 0.0) + chunks(1.0, 0.3) + chunks(0.8, 0.0))
                await until(lambda: state_is(VoiceState.IDLE) if len(received) >= 3 else None)
                assert [c.state for c in received] == [
                    VoiceState.IDLE,
                    VoiceState.BUSY,
                    VoiceState.IDLE,
                ]
                busy = received[1]
                assert busy.at_ns - busy.since_ns == 4 * 32_000_000

                # 实时核没了：音频环还在，块号不再前进。
                pid = link.pid
                assert pid is not None
                kill(pid)
                await until(lambda: True if not link.running else None)
                await until(lambda: state_is(VoiceState.UNKNOWN, UnknownReason.STALLED))

                # 拉起新实例：块号续写，新实例的第一块带不连续标志，开始新的一段。
                health.clear()
                await link.start()
                await until(
                    lambda: True if health and health[-1].audio is CaptureState.CAPTURING else None
                )
                play(output, chunks(0.5, 0.3))
                await until(lambda: state_is(VoiceState.BUSY))
            finally:
                await kernel.shutdown()
                await link.stop()

    asyncio.run(main())
