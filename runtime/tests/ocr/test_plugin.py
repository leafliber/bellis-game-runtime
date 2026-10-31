"""`ocr@1` 在插件内核中：依赖 `game-adapter@1`、提供服务、模型制品缺失、语言不符、配置不合法与停用。

识别引擎换成按脚本回答的假识别器（替换 `load_engine`），不需要模型文件；模型缺失的一项用空的
本地数据目录。内核用不提交动作的假实时核。
"""

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from core_protocol import models
from game_runtime.adapter import ADAPTER_FILE, GAMES_DIR, GameAdapterPlugin
from game_runtime.frame_ring import RingFrame
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
from game_runtime.local_data import DATA_DIR_ENV
from game_runtime.ocr import OCR, OcrClosedError, OcrPlugin, OcrService, Recognizer
from game_runtime.ocr import plugin as ocr_plugin
from game_runtime.ocr.engine import EngineOptions
from game_runtime.ocr.fake import ScriptedRecognizer, scripted_line
from game_runtime.ocr.models import ModelSet


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


received: list[OcrService] = []


class Consumer(Plugin[NoConfig]):
    """依赖 `ocr@1` 的普通插件，记下拿到的服务。"""

    manifest = Manifest(id="ocr-consumer", version="1", tier=Tier.ORDINARY, inject=(OCR.ref,))

    async def activate(self, ctx: PluginContext[NoConfig]) -> None:
        received.append(ctx.service(OCR))


@pytest.fixture(autouse=True)
def _reset_received() -> None:
    received.clear()


@pytest.fixture
def loaded(monkeypatch: pytest.MonkeyPatch) -> list[tuple[ModelSet, EngineOptions]]:
    """把引擎换成假识别器，记下加载时的模型组与选项。"""
    calls: list[tuple[ModelSet, EngineOptions]] = []

    def fake_load(model_set: ModelSet, data_dir: Path, options: EngineOptions) -> Recognizer:
        calls.append((model_set, options))
        return ScriptedRecognizer(answer=lambda image: [scripted_line("开始")])

    monkeypatch.setattr(ocr_plugin, "load_engine", fake_load)
    return calls


def entry(entry_id: str, plugin: str, config: dict[str, Any] | None = None) -> ResolvedEntry:
    return ResolvedEntry(
        id=entry_id, plugin=plugin, config=config or {}, disabled=False, grant=frozenset()
    )


def kernel() -> Kernel:
    return Kernel(
        catalog=catalog(GameAdapterPlugin, OcrPlugin, Consumer),
        core=NoCore(),
        options=KernelOptions(retry_delays=()),
    )


def game(games_dir: Path | None = None) -> ResolvedEntry:
    config: dict[str, Any] = {"game": "synthetic-game"}
    if games_dir is not None:
        config["games_dir"] = str(games_dir)
    return entry("game", "game-adapter", config)


def test_manifest_is_an_ordinary_plugin_without_permissions() -> None:
    manifest = OcrPlugin.manifest

    assert manifest.tier is Tier.ORDINARY
    assert manifest.provides == ("ocr@1",)
    assert manifest.inject == ("game-adapter@1",)
    assert manifest.permissions == ()


def test_the_service_reaches_dependents_and_recognizes(
    loaded: list[tuple[ModelSet, EngineOptions]],
) -> None:
    async def scenario() -> None:
        k = kernel()
        k.add(
            [
                entry("consumer", "ocr-consumer"),
                entry("ocr", "ocr", {"threads": 2, "min_confidence": 0.7}),
                game(),
            ]
        )
        await k.settled()

        assert k.status("ocr").state is State.ACTIVE
        assert k.status("consumer").state is State.ACTIVE
        (service,) = received
        assert service.adapter.id == "synthetic-game"
        assert service.model == "ppocrv5-mobile"
        frame = RingFrame(1, 0, 1, 1280, 720, 1280 * 4, bytes(1280 * 720 * 4))
        result = await service.recognize(frame, "menu-start")
        assert result.text == "开始"

        await k.disable("ocr")
        assert k.status("ocr").state is State.DISPOSED
        assert k.status("consumer").state is State.WAITING
        with pytest.raises(OcrClosedError):
            await service.recognize(frame, "menu-start")
        await k.shutdown()

    asyncio.run(scenario())
    ((model_set, options),) = loaded
    assert model_set.name == "ppocrv5-mobile"
    assert (options.threads, options.min_confidence) == (2, 0.7)


def test_ocr_waits_for_the_game_adapter(loaded: list[tuple[ModelSet, EngineOptions]]) -> None:
    async def scenario() -> None:
        k = kernel()
        k.add([entry("ocr", "ocr")])
        await k.settled()

        status = k.status("ocr")
        assert status.state is State.WAITING
        assert [(r.code, r.subject) for r in status.reasons] == [
            (ReasonCode.SERVICE_MISSING, "game-adapter@1")
        ]
        await k.shutdown()

    asyncio.run(scenario())
    assert loaded == []


def test_missing_models_fail_activation_and_name_the_fetch_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))

    async def scenario() -> None:
        k = kernel()
        k.add([game(), entry("ocr", "ocr"), entry("consumer", "ocr-consumer")])
        await k.settled()

        status = k.status("ocr")
        assert status.state is State.FAILED
        assert status.reasons[0].code is ReasonCode.RETRIES_EXHAUSTED
        assert "ppocrv5-mobile-det" in status.reasons[0].message
        assert "先运行 uv run fetch-models" in status.reasons[0].message
        assert k.status("consumer").state is State.WAITING
        await k.shutdown()

    asyncio.run(scenario())


def test_the_configured_data_dir_is_where_models_are_looked_for(tmp_path: Path) -> None:
    async def scenario() -> None:
        k = kernel()
        k.add([game(), entry("ocr", "ocr", {"data_dir": str(tmp_path)})])
        await k.settled()

        status = k.status("ocr")
        assert status.state is State.FAILED
        assert str(tmp_path) in status.reasons[0].message
        await k.shutdown()

    asyncio.run(scenario())


def test_a_language_the_models_were_not_checked_on_fails_activation(
    tmp_path: Path, loaded: list[tuple[ModelSet, EngineOptions]]
) -> None:
    source = (GAMES_DIR / "synthetic-game" / ADAPTER_FILE).read_text(encoding="utf-8")
    assert 'tag = "zh-Hans"' in source
    package = tmp_path / "synthetic-game"
    package.mkdir()
    (package / ADAPTER_FILE).write_text(
        source.replace('tag = "zh-Hans"', 'tag = "ja"'), encoding="utf-8"
    )

    async def scenario() -> None:
        k = kernel()
        k.add([game(tmp_path), entry("ocr", "ocr")])
        await k.settled()

        status = k.status("ocr")
        assert status.state is State.FAILED
        assert "语言 ja 不在模型组 ppocrv5-mobile 核对过的语言（zh-Hans）里" in (
            status.reasons[0].message
        )
        await k.shutdown()

    asyncio.run(scenario())
    assert loaded == []


@pytest.mark.parametrize(
    "config",
    [
        pytest.param({"model": "ppocrv4"}, id="unknown model set"),
        pytest.param({"threads": 0}, id="no threads"),
        pytest.param({"min_confidence": 1.5}, id="confidence above 1"),
        pytest.param({"language": "zh-Hans"}, id="unknown field"),
        pytest.param({"data_dir": "models"}, id="relative data_dir"),
    ],
)
def test_invalid_config_fails_without_retrying(
    config: dict[str, Any], loaded: list[tuple[ModelSet, EngineOptions]]
) -> None:
    async def scenario() -> None:
        k = kernel()
        k.add([game(), entry("ocr", "ocr", config)])
        await k.settled()

        status = k.status("ocr")
        assert status.state is State.FAILED
        assert status.reasons[0].code is ReasonCode.CONFIG_INVALID
        await k.shutdown()

    asyncio.run(scenario())
    assert loaded == []
