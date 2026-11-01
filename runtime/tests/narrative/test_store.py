"""剧情记录的本机存储：位置、写入与读回、运行号、留存上限与清理、不认识的文件。"""

import dataclasses
import sqlite3
import sys
from pathlib import Path

import pytest

from game_runtime.narrative import (
    Completeness,
    Discontinuity,
    LowConfidenceLine,
    NarrativeRecord,
    NarrativeStore,
    NarrativeStoreError,
    Scope,
    read_discontinuities,
    read_records,
    read_scopes,
    read_sessions,
    store_path,
)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """临时的本地数据目录。Windows 上加扩展长度前缀：临时目录较深时可能超过 MAX_PATH。"""
    if sys.platform == "win32":
        return Path("\\\\?\\" + str(tmp_path.resolve())) / "data"
    return tmp_path / "data"


def open_store(data_dir: Path, *, keep: int = 20, limit: int = 200_000) -> NarrativeStore:
    return NarrativeStore.open(
        data_dir, "synthetic-game", "0.1.0", keep_sessions=keep, max_records=limit
    )


def record(session: int, occurrence: int, **changes: object) -> NarrativeRecord:
    base = NarrativeRecord(
        session=session,
        occurrence=occurrence,
        scope=1,
        screen="dialogue",
        speaker="阿遥",
        text="你听见了吗？",
        revision=3,
        completeness=Completeness.PARTIAL,
        first_frame=10,
        first_ns=1_000,
        last_frame=12,
        last_ns=3_000,
        revision_frame=12,
        complete_frame=None,
        closed_frame=None,
        low_confidence=(),
    )
    return dataclasses.replace(base, **changes)


def test_the_store_lives_in_the_local_data_dir_per_game(data_dir: Path) -> None:
    store = open_store(data_dir)
    store.close()

    assert store.path == data_dir / "narrative" / "synthetic-game.sqlite3"
    assert store_path(data_dir, "witch-trial") == data_dir / "narrative" / "witch-trial.sqlite3"
    assert store.path.is_file()


def test_records_are_written_and_read_back(data_dir: Path) -> None:
    store = open_store(data_dir)
    session = store.session
    partial = record(session, 1)
    low = (LowConfidenceLine("speaker", "阿遥", 0.62),)
    store.write([partial], [Scope(session, 1, None, 10)])
    # 同一句的新状态按运行号与出现编号覆盖。
    complete = record(
        session,
        1,
        completeness=Completeness.COMPLETE,
        complete_frame=13,
        closed_frame=20,
        last_frame=19,
        low_confidence=low,
    )
    second = record(session, 2, speaker=None, text="门", scope=2, first_frame=20)
    gap = Discontinuity(session, 30, 40, 11)
    store.write([complete, second], [Scope(session, 2, "bad-ending", 20)], [gap])
    store.close()

    assert read_sessions(store.path) == [(session, "0.1.0")]
    assert read_records(store.path, session) == [complete, second]
    assert read_scopes(store.path, session) == [
        Scope(session, 1, None, 10),
        Scope(session, 2, "bad-ending", 20),
    ]
    assert read_discontinuities(store.path, session) == [gap]


def test_a_failed_write_changes_nothing(data_dir: Path) -> None:
    store = open_store(data_dir)
    session = store.session
    store.write([record(session, 1)], [Scope(session, 1, None, 10)])
    with pytest.raises(sqlite3.IntegrityError):
        # 范围号重复：整个事务回滚，记录也不写。
        store.write([record(session, 2)], [Scope(session, 1, None, 10)])
    store.close()

    assert [r.occurrence for r in read_records(store.path, session)] == [1]


def test_each_open_starts_a_new_session(data_dir: Path) -> None:
    sessions: list[int] = []
    for _ in range(3):
        store = open_store(data_dir)
        sessions.append(store.session)
        store.close()

    assert sessions == sorted(sessions)
    assert len(set(sessions)) == 3


def test_only_the_newest_sessions_are_kept(data_dir: Path) -> None:
    opened: list[int] = []
    for _ in range(4):
        store = open_store(data_dir, keep=2)
        store.write([record(store.session, 1)])
        opened.append(store.session)
        store.close()

    path = store_path(data_dir, "synthetic-game")
    assert [s for s, _ in read_sessions(path)] == opened[-2:]
    assert read_records(path, opened[0]) == []


def test_old_sessions_go_when_there_are_too_many_records(data_dir: Path) -> None:
    opened: list[int] = []
    for count in (3, 2, 2):
        store = open_store(data_dir, limit=4)
        store.write([record(store.session, n) for n in range(1, count + 1)])
        opened.append(store.session)
        store.close()
    # 第三次打开时有 3 + 2 条：最旧的一次被删掉，剩 2 条；第三次自己写了 2 条。
    store = open_store(data_dir, limit=4)
    current = store.session
    store.close()

    path = store_path(data_dir, "synthetic-game")
    assert [s for s, _ in read_sessions(path)] == [opened[1], opened[2], current]


def test_the_current_session_is_never_removed(data_dir: Path) -> None:
    store = open_store(data_dir, keep=1, limit=1)
    store.write([record(store.session, n) for n in (1, 2, 3)])
    store.close()
    store = open_store(data_dir, keep=1, limit=1)
    current = store.session
    store.close()

    assert [s for s, _ in read_sessions(store.path)] == [current]


def test_a_file_that_is_not_a_narrative_store_is_refused(data_dir: Path) -> None:
    path = store_path(data_dir, "synthetic-game")
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE other (x INTEGER)")
    connection.commit()
    connection.close()

    with pytest.raises(NarrativeStoreError, match="别的表"):
        open_store(data_dir)


def test_an_unknown_schema_version_is_refused(data_dir: Path) -> None:
    open_store(data_dir).close()
    path = store_path(data_dir, "synthetic-game")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 99")
    connection.commit()
    connection.close()

    with pytest.raises(NarrativeStoreError, match="版本 99"):
        open_store(data_dir)


VERSION_1: str = """
PRAGMA journal_mode = WAL;
BEGIN;
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
CREATE TABLE records (
    session INTEGER NOT NULL REFERENCES sessions (session) ON DELETE CASCADE,
    occurrence INTEGER NOT NULL,
    scope INTEGER NOT NULL,
    screen TEXT NOT NULL,
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
    PRIMARY KEY (session, occurrence)
);
CREATE TABLE discontinuities (
    session INTEGER NOT NULL REFERENCES sessions (session) ON DELETE CASCADE,
    first_frame INTEGER NOT NULL,
    last_frame INTEGER NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (session, first_frame)
);
INSERT INTO sessions (adapter_version) VALUES ('0.0.9');
INSERT INTO scopes VALUES (1, 1, NULL, 10);
INSERT INTO records VALUES (1, 1, 1, 'dialogue', '阿遥', '你听见了吗？', 3, 'partial', 10, 1000, 12,
    3000, 12, NULL, NULL, '[]');
PRAGMA user_version = 1;
COMMIT;
"""
"""版本 1 的存储文件（`records.screen` 不能为空），里面有一个运行的一句。"""


def test_a_version_1_file_is_upgraded_in_place(data_dir: Path) -> None:
    """旧版本的文件打开时就地升级：记录照留，此后可以写 screen 为空的记录（对白框没写画面）。"""
    path = store_path(data_dir, "synthetic-game")
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.executescript(VERSION_1)
    connection.close()

    store = open_store(data_dir)
    generic = record(store.session, 1, screen=None, text="门后也许有路。")
    store.write([generic])
    store.close()

    assert [s for s, _ in read_sessions(path)] == [1, store.session]
    assert read_records(path, 1) == [record(1, 1)]
    assert read_scopes(path, 1) == [Scope(1, 1, None, 10)]
    assert read_records(path, store.session) == [generic]
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        connection.close()
    # 再打开不再升级，照常新开运行。
    open_store(data_dir).close()
    assert len(read_sessions(path)) == 3
