"""同一监督会话的假输入、录制和真实 Rust 核停止。"""

import asyncio
import threading
from pathlib import Path
from typing import IO, cast

import pytest

from core_protocol import models
from game_runtime.adapter import GAMES_DIR, ScreenType
from game_runtime.core_link import CoreLink, CoreLinkOptions, StopResult, StopStage
from game_runtime.kernel import ControlGrant
from game_runtime.operator import EventKind
from game_runtime.recording import Recording
from game_runtime.recording import consumer as consumer_module
from game_runtime.recording.layout import META_FILE, read_meta
from game_runtime.recording.replay import adapter_sha256
from game_runtime.recording.retention import QuotaKeeper, QuotaRefusedError, QuotaStatus
from game_runtime.secret_store import MemorySecretStore
from game_runtime.step_control import (
    Category,
    Click,
    EffectKind,
    ExpectedEffect,
    Plan,
    PolicyInput,
    StepProposal,
    StepStatus,
    TextTarget,
    supervise,
)

from .test_step_control import Rig, rig, until
from .test_step_control import root as root

KEY = 30
SAMPLES = bytes.fromhex("0000803e000000bf")


class IdlePolicy:
    async def propose(self, inputs: PolicyInput) -> Plan:
        del inputs
        return Plan()


class StartGame:
    async def propose(self, inputs: PolicyInput) -> Plan:
        if inputs.scene.screen_state.classification.screen_type is not ScreenType.MENU:
            return Plan()
        return Plan(
            steps=(
                StepProposal(
                    Category.NAVIGATE,
                    Click(TextTarget("开始", "menu-start")),
                    ExpectedEffect(EffectKind.SCREEN, screen_type=ScreenType.DIALOGUE),
                ),
            )
        )


class RecordingIOGate:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self.loop = asyncio.get_running_loop()

    def wait(self) -> None:
        self.loop.call_soon_threadsafe(self.entered.set)
        if not self.release.wait(12):
            raise TimeoutError("录制 I/O 未获放行")

    def block(self, stream: IO[str] | IO[bytes], monkeypatch: pytest.MonkeyPatch) -> None:
        flush = stream.flush

        def blocked() -> None:
            self.wait()
            flush()

        monkeypatch.setattr(stream, "flush", blocked)


def test_supervise_records_the_frames_and_audio_of_an_executed_action(
    core_exe: Path, root: Path
) -> None:
    async def run() -> None:
        async with rig(core_exe, root, policy=StartGame(), record=True) as r:
            assert r.audio is not None
            controller = r.run.controller
            assert controller is not None
            r.world.presenting = True
            r.audio.play(SAMPLES)
            await r.start()
            await until(
                lambda: next(
                    (item for item in controller.recent if item.status is StepStatus.VERIFIED),
                    None,
                )
            )
            frame_number = r.world.submitted_click_frame
            assert frame_number is not None
            down = next(
                event
                for event in r.device.events()
                if isinstance(event.event, models.FakeMouseDown)
            )
            path = r.run.recording_path
            assert path is not None
        recording = Recording.open(path)
        assert recording.meta.state == "complete"
        assert recording.meta.name == "supervised"
        assert recording.meta.game.adapter_sha256 == adapter_sha256(
            "synthetic-game", root / "games"
        )
        frame = recording.frame(recording.frame_numbers.index(frame_number))
        assert frame.capture_ns <= down.at_ns < recording.frames[-1].capture_ns
        assert recording.frame(len(recording.frames) - 1).pixels != frame.pixels
        assert [block.samples for block in recording.blocks(0, len(recording.audio))] == [SAMPLES]
        assert not r.device.anomalies

    asyncio.run(run())


async def hold_key(r: Rig) -> int:
    await r.start()
    r.world.presenting = True
    core = r.run.core
    ring = core.frames
    grant = r.run.kernel.active_control
    assert ring is not None
    assert grant is not None

    def leased_frame() -> int | None:
        health = core.last_health
        return (
            ring.latest_frame_number() or None
            if health is not None and health.lease is models.LeaseStatus.VALID
            else None
        )

    base = await until(leased_frame)
    await grant.submit(
        (
            models.ControlStep(
                offset_ns=0, held=[models.HeldInput(root=models.HeldKey(scan_code=KEY))], actions=[]
            ),
        ),
        base_frame=base,
        valid_for=5,
    )
    down = await until(
        lambda: next(
            (
                event
                for event in r.device.events()
                if isinstance(event.event, models.FakeKeyDown) and event.event.scan_code == KEY
            ),
            None,
        ),
        timeout=2,
    )
    assert down.held_after == {("key", KEY)}
    return down.seq


@pytest.mark.parametrize("media", ["frame", "audio"])
def test_recording_interruption_revokes_held_input_and_stops_before_flush(
    core_exe: Path, root: Path, monkeypatch: pytest.MonkeyPatch, media: str
) -> None:
    fault = threading.Event()
    check = consumer_module.check_recording_quota

    def check_with_fault(keeper: QuotaKeeper) -> QuotaStatus:
        if fault.is_set():
            keeper.quota = 1
        return check(keeper)

    monkeypatch.setattr(consumer_module, "check_recording_quota", check_with_fault)
    monkeypatch.setattr(consumer_module, "QUOTA_INTERVAL", 0.05)

    async def run() -> None:
        started: asyncio.Future[Rig] = asyncio.get_running_loop().create_future()
        gate = RecordingIOGate()

        async def supervise_until_cancelled() -> None:
            async with rig(core_exe, root, policy=IdlePolicy(), record=True, hold_key=True) as r:
                started.set_result(r)
                await asyncio.Event().wait()

        owner = asyncio.create_task(supervise_until_cancelled())
        try:
            r = await asyncio.wait_for(started, 6)
            down_seq = await hold_key(r)
            grant = r.run.kernel.active_control
            assert grant is not None
            assert grant.is_open
            on_end = r.run.signals.on_end
            grant_open_at_end: list[bool] = []

            def record_end(reason: str) -> None:
                if reason.startswith("recording-"):
                    grant_open_at_end.append(grant.is_open)
                on_end(reason)

            monkeypatch.setattr(r.run.signals, "on_end", record_end)
            writer = r.run._writer  # pyright: ignore[reportPrivateUsage]
            assert writer is not None
            file = "_frames_index" if media == "frame" else "_audio_samples"
            stream = cast("IO[str] | IO[bytes] | None", getattr(writer, file))
            assert stream is not None
            gate.block(stream, monkeypatch)
            if media == "audio":
                assert r.audio is not None
                r.audio.play(SAMPLES)
            await asyncio.wait_for(gate.entered.wait(), 2)
            assert r.device.held == {("key", KEY)}

            if media == "frame":
                fault.set()
            else:
                # 继续复制输入，使卡在音频 flush 的消费者实际达到队列上限。
                assert r.audio is not None
                for _ in range(64):
                    if r.run.signals.ended.is_set():
                        break
                    r.audio.play(SAMPLES)
                    await asyncio.sleep(0.025)
            await asyncio.wait_for(r.run.wait_ended(), 3)
            assert grant_open_at_end == [False]
            await until(
                lambda: True if r.run.core.frames is None and r.run.core.audio is None else None,
                timeout=3,
            )
            reason = models.ReleaseReason.GENERATION_REVOKED
            await until(lambda: r.released(reason), timeout=2)
            await until(
                lambda: next(
                    (
                        event
                        for event in r.events
                        if event.kind is EventKind.INPUT_RELEASED
                        and event.fields["reason"] == reason.value
                    ),
                    None,
                ),
                timeout=2,
            )
            release = next(
                event
                for event in r.device.events()
                if isinstance(event.event, models.FakeReleaseAll) and event.event.reason is reason
            )
            [up] = [
                event
                for event in r.device.events()
                if down_seq < event.seq < release.seq and isinstance(event.event, models.FakeKeyUp)
            ]
            assert up.event == models.FakeKeyUp(scan_code=KEY)
            assert up.seq + 1 == release.seq
            assert not r.device.held
            assert not r.device.anomalies
            assert not r.run.core.running
            assert r.run.kernel.active_control is None
            assert not owner.done()
            assert read_meta(writer.path).state == "recording"
            print(
                f"{media} 假键抬起 {up.seq}，代次撤权确认 {release.seq}。"
                "实时核退出时 flush 仍被阻塞。"
            )

            for _ in range(2):
                owner.cancel()
                await asyncio.wait((owner,), timeout=0.05)
                assert not owner.done()
        finally:
            gate.release.set()
            if not owner.cancelling():
                owner.cancel()
            (outcome,) = await asyncio.gather(owner, return_exceptions=True)
        assert isinstance(outcome, asyncio.CancelledError)
        meta = read_meta(writer.path)
        assert (meta.state, meta.stop_reason) == (
            ("complete", "quota") if media == "frame" else ("failed", "error")
        )
        assert Recording.open(writer.path).frames

    asyncio.run(run())


def test_live_writer_error_ends_supervision_without_the_caller_exiting(
    core_exe: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_flush() -> None:
        raise OSError("音频写入故障")

    async def run() -> None:
        async with rig(core_exe, root, policy=IdlePolicy(), record=True, hold_key=True) as r:
            await hold_key(r)
            writer = r.run._writer  # pyright: ignore[reportPrivateUsage]
            assert writer is not None
            stream = writer._audio_samples  # pyright: ignore[reportPrivateUsage]
            assert stream is not None
            assert r.audio is not None
            monkeypatch.setattr(stream, "flush", fail_flush)
            r.audio.play(SAMPLES)
            await asyncio.wait_for(r.run.wait_ended(), 3)
            await until(lambda: r.released(models.ReleaseReason.GENERATION_REVOKED), timeout=2)
            await until(lambda: True if not r.run.core.running else None, timeout=2)
            assert not r.device.held
            assert r.run.kernel.active_control is None
        meta = read_meta(writer.path)
        assert (meta.state, meta.stop_reason) == ("failed", "error")

    asyncio.run(run())


def test_cancel_releases_input_and_stops_core_while_quota_check_is_blocked(
    core_exe: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        gate = RecordingIOGate()
        block = threading.Event()
        check = consumer_module.check_recording_quota

        def check_with_gate(keeper: QuotaKeeper) -> QuotaStatus:
            if block.is_set():
                gate.wait()
            return check(keeper)

        monkeypatch.setattr(consumer_module, "check_recording_quota", check_with_gate)
        monkeypatch.setattr(consumer_module, "QUOTA_INTERVAL", 0.05)
        started: asyncio.Future[Rig] = asyncio.get_running_loop().create_future()

        async def supervise_until_cancelled() -> None:
            async with rig(core_exe, root, policy=IdlePolicy(), record=True, hold_key=True) as r:
                started.set_result(r)
                await asyncio.Event().wait()

        owner = asyncio.create_task(supervise_until_cancelled())
        try:
            r = await asyncio.wait_for(started, 6)
            await hold_key(r)
            block.set()
            await asyncio.wait_for(gate.entered.wait(), 2)
            owner.cancel()
            await until(lambda: r.released(models.ReleaseReason.GENERATION_REVOKED), timeout=2)
            await until(lambda: True if not r.run.core.running else None, timeout=3)
            assert not r.device.held
            assert r.run.kernel.active_control is None
            assert not owner.done()
            print("配额扫描仍被阻塞时已撤权、抬键并停核。")
        finally:
            gate.release.set()
            if not owner.cancelling():
                owner.cancel()
            (outcome,) = await asyncio.gather(owner, return_exceptions=True)
        assert isinstance(outcome, asyncio.CancelledError)
        meta = r.run.recording_meta
        assert meta is not None
        assert (meta.state, meta.stop_reason) == ("complete", "interrupt")

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["release", "stop"])
def test_failed_control_shutdown_marks_the_recording_failed(
    core_exe: Path, root: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    async def run() -> None:
        async with rig(core_exe, root, policy=IdlePolicy(), record=True, hold_key=True) as r:
            await hold_key(r)
            core = r.run.core
            if failure == "release":

                async def fail_release(_grant: ControlGrant) -> None:
                    raise RuntimeError("输入释放确认故障")

                monkeypatch.setattr(core, "wait_released", fail_release)
            else:
                stop = core.stop

                async def report_forced_stop() -> StopResult:
                    assert (await stop()).returncode == 0
                    return StopResult(StopStage.KILLED, 1)

                monkeypatch.setattr(core, "stop", report_forced_stop)
        assert not core.running
        assert not r.device.held
        meta = r.run.recording_meta
        assert meta is not None
        assert (meta.state, meta.stop_reason) == ("failed", "error")

    asyncio.run(run())


@pytest.mark.parametrize("deny_metadata", [False, True])
def test_partial_supervised_start_stops_core_before_recording_metadata(
    core_exe: Path, root: Path, monkeypatch: pytest.MonkeyPatch, deny_metadata: bool
) -> None:
    async def run() -> None:
        gate = RecordingIOGate()
        attached: asyncio.Future[CoreLink] = asyncio.get_running_loop().create_future()
        replace = Path.replace
        closing_meta: list[Path] = []

        def replace_when_stopping(source: Path, target: str | Path) -> Path:
            path = Path(target)
            if path.name == META_FILE and attached.done():
                closing_meta.append(path)
                gate.wait()
                if deny_metadata:
                    raise PermissionError("结束元数据替换失败")
            return replace(source, target)

        monkeypatch.setattr(Path, "replace", replace_when_stopping)

        async def fail_after_core(core: CoreLink) -> None:
            attached.set_result(core)
            raise RuntimeError("假设备挂接后的启动失败")

        async def open_session() -> None:
            async with rig(core_exe, root, record=True, after_core_start=fail_after_core):
                pytest.fail("启动故障后仍交付了会话")

        owner = asyncio.create_task(open_session())
        try:
            core = await asyncio.wait_for(attached, 6)
            await asyncio.wait_for(gate.entered.wait(), 4)
            assert not core.running
            assert core.frames is None
            assert core.audio is None
            assert not owner.done()
        finally:
            gate.release.set()
            (outcome,) = await asyncio.gather(owner, return_exceptions=True)
        assert isinstance(outcome, RuntimeError)
        assert str(outcome) == "假设备挂接后的启动失败"
        [path] = closing_meta
        meta = read_meta(path.parent)
        assert (meta.state, meta.stop_reason, meta.frames.count) == (
            ("recording", None, 0) if deny_metadata else ("failed", "error", 0)
        )

    asyncio.run(run())


def test_no_recording_quota_fails_before_core_construction(core_exe: Path, root: Path) -> None:
    games = root / "games"
    adapter = games / "synthetic-game" / "adapter.toml"
    adapter.parent.mkdir(parents=True)
    content = (GAMES_DIR / "synthetic-game" / "adapter.toml").read_text(encoding="utf-8")
    adapter.write_text(
        content.replace("recording_quota_gb = 10", "recording_quota_gb = 0"), encoding="utf-8"
    )

    class UnusedCore(CoreLink):
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pytest.fail("配额不允许仍创建实时核")

    async def run() -> None:
        with pytest.raises(QuotaRefusedError):
            async with supervise(
                "synthetic-game",
                IdlePolicy(),
                CoreLinkOptions(core_exe),
                games_dir=games,
                data_dir=root / "data",
                store=MemorySecretStore(),
                core_factory=UnusedCore,
                record=True,
            ):
                pytest.fail("配额不允许仍交付了会话")
        assert not (root / "data" / "recordings").exists()

    asyncio.run(run())
