"""录制的写入方：定序帧源的接收方，加上音频块与元数据。

格式见 `docs/modules/offline-tools.md` 的“样本格式”。帧的编码在线程池里并行，索引按接纳的顺序写；
在途的帧至多为线程数的两倍，再多就让定序帧源等着（接纳帧率随之降下来）。帧索引和收尾在工作线程
写盘，实时录制也在工作线程调用同步的音频写入。
"""

import asyncio
import functools
import logging
import os
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import IO, Final

from game_runtime._threads import in_thread
from game_runtime.audio_ring import AudioRead
from game_runtime.frame_ring import RingFrame
from game_runtime.recording.annotations import Annotations, save_annotations
from game_runtime.recording.codec import crc32, encode_delta, encode_png, packed_pixels
from game_runtime.recording.layout import (
    AUDIO_INDEX,
    AUDIO_SAMPLES,
    FRAMES_DIR,
    FRAMES_INDEX,
    LIVE_DIR,
    AdmissionInfo,
    AudioEntry,
    AudioInfo,
    Encoding,
    FrameEntry,
    FramesInfo,
    GameInfo,
    RecordingMeta,
    State,
    ToolInfo,
    directory_name,
    frame_file,
    write_atomic,
    write_meta,
)

logger = logging.getLogger(__name__)

DEFAULT_KEYFRAME_INTERVAL: Final = 5.0
"""定时关键帧的间隔（秒，按采集时刻）。"""


def default_threads() -> int:
    return max(1, min(4, os.cpu_count() or 1))


@dataclass(frozen=True, slots=True)
class WriterOptions:
    keyframe_interval: float = DEFAULT_KEYFRAME_INTERVAL
    threads: int = 0
    """编码线程数；0 即 `min(4, CPU 数)`。"""

    def __post_init__(self) -> None:
        if not self.keyframe_interval > 0:
            raise ValueError(f"keyframe_interval 须为正数：{self.keyframe_interval}")
        if self.threads < 0:
            raise ValueError(f"threads 不能为负：{self.threads}")


@dataclass(frozen=True, slots=True)
class _Planned:
    frame: RingFrame
    encoding: Encoding
    """计划的编码；`delta` 在变化太多时改写成 `png`。"""


@dataclass(frozen=True, slots=True)
class _Encoded:
    encoding: Encoding
    file: str | None
    size: int
    crc32: int | None
    """`same` 为 None：与上一行相同。"""


class RecordingWriter:
    """一段录制的写入方。用 `create` 新建目录，`write_frame` 是定序帧源的接收方，
    `write_audio` 写块，`finish` 收尾。"""

    def __init__(
        self,
        directory: Path,
        meta: RecordingMeta,
        *,
        options: WriterOptions | None = None,
    ) -> None:
        """在已建好的空目录里开始写；一般用 `create`。"""
        self.path = directory
        self._options = options or WriterOptions()
        self._meta = meta
        threads = self._options.threads or default_threads()
        self._executor = ThreadPoolExecutor(threads, thread_name_prefix="record-encode")
        self._max_pending = 2 * threads
        self._keyframe_ns = round(self._options.keyframe_interval * 1e9)
        (directory / FRAMES_DIR).mkdir()
        (directory / LIVE_DIR).mkdir()
        save_annotations(directory, Annotations())
        write_meta(directory, meta)
        self._frames_index: IO[str] = (directory / FRAMES_INDEX).open(
            "x", encoding="utf-8", newline="\n"
        )
        self._audio_index: IO[str] | None = None
        self._audio_samples: IO[bytes] | None = None
        if meta.audio is not None:
            self._audio_index = (directory / AUDIO_INDEX).open("x", encoding="utf-8", newline="\n")
            self._audio_samples = (directory / AUDIO_SAMPLES).open("xb")
        self._pending: deque[tuple[_Planned, asyncio.Future[_Encoded]]] = deque()
        self._previous: tuple[RingFrame, bytes] | None = None
        """上一接纳帧与它去掉填充的像素。"""
        self._last_key_ns: int | None = None
        self._last_crc: int | None = None
        self._frames = FramesInfo()
        self._audio = meta.audio
        self._last_block: int | None = None
        self._frame_lock = asyncio.Lock()
        self._audio_lock = Lock()
        self._frame_failure: BaseException | None = None
        self._file_failure: Exception | None = None
        self._closing = False
        self._finished = False

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        game: GameInfo,
        name: str | None,
        admission: AdmissionInfo,
        audio_format: tuple[int, int] | None,
        tool: ToolInfo,
        options: WriterOptions | None = None,
        started_at: datetime | None = None,
    ) -> "RecordingWriter":
        """在 `root`（`recordings/<适配包 id>/`）下新建这段录制的目录并写好元数据，`state` 为
        `recording`。`audio_format` 是（采样率，声道数），没有音频为 None。"""
        started = (started_at or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
        base = directory_name(started, name)
        root.mkdir(parents=True, exist_ok=True)
        directory = root / base
        serial = 1
        while True:
            try:
                directory.mkdir()
                break
            except FileExistsError:
                serial += 1
                directory = root / f"{base}-{serial}"
        audio = None
        if audio_format is not None:
            audio = AudioInfo(sample_rate=audio_format[0], channels=audio_format[1])
        meta = RecordingMeta(
            state="recording",
            name=name,
            started_at=started,
            game=game,
            tool=tool,
            admission=admission,
            audio=audio,
        )
        return cls(directory, meta, options=options)

    # 状态

    @property
    def meta(self) -> RecordingMeta:
        """当前的元数据（帧与音频的计数随写入更新）。"""
        return self._meta.model_copy(update={"frames": self._frames, "audio": self._audio})

    @property
    def pending(self) -> int:
        """在途（编码中、还没进索引）的帧数。"""
        return len(self._pending)

    @property
    def failure(self) -> BaseException | None:
        return self._frame_failure or self._file_failure

    def _check_writable(self) -> None:
        failure = self.failure
        if failure is not None:
            raise failure
        if self._closing:
            raise RuntimeError("录制已收尾")

    # 帧

    async def write_frame(self, frame: RingFrame) -> None:
        """定序帧源的接收方：按顺序计划这一帧的编码，交给线程池；在途的帧太多时等最旧的写完。"""
        async with self._frame_lock:
            self._check_writable()
            self._queue_frame(frame)
            await self._drain(self._max_pending)

    async def flush_frames(self) -> None:
        """写完已提交的帧，供录制消费者在一批帧结束时及时发现编码或索引错误。"""
        async with self._frame_lock:
            self._check_writable()
            await self._drain(0)

    def _queue_frame(self, frame: RingFrame) -> None:
        pixels = packed_pixels(frame)
        previous = self._previous
        if previous is None or (previous[0].width, previous[0].height) != (
            frame.width,
            frame.height,
        ):
            encoding: Encoding = "png"
            self._last_key_ns = frame.capture_ns
        elif previous[1] == pixels:
            encoding = "same"
        elif self._last_key_ns is None or frame.capture_ns - self._last_key_ns >= self._keyframe_ns:
            encoding = "png"
            self._last_key_ns = frame.capture_ns
        else:
            encoding = "delta"
        loop = asyncio.get_running_loop()
        if encoding == "same":
            future: asyncio.Future[_Encoded] = loop.create_future()
            future.set_result(_Encoded("same", None, 0, None))
        else:
            work = functools.partial(
                self._encode,
                frame,
                encoding,
                pixels,
                None if previous is None else previous[1],
            )
            future = loop.run_in_executor(self._executor, work)
        self._pending.append((_Planned(frame, encoding), future))
        self._previous = (frame, pixels)

    def _encode(
        self, frame: RingFrame, encoding: Encoding, pixels: bytes, previous: bytes | None
    ) -> _Encoded:
        """在工作线程里编码并写像素文件：先写临时文件再改名。"""
        data: bytes | None = None
        if encoding == "delta" and previous is not None:
            data = encode_delta(pixels, previous)
            if data is None:
                encoding = "png"
        if data is None:
            encoding = "png"
            data = encode_png(pixels, frame.width, frame.height)
        relative = frame_file(frame.frame_number, encoding)
        write_atomic(self.path / relative, data)
        return _Encoded(encoding, relative, len(data), crc32(pixels))

    async def _drain(self, limit: int) -> None:
        """按顺序把编码完的帧写进索引，直到在途的不超过 `limit` 且最旧的一帧还没编码完。

        等编码时取消则保留这一帧。开始写索引后，等这次写完再取消，收尾时从下一帧继续。
        """
        if self._frame_failure is not None:
            raise self._frame_failure
        while self._pending and (len(self._pending) > limit or self._pending[0][1].done()):
            planned, future = self._pending[0]
            try:
                encoded = await asyncio.shield(future)
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                self._frame_failure = error
                raise
            try:
                await in_thread(functools.partial(self._index, planned.frame, encoded))
            except Exception as error:
                self._frame_failure = error
                raise
            finally:
                self._pending.popleft()

    def _index(self, frame: RingFrame, encoded: _Encoded) -> None:
        try:
            crc = encoded.crc32 if encoded.crc32 is not None else self._last_crc
            if crc is None:
                raise RuntimeError("第一帧不能是 same")
            entry = FrameEntry(
                frame_number=frame.frame_number,
                capture_ns=frame.capture_ns,
                transform_version=frame.transform_version,
                width=frame.width,
                height=frame.height,
                encoding=encoded.encoding,
                file=encoded.file,
                crc32=crc,
            )
            info = self._frames
            frames = info.model_copy(
                update={
                    "count": info.count + 1,
                    "keyframes": info.keyframes + (encoded.encoding == "png"),
                    "bytes": info.bytes + encoded.size,
                    "first_capture_ns": (
                        frame.capture_ns if info.first_capture_ns is None else info.first_capture_ns
                    ),
                    "last_capture_ns": frame.capture_ns,
                }
            )
            self._frames_index.write(entry.model_dump_json() + "\n")
            self._frames_index.flush()
        except Exception as error:
            # 即使等这个写入的任务被取消，收尾仍需知道索引写入失败，不能再追加后续差分帧。
            self._frame_failure = error
            raise
        self._last_crc = crc
        self._frames = frames

    # 声音

    def write_audio(self, read: AudioRead) -> None:
        """同步追加音频块，按块号写并计空缺。实时录制须在工作线程调用。"""
        with self._audio_lock:
            self._check_writable()
            self._append_audio(read)

    def _append_audio(self, read: AudioRead) -> None:
        audio = self._audio
        index, samples = self._audio_index, self._audio_samples
        if audio is None or index is None or samples is None:
            raise RuntimeError("这段录制没有音频")
        if (read.sample_rate, read.channels) != (audio.sample_rate, audio.channels):
            raise ValueError(
                f"音频格式 {read.sample_rate} Hz、{read.channels} 声道与录制的 "
                f"{audio.sample_rate} Hz、{audio.channels} 声道不同"
            )
        blocks, gaps, size = audio.blocks, audio.gaps, audio.bytes
        first, last = audio.first_capture_ns, audio.last_capture_ns
        last_block = self._last_block
        try:
            for block in read.blocks:
                if last_block is not None and block.block_number <= last_block:
                    continue  # 已经写过
                if last_block is not None and block.block_number != last_block + 1:
                    gaps += 1
                entry = AudioEntry(
                    block_number=block.block_number,
                    capture_ns=block.capture_ns,
                    sample_count=block.sample_count,
                    discontinuity=block.discontinuity,
                    offset=size,
                )
                samples.write(block.samples)
                index.write(entry.model_dump_json() + "\n")
                blocks += 1
                size += len(block.samples)
                first = block.capture_ns if first is None else first
                last = block.capture_ns
                last_block = block.block_number
            updated = audio.model_copy(
                update={
                    "blocks": blocks,
                    "gaps": gaps,
                    "bytes": size,
                    "first_capture_ns": first,
                    "last_capture_ns": last,
                }
            )
            samples.flush()
            index.flush()
        except Exception as error:
            self._file_failure = error
            raise
        self._audio = updated
        self._last_block = last_block

    @property
    def last_block(self) -> int:
        """写过的最大块号，还没有为 0。"""
        return self._last_block or 0

    # 收尾

    async def finish(
        self, *, stop_reason: str, reader_overflows: int = 0, state: State | None = None
    ) -> RecordingMeta:
        """写完在途的帧，关闭文件，把元数据改写为 `complete`（写入出过错为 `failed`）。

        开始收尾后拒绝新的写入。可以重复调用，完成后的调用只返回元数据。
        """
        self._closing = True
        async with self._frame_lock:
            if self._finished:
                return self._meta
            try:
                await self._drain(0)
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                logger.error("录制 %s 写帧失败：%s", self.path, error)
                # 出错的那一帧之后的不进索引：等编码做完，免得线程池还在往目录里写。
                for _, future in self._pending:
                    await asyncio.wait((future,))
                    if not future.cancelled():
                        future.exception()
                self._pending.clear()
            return await in_thread(
                functools.partial(self._finish_files, stop_reason, reader_overflows, state)
            )

    def _finish_files(
        self, stop_reason: str, reader_overflows: int, state: State | None
    ) -> RecordingMeta:
        self._executor.shutdown(wait=True)
        with self._audio_lock:
            for file in (self._frames_index, self._audio_index, self._audio_samples):
                if file is not None:
                    try:
                        file.close()
                    except OSError as error:
                        self._file_failure = self._file_failure or error
                        logger.error("录制 %s 关闭文件失败：%s", self.path, error)
            final: State = "failed" if self.failure is not None else state or "complete"
            meta = self.meta.model_copy(
                update={
                    "state": final,
                    "ended_at": datetime.now(UTC).replace(microsecond=0),
                    "stop_reason": stop_reason,
                    "reader_overflows": reader_overflows,
                }
            )
            try:
                write_meta(self.path, meta)
            except OSError as error:
                self._file_failure = self._file_failure or error
                raise
            self._meta = meta
            self._finished = True
            return meta
