"""以监督宿主和假设备执行一段录制。播放端只接收帧和操作员目标。"""

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from core_protocol.models import LeaseStatus
from game_runtime.adapter import GAMES_DIR, GameAdapterConfig, load_adapter
from game_runtime.core_link import CaptureConfig, CoreLinkOptions
from game_runtime.core_link.fake_device import FakeDevice
from game_runtime.frame_ring.fake import FakeDisplay
from game_runtime.frames import AdmissionPolicy
from game_runtime.kernel import Plugin, ResolvedEntry
from game_runtime.narrative import NarrativePlugin
from game_runtime.ocr import OcrConfig, OcrPlugin
from game_runtime.operator import OperatorConsole
from game_runtime.operator.terminal import TerminalConsole
from game_runtime.recording.reader import Recording
from game_runtime.secret_store import MemorySecretStore
from game_runtime.step_control import STEP_ENTRY, Policy, StepConfig, SupervisedRun, supervise
from game_runtime.voice_activity import VoiceActivityPlugin
from replay_benchmark.score import Trace
from replay_benchmark.telemetry import collect, core_for, record_device
from replay_benchmark.timeline import Timeline


def _replay_profile(
    entries: Sequence[ResolvedEntry], scratch: Path, *, fast_paths: bool
) -> list[ResolvedEntry]:
    profile: list[ResolvedEntry] = []
    for entry in entries:
        config = dict(entry.config)
        disabled = entry.disabled
        if entry.plugin == VoiceActivityPlugin.manifest.id:
            disabled = True
        elif entry.plugin == NarrativePlugin.manifest.id:
            config["data_dir"] = str(scratch)
        elif entry.plugin == STEP_ENTRY:
            config["fast_paths"] = fast_paths
        profile.append(replace(entry, config=config, disabled=disabled))
    return profile


async def _play_after_start(
    run: SupervisedRun,
    console: OperatorConsole,
    display: FakeDisplay,
    timeline: Timeline,
    goal: str,
    settle: float,
) -> None:
    while not console.ended:
        health = run.core.last_health
        if (
            run.operator.may_autoplay
            and run.core.healthy
            and health is not None
            and health.lease is LeaseStatus.VALID
        ):
            break
        await asyncio.sleep(0.02)
    else:
        return
    console.set_goal(goal)
    revision = console.session().goal_revision
    while run.operator.session is None or run.operator.session.goal_revision < revision:
        if console.ended:
            return
        await asyncio.sleep(0.02)
    await timeline.play(display)
    await asyncio.sleep(settle)
    timeline.trace.finished = True


async def replay_case(
    recording: Recording,
    *,
    core: Path,
    data_dir: Path,
    goal: str,
    commands: AsyncIterator[str],
    write: Callable[[str], None],
    profile: Sequence[ResolvedEntry] | None = None,
    policy: Policy | None = None,
    games_dir: Path = GAMES_DIR,
    step: StepConfig | None = None,
    ocr_plugin: type[Plugin[OcrConfig]] = OcrPlugin,
    fast_paths: bool = False,
) -> Trace:
    if (profile is None) == (policy is None):
        raise ValueError("须指定模型 profile 或可信的 Policy，二选一")
    if profile is not None and step is not None:
        raise ValueError("选定模型 profile 时逐步设置由 profile 提供")
    if not 0 < len(goal.strip()) <= 2000:
        raise ValueError("评测目标须为 1–2000 字")
    trace = Trace()
    timeline = Timeline(recording, trace)
    game = GameAdapterConfig(game=recording.meta.game.id)
    adapter = load_adapter(games_dir / game.game)
    admission_info = recording.meta.admission
    admission = AdmissionPolicy(
        max_rate=admission_info.max_rate,
        idle_interval=admission_info.idle_interval,
    )
    if profile is None:
        step = (step or StepConfig()).model_copy(update={"fast_paths": fast_paths})

    with (
        TemporaryDirectory(prefix="bellis-replay-benchmark-") as temporary,
        FakeDisplay.create(timeline.width, timeline.height) as display,
        FakeDevice() as device,
        collect(trace),
    ):
        scratch = Path(temporary).resolve()
        entries = (
            None if profile is None else _replay_profile(profile, scratch, fast_paths=fast_paths)
        )
        options = CoreLinkOptions(
            core,
            capture=CaptureConfig(
                target=display.target, max_width=timeline.width, max_height=timeline.height
            ),
            test_device=device.credentials,
            restart=None,
        )
        store = MemorySecretStore()
        console = OperatorConsole(adapter, store=store, data_dir=scratch)
        await console.open()
        try:
            async with supervise(
                adapter.id,
                policy,
                options,
                data_dir=data_dir,
                store=store,
                games_dir=games_dir,
                profile=entries,
                step=step,
                admission=admission,
                ocr_plugin=ocr_plugin,
                core_factory=core_for(trace),
            ) as run:
                await device.until_connected()
                controller = run.controller
                if controller is None:
                    reasons = [
                        f"{entry.id} {entry.state} {reason.code}（{reason.subject}）"
                        for entry in run.kernel.snapshot()
                        if entry.enabled
                        for reason in entry.reasons
                    ]
                    raise RuntimeError("逐步控制未激活。" + "，".join(reasons))
                config = controller.config
                trace.fast_paths_enabled = config.fast_paths
                settle = config.policy_timeout + config.receipt_timeout + config.effect_timeout
                unsubscribe = controller.scene.subscribe(timeline.observe)
                terminal = asyncio.create_task(
                    TerminalConsole(console, lines=commands, write=write).run()
                )
                playback = asyncio.create_task(
                    _play_after_start(run, console, display, timeline, goal, settle)
                )
                try:
                    done, _ = await asyncio.wait(
                        (terminal, playback), return_when=asyncio.FIRST_COMPLETED
                    )
                    if terminal in done:
                        await terminal
                    if playback in done:
                        await playback
                finally:
                    for task in (terminal, playback):
                        task.cancel()
                    await asyncio.gather(terminal, playback, return_exceptions=True)
                    unsubscribe()
                    if not console.ended:
                        await console.end("benchmark")
        finally:
            await console.close()
        record_device(trace, device.events())
    return trace
