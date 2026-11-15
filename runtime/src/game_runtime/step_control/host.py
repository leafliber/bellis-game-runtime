"""受监督运行的主进程组装。操作员控制台直接向实时核授予测试许可。"""

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from core_protocol.models import LeaseStatus
from game_runtime.adapter import (
    GAME_ADAPTER,
    GAMES_DIR,
    GameAdapterConfig,
    GameAdapterPlugin,
    load_adapter,
)
from game_runtime.core_link import CoreLink, CoreLinkOptions
from game_runtime.frames import AdmissionPolicy, SequencedFrameSource
from game_runtime.kernel import (
    AUDIO_READ,
    CONTROL_SUBMIT,
    FRAMES_READ,
    Kernel,
    Plugin,
    PluginContext,
    ResolvedEntry,
    catalog,
)
from game_runtime.models import VLM, VlmConfig, VlmPlugin, vlm_plugin_for
from game_runtime.narrative import NarrativePlugin
from game_runtime.ocr import OcrConfig, OcrPlugin
from game_runtime.operator import MonotonicClock, OperatorClient
from game_runtime.recording.session import audio_config, capture_config
from game_runtime.scene import ScenePlugin
from game_runtime.secret_store import SecretStore
from game_runtime.step_control.contracts import Policy
from game_runtime.step_control.controller import StepConfig, StepController
from game_runtime.step_control.plugin import SessionSignals, step_plugin_for
from game_runtime.voice_activity import VoiceActivityPlugin

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


def _profile_entries(
    profile: Sequence[ResolvedEntry],
    game: GameAdapterConfig,
    data_dir: Path,
    ocr_id: str,
    frame_age: float,
) -> tuple[list[ResolvedEntry], str]:
    selected = [entry.id for entry in profile if entry.plugin == STEP_ENTRY and not entry.disabled]
    if len(selected) != 1:
        raise ValueError("模型游玩 profile 须启用一个 step-control 条目")
    game_config = game.model_dump(mode="json")
    game_dir = game.games_dir or GAMES_DIR
    data_plugins = {
        ocr_id,
        ScenePlugin.manifest.id,
        NarrativePlugin.manifest.id,
        VoiceActivityPlugin.manifest.id,
    }
    entries: list[ResolvedEntry] = []
    for entry in profile:
        config = dict(entry.config)
        if entry.plugin == GameAdapterPlugin.manifest.id:
            configured = GameAdapterConfig.model_validate(game_config | config)
            configured_dir = (configured.games_dir or GAMES_DIR).resolve()
            if configured.game != game.game or configured_dir != game_dir:
                raise ValueError(f"profile 的 {entry.id} 使用了不同的游戏适配包")
            config = game_config
        elif entry.plugin in data_plugins:
            config.setdefault("data_dir", str(data_dir))
        elif entry.id == selected[0]:
            config.setdefault("max_frame_age", frame_age)
            step = StepConfig.model_validate(config)
            if step.max_frame_age > frame_age:
                raise ValueError("逐步控制的帧龄上限不能超过实时核")
        entries.append(replace(entry, config=config))
    return entries, selected[0]


def _profile_plugins(entries: Sequence[ResolvedEntry]) -> tuple[type[Plugin[Any]], ...]:
    from game_runtime.planner import PlannerPlugin

    models = [
        entry for entry in entries if entry.plugin == VlmPlugin.manifest.id and not entry.disabled
    ]
    if len(models) > 1:
        raise ValueError("模型游玩 profile 最多启用一个 vlm 条目")
    vlm = VlmPlugin if not models else vlm_plugin_for(VlmConfig.model_validate(models[0].config))
    return vlm, PlannerPlugin, NarrativePlugin, VoiceActivityPlugin


def _model_policy(ctx: PluginContext[StepConfig]) -> Policy:
    from game_runtime.vision_agent import VisionPolicy

    return VisionPolicy(ctx.service(GAME_ADAPTER), ctx.service(VLM))


@asynccontextmanager
async def supervise(
    game: str,
    policy: Policy | None,
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
    profile: Sequence[ResolvedEntry] | None = None,
) -> AsyncGenerator[SupervisedRun]:
    """用模型 profile 或调用方的单独策略启动监督会话。"""
    games_dir, data_dir = games_dir.resolve(), data_dir.resolve()
    game_config = GameAdapterConfig(game=game, games_dir=games_dir)
    adapter = load_adapter(games_dir / game_config.game)
    frame_age = link_options.max_frame_age
    configured_plugins: tuple[type[Plugin[Any]], ...] = ()
    audio = link_options.audio
    if profile is None:
        if policy is None:
            raise ValueError("须指定模型游玩 profile 或策略")
        step_config = step or StepConfig(max_frame_age=frame_age)
        if step_config.max_frame_age > frame_age:
            raise ValueError("逐步控制的帧龄上限不能超过实时核")
        data = {"data_dir": str(data_dir)}
        entries = [
            _entry("game", GameAdapterPlugin.manifest.id, game_config.model_dump(mode="json")),
            _entry("ocr", ocr_plugin.manifest.id, data),
            _entry("scene", ScenePlugin.manifest.id, data, frozenset({FRAMES_READ})),
            *extra_entries,
            _entry(
                STEP_ENTRY,
                STEP_ENTRY,
                step_config.model_dump(mode="json"),
                frozenset({CONTROL_SUBMIT}),
            ),
        ]
        producer = STEP_ENTRY
    else:
        if policy is not None or step is not None or extra_entries:
            raise ValueError("模型游玩 profile 已指定策略、配置和权限")
        entries, producer = _profile_entries(
            profile, game_config, data_dir, ocr_plugin.manifest.id, frame_age
        )
        configured_plugins = _profile_plugins(entries)
        if audio is None and any(
            entry.plugin == VoiceActivityPlugin.manifest.id
            and not entry.disabled
            and AUDIO_READ in entry.grant
            for entry in entries
        ):
            audio = audio_config(adapter)
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
            audio=audio,
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

            kernel = Kernel(
                catalog=catalog(
                    GameAdapterPlugin,
                    ocr_plugin,
                    ScenePlugin,
                    *configured_plugins,
                    *extra_plugins,
                    step_plugin_for(
                        client,
                        policy,
                        signals,
                        clock,
                        ready,
                        model_policy=_model_policy if profile is not None else None,
                    ),
                ),
                core=core,
                frames=frames,
                audio=core.audio_reader,
            )
            core.bind_control(lambda: kernel.active_control)
            kernel.add(entries)
            await kernel.set_control_producer(producer)
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
