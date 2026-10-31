"""把插件的输出逐条写成文件，供实时运行与回放逐条比对。

普通插件 `results` 订阅感知服务，每条结果写一行 JSON；录制时写进录制目录的 `live/`，回放时写进
`replay/`，剧情记录的 SQLite 也在同一个目录里。`voice-feed.jsonl` 同时是回放的输入：回放时
`voice-activity@1` 按 `live/` 里的这份在同样的位置喂入中断。比对见 `compare_results`。设计见
`docs/modules/offline-tools.md` 的“M0 回放”。
"""

import asyncio
import dataclasses
import enum
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Final, Self, cast

import numpy as np
from pydantic import BaseModel, ConfigDict, model_validator

from game_runtime.kernel import Manifest, Plugin, PluginContext, Tier
from game_runtime.narrative import (
    NARRATIVE,
    Discontinuity,
    NarrativeRecord,
    NarrativeService,
    Scope,
)
from game_runtime.recording.layout import write_atomic
from game_runtime.scene import SCENE, SceneState, SceneUpdate
from game_runtime.voice_activity import (
    VOICE_ACTIVITY,
    FeedEnd,
    FeedInterrupt,
    FeedMark,
    FeedSkip,
    UnknownReason,
    VoiceChange,
)

SCENE_FILE: Final = "scene.jsonl"
"""`scene@1` 的逐帧状态。"""
VOICE_FILE: Final = "voice.jsonl"
"""`voice-activity@1` 的每一条变化。"""
VOICE_FEED_FILE: Final = "voice-feed.jsonl"
"""`voice-activity@1` 的喂入标记，末尾是停止记录时的 `FeedEnd`。"""
NARRATIVE_FILE: Final = "narrative.jsonl"
"""`narrative@1` 每句的每次变化。"""
NARRATIVE_FINAL_FILE: Final = "narrative-final.jsonl"
"""处理完全部更新后 `narrative@1` 的各句、剧情范围与中断。"""
STREAM_FILES: Final = (SCENE_FILE, VOICE_FILE, VOICE_FEED_FILE)
RESULT_FILES: Final = (*STREAM_FILES, NARRATIVE_FILE, NARRATIVE_FINAL_FILE)
"""逐条比对的文件。"""
PACE_BACKLOG: Final = 32
"""回放时剧情记录积压超过这么多个更新就等它处理完再给下一帧。"""


def plain(value: object) -> Any:
    """把结果对象转成 JSON 能写的值：数据类成字典，元组与命名元组成列表，枚举取值，numpy 的标量
    转成 Python 的数。浮点数由 `json` 按 `repr` 写出，读回逐位相同。"""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: plain(getattr(value, field.name)) for field in dataclasses.fields(value)
        }
    if isinstance(value, enum.Enum):
        return plain(cast(object, value.value))
    if isinstance(value, np.generic):
        return plain(cast(object, value.item()))
    if isinstance(value, Mapping):
        items = cast(Mapping[object, object], value)
        return {str(key): plain(item) for key, item in items.items()}
    if isinstance(value, list | tuple):
        sequence = cast(Sequence[object], value)
        return [plain(item) for item in sequence]
    return value


def result_line(value: object) -> str:
    """一条结果写成的一行（不含换行）。"""
    return json.dumps(plain(value), ensure_ascii=False, separators=(",", ":"))


def scene_line(state: SceneState) -> str:
    return result_line(state)


def voice_line(change: VoiceChange) -> str:
    return result_line(change)


def mark_line(mark: FeedMark) -> str:
    """一个喂入标记写成的一行：`type` 为 `skip`、`interrupt` 或 `end`。"""
    kind = {FeedSkip: "skip", FeedInterrupt: "interrupt", FeedEnd: "end"}[type(mark)]
    return result_line({"type": kind, **plain(mark)})


def parse_mark(line: str) -> FeedMark:
    """`mark_line` 的逆；不合格式即 ValueError。"""
    try:
        record = json.loads(line)
        kind = record.pop("type")
        if kind == "skip":
            return FeedSkip(after=int(record["after"]), next_block=int(record["next_block"]))
        if kind == "interrupt":
            return FeedInterrupt(
                after=int(record["after"]),
                reason=UnknownReason(record["reason"]),
                at_ns=int(record["at_ns"]),
            )
        if kind == "end":
            return FeedEnd(after=int(record["after"]))
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise ValueError(f"喂入标记不合格式：{line}（{error}）") from None
    raise ValueError(f"喂入标记的 type 不认识：{line}")


def read_marks(path: Path) -> list[FeedMark]:
    """读 `voice-feed.jsonl`；没有文件时为空。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [parse_mark(line) for line in text.splitlines() if line.strip()]


class ResultsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    directory: Path
    """输出目录，绝对路径；不在就建。"""

    @model_validator(mode="after")
    def _absolute(self) -> Self:
        if not self.directory.is_absolute():
            raise ValueError("directory 须为绝对路径")
        return self


class ResultsHub:
    """宿主与插件之间：`results` 插件激活时交来 `narrative@1` 与输出目录，宿主据此让回放等剧情记录
    跟上，停插件内核之前等它处理完积压的更新，并写下最后的剧情记录。不录制的只读运行没有 `results`，
    由 `observer` 以 `track` 交来剧情记录，只等、不写。只在事件循环的线程里使用。"""

    def __init__(self, *, pace_backlog: int = PACE_BACKLOG) -> None:
        self.narrative: NarrativeService | None = None
        self.directory: Path | None = None
        self._pace_backlog = pace_backlog

    async def pace(self) -> None:
        """剧情记录积压超过 `pace_backlog` 个更新时等它处理完：回放给帧比实时快，不让它积压超限
        而丢弃（那会与实时不同）。"""
        narrative = self.narrative
        if narrative is not None and narrative.backlog > self._pace_backlog:
            await narrative.wait_idle()

    def track(self, narrative: NarrativeService) -> Callable[[], None]:
        """还没有剧情记录时记下 `narrative`，让 `drain` 等它处理完（没有输出目录，不写文件）；
        已有时不变。返回撤销的函数，登记为撤销函数。"""
        if self.narrative is not None:
            return lambda: None

        def release() -> None:
            if self.narrative is narrative:
                self.narrative = None

        self.narrative = narrative
        return release

    async def drain(self, timeout: float) -> bool:
        """等剧情记录处理完积压的更新（至多 `timeout` 秒）；有输出目录时再把它最后的记录、范围与
        中断写进 `narrative-final.jsonl`。没有剧情记录时直接返回真；超时返回假，不写。"""
        narrative, directory = self.narrative, self.directory
        if narrative is None:
            return True
        try:
            await asyncio.wait_for(narrative.wait_idle(), timeout)
        except TimeoutError:
            return False
        if directory is None:
            return True
        lines = [narrative_line(record) for record in narrative.records()]
        lines += [_tagged("scope", scope) for scope in narrative.scopes]
        lines += [_tagged("discontinuity", gap) for gap in narrative.discontinuities]
        write_atomic(directory / NARRATIVE_FINAL_FILE, "".join(f"{x}\n" for x in lines).encode())
        return True


def _without_session(value: object) -> dict[str, Any]:
    """剧情记录的对象去掉运行号：每次运行都新开一个，比对时不算。"""
    record = cast(dict[str, Any], plain(value))
    record.pop("session", None)
    return record


def narrative_line(record: NarrativeRecord) -> str:
    return json.dumps(_without_session(record), ensure_ascii=False, separators=(",", ":"))


def _tagged(kind: str, value: Scope | Discontinuity) -> str:
    record = {"type": kind, **_without_session(value)}
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


class ResultsPlugin(Plugin[ResultsConfig]):
    """订阅感知服务，每条结果写一行：`scene.jsonl` 每帧一个 `SceneState`，`voice.jsonl` 每条
    `VoiceChange`，`voice-feed.jsonl` 每个喂入标记（停用时末尾写一个 `FeedEnd`），
    `narrative.jsonl` 每句的每次变化（`NarrativeRecord`，不含运行号）。宿主用 `results_plugin`
    得到交服务给 `ResultsHub` 的子类。"""

    manifest = Manifest(
        id="results",
        version="1",
        tier=Tier.ORDINARY,
        inject=(SCENE.ref, VOICE_ACTIVITY.ref, f"{NARRATIVE.ref}?"),
        config=ResultsConfig,
    )
    hub: ClassVar[ResultsHub | None] = None

    async def activate(self, ctx: PluginContext[ResultsConfig]) -> None:
        scene = ctx.service(SCENE)
        voice = ctx.service(VOICE_ACTIVITY)
        # 剧情记录排在它前面、先激活；没有对白框时不运行，这里就没有。
        narrative = ctx.optional(NARRATIVE)
        directory = ctx.config.directory
        directory.mkdir(parents=True, exist_ok=True)
        names = [*STREAM_FILES, *([NARRATIVE_FILE] if narrative is not None else [])]
        files = {
            name: (directory / name).open("w", encoding="utf-8", newline="\n") for name in names
        }
        for file in files.values():
            ctx.register(file.close)

        def write(name: str, line: str) -> None:
            files[name].write(line + "\n")

        def on_scene(update: SceneUpdate) -> None:
            write(SCENE_FILE, scene_line(update.state))

        def on_voice(change: VoiceChange) -> None:
            write(VOICE_FILE, voice_line(change))

        def on_mark(mark: FeedMark) -> None:
            write(VOICE_FEED_FILE, mark_line(mark))

        def on_record(record: NarrativeRecord) -> None:
            write(NARRATIVE_FILE, narrative_line(record))

        def end() -> None:
            write(VOICE_FEED_FILE, mark_line(FeedEnd(voice.fed_block)))

        ctx.register(scene.subscribe(on_scene))
        ctx.register(voice.subscribe(on_voice))
        ctx.register(voice.subscribe_feed(on_mark))
        if narrative is not None:
            ctx.register(narrative.subscribe(on_record))
        hub = type(self).hub
        if hub is not None:
            hub.narrative, hub.directory = narrative, directory

            def release() -> None:
                hub.narrative, hub.directory = None, None

            ctx.register(release)
        # 后登记先撤销：先写停止记录时喂到的位置，再退订、关文件。
        ctx.register(end)


def results_plugin(hub: ResultsHub) -> type[ResultsPlugin]:
    """交服务给 `hub` 的 `results` 插件类，放进插件目录。"""
    return type("BoundResultsPlugin", (ResultsPlugin,), {"hub": hub})


@dataclass(frozen=True, slots=True)
class Difference:
    line: int
    """第几行，从 1 起。"""
    live: Any
    replay: Any


@dataclass(frozen=True, slots=True)
class Comparison:
    """两份结果逐行比对的结论。"""

    same: int
    """从头起相同的行数。"""
    live_lines: int
    replay_lines: int
    first_difference: Difference | None

    @property
    def identical(self) -> bool:
        return self.first_difference is None and self.live_lines == self.replay_lines


def _read_lines(path: Path) -> list[Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in text.splitlines() if line]


def compare_dirs(live: Path, replay: Path) -> dict[str, Comparison]:
    """逐个比对两个目录里的结果文件；实时一侧没有的文件不比。"""
    return {
        name: compare_results(live / name, replay / name)
        for name in RESULT_FILES
        if (live / name).is_file()
    }


def compare_results(live: Path, replay: Path) -> Comparison:
    """逐行比对两份结果（解析成 JSON 后相等）。"""
    left, right = _read_lines(live), _read_lines(replay)
    same = 0
    difference: Difference | None = None
    for index, (one, other) in enumerate(zip(left, right, strict=False)):
        if one != other:
            difference = Difference(index + 1, one, other)
            break
        same += 1
    if difference is None and len(left) != len(right):
        index = same
        difference = Difference(
            index + 1,
            left[index] if index < len(left) else None,
            right[index] if index < len(right) else None,
        )
    return Comparison(same, len(left), len(right), difference)
