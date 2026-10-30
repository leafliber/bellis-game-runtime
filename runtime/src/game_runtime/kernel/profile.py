"""profile 与 patch：格式同 Bellis，见 `docs/modules/plugin-kernel.md` 的“profile”。

游戏与 profile 由操作员选择：只有主进程的启动入口读取并展开 profile，插件拿不到这里的任何接口。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool, StringConstraints

from game_runtime.kernel.manifest import Permission, PluginId

_Name = Annotated[str, StringConstraints(min_length=1, max_length=48)]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProfileEntry(_Closed):
    """profile 中的一个条目：`id` 是位置，`plugin` 是填在这个位置上的实现。"""

    id: PluginId
    plugin: PluginId
    disabled: StrictBool | None = None
    """显式禁用：保留条目，但不加载实例。"""
    config: dict[str, JsonValue] | None = None
    grant: tuple[Permission, ...] | None = None
    """授予该条目的权限；插件申请而未授予的权限使它停在 WAITING。"""


class ProfilePack(_Closed):
    name: _Name
    entries: tuple[ProfileEntry, ...]


class Profile(_Closed):
    """profile：有序的插件包，按顺序展开后再依次应用 patch。"""

    name: PluginId
    packs: tuple[ProfilePack, ...]


class ReplaceOp(_Closed):
    """按 id 替换整个条目。"""

    op: Literal["replace"]
    entry: ProfileEntry


class InsertOp(_Closed):
    """插入新条目：有 `after` 时插在该条目之后，否则追加到末尾。"""

    op: Literal["insert"]
    entry: ProfileEntry
    after: PluginId | None = None


class DisableOp(_Closed):
    op: Literal["disable"]
    id: PluginId


class EnableOp(_Closed):
    op: Literal["enable"]
    id: PluginId


PatchOp = Annotated[ReplaceOp | InsertOp | DisableOp | EnableOp, Field(discriminator="op")]


class ProfilePatch(_Closed):
    name: _Name
    ops: tuple[PatchOp, ...]


@dataclass(frozen=True, slots=True)
class ResolvedEntry:
    """profile 展开并应用 patch 后的条目，交给内核加载。"""

    id: str
    plugin: str
    config: Mapping[str, JsonValue]
    disabled: bool
    grant: frozenset[str]


def resolve_profile(profile: Profile, patches: Sequence[ProfilePatch] = ()) -> list[ResolvedEntry]:
    """按包的顺序展开 profile，再依次应用 patch。"""
    entries: list[ProfileEntry] = []
    for pack in profile.packs:
        for entry in pack.entries:
            if any(existing.id == entry.id for existing in entries):
                raise ValueError(f"profile {profile.name}：条目 {entry.id} 重复")
            entries.append(entry)
    for patch in patches:
        for op in patch.ops:
            _apply(entries, op, patch.name)
    return [
        ResolvedEntry(
            id=entry.id,
            plugin=entry.plugin,
            config=entry.config or {},
            disabled=bool(entry.disabled),
            grant=frozenset(entry.grant or ()),
        )
        for entry in entries
    ]


def _apply(entries: list[ProfileEntry], op: PatchOp, patch: str) -> None:
    target = op.entry.id if isinstance(op, ReplaceOp | InsertOp) else op.id
    index = next((i for i, entry in enumerate(entries) if entry.id == target), None)
    match op:
        case ReplaceOp():
            if index is None:
                raise ValueError(f"patch {patch}：没有可替换的条目 {target}")
            entries[index] = op.entry
        case InsertOp():
            if index is not None:
                raise ValueError(f"patch {patch}：条目 {target} 已存在")
            if op.after is None:
                entries.append(op.entry)
                return
            after = next((i for i, entry in enumerate(entries) if entry.id == op.after), None)
            if after is None:
                raise ValueError(f"patch {patch}：没有条目 {op.after}")
            entries.insert(after + 1, op.entry)
        case DisableOp() | EnableOp():
            if index is None:
                raise ValueError(f"patch {patch}：没有条目 {target}")
            entries[index] = entries[index].model_copy(
                update={"disabled": isinstance(op, DisableOp)}
            )


def read_profile(path: Path) -> Profile:
    """读取 profile 的 JSON 文件。"""
    return Profile.model_validate_json(path.read_bytes())


def read_patch(path: Path) -> ProfilePatch:
    """读取 patch 的 JSON 文件。"""
    return ProfilePatch.model_validate_json(path.read_bytes())
