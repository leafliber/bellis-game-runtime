"""`narrative@1` 在插件内核中：清单，缺 OCR 或权限时等待，对白框尚未标定或没有声明时激活失败并写明
原因，配置不合法。

逐句的记录、订阅、积压与停用见 `test_synthetic.py`。内核用不提交动作的假实时核，OCR 用按脚本回答的
识别器，帧源是空的内存帧源。
"""

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from core_protocol import models
from game_runtime.adapter import ADAPTER_FILE, GAME_ADAPTER, GAMES_DIR, GameAdapterPlugin
from game_runtime.frames import MemoryFrameSource
from game_runtime.kernel import (
    FRAMES_READ,
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
from game_runtime.narrative import NARRATIVE_WRITE, NarrativeConfig, NarrativePlugin
from game_runtime.ocr import OCR, OcrService
from game_runtime.ocr.fake import ScriptedRecognizer
from game_runtime.scene import ScenePlugin


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


class FakeOcr(Plugin[NoConfig]):
    manifest = Manifest(
        id="fake-ocr",
        version="1",
        tier=Tier.ORDINARY,
        provides=(OCR.ref,),
        inject=(GAME_ADAPTER.ref,),
    )

    async def activate(self, ctx: PluginContext[NoConfig]) -> None:
        service = OcrService(ctx.service(GAME_ADAPTER), ScriptedRecognizer(), model="scripted")
        ctx.register(service.aclose)
        ctx.provide(OCR, service)


def entry(
    entry_id: str,
    plugin: str,
    config: dict[str, Any] | None = None,
    grant: Sequence[str] = (),
) -> ResolvedEntry:
    return ResolvedEntry(
        id=entry_id, plugin=plugin, config=config or {}, disabled=False, grant=frozenset(grant)
    )


def profile(
    data_dir: Path,
    *,
    game: str = "synthetic-game",
    games_dir: Path = GAMES_DIR,
    ocr: bool = True,
    grant: bool = True,
    narrative: dict[str, Any] | None = None,
) -> list[ResolvedEntry]:
    entries = [
        entry("game", "game-adapter", {"game": game, "games_dir": str(games_dir)}),
        entry("scene", "scene", {"data_dir": str(data_dir)}, [FRAMES_READ]),
        entry(
            "narrative",
            "narrative",
            narrative or {"data_dir": str(data_dir)},
            [NARRATIVE_WRITE] if grant else [],
        ),
    ]
    if ocr:
        entries.append(entry("ocr", "fake-ocr"))
    return entries


async def settle(entries: list[ResolvedEntry]) -> Kernel:
    kernel = Kernel(
        catalog=catalog(GameAdapterPlugin, ScenePlugin, FakeOcr, NarrativePlugin),
        core=NoCore(),
        options=KernelOptions(retry_delays=()),
        frames=MemoryFrameSource(),
    )
    kernel.add(entries)
    await kernel.settled()
    return kernel


def test_manifest() -> None:
    manifest = NarrativePlugin.manifest

    assert manifest.tier is Tier.ORDINARY
    assert manifest.provides == ("narrative@1",)
    assert manifest.inject == ("game-adapter@1", "scene@1", "ocr@1")
    assert manifest.permissions == (NARRATIVE_WRITE,) == ("narrative.write",)
    config = NarrativeConfig()
    assert (config.complete_frames, config.complete_after, config.low_confidence) == (3, 0.5, 0.8)
    assert (config.ocr_interval, config.ocr_interval_ns) == (0.25, 250_000_000)
    assert (config.max_backlog, config.keep_sessions, config.max_records) == (600, 20, 200_000)
    assert config.tracker_params().complete_after_ns == 500_000_000


def test_without_ocr_it_waits_for_the_service(data_dir: Path) -> None:
    async def scenario() -> None:
        kernel = await settle(profile(data_dir, ocr=False))
        status = kernel.status("narrative")
        assert status.state is State.WAITING
        assert [(r.code, r.subject) for r in status.reasons] == [
            (ReasonCode.SERVICE_MISSING, "ocr@1")
        ]
        assert kernel.status("scene").state is State.ACTIVE
        kernel.add([entry("ocr", "fake-ocr")])
        await kernel.settled()
        assert kernel.status("narrative").state is State.ACTIVE
        await kernel.shutdown()

    asyncio.run(scenario())


def test_without_the_permission_it_does_not_activate(data_dir: Path) -> None:
    async def scenario() -> None:
        kernel = await settle(profile(data_dir, grant=False))
        [reason] = kernel.status("narrative").reasons
        assert (reason.code, reason.subject) == (ReasonCode.PERMISSION_NOT_GRANTED, NARRATIVE_WRITE)
        await kernel.shutdown()

    asyncio.run(scenario())


def test_uncalibrated_dialogue_boxes_fail_activation_and_name_every_item(data_dir: Path) -> None:
    """《魔女审判》的对白框在第 9 步标定前：激活失败，原因列出全部尚未标定的项，不猜坐标。"""

    async def scenario() -> None:
        kernel = await settle(profile(data_dir, game="witch-trial"))
        assert kernel.status("scene").state is State.ACTIVE
        status = kernel.status("narrative")
        assert status.state is State.FAILED
        [reason] = status.reasons
        assert reason.code is ReasonCode.RETRIES_EXHAUSTED
        assert (
            "适配包 witch-trial 的对白框的ROI speaker（名字栏）、ROI dialogue（对白框）、"
            "继续标记的签名 dialogue-continue尚未标定"
        ) in reason.message
        await kernel.shutdown()

    asyncio.run(scenario())
    assert not (data_dir / "narrative").exists()  # 没有打开存储


def test_an_adapter_without_dialogue_boxes_fails_activation(root: Path, data_dir: Path) -> None:
    games = root / "games" / "synthetic-game"
    games.mkdir(parents=True)
    source = (GAMES_DIR / "synthetic-game" / ADAPTER_FILE).read_text(encoding="utf-8")
    start = source.index("[[dialogue_boxes]]")
    end = source.index("[pacing]")
    (games / ADAPTER_FILE).write_text(source[:start] + source[end:], encoding="utf-8")

    async def scenario() -> None:
        kernel = await settle(profile(data_dir, games_dir=root / "games"))
        [reason] = kernel.status("narrative").reasons
        assert reason.code is ReasonCode.RETRIES_EXHAUSTED
        assert "适配包 synthetic-game 没有声明对白框" in reason.message
        await kernel.shutdown()

    asyncio.run(scenario())


def test_a_relative_data_dir_is_a_config_error(data_dir: Path) -> None:
    async def scenario() -> None:
        kernel = await settle(profile(data_dir, narrative={"data_dir": "relative"}))
        [reason] = kernel.status("narrative").reasons
        assert reason.code is ReasonCode.CONFIG_INVALID
        kernel_bad = await settle(profile(data_dir, narrative={"max_backlog": 1}))
        [bad] = kernel_bad.status("narrative").reasons
        assert bad.code is ReasonCode.CONFIG_INVALID
        kernel_interval = await settle(profile(data_dir, narrative={"ocr_interval": -0.1}))
        [interval] = kernel_interval.status("narrative").reasons
        assert interval.code is ReasonCode.CONFIG_INVALID
        await kernel.shutdown()
        await kernel_bad.shutdown()
        await kernel_interval.shutdown()

    asyncio.run(scenario())
