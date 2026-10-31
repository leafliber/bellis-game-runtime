"""`voice-activity@1` 在插件内核中：清单与配置、发布状态与变化、一次读到几块不影响变化序列、
没有音频环、块号不前进、换了音频环、读失败、出错、模型制品缺失、停用时等在途的推理。

音频用内存里的读取方替身，模型用按音量判断的假模型（`EnergyModel`），不要模型文件。经音频环与真实
实时核的联调见 `runtime/tests/core_link/test_voice_activity.py`。
"""

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pytest
from numpy.typing import NDArray
from pydantic import ValidationError

from core_protocol import models
from game_runtime.audio_ring import AudioBlock, AudioRead, AudioRingTornError
from game_runtime.kernel import (
    AUDIO_READ,
    ControlGrant,
    Kernel,
    KernelOptions,
    Manifest,
    NoConfig,
    Plugin,
    PluginContext,
    ReasonCode,
    ResolvedEntry,
    State,
    Submission,
    Tier,
    catalog,
)
from game_runtime.voice_activity import (
    VOICE_ACTIVITY,
    SpeechModel,
    UnknownReason,
    VoiceActivityConfig,
    VoiceActivityDetector,
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


class MemoryAudio:
    """音频环的替身：块在内存里，`read_recent` 按音频环的规则给出；`present` 为假时像没有音频环。"""

    def __init__(self, capacity: int = 400) -> None:
        self.capacity = capacity
        self.blocks: list[AudioBlock] = []
        self.present = True
        self.fail: BaseException | None = None
        """下一次读取抛出它。"""
        self.reads = 0

    def push(self, samples: NDArray[np.float32], capture_ns: int) -> AudioBlock:
        number = self.blocks[-1].block_number + 1 if self.blocks else 1
        block = AudioBlock(number, capture_ns, len(samples), False, samples.tobytes())
        self.blocks.append(block)
        del self.blocks[: -self.capacity]
        return block

    def replace(self) -> None:
        """换一个新的音频环：块号从 1 起。"""
        self.blocks = []

    def read_recent(self, duration_ns: int, *, after: int = 0) -> AudioRead | None:
        self.reads += 1
        if self.fail is not None:
            error, self.fail = self.fail, None
            raise error
        if not self.present:
            return None
        picked: list[AudioBlock] = []
        total = 0
        for block in reversed(self.blocks):
            if block.block_number <= after:
                break
            picked.append(block)
            total += block.sample_count * 10**9 // RATE
            if total >= duration_ns:
                break
        return AudioRead(RATE, 2, tuple(reversed(picked)))


def stereo(seconds: float, amplitude: float) -> list[NDArray[np.float32]]:
    """10 ms 一块的立体声：幅度为 0 是静音，否则是 300 Hz 的正弦。"""
    t = np.arange(round(seconds * RATE)) / RATE
    mono = amplitude * np.sin(2 * np.pi * 300 * t)
    samples = np.stack([mono, mono], axis=1).astype(np.float32)
    return [np.ascontiguousarray(samples[i : i + BLOCK]) for i in range(0, len(samples), BLOCK)]


@dataclass
class _Shared:
    """替身插件与测试之间共享的东西，每个测试重置。"""

    model: Callable[[], SpeechModel] = EnergyModel
    service: VoiceActivityService | None = None


Shared = _Shared()


@pytest.fixture(autouse=True)
def _reset() -> None:
    Shared.model = EnergyModel
    Shared.service = None


class FakeModelPlugin(VoiceActivityPlugin):
    """同一个插件，模型换成假模型。"""

    def load_model(self, data_dir: Path) -> SpeechModel:
        del data_dir
        return Shared.model()


class Probe(Plugin[NoConfig]):
    """依赖 `voice-activity@1` 的探针插件，把服务交给测试。"""

    manifest: ClassVar = Manifest(
        id="voice-probe", version="1", tier=Tier.ORDINARY, inject=(VOICE_ACTIVITY.ref,)
    )

    async def activate(self, ctx: PluginContext[NoConfig]) -> None:
        Shared.service = ctx.service(VOICE_ACTIVITY)


def entry(
    entry_id: str,
    plugin: str | None = None,
    *,
    config: dict[str, Any] | None = None,
    grant: Sequence[str] = (),
) -> ResolvedEntry:
    return ResolvedEntry(
        id=entry_id,
        plugin=plugin or entry_id,
        config=config or {},
        disabled=False,
        grant=frozenset(grant),
    )


FAST = {"poll_interval": 0.005, "stall_timeout": 30.0}


def kernel(
    audio: MemoryAudio | None, plugin: type[VoiceActivityPlugin] = FakeModelPlugin
) -> Kernel:
    return Kernel(
        catalog=catalog(plugin, Probe),
        core=NoCore(),
        options=KernelOptions(retry_delays=()),
        audio=audio,
    )


def profile(config: dict[str, Any] | None = None) -> list[ResolvedEntry]:
    return [
        entry("voice", "voice-activity", config={**FAST, **(config or {})}, grant=[AUDIO_READ]),
        entry("probe", "voice-probe"),
    ]


async def until[T](probe: Callable[[], T | None], timeout: float = WAIT) -> T:
    deadline = time.monotonic() + timeout
    while (value := probe()) is None:
        assert time.monotonic() < deadline, "等待的条件没有成立"
        await asyncio.sleep(0.005)
    return value


async def service() -> VoiceActivityService:
    return await until(lambda: Shared.service)


async def reaches(
    voice: VoiceActivityService, state: VoiceState, reason: UnknownReason | None = None
) -> None:
    def probe() -> bool | None:
        status = voice.status
        return True if status.state is state and status.reason is reason else None

    await until(probe)


def test_manifest_and_config() -> None:
    manifest = VoiceActivityPlugin.manifest
    assert (manifest.id, manifest.tier, manifest.provides, manifest.inject) == (
        "voice-activity",
        Tier.ORDINARY,
        ("voice-activity@1",),
        (),
    )
    assert manifest.permissions == (AUDIO_READ,)
    config = VoiceActivityConfig()
    assert (config.data_dir, config.poll_interval, config.stall_timeout) == (None, 0.02, 0.5)
    params = config.params()
    assert (params.onset, params.offset, params.min_speech, params.min_silence) == (
        0.5,
        0.35,
        0.1,
        0.5,
    )
    for bad in (
        {"offset": 0.6},
        {"onset": 0.0},
        {"min_silence": 0.0},
        {"poll_interval": 2.0},
        {"stall_timeout": float("inf")},
        {"data_dir": "relative"},
        {"model_path": "/m.onnx"},
    ):
        with pytest.raises(ValidationError):
            VoiceActivityConfig.model_validate(bad)


def test_the_plugin_waits_for_the_permission() -> None:
    async def scenario() -> None:
        k = kernel(MemoryAudio())
        k.add([entry("voice", "voice-activity", config=FAST)])
        await k.settled()
        assert [r.code for r in k.status("voice").reasons] == [ReasonCode.PERMISSION_NOT_GRANTED]
        await k.shutdown()

    asyncio.run(scenario())


def test_changes_match_the_core_fed_block_by_block(caplog: pytest.LogCaptureFixture) -> None:
    """一次读到几块因读取时机而异，变化序列与直接逐块喂给核心的逐条相同。"""

    async def scenario() -> None:
        audio = MemoryAudio()
        k = kernel(audio)
        k.add(profile())
        await k.settled()
        voice = await service()
        received: list[VoiceChange] = []
        unsubscribe = voice.subscribe(received.append)

        def broken(change: VoiceChange) -> None:
            raise RuntimeError("监听者出错")

        voice.subscribe(broken)
        chunks = stereo(0.7, 0.0) + stereo(1.0, 0.3) + stereo(0.7, 0.0) + stereo(0.5, 0.2)
        chunks += stereo(1.0, 0.0)
        start = time.perf_counter_ns()
        pushed: list[AudioBlock] = []
        rng = np.random.default_rng(7)
        index = 0
        while index < len(chunks):
            for _ in range(int(rng.integers(1, 41))):
                if index < len(chunks):
                    pushed.append(audio.push(chunks[index], start + index * BLOCK_NS))
                    index += 1
            await asyncio.sleep(float(rng.choice([0.0, 0.003, 0.012])))
        end = start + len(chunks) * BLOCK_NS

        def done() -> bool | None:
            processed = voice.status.processed_ns
            return True if processed is not None and processed > end - 40 * 1_000_000 else None

        await until(done)
        offline = VoiceActivityDetector(EnergyModel(), VoiceActivityConfig().params())
        expected: list[VoiceChange] = []
        for block in pushed:
            expected.extend(offline.feed(block, sample_rate=RATE, channels=2))
        assert received == expected
        assert [c.state for c in received] == [
            VoiceState.IDLE,
            VoiceState.BUSY,
            VoiceState.IDLE,
            VoiceState.BUSY,
            VoiceState.IDLE,
        ]
        assert voice.status == offline.status
        assert "监听者处理变化" in caplog.text
        unsubscribe()
        unsubscribe()
        await k.shutdown()

    asyncio.run(scenario())


def test_no_audio_ring_is_unknown_until_it_appears() -> None:
    async def scenario() -> None:
        audio = MemoryAudio()
        audio.present = False
        k = kernel(audio)
        k.add(profile())
        await k.settled()
        voice = await service()
        await reaches(voice, VoiceState.UNKNOWN, UnknownReason.NO_AUDIO)
        audio.present = True
        start = time.perf_counter_ns()
        for i, chunk in enumerate(stereo(0.7, 0.0)):
            audio.push(chunk, start + i * BLOCK_NS)
        await reaches(voice, VoiceState.IDLE)
        # 音频环又没了。
        audio.present = False
        await reaches(voice, VoiceState.UNKNOWN, UnknownReason.NO_AUDIO)
        await k.shutdown()

    asyncio.run(scenario())


def test_stalled_block_numbers_are_unknown() -> None:
    async def scenario() -> None:
        audio = MemoryAudio()
        k = kernel(audio)
        k.add(profile({"stall_timeout": 0.1}))
        await k.settled()
        voice = await service()
        # 有音频环而一直没有块：从开始等的时刻算。
        await reaches(voice, VoiceState.UNKNOWN, UnknownReason.STALLED)
        chunks = stereo(0.7, 0.0)
        start = time.perf_counter_ns() - len(chunks) * BLOCK_NS
        for i, chunk in enumerate(chunks):
            audio.push(chunk, start + i * BLOCK_NS)
        await reaches(voice, VoiceState.IDLE)
        # 块不再前进：最新一块的末尾早于现在超过 0.1 秒。
        await reaches(voice, VoiceState.UNKNOWN, UnknownReason.STALLED)
        # 恢复后开始新的一段。
        start = time.perf_counter_ns()
        for i, chunk in enumerate(stereo(0.3, 0.3)):
            audio.push(chunk, start + i * BLOCK_NS)
        await reaches(voice, VoiceState.BUSY)
        await k.shutdown()

    asyncio.run(scenario())


def test_a_replaced_ring_is_read_from_the_start() -> None:
    async def scenario() -> None:
        audio = MemoryAudio()
        k = kernel(audio)
        k.add(profile())
        await k.settled()
        voice = await service()
        received: list[VoiceChange] = []
        voice.subscribe(received.append)
        start = time.perf_counter_ns()
        for i, chunk in enumerate(stereo(1.0, 0.0)):
            audio.push(chunk, start + i * BLOCK_NS)
        await reaches(voice, VoiceState.IDLE)
        audio.replace()
        start = time.perf_counter_ns()
        for i, chunk in enumerate(stereo(0.3, 0.3)):
            audio.push(chunk, start + i * BLOCK_NS)
        await reaches(voice, VoiceState.BUSY)
        assert [(c.state, c.reason) for c in received] == [
            (VoiceState.IDLE, None),
            (VoiceState.UNKNOWN, UnknownReason.GAP),
            (VoiceState.BUSY, None),
        ]
        await k.shutdown()

    asyncio.run(scenario())


def test_a_failed_read_is_retried(caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> None:
        audio = MemoryAudio()
        audio.fail = AudioRingTornError("连续 8 次读到撕裂")
        k = kernel(audio)
        k.add(profile())
        await k.settled()
        voice = await service()
        start = time.perf_counter_ns()
        for i, chunk in enumerate(stereo(0.7, 0.0)):
            audio.push(chunk, start + i * BLOCK_NS)
        await reaches(voice, VoiceState.IDLE)
        assert "读音频环失败" in caplog.text
        assert "恢复读音频环" in caplog.text
        await k.shutdown()

    with caplog.at_level(logging.INFO, logger="game_runtime.voice_activity.plugin"):
        asyncio.run(scenario())


def test_an_error_in_detection_stops_it_as_unknown(caplog: pytest.LogCaptureFixture) -> None:
    def broken() -> None:
        raise RuntimeError("推理失败")

    Shared.model = lambda: EnergyModel(before=broken)

    async def scenario() -> None:
        audio = MemoryAudio()
        k = kernel(audio)
        k.add(profile())
        await k.settled()
        voice = await service()
        for chunk in stereo(0.1, 0.0):
            audio.push(chunk, time.perf_counter_ns())
        await reaches(voice, VoiceState.UNKNOWN, UnknownReason.ERROR)
        assert "检测出错" in caplog.text
        # 插件仍是 ACTIVE，停在未知。
        assert k.status("voice").state is State.ACTIVE
        await k.shutdown()

    asyncio.run(scenario())


def test_a_missing_model_artifact_fails_activation(tmp_path: Path) -> None:
    async def scenario() -> None:
        k = kernel(MemoryAudio(), plugin=VoiceActivityPlugin)
        k.add(profile({"data_dir": str(tmp_path)}))
        await k.settled()
        [reason] = k.status("voice").reasons
        assert reason.code is ReasonCode.RETRIES_EXHAUSTED
        assert "silero-vad" in reason.message
        assert "uv run fetch-models" in reason.message
        await k.shutdown()

    asyncio.run(scenario())


def test_without_audio_from_the_host_activation_fails() -> None:
    async def scenario() -> None:
        k = kernel(None)
        k.add(profile())
        await k.settled()
        [reason] = k.status("voice").reasons
        assert reason.code is ReasonCode.RETRIES_EXHAUSTED
        assert "宿主没有给出音频" in reason.message
        await k.shutdown()

    asyncio.run(scenario())


def test_disabling_waits_for_the_inference_in_progress() -> None:
    gate = threading.Event()
    started = threading.Event()

    def hold() -> None:
        started.set()
        assert gate.wait(WAIT)

    model = EnergyModel(before=hold)
    Shared.model = lambda: model

    async def scenario() -> None:
        audio = MemoryAudio()
        k = kernel(audio)
        k.add(profile())
        await k.settled()
        voice = await service()
        start = time.perf_counter_ns()
        for i, chunk in enumerate(stereo(0.1, 0.3)):
            audio.push(chunk, start + i * BLOCK_NS)
        await asyncio.to_thread(started.wait, WAIT)
        disabling = asyncio.create_task(k.disable("voice"))
        await asyncio.sleep(0.1)
        assert not disabling.done()  # 推理还在工作线程里
        assert k.status("voice").state is State.UNLOADING
        gate.set()
        await asyncio.wait_for(disabling, WAIT)
        assert k.status("voice").state is State.DISPOSED
        frames = model.frames
        await asyncio.sleep(0.05)
        assert model.frames == frames  # 停用后不再推理
        assert voice.status.state is VoiceState.UNKNOWN
        await k.shutdown()

    asyncio.run(scenario())
