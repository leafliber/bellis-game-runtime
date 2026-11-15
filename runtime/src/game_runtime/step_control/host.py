"""受监督运行的主进程组装。操作员控制台直接向实时核授予测试许可。"""

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from core_protocol.models import LeaseStatus
from game_runtime.adapter import GAMES_DIR, GameAdapterPlugin, load_adapter
from game_runtime.core_link import CoreLink, CoreLinkOptions
from game_runtime.frames import AdmissionPolicy, SequencedFrameSource
from game_runtime.kernel import CONTROL_SUBMIT, FRAMES_READ, Kernel, Plugin, ResolvedEntry, catalog
from game_runtime.ocr import OcrConfig, OcrPlugin
from game_runtime.operator import MonotonicClock, OperatorClient
from game_runtime.recording.session import capture_config
from game_runtime.scene import ScenePlugin
from game_runtime.secret_store import SecretStore
from game_runtime.step_control.contracts import Policy
from game_runtime.step_control.controller import StepConfig, StepController
from game_runtime.step_control.plugin import SessionSignals, step_plugin_for

STEP_ENTRY = "step-control"


@dataclass(frozen=True, slots=True)
class SupervisedRun:
    core: CoreLink
    kernel: Kernel
    operator: OperatorClient
    signals: SessionSignals

    @property
    def controller(self) -> StepController | None:
        return self.signals.controller

    async def wait_ended(self) -> None:
        await self.signals.ended.wait()


def _entry(
    id: str, plugin: str, config: dict[str, Any], grant: frozenset[str] = frozenset()
) -> ResolvedEntry:
    return ResolvedEntry(id=id, plugin=plugin, config=config, disabled=False, grant=grant)


@asynccontextmanager
async def supervise(
    game: str,
    policy: Policy,
    link_options: CoreLinkOptions,
    *,
    data_dir: Path,
    store: SecretStore,
    games_dir: Path = GAMES_DIR,
    real_input: bool = False,
    step: StepConfig | None = None,
    admission: AdmissionPolicy | None = None,
    ocr_plugin: type[Plugin[OcrConfig]] = OcrPlugin,
    extra_plugins: Sequence[type[Plugin[Any]]] = (),
    extra_entries: Sequence[ResolvedEntry] = (),
) -> AsyncGenerator[SupervisedRun]:
    """启动已选游戏和策略。Windows 真输入须显式选择，测试仍由真实操作员连接授予许可。"""
    games_dir, data_dir = games_dir.resolve(), data_dir.resolve()
    adapter = load_adapter(games_dir / game)
    signals = SessionSignals()
    clock = MonotonicClock()
    client = OperatorClient(
        adapter,
        store=store,
        on_session=signals.session,
        on_ended=signals.on_end,
        on_decision_resolved=signals.resolved,
    )
    try:
        await client.connect()
        options = replace(
            link_options,
            operator=client.credentials,
            capture=link_options.capture or capture_config(adapter),
            real_input=real_input,
            input_policy=adapter.input_policy(),
        )
        core = CoreLink(
            options,
            on_health=lambda _: signals.became_ready(),
            on_released=signals.released,
            on_restart=signals.restart,
        )
        kernel: Kernel | None = None
        frames: SequencedFrameSource | None = None
        try:
            await core.start()
            ring = core.frames
            if ring is None:
                raise RuntimeError("实时核未提供帧环")
            frames = SequencedFrameSource(ring, policy=admission or AdmissionPolicy())

            def ready() -> bool:
                health = core.last_health
                return core.healthy and health is not None and health.lease is LeaseStatus.VALID

            step_config = step or StepConfig(max_frame_age=options.max_frame_age)
            if step_config.max_frame_age > options.max_frame_age:
                raise ValueError("逐步控制的帧龄上限不能超过实时核")
            kernel = Kernel(
                catalog=catalog(
                    GameAdapterPlugin,
                    ocr_plugin,
                    ScenePlugin,
                    *extra_plugins,
                    step_plugin_for(client, policy, signals, clock, ready),
                ),
                core=core,
                frames=frames,
                audio=core.audio_reader,
            )
            core.bind_control(lambda: kernel.active_control)
            data = {"data_dir": str(data_dir)}
            kernel.add(
                [
                    _entry(
                        "game", "game-adapter", {"game": adapter.id, "games_dir": str(games_dir)}
                    ),
                    _entry("ocr", ocr_plugin.manifest.id, data),
                    _entry("scene", "scene", data, frozenset({FRAMES_READ})),
                    *extra_entries,
                    _entry(
                        STEP_ENTRY,
                        STEP_ENTRY,
                        step_config.model_dump(mode="json"),
                        frozenset({CONTROL_SUBMIT}),
                    ),
                ]
            )
            await kernel.set_control_producer(STEP_ENTRY)
            frames.start()
            yield SupervisedRun(core, kernel, client, signals)
        finally:
            try:
                if kernel is not None:
                    await kernel.shutdown()
            finally:
                try:
                    if frames is not None:
                        await frames.close()
                finally:
                    await core.stop()
    finally:
        await client.close()
