"""受监督运行的主进程组装。许可源分别直连实时核。"""

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from core_protocol.models import InputReleasedParams, LeaseStatus, ReleaseReason
from game_runtime.adapter import (
    GAME_ADAPTER,
    GAMES_DIR,
    GameAdapterConfig,
    GameAdapterPlugin,
    load_adapter,
)
from game_runtime.core_link import BellisCoreOptions, CoreLink, CoreLinkOptions
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
from game_runtime.step_control.supervision import PlaySession
from game_runtime.voice_activity import VoiceActivityPlugin

if TYPE_CHECKING:
    from game_runtime.game_bridge.session import BellisConfig

STEP_ENTRY = "step-control"
_MANUAL_RELEASES = frozenset(
    {
        ReleaseReason.TAKEOVER,
        ReleaseReason.FOCUS_LOST,
        ReleaseReason.EMERGENCY_STOP,
        ReleaseReason.INJECTION_BLOCKED,
        ReleaseReason.HOOK_LOST,
    }
)
_SEMANTIC_RELEASES = _MANUAL_RELEASES | {
    ReleaseReason.CORE_START,
    ReleaseReason.LEASE_SOURCE_LOST,
    ReleaseReason.LEASE_EXPIRED,
    ReleaseReason.LEASE_REVOKED,
    ReleaseReason.NO_LEASE,
    ReleaseReason.HEARTBEAT_TIMEOUT,
    ReleaseReason.FRAME_STALE,
    ReleaseReason.STOP,
}


@dataclass(frozen=True, slots=True)
class SupervisedRun:
    core: CoreLink
    kernel: Kernel
    operator: PlaySession
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
    core_factory: type[CoreLink] | None = None,
    bellis: "BellisConfig | None" = None,
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
    if link_options.bellis is not None or (
        bellis is not None and link_options.operator is not None
    ):
        raise ValueError("许可源由监督启动器选择，操作员与 Bellis 只能选一个")
    client = None
    semantic = None
    bellis_control = None
    session: PlaySession
    if bellis is None:
        client = OperatorClient(
            adapter,
            store=store,
            on_session=signals.session,
            on_ended=signals.on_end,
            on_decision_resolved=signals.resolved,
        )
        session = client
    else:
        from game_runtime.game_bridge.session import BellisSession, read_bellis_session

        info = read_bellis_session(bellis.session_store)
        if info is None:
            raise RuntimeError("未找到 Bellis 本次会话")
        runtime_id = uuid4().hex
        bellis_control = BellisCoreOptions(info.session_id, runtime_id, bellis.test_session_file)
        semantic = BellisSession(bellis, info, runtime_id, adapter, clock, signals)
        session = semantic

    def released(message: InputReleasedParams) -> None:
        if semantic is not None and message.reason in _SEMANTIC_RELEASES:
            semantic.suspend(manual=message.reason in _MANUAL_RELEASES)
        signals.released(message)

    def restarted(attempt: int) -> None:
        if semantic is not None:
            semantic.suspend()
        signals.restart(attempt)

    try:
        if client is not None:
            await client.connect()
        options = replace(
            link_options,
            operator=None if client is None else client.credentials,
            bellis=bellis_control,
            capture=link_options.capture or capture_config(adapter),
            audio=audio,
            real_input=real_input,
            input_policy=adapter.input_policy(),
        )
        factory = core_factory or CoreLink
        if semantic is not None:
            factory = partial(factory, action_deadline=semantic.action_deadline)
        core = factory(
            options,
            on_health=lambda _: signals.became_ready(),
            on_released=released,
            on_restart=restarted,
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

            async def select(selected: bool) -> None:
                target = producer if selected else None
                if kernel is not None and kernel.control_producer != target:
                    await kernel.set_control_producer(target)

            bridge_plugins: tuple[type[Plugin[Any]], ...] = ()
            if semantic is not None:
                from game_runtime.game_bridge.plugin import game_bridge_for

                bridge_plugins = (game_bridge_for(semantic, core, round(frame_age * 1e9), select),)
                entries.append(_entry("game-bridge", "game-bridge", {}))
            kernel = Kernel(
                catalog=catalog(
                    GameAdapterPlugin,
                    ocr_plugin,
                    ScenePlugin,
                    *configured_plugins,
                    *extra_plugins,
                    *bridge_plugins,
                    step_plugin_for(
                        session,
                        policy,
                        signals,
                        clock,
                        ready,
                        model_policy=_model_policy if profile is not None else None,
                        semantic=semantic,
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
            yield SupervisedRun(core, kernel, session, signals)
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
        if client is not None:
            await client.close()
