"""受监督运行的主进程组装。许可源分别直连实时核。"""

import asyncio
import logging
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from core_protocol.models import InputReleasedParams, LeaseStatus, ReleaseReason
from game_runtime._threads import in_thread
from game_runtime.adapter import (
    GAME_ADAPTER,
    GAMES_DIR,
    Adapter,
    GameAdapterConfig,
    GameAdapterPlugin,
    load_adapter,
)
from game_runtime.audio_ring import AudioRingTornError
from game_runtime.core_link import (
    AudioConfig,
    BellisCoreOptions,
    CoreLink,
    CoreLinkOptions,
    StopStage,
)
from game_runtime.frames import DEFAULT_BACKLOG, AdmissionPolicy, SequencedFrameSource
from game_runtime.kernel import (
    AUDIO_READ,
    CONTROL_SUBMIT,
    FRAMES_READ,
    Kernel,
    Plugin,
    PluginContext,
    ReasonCode,
    ResolvedEntry,
    State,
    Transition,
    catalog,
)
from game_runtime.models import VLM, VlmConfig, VlmPlugin, vlm_plugin_for
from game_runtime.narrative import NarrativePlugin
from game_runtime.ocr import OcrConfig, OcrPlugin
from game_runtime.operator import MonotonicClock, OperatorClient
from game_runtime.recording.consumer import RecordingConsumer, check_recording_quota
from game_runtime.recording.layout import (
    AdmissionInfo,
    GameInfo,
    RecordingMeta,
    Resolution,
    check_name,
    recordings_root,
)
from game_runtime.recording.replay import adapter_sha256
from game_runtime.recording.retention import QuotaKeeper, QuotaRefusedError, quota_bytes
from game_runtime.recording.session import AUDIO_POLL_INTERVAL, audio_config, capture_config
from game_runtime.recording.writer import RecordingWriter
from game_runtime.scene import ScenePlugin
from game_runtime.secret_store import SecretStore
from game_runtime.step_control.contracts import Policy
from game_runtime.step_control.controller import StepConfig, StepController
from game_runtime.step_control.plugin import SessionSignals, step_plugin_for
from game_runtime.step_control.supervision import PlaySession
from game_runtime.telemetry import RuntimeTelemetry, TelemetryPlugin
from game_runtime.telemetry.plugin import telemetry_plugin_for
from game_runtime.voice_activity import VoiceActivityPlugin

if TYPE_CHECKING:
    from game_runtime.game_bridge.session import BellisConfig

logger = logging.getLogger(__name__)
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
    _writer: RecordingWriter | None = None

    @property
    def controller(self) -> StepController | None:
        return self.signals.controller

    @property
    def recording_path(self) -> Path | None:
        return None if self._writer is None else self._writer.path

    @property
    def recording_meta(self) -> RecordingMeta | None:
        """录制状态。等待会话收尾后可读到最终结果。"""
        return None if self._writer is None else self._writer.meta

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


def _prepare_recording(
    adapter: Adapter,
    games_dir: Path,
    data_dir: Path,
    name: str | None,
    admission: AdmissionPolicy,
    audio: AudioConfig | None,
) -> tuple[RecordingWriter, QuotaKeeper]:
    from game_runtime.recording.cli import tool_info

    root = recordings_root(data_dir, adapter.id)
    quota = QuotaKeeper(root, quota_bytes(adapter))
    if check_recording_quota(quota).over:
        raise QuotaRefusedError(f"{adapter.id} 的录制配额已满")
    writer = RecordingWriter.create(
        root,
        game=GameInfo(
            id=adapter.id,
            name=adapter.name,
            version=adapter.version,
            adapter_sha256=adapter_sha256(adapter.id, games_dir),
            resolution=Resolution(width=adapter.resolution.width, height=adapter.resolution.height),
        ),
        name=name,
        admission=AdmissionInfo(
            max_rate=admission.max_rate,
            idle_interval=admission.idle_interval,
            backlog=DEFAULT_BACKLOG,
        ),
        audio_format=None if audio is None else (audio.sample_rate, audio.channels),
        tool=tool_info(),
    )
    return writer, quota


async def _record_audio(core: CoreLink, recorder: RecordingConsumer) -> None:
    after = 0
    try:
        while True:
            ring = core.audio
            if ring is not None and ring.latest_block_number() > after:
                try:
                    read = ring.read_recent(2**63 - 1, after=after)
                except AudioRingTornError:
                    read = None
                if read is not None and read.blocks:
                    if not recorder.offer_audio(read):
                        return
                    after = read.blocks[-1].block_number
            await asyncio.sleep(AUDIO_POLL_INTERVAL)
    except Exception as error:
        recorder.fail(error)


async def _wait_shutdown(task: asyncio.Task[None]) -> None:
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.wait((task,))
            except asyncio.CancelledError:
                continue
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("监督会话收尾失败", exc_info=error)
        raise


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
    record: bool = False,
    name: str | None = None,
) -> AsyncGenerator[SupervisedRun]:
    """用模型 profile 或调用方的单独策略启动监督会话。"""
    if name is not None:
        if not record:
            raise ValueError("录制名称只用于启用录制的会话")
        check_name(name)
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

    telemetry_id = TelemetryPlugin.manifest.id
    telemetry_entries = [entry for entry in entries if entry.plugin == telemetry_id]
    if telemetry_entries:
        # 逆序停用时让控制产出者和语义桥先结束各自的在途步骤。
        entries = telemetry_entries + [entry for entry in entries if entry.plugin != telemetry_id]
    telemetry: RuntimeTelemetry | None = None

    def remember_telemetry(service: RuntimeTelemetry) -> None:
        nonlocal telemetry
        telemetry = service

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

    core: CoreLink | None = None
    kernel: Kernel | None = None
    frames: SequencedFrameSource | None = None
    preparing: asyncio.Task[tuple[RecordingWriter, QuotaKeeper]] | None = None
    writer: RecordingWriter | None = None
    quota: QuotaKeeper | None = None
    recorder: RecordingConsumer | None = None
    audio_task: asyncio.Task[None] | None = None
    recorder_watch: asyncio.Task[None] | None = None
    shutdown_task: asyncio.Task[None] | None = None

    async def stop_capture() -> None:
        if recorder is not None:
            recorder.close_input()
        tasks = tuple(task for task in (audio_task, recorder_watch) if task is not None)
        for task in tasks:
            task.cancel()
        try:
            if frames is not None:
                await frames.close()
        finally:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def stop_runtime() -> None:
        try:
            if kernel is not None:
                await kernel.shutdown()
        finally:
            try:
                await stop_capture()
            finally:
                if core is not None:
                    stopped = await core.stop()
                    if recorder is not None:
                        if stopped.returncode is None:
                            recorder.fail(RuntimeError("未确认实时核退出"))
                        elif stopped.returncode != 0 or stopped.stage is StopStage.KILLED:
                            detail = f"{stopped.stage}，退出码 {stopped.returncode}"
                            recorder.fail(RuntimeError(f"实时核异常退出（{detail}）"))
                    if telemetry is not None and (
                        stopped.returncode is not None or stopped.stage is StopStage.ALREADY_EXITED
                    ):
                        with suppress(Exception):
                            await telemetry.aclose()

    async def finish_recording(reason: str) -> None:
        if preparing is None:
            return
        owned_writer = writer
        if owned_writer is None:
            # 预检被取消时仍取回已建的 writer。此时还没有启动实时核。
            try:
                owned_writer, _ = await preparing
            except Exception:
                return
        try:
            if recorder is None:
                await owned_writer.finish(stop_reason="error", state="failed")
            else:
                await recorder.finish(
                    stop_reason=reason,
                    reader_overflows=0 if frames is None else frames.stats.overflows,
                )
        except Exception:
            logger.exception("监督录制 %s 未完整封存", owned_writer.path)

    async def shutdown(reason: str) -> None:
        try:
            try:
                await stop_runtime()
            finally:
                if client is not None:
                    await client.close()
        except Exception as error:
            if recorder is not None:
                recorder.fail(error)
            raise
        finally:
            await finish_recording(reason)

    def begin_shutdown(reason: str) -> asyncio.Task[None]:
        nonlocal shutdown_task
        if shutdown_task is None:
            shutdown_task = asyncio.create_task(shutdown(reason), name="监督会话收尾")
        return shutdown_task

    async def watch_recording(consumer: RecordingConsumer) -> None:
        reason = await consumer.wait_interruption()
        begin_shutdown(reason)
        signals.on_end("recording-" + reason)

    def control_open() -> bool:
        return shutdown_task is None and (recorder is None or recorder.interruption is None)

    def observe_shutdown(transition: Transition) -> None:
        if recorder is None or shutdown_task is None or transition.after is not State.UNLOADING:
            return
        for failure in transition.reasons:
            if failure.code in (ReasonCode.RELEASE_TIMEOUT, ReasonCode.RELEASE_FAILED):
                recorder.fail(RuntimeError(failure.message))
                return

    reason = "error"
    try:
        policy_admission = admission or AdmissionPolicy()
        if record:
            preparing = asyncio.create_task(
                in_thread(
                    partial(
                        _prepare_recording,
                        adapter,
                        games_dir,
                        data_dir,
                        name,
                        policy_admission,
                        audio,
                    )
                ),
                name="监督录制建档",
            )
            opened_writer, opened_quota = await asyncio.shield(preparing)
            writer, quota = opened_writer, opened_quota
            opened_quota.set_current(opened_writer.path)
            if (await in_thread(partial(check_recording_quota, opened_quota))).over:
                raise QuotaRefusedError(f"{adapter.id} 建档后已超出录制配额")
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
        runtime = core = factory(
            options,
            on_health=lambda _: signals.became_ready(),
            on_released=released,
            on_restart=restarted,
        )
        await runtime.start()
        ring = runtime.frames
        if ring is None:
            raise RuntimeError("实时核未提供帧环")
        if writer is not None and quota is not None:
            recorder = RecordingConsumer(writer, quota, capacity=DEFAULT_BACKLOG)
        source = frames = SequencedFrameSource(
            ring,
            policy=policy_admission,
            sink=None if recorder is None else recorder.accept_frame,
        )

        def ready() -> bool:
            health = runtime.last_health
            return (
                control_open()
                and runtime.healthy
                and health is not None
                and health.lease is LeaseStatus.VALID
            )

        async def select(selected: bool) -> None:
            target = producer if selected else None
            if control_open() and kernel is not None and kernel.control_producer != target:
                await kernel.set_control_producer(target)

        bridge_plugins: tuple[type[Plugin[Any]], ...] = ()
        if semantic is not None:
            from game_runtime.game_bridge.plugin import game_bridge_for

            bridge_plugins = (game_bridge_for(semantic, runtime, round(frame_age * 1e9), select),)
            entries.append(_entry("game-bridge", "game-bridge", {}))
        control_kernel = kernel = Kernel(
            catalog=catalog(
                GameAdapterPlugin,
                ocr_plugin,
                ScenePlugin,
                *((telemetry_plugin_for(remember_telemetry),) if telemetry_entries else ()),
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
            core=runtime,
            on_transition=None if recorder is None else observe_shutdown,
            control_open=None if recorder is None else control_open,
            frames=source,
            audio=runtime.audio_reader,
        )
        runtime.bind_control(lambda: control_kernel.active_control)
        if recorder is not None:
            recorder_watch = asyncio.create_task(watch_recording(recorder), name="监督录制中断")
            if audio is not None:
                audio_task = asyncio.create_task(
                    _record_audio(runtime, recorder), name="监督录音读环"
                )
        control_kernel.add(entries)
        await control_kernel.set_control_producer(producer)
        if not control_open():
            raise RuntimeError("监督录制在启动时中断")
        source.start()
        if recorder is not None and (pump := source.done) is not None:
            consumer = recorder

            def stopped_frames(_: asyncio.Task[None]) -> None:
                failure = source.failure
                if isinstance(failure, Exception):
                    consumer.fail(failure)

            pump.add_done_callback(stopped_frames)
        try:
            yield SupervisedRun(runtime, control_kernel, session, signals, writer)
        except asyncio.CancelledError:
            reason = "interrupt"
            raise
        else:
            reason = "enter"
    finally:
        await _wait_shutdown(begin_shutdown(reason))
