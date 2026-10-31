"""录制的留存：一款游戏的用量与上限，超出时删最旧的录制。

规则见 `docs/modules/offline-tools.md` 的“留存”，依据是合规确认 C-4。上限取适配包合规声明的
`recording_quota_gb`（单位 10⁹ 字节），为 0 时不允许录制。
"""

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from game_runtime.adapter import Adapter
from game_runtime.recording.layout import DIR_PATTERN, RecordingMeta, read_meta

GB: Final = 1_000_000_000


class QuotaRefusedError(Exception):
    """适配包的合规声明不允许录制。"""


def quota_bytes(adapter: Adapter) -> int:
    """录制的留存上限（字节）；为 0 时抛 `QuotaRefusedError`。"""
    quota = adapter.compliance.recording_quota_gb
    if quota <= 0:
        raise QuotaRefusedError(
            f"适配包 {adapter.id} 的合规声明不允许录制（recording_quota_gb = {quota}）"
        )
    return quota * GB


def directory_size(path: Path) -> int:
    """目录里全部文件的字节数；文件在统计期间消失的跳过。"""
    total = 0
    try:
        entries = list(os.scandir(path))
    except FileNotFoundError:
        return 0
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                total += directory_size(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                total += entry.stat(follow_symlinks=False).st_size
        except FileNotFoundError:
            continue
    return total


@dataclass(frozen=True, slots=True)
class StoredRecording:
    """本地数据目录里的一段录制。"""

    path: Path
    size: int
    meta: RecordingMeta | None
    """读不出元数据时为 None。"""

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def state(self) -> str:
        return "unreadable" if self.meta is None else self.meta.state


def list_recordings(root: Path, *, sizes: bool = True) -> list[StoredRecording]:
    """`recordings/<适配包 id>/` 下名字合格式的录制，按目录名从旧到新。"""
    try:
        names = sorted(
            entry.name
            for entry in os.scandir(root)
            if entry.is_dir(follow_symlinks=False) and DIR_PATTERN.fullmatch(entry.name)
        )
    except FileNotFoundError:
        return []
    found: list[StoredRecording] = []
    for name in names:
        path = root / name
        try:
            meta = read_meta(path)
        except (OSError, ValidationError, ValueError):
            meta = None
        found.append(StoredRecording(path, directory_size(path) if sizes else 0, meta))
    return found


type Report = Callable[[StoredRecording], None]
"""删除一段录制之前调用，交代删了什么。"""


@dataclass(frozen=True, slots=True)
class QuotaStatus:
    used: int
    """这款游戏全部录制的字节数，含正在录的。"""
    quota: int
    current: int
    """正在录的这段的字节数。"""
    over: bool
    """删完其它录制后仍超出上限：正在录的这段须停止。"""

    @property
    def remaining(self) -> int:
        return max(0, self.quota - self.used)


class QuotaKeeper:
    """守住一款游戏的录制上限。其它录制的体积在构造时统计一次，删除后更新；正在录的这段每次核对时
    重新统计。"""

    def __init__(self, root: Path, quota: int, *, current: Path | None = None) -> None:
        self.root = root
        self.quota = quota
        self._current = current
        self._others = [
            stored for stored in list_recordings(root) if current is None or stored.path != current
        ]
        self.deleted: list[StoredRecording] = []
        self._stuck = 0
        """删不干净的录制剩下的字节数，仍计入用量。"""

    def set_current(self, current: Path) -> None:
        """正在录的那段：不删，体积每次重新统计。"""
        self._current = current
        self._others = [stored for stored in self._others if stored.path != current]

    def check(self, report: Report, *, failed: Report | None = None) -> QuotaStatus:
        """超出上限时从旧到新删除其它录制，每删一段之前调用 `report`；删不干净时调用 `failed`，
        剩下的仍计入用量。"""
        current = 0 if self._current is None else directory_size(self._current)
        while self._others and self._used(current) > self.quota:
            oldest = self._others.pop(0)
            report(oldest)
            try:
                shutil.rmtree(oldest.path)
            except OSError:
                left = directory_size(oldest.path)
                self._stuck += left
                if failed is not None:
                    failed(StoredRecording(oldest.path, left, oldest.meta))
            self.deleted.append(oldest)
        used = self._used(current)
        return QuotaStatus(used, self.quota, current, used > self.quota)

    def _used(self, current: int) -> int:
        return sum(stored.size for stored in self._others) + self._stuck + current
