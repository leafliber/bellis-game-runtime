"""在守护线程中把已关闭的 span 分批追加到本机的 OTLP JSONL。"""

import asyncio
import json
from collections import deque
from contextlib import suppress
from itertools import count
from os import getpid
from pathlib import Path
from re import fullmatch
from threading import Condition, Lock, Thread
from time import monotonic_ns, time_ns
from typing import Annotated, BinaryIO, NotRequired, Self, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

_BATCH_SIZE = 32
_STOP_NS = 250_000_000
_FILE_PREFIX = f"game-runtime-{getpid()}-{time_ns()}"
_FILE_SEQUENCE = count()
# 重启插件时，上一条线程可能还在等文件系统。进程中最多留一个写者。
_WRITER_SLOT = Lock()


class TelemetryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    directory: Path
    max_file_bytes: Annotated[int, Field(strict=True, ge=65_536)] = 8 * 1024 * 1024
    max_file_age_ms: Annotated[int, Field(strict=True, ge=1000)] = 3_600_000
    max_files: Annotated[int, Field(strict=True, ge=1, le=1000)] = 24
    max_queue: Annotated[int, Field(strict=True, ge=1, le=65_536)] = 1024
    max_active_spans: Annotated[int, Field(strict=True, ge=1, le=4096)] = 128
    flush_interval_ms: Annotated[int, Field(strict=True, ge=1, le=60_000)] = 100

    @model_validator(mode="after")
    def _local_directory(self) -> Self:
        if (
            not self.directory.is_absolute()
            or self.directory.drive.startswith("\\\\")
            or "\0" in str(self.directory)
        ):
            raise ValueError("directory 须为本地绝对路径")
        return self


class EventRecord(TypedDict):
    name: str
    timeUnixNano: str


class _StringValue(TypedDict):
    stringValue: str


class AttributeRecord(TypedDict):
    key: str
    value: _StringValue


class _Status(TypedDict):
    code: int


class SpanRecord(TypedDict):
    traceId: str
    spanId: str
    parentSpanId: NotRequired[str]
    name: str
    kind: int
    startTimeUnixNano: str
    endTimeUnixNano: str
    attributes: list[AttributeRecord]
    events: NotRequired[list[EventRecord]]
    status: _Status


def _encode(spans: list[SpanRecord]) -> bytes:
    request = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "bellis.game-runtime"}}
                    ]
                },
                "scopeSpans": [{"scope": {"name": "game-runtime"}, "spans": spans}],
            }
        ]
    }
    return (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")


class _RotatingFiles:
    def __init__(self, config: TelemetryConfig) -> None:
        self._config = config
        self._file: BinaryIO | None = None
        self._size = 0
        self._opened_ns = 0

    def write(self, data: bytes) -> None:
        if len(data) > self._config.max_file_bytes:
            return
        if self._file is not None and (
            self._size + len(data) > self._config.max_file_bytes
            or monotonic_ns() - self._opened_ns >= self._config.max_file_age_ms * 1_000_000
        ):
            self.close()
        file = self._file if self._file is not None else self._open()
        try:
            if file.write(data) != len(data):
                raise OSError("遥测文件写入不完整")
        except OSError:
            with suppress(OSError):
                file.truncate(self._size)
            self.close()
            raise
        self._size += len(data)

    def _open(self) -> BinaryIO:
        directory = self._config.directory
        directory.mkdir(parents=True, exist_ok=True)
        own = sorted(
            path
            for path in directory.iterdir()
            if fullmatch(rf"{_FILE_PREFIX}-[0-9]{{12}}\.jsonl", path.name)
        )
        # 先腾位置。删除失败时丢这一批，避免在保留上限之外继续建文件。
        for path in own[: max(0, len(own) + 1 - self._config.max_files)]:
            path.unlink(missing_ok=True)
        path = directory / f"{_FILE_PREFIX}-{next(_FILE_SEQUENCE):012}.jsonl"
        self._file = path.open("xb", buffering=0)
        self._size = 0
        self._opened_ns = monotonic_ns()
        return self._file

    def close(self) -> None:
        file, self._file = self._file, None
        if file is not None:
            with suppress(OSError):
                file.close()


class LocalExporter:
    def __init__(self, config: TelemetryConfig) -> None:
        self._files = _RotatingFiles(config)
        self._interval = config.flush_interval_ms / 1000
        self._pending: deque[SpanRecord] = deque(maxlen=config.max_queue)
        self._changed = Condition()
        self._loop = asyncio.get_running_loop()
        self._done = asyncio.Event()
        self._closing = False
        self._stop_by_ns: int | None = None

        if _WRITER_SLOT.acquire(blocking=False):
            try:
                Thread(target=self._run, name="game-runtime-telemetry", daemon=True).start()
                return
            except (RuntimeError, OSError):
                _WRITER_SLOT.release()
        self._closing = True
        self._done.set()

    @property
    def accepting(self) -> bool:
        with self._changed:
            return not self._closing

    def put(self, span: SpanRecord) -> None:
        with self._changed:
            if not self._closing:
                self._pending.append(span)
                self._changed.notify()

    def _next_batch(self) -> list[SpanRecord]:
        with self._changed:
            self._changed.wait_for(lambda: self._closing or bool(self._pending))
            if self._pending:
                self._changed.wait_for(
                    lambda: self._closing or len(self._pending) >= _BATCH_SIZE,
                    timeout=self._interval,
                )
            if self._stop_by_ns is not None and monotonic_ns() >= self._stop_by_ns:
                self._pending.clear()
            return [self._pending.popleft() for _ in range(min(len(self._pending), _BATCH_SIZE))]

    def _run(self) -> None:
        try:
            while batch := self._next_batch():
                with suppress(OSError):
                    self._files.write(_encode(batch))
        finally:
            self._files.close()
            with self._changed:
                self._closing = True
                self._pending.clear()
            _WRITER_SLOT.release()
            with suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._done.set)

    def close(self) -> None:
        with self._changed:
            if self._closing:
                return
            self._stop_by_ns = monotonic_ns() + _STOP_NS
            self._closing = True
            self._changed.notify()

    async def aclose(self) -> None:
        self.close()
        remaining = (
            0.0 if self._stop_by_ns is None else max(0, self._stop_by_ns - monotonic_ns()) / 1e9
        )
        try:
            async with asyncio.timeout(remaining):
                await self._done.wait()
        except TimeoutError:
            pass
        finally:
            # 文件系统调用若仍未返回，丢弃未开始的批次，由守护线程自行收尾。
            with self._changed:
                self._pending.clear()
