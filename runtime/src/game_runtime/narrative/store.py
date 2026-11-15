"""剧情记录的本机存储：本地数据目录下按游戏分开的 SQLite 文件，留存有上限。

位置、表与留存见 `docs/modules/narrative-planner.md` 的“存储”。完整日志只在本机，不进版本库，
也不出本机。`NarrativeStore` 的方法都是同步的，由插件放在存储自己的线程里调用；同一个连接只在
一个线程里用。
"""

import contextlib
import json
import sqlite3
from collections.abc import Generator, Iterable, Sequence
from pathlib import Path
from typing import Final

from game_runtime.adapter import ScreenType
from game_runtime.narrative.record import (
    Completeness,
    Cutscene,
    Discontinuity,
    LowConfidenceLine,
    NarrativeRecord,
    Scope,
    ScreenRecord,
)

NARRATIVE_DIR: Final = "narrative"
"""本地数据目录下放剧情记录的子目录。"""
SCHEMA_VERSION: Final = 4
"""存储文件的格式版本（`user_version`）。2：`records.screen` 可为空（对白框没写画面）。3：加
`records.cutscene` 与 `cutscenes` 表（播片段）。4：加入画面观察表。"""


def _records_table(name: str, *, cutscene: bool = True) -> str:
    """`records` 表；`cutscene` 为假时是版本 2 的表（没有 `cutscene` 列，升级时用）。新加的列
    排在最后，与旧文件上 `ALTER TABLE … ADD COLUMN` 加出来的列顺序相同。"""
    added = "    cutscene INTEGER,\n" if cutscene else ""
    return f"""
CREATE TABLE {name} (
    session INTEGER NOT NULL REFERENCES sessions (session) ON DELETE CASCADE,
    occurrence INTEGER NOT NULL,
    scope INTEGER NOT NULL,
    screen TEXT,
    speaker TEXT,
    text TEXT NOT NULL,
    revision INTEGER NOT NULL,
    completeness TEXT NOT NULL,
    first_frame INTEGER NOT NULL,
    first_ns INTEGER NOT NULL,
    last_frame INTEGER NOT NULL,
    last_ns INTEGER NOT NULL,
    revision_frame INTEGER NOT NULL,
    complete_frame INTEGER,
    closed_frame INTEGER,
    low_confidence TEXT NOT NULL,
{added}    PRIMARY KEY (session, occurrence)
);
"""


_CUTSCENES_TABLE: Final = """
CREATE TABLE cutscenes (
    session INTEGER NOT NULL REFERENCES sessions (session) ON DELETE CASCADE,
    cutscene INTEGER NOT NULL,
    scope INTEGER NOT NULL,
    first_frame INTEGER NOT NULL,
    first_ns INTEGER NOT NULL,
    last_frame INTEGER NOT NULL,
    last_ns INTEGER NOT NULL,
    end_ns INTEGER,
    closed_frame INTEGER,
    sentences TEXT NOT NULL,
    PRIMARY KEY (session, cutscene)
);
"""

_OBSERVATIONS_TABLE: Final = """
CREATE TABLE observations (
    session INTEGER NOT NULL REFERENCES sessions (session) ON DELETE CASCADE,
    scope INTEGER NOT NULL,
    frame INTEGER NOT NULL,
    capture_ns INTEGER NOT NULL,
    transform_version INTEGER NOT NULL,
    screen_type TEXT NOT NULL,
    confidence REAL NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (session, frame)
);
"""

_SCHEMA: Final = f"""
CREATE TABLE sessions (
    session INTEGER PRIMARY KEY AUTOINCREMENT,
    adapter_version TEXT NOT NULL
);
CREATE TABLE scopes (
    session INTEGER NOT NULL REFERENCES sessions (session) ON DELETE CASCADE,
    scope INTEGER NOT NULL,
    after_screen TEXT,
    first_frame INTEGER NOT NULL,
    PRIMARY KEY (session, scope)
);
{_records_table("records")}
{_CUTSCENES_TABLE}
{_OBSERVATIONS_TABLE}
CREATE TABLE discontinuities (
    session INTEGER NOT NULL REFERENCES sessions (session) ON DELETE CASCADE,
    first_frame INTEGER NOT NULL,
    last_frame INTEGER NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (session, first_frame)
);
"""

_UPGRADES: Final = {
    1: (
        _records_table("records_v2", cutscene=False),
        "INSERT INTO records_v2 SELECT * FROM records;",
        "DROP TABLE records;",
        "ALTER TABLE records_v2 RENAME TO records;",
    ),
    2: (
        "ALTER TABLE records ADD COLUMN cutscene INTEGER;",
        _CUTSCENES_TABLE,
    ),
    3: (_OBSERVATIONS_TABLE,),
}
"""从各旧版本升到下一版的语句，记录照留：版本 1 的 `records.screen` 不能为空，按新表重建；版本 2
没有播片段，加列与表。"""

_INSERT_RECORD: Final = (
    "INSERT OR REPLACE INTO records (session, occurrence, scope, screen, cutscene, speaker, text, "
    "revision, completeness, first_frame, first_ns, last_frame, last_ns, revision_frame, "
    "complete_frame, closed_frame, low_confidence) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_SELECT_RECORDS: Final = (
    "SELECT session, occurrence, scope, screen, cutscene, speaker, text, revision, completeness, "
    "first_frame, first_ns, last_frame, last_ns, revision_frame, complete_frame, closed_frame, "
    "low_confidence FROM records WHERE session = ? ORDER BY occurrence"
)
_INSERT_CUTSCENE: Final = (
    "INSERT OR REPLACE INTO cutscenes (session, cutscene, scope, first_frame, first_ns, "
    "last_frame, last_ns, end_ns, closed_frame, sentences) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


class NarrativeStoreError(Exception):
    """存储文件不是本插件的格式，或版本不认识。"""


def store_path(data_dir: Path, game: str) -> Path:
    """游戏 `game` 的剧情记录文件：`<本地数据目录>/narrative/<游戏>.sqlite3`。"""
    return data_dir / NARRATIVE_DIR / f"{game}.sqlite3"


def _connect(path: Path) -> sqlite3.Connection:
    # 连接只在存储的线程里用，但可能在另一个线程里创建，所以不让 sqlite3 检查线程。
    connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


class NarrativeStore:
    """一个游戏的剧情记录文件。打开时新开一个运行并按留存上限清理。"""

    def __init__(self, connection: sqlite3.Connection, path: Path, session: int) -> None:
        self._db = connection
        self.path = path
        self.session = session
        """本次运行号。"""

    @classmethod
    def open(
        cls,
        data_dir: Path,
        game: str,
        adapter_version: str,
        *,
        keep_sessions: int,
        max_records: int,
    ) -> "NarrativeStore":
        """打开（没有时创建）游戏 `game` 的存储，新开一个运行，删掉超出留存上限的旧运行。"""
        path = store_path(data_dir, game)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = _connect(path)
        try:
            _prepare(connection)
            with _transaction(connection):
                cursor = connection.execute(
                    "INSERT INTO sessions (adapter_version) VALUES (?)", (adapter_version,)
                )
                session = cursor.lastrowid
                if session is None:
                    raise NarrativeStoreError("新开运行没有得到运行号")
                _retain(connection, session, keep_sessions=keep_sessions, max_records=max_records)
            connection.execute("PRAGMA incremental_vacuum")
        except BaseException:
            connection.close()
            raise
        return cls(connection, path, session)

    def write(
        self,
        records: Iterable[NarrativeRecord],
        scopes: Iterable[Scope] = (),
        discontinuities: Iterable[Discontinuity] = (),
        cutscenes: Iterable[Cutscene] = (),
    ) -> None:
        """在一个事务里写入变化：记录与播片段按运行号与编号覆盖为新状态。"""
        with _transaction(self._db):
            self._db.executemany(
                "INSERT INTO scopes (session, scope, after_screen, first_frame) "
                "VALUES (?, ?, ?, ?)",
                [(s.session, s.scope, s.after_screen, s.first_frame) for s in scopes],
            )
            self._db.executemany(_INSERT_RECORD, [_record_row(record) for record in records])
            self._db.executemany(_INSERT_CUTSCENE, [_cutscene_row(c) for c in cutscenes])
            self._db.executemany(
                "INSERT INTO discontinuities (session, first_frame, last_frame, count) "
                "VALUES (?, ?, ?, ?)",
                [(d.session, d.first_frame, d.last_frame, d.count) for d in discontinuities],
            )

    def close(self) -> None:
        self._db.close()

    def write_observation(self, record: ScreenRecord) -> bool:
        """同一运行的每帧只写一次。"""
        cursor = self._db.execute(
            "INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (session, frame) DO NOTHING",
            (
                record.session,
                record.scope,
                record.frame_number,
                record.capture_ns,
                record.transform_version,
                record.screen_type.value,
                record.confidence,
                record.text,
            ),
        )
        return cursor.rowcount == 1


def _prepare(connection: sqlite3.Connection) -> None:
    """新文件建表；已有的文件核对版本，旧版本就地升级。"""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        tables = connection.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
        if tables:
            raise NarrativeStoreError("存储文件里已有别的表，不是剧情记录")
        # 留存清理之后回收空闲页；auto_vacuum 只能在建表之前设定。
        connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            f"BEGIN; {_SCHEMA} PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;"
        )
    elif version != SCHEMA_VERSION and version not in _UPGRADES:
        raise NarrativeStoreError(f"存储文件的版本 {version} 不认识，应为 {SCHEMA_VERSION}")
    else:
        connection.execute("PRAGMA journal_mode = WAL")
        while version < SCHEMA_VERSION:
            statements = "\n".join(_UPGRADES[version])
            connection.executescript(
                f"BEGIN; {statements} PRAGMA user_version = {version + 1}; COMMIT;"
            )
            version += 1
    connection.execute("PRAGMA synchronous = NORMAL")


@contextlib.contextmanager
def _transaction(connection: sqlite3.Connection) -> Generator[None]:
    """`BEGIN` … `COMMIT`，出错即 `ROLLBACK`。连接是自动提交模式（`isolation_level=None`）。"""
    connection.execute("BEGIN")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


def _retain(
    connection: sqlite3.Connection, current: int, *, keep_sessions: int, max_records: int
) -> None:
    """只留最近 `keep_sessions` 个运行；记录总数仍超过 `max_records` 时继续删最旧的。

    当前运行不删。
    """
    rows = connection.execute(
        "SELECT s.session, (SELECT COUNT(*) FROM records r WHERE r.session = s.session) + "
        "(SELECT COUNT(*) FROM observations o WHERE o.session = s.session) "
        "FROM sessions s ORDER BY s.session DESC"
    ).fetchall()
    kept = 0
    total = 0
    doomed: list[int] = []
    for session, count in rows:
        if session != current and (kept >= keep_sessions or total + count > max_records):
            doomed.append(session)
            continue
        kept += 1
        total += count
    connection.executemany("DELETE FROM sessions WHERE session = ?", [(s,) for s in doomed])


def _record_row(record: NarrativeRecord) -> tuple[object, ...]:
    low = [[line.roi, line.text, line.confidence] for line in record.low_confidence]
    return (
        record.session,
        record.occurrence,
        record.scope,
        record.screen,
        record.cutscene,
        record.speaker,
        record.text,
        record.revision,
        record.completeness.value,
        record.first_frame,
        record.first_ns,
        record.last_frame,
        record.last_ns,
        record.revision_frame,
        record.complete_frame,
        record.closed_frame,
        json.dumps(low, ensure_ascii=False),
    )


def _cutscene_row(cutscene: Cutscene) -> tuple[object, ...]:
    return (
        cutscene.session,
        cutscene.index,
        cutscene.scope,
        cutscene.first_frame,
        cutscene.first_ns,
        cutscene.last_frame,
        cutscene.last_ns,
        cutscene.end_ns,
        cutscene.closed_frame,
        json.dumps(list(cutscene.sentences)),
    )


# 读取：给测试与本机的查看工具用，单独打开文件，不经插件。


def read_sessions(path: Path) -> list[tuple[int, str]]:
    """存储里的运行：运行号与适配包版本，按运行号。"""
    connection = _connect(path)
    try:
        return [
            (int(session), str(version))
            for session, version in connection.execute(
                "SELECT session, adapter_version FROM sessions ORDER BY session"
            )
        ]
    finally:
        connection.close()


def read_records(path: Path, session: int) -> list[NarrativeRecord]:
    """运行 `session` 的记录，按出现编号。"""
    connection = _connect(path)
    try:
        rows: Sequence[tuple[object, ...]] = connection.execute(
            _SELECT_RECORDS, (session,)
        ).fetchall()
    finally:
        connection.close()
    return [_record_of(row) for row in rows]


def read_scopes(path: Path, session: int) -> list[Scope]:
    connection = _connect(path)
    try:
        rows = connection.execute(
            "SELECT session, scope, after_screen, first_frame FROM scopes WHERE session = ? "
            "ORDER BY scope",
            (session,),
        ).fetchall()
    finally:
        connection.close()
    return [Scope(int(a), int(b), None if c is None else str(c), int(d)) for a, b, c, d in rows]


def read_observations(path: Path, session: int) -> list[ScreenRecord]:
    connection = _connect(path)
    try:
        rows = connection.execute(
            "SELECT scope, frame, capture_ns, transform_version, screen_type, confidence, text "
            "FROM observations WHERE session = ? ORDER BY frame",
            (session,),
        )
        return [
            ScreenRecord(
                session,
                int(scope),
                int(frame),
                int(capture),
                int(version),
                ScreenType(kind),
                float(confidence),
                str(text),
            )
            for scope, frame, capture, version, kind, confidence, text in rows
        ]
    finally:
        connection.close()


def read_discontinuities(path: Path, session: int) -> list[Discontinuity]:
    connection = _connect(path)
    try:
        rows = connection.execute(
            "SELECT session, first_frame, last_frame, count FROM discontinuities "
            "WHERE session = ? ORDER BY first_frame",
            (session,),
        ).fetchall()
    finally:
        connection.close()
    return [Discontinuity(int(a), int(b), int(c), int(d)) for a, b, c, d in rows]


def read_cutscenes(path: Path, session: int) -> list[Cutscene]:
    """运行 `session` 的播片段，按编号。"""
    connection = _connect(path)
    try:
        rows: Sequence[tuple[object, ...]] = connection.execute(
            "SELECT session, cutscene, scope, first_frame, first_ns, last_frame, last_ns, end_ns, "
            "closed_frame, sentences FROM cutscenes WHERE session = ? ORDER BY cutscene",
            (session,),
        ).fetchall()
    finally:
        connection.close()
    return [_cutscene_of(row) for row in rows]


def _cutscene_of(row: Sequence[object]) -> Cutscene:
    (
        session,
        index,
        scope,
        first_frame,
        first_ns,
        last_frame,
        last_ns,
        end_ns,
        closed_frame,
        sentences,
    ) = row
    occurrences: list[object] = json.loads(str(sentences))
    return Cutscene(
        session=_int(session),
        index=_int(index),
        scope=_int(scope),
        first_frame=_int(first_frame),
        first_ns=_int(first_ns),
        last_frame=_int(last_frame),
        last_ns=_int(last_ns),
        end_ns=None if end_ns is None else _int(end_ns),
        closed_frame=None if closed_frame is None else _int(closed_frame),
        sentences=tuple(_int(occurrence) for occurrence in occurrences),
    )


def _record_of(row: Sequence[object]) -> NarrativeRecord:
    (
        session,
        occurrence,
        scope,
        screen,
        cutscene,
        speaker,
        text,
        revision,
        completeness,
        first_frame,
        first_ns,
        last_frame,
        last_ns,
        revision_frame,
        complete_frame,
        closed_frame,
        low,
    ) = row
    lines: list[list[object]] = json.loads(str(low))
    return NarrativeRecord(
        session=_int(session),
        occurrence=_int(occurrence),
        scope=_int(scope),
        screen=None if screen is None else str(screen),
        cutscene=None if cutscene is None else _int(cutscene),
        speaker=None if speaker is None else str(speaker),
        text=str(text),
        revision=_int(revision),
        completeness=Completeness(str(completeness)),
        first_frame=_int(first_frame),
        first_ns=_int(first_ns),
        last_frame=_int(last_frame),
        last_ns=_int(last_ns),
        revision_frame=_int(revision_frame),
        complete_frame=None if complete_frame is None else _int(complete_frame),
        closed_frame=None if closed_frame is None else _int(closed_frame),
        low_confidence=tuple(
            LowConfidenceLine(str(roi), str(line), _float(confidence))
            for roi, line, confidence in lines
        ),
    )


def _int(value: object) -> int:
    if not isinstance(value, int):
        raise NarrativeStoreError(f"存储里的值 {value!r} 不是整数")
    return value


def _float(value: object) -> float:
    if not isinstance(value, int | float):
        raise NarrativeStoreError(f"存储里的值 {value!r} 不是数")
    return float(value)
