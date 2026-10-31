"""录制时实时运行与 M0 回放共用的一组插件：同一份目录与条目，得到能逐条比对的输出。

`game-adapter@1`、`ocr@1`、`scene@1`、`voice-activity@1`、`narrative@1`（适配包的对白框标定了才有）
与把结果写成文件的 `results`。设计见 `docs/modules/offline-tools.md` 的“M0 回放”。
"""

import logging
from collections.abc import Sequence
from pathlib import Path

from core_protocol import models
from game_runtime.adapter import Adapter, GameAdapterPlugin, NotCalibratedError
from game_runtime.kernel import (
    AUDIO_READ,
    FRAMES_READ,
    Catalog,
    ControlGrant,
    ResolvedEntry,
    Submission,
    catalog,
)
from game_runtime.narrative import NARRATIVE_WRITE, NarrativePlugin
from game_runtime.ocr import OcrPlugin
from game_runtime.recording.results import ResultsHub, results_plugin
from game_runtime.scene import ScenePlugin
from game_runtime.voice_activity import VoiceActivityPlugin

logger = logging.getLogger(__name__)


def perception_catalog(
    hub: ResultsHub,
    *,
    ocr: type[OcrPlugin] = OcrPlugin,
    voice: type[VoiceActivityPlugin] = VoiceActivityPlugin,
) -> Catalog:
    """录制与回放的插件目录，`results` 把服务交给 `hub`；测试可换成不要模型文件的 OCR 与
    配音检测。"""
    return catalog(GameAdapterPlugin, ocr, ScenePlugin, voice, NarrativePlugin, results_plugin(hub))


def narrative_problem(adapter: Adapter) -> str | None:
    """适配包的对白框不能用（没有声明或尚未标定）时给出原因，此时不运行 `narrative@1`。"""
    try:
        adapter.require_dialogue_boxes()
    except (LookupError, NotCalibratedError) as error:
        return str(error)
    return None


def perception_entries(
    adapter: Adapter,
    *,
    data_dir: Path,
    results_dir: Path,
    games_dir: Path | None = None,
) -> list[ResolvedEntry]:
    """插件内核的条目。模型制品与锚点模板在 `data_dir`；结果与剧情记录的 SQLite 写进
    `results_dir`（剧情记录的本地数据目录就是它，与实时运行的剧情记录分开）。对白框不能用时不运行
    `narrative@1`，记一条告警。"""
    game: dict[str, str] = {"game": adapter.id}
    if games_dir is not None:
        game["games_dir"] = str(games_dir)
    data = {"data_dir": str(data_dir)}
    entries = [
        _entry("game", "game-adapter", game),
        _entry("ocr", "ocr", data),
        _entry("scene", "scene", data, (FRAMES_READ,)),
        _entry("voice", "voice-activity", data, (AUDIO_READ,)),
    ]
    problem = narrative_problem(adapter)
    if problem is None:
        narrative = {"data_dir": str(results_dir)}
        entries.append(_entry("narrative", "narrative", narrative, (NARRATIVE_WRITE,)))
    else:
        logger.warning("不运行 narrative@1：%s", problem)
    entries.append(_entry("results", "results", {"directory": str(results_dir)}))
    return entries


def adapter_entries(game: str, *, games_dir: Path | None = None) -> list[ResolvedEntry]:
    """只有适配包的条目：不运行感知插件时用。"""
    adapter: dict[str, str] = {"game": game}
    if games_dir is not None:
        adapter["games_dir"] = str(games_dir)
    return [_entry("game", "game-adapter", adapter)]


def readers(entries: Sequence[ResolvedEntry], permission: str) -> int:
    """启用的条目里被授予了 `permission`（`frames.read` 或 `audio.read`）的个数，
    即要读到结束的读者数。"""
    if permission not in (FRAMES_READ, AUDIO_READ):
        raise ValueError(f"不是读帧或读音频的权限：{permission}")
    return sum(1 for entry in entries if not entry.disabled and permission in entry.grant)


def _entry(
    entry_id: str, plugin: str, config: dict[str, str], grant: Sequence[str] = ()
) -> ResolvedEntry:
    return ResolvedEntry(
        id=entry_id, plugin=plugin, config=config, disabled=False, grant=frozenset(grant)
    )


class NoControl:
    """没有控制产出者时内核的 `CoreControl`：录制与回放都不发输入，任何提交都拒绝。"""

    async def invalidate_generation(self, grant: ControlGrant) -> None:
        raise RuntimeError("录制与回放没有控制产出者")

    async def wait_released(self, grant: ControlGrant) -> None:
        raise RuntimeError("录制与回放没有控制产出者")

    async def submit(
        self,
        grant: ControlGrant,
        steps: Sequence[models.ControlStep],
        *,
        base_frame: int,
        valid_for: float,
        start_delay: float = 0.0,
    ) -> Submission:
        raise RuntimeError("录制与回放不发输入")
