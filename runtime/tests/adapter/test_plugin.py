"""`game-adapter@1` 在插件内核中：激活、提供服务、依赖方的 WAITING 原因与失败。

用本仓库的合成游戏适配包与测试里写的合成声明；内核用不提交动作的假实时核。
"""

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from core_protocol import models
from game_runtime.adapter import (
    ADAPTER_FILE,
    GAME_ADAPTER,
    GAMES_DIR,
    Adapter,
    GameAdapterPlugin,
    load_adapter,
)
from game_runtime.kernel import (
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


received: list[Adapter] = []


class Consumer(Plugin[NoConfig]):
    """依赖 `game-adapter@1` 的普通插件，记下拿到的适配包。"""

    manifest = Manifest(
        id="adapter-consumer", version="1", tier=Tier.ORDINARY, inject=(GAME_ADAPTER.ref,)
    )

    async def activate(self, ctx: PluginContext[NoConfig]) -> None:
        received.append(ctx.service(GAME_ADAPTER))


@pytest.fixture(autouse=True)
def _reset_received() -> None:
    received.clear()


def entry(entry_id: str, plugin: str, config: dict[str, Any] | None = None) -> ResolvedEntry:
    return ResolvedEntry(
        id=entry_id, plugin=plugin, config=config or {}, disabled=False, grant=frozenset()
    )


def kernel() -> Kernel:
    return Kernel(
        catalog=catalog(GameAdapterPlugin, Consumer),
        core=NoCore(),
        options=KernelOptions(retry_delays=()),
    )


def test_manifest_is_a_critical_plane_without_permissions() -> None:
    manifest = GameAdapterPlugin.manifest

    assert manifest.tier is Tier.CRITICAL
    assert manifest.provides == ("game-adapter@1",)
    assert manifest.permissions == ()
    assert not manifest.is_control_producer


def test_the_selected_package_is_provided_to_dependents() -> None:
    async def scenario() -> None:
        k = kernel()
        # 依赖方排在前面：先停在 WAITING，适配包激活后随即激活。
        k.add(
            [
                entry("consumer", "adapter-consumer"),
                entry("game", "game-adapter", {"game": "synthetic-game"}),
            ]
        )
        await k.settled()

        assert k.status("game").state is State.ACTIVE
        assert k.status("consumer").state is State.ACTIVE
        assert received == [load_adapter(GAMES_DIR / "synthetic-game")]
        await k.shutdown()

    asyncio.run(scenario())


def test_dependents_wait_for_the_service_while_the_package_is_invalid(tmp_path: Path) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ADAPTER_FILE).write_text('id = "broken"\n', encoding="utf-8")

    async def scenario() -> None:
        k = kernel()
        k.add(
            [
                entry("game", "game-adapter", {"game": "broken", "games_dir": str(tmp_path)}),
                entry("consumer", "adapter-consumer"),
            ]
        )
        await k.settled()

        game = k.status("game")
        assert game.state is State.FAILED
        assert game.reasons[0].code is ReasonCode.RETRIES_EXHAUSTED
        assert "window：Field required" in game.reasons[0].message
        consumer = k.status("consumer")
        assert consumer.state is State.WAITING
        assert [(r.code, r.subject) for r in consumer.reasons] == [
            (ReasonCode.SERVICE_MISSING, "game-adapter@1")
        ]
        assert received == []
        await k.shutdown()

    asyncio.run(scenario())


def test_an_unknown_game_fails_activation(tmp_path: Path) -> None:
    async def scenario() -> None:
        k = kernel()
        k.add([entry("game", "game-adapter", {"game": "absent", "games_dir": str(tmp_path)})])
        await k.settled()

        status = k.status("game")
        assert status.state is State.FAILED
        assert f"没有 {ADAPTER_FILE}" in status.reasons[0].message
        await k.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "config",
    [
        pytest.param({}, id="no game"),
        pytest.param({"game": "Witch Trial"}, id="bad id"),
        pytest.param({"game": "synthetic-game", "games_dir": "games"}, id="relative games_dir"),
        pytest.param({"game": "synthetic-game", "path": "/x"}, id="unknown field"),
    ],
)
def test_invalid_config_fails_without_retrying(config: dict[str, Any]) -> None:
    async def scenario() -> None:
        k = kernel()
        k.add([entry("game", "game-adapter", config)])
        await k.settled()

        status = k.status("game")
        assert status.state is State.FAILED
        assert status.reasons[0].code is ReasonCode.CONFIG_INVALID
        await k.shutdown()

    asyncio.run(scenario())


def test_uncalibrated_items_are_logged_on_activation(caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> None:
        k = kernel()
        k.add([entry("game", "game-adapter", {"game": "witch-trial"})])
        await k.settled()
        assert k.status("game").state is State.ACTIVE
        await k.shutdown()

    with caplog.at_level(logging.WARNING, logger="game_runtime.adapter.plugin"):
        asyncio.run(scenario())

    messages = [record.getMessage() for record in caplog.records]
    assert "适配包 witch-trial 的ROI speaker（名字栏）尚未标定" in messages
    assert "适配包 witch-trial 的阅读节奏尚未标定" in messages


def test_stopping_the_package_stops_its_dependents_first() -> None:
    async def scenario() -> None:
        k = kernel()
        k.add(
            [
                entry("game", "game-adapter", {"game": "synthetic-game"}),
                entry("consumer", "adapter-consumer"),
            ]
        )
        await k.settled()

        await k.disable("game")

        assert k.status("game").state is State.DISPOSED
        consumer = k.status("consumer")
        assert consumer.state is State.WAITING
        assert consumer.reasons[0].code is ReasonCode.SERVICE_MISSING
        await k.shutdown()

    asyncio.run(scenario())
