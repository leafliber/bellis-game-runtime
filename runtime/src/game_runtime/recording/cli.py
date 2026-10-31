"""录制、回放与导出的命令行：`uv run record`、`uv run replay`、`uv run recording`。

用法见 `docs/modules/offline-tools.md` 的“录制”。这些是维护者在自己终端里运行的程序：录制按回车或
Ctrl+C 停止，Ctrl+C 以取消主任务的方式到达，照常按顺序收尾。
"""

import argparse
import asyncio
import io
import json
import logging
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Final

from game_runtime.adapter import GAMES_DIR, Adapter, AdapterError, load_adapter
from game_runtime.core_link import CoreLinkOptions, CoreStartError
from game_runtime.frames import DEFAULT_IDLE_INTERVAL, DEFAULT_MAX_RATE, AdmissionPolicy
from game_runtime.local_data import LocalDataError, local_data_dir
from game_runtime.recording.annotations import check_annotations, load_annotations
from game_runtime.recording.export import (
    FrameSelection,
    export_audio,
    export_frames,
    select_frames,
)
from game_runtime.recording.layout import (
    RECORDINGS_DIR,
    ToolInfo,
    check_name,
    exports_root,
    recordings_root,
)
from game_runtime.recording.reader import Recording, RecordingError
from game_runtime.recording.replay import adapter_sha256, replay
from game_runtime.recording.results import Comparison
from game_runtime.recording.retention import (
    GB,
    QuotaKeeper,
    QuotaRefusedError,
    QuotaStatus,
    StoredRecording,
    list_recordings,
    quota_bytes,
)
from game_runtime.recording.session import (
    LiveOptions,
    LiveSession,
    SessionStatus,
    audio_config,
    capture_config,
)

REPO_ROOT: Final = GAMES_DIR.parent
"""本仓库的根目录。"""
STATUS_INTERVAL: Final = 2.0
"""状态行的间隔（秒）。"""
QUOTA_INTERVAL: Final = 5.0
"""录制期间核对配额的间隔（秒）。"""


def _out(line: str = "") -> None:
    print(line, flush=True)


def _err(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _setup_output(verbose: bool) -> None:
    for stream in (sys.stdout, sys.stderr):
        # 输出含中文；终端的代码页编不出来时不让程序因此失败。
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="backslashreplace")
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _gb(size: int) -> str:
    """字节数写成 GB，不到 1 GB 时写成 MB（都按 10⁹、10⁶）。"""
    return f"{size / GB:.2f} GB" if size >= GB else f"{size / 1e6:.1f} MB"


def _clock(seconds: float) -> str:
    return str(timedelta(seconds=round(seconds)))


# 实时核与工具版本


def build_realtime_core(*, release: bool = True) -> Path:
    """以 cargo 编译 `realtime-core`，返回可执行文件。尊重 `CARGO_TARGET_DIR`。"""
    cargo = shutil.which("cargo")
    if cargo is None:
        raise RuntimeError("PATH 上没有 cargo：先 `rustup toolchain install`，或以 --core 指定")
    argv = [cargo, "build", "--locked", "-p", "realtime-core", "--bin", "realtime-core"]
    if release:
        argv.append("--release")
    argv.append("--message-format=json")
    with subprocess.Popen(
        argv,
        cwd=REPO_ROOT / "core",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    ) as process:
        stdout, stderr = process.communicate(timeout=1800)
    if process.returncode != 0:
        raise RuntimeError(f"编译 realtime-core 失败：\n{stderr}")
    for line in stdout.splitlines():
        message = json.loads(line)
        if (
            message.get("reason") == "compiler-artifact"
            and message.get("target", {}).get("name") == "realtime-core"
            and "bin" in message.get("target", {}).get("kind", [])
            and message.get("executable")
        ):
            return Path(message["executable"])
    raise RuntimeError("cargo 没有给出 realtime-core 的路径")


def _git(*args: str) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        completed = subprocess.run(
            [git, *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout if completed.returncode == 0 else None


def tool_info() -> ToolInfo:
    """本仓库的提交与工作树有无改动；取不到为 None。"""
    commit = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain")
    return ToolInfo(
        commit=None if commit is None else commit.strip(),
        dirty=None if status is None else bool(status.strip()),
    )


# 录制


def _size(text: str) -> tuple[int, int]:
    width, _, height = text.lower().partition("x")
    try:
        size = int(width), int(height)
    except ValueError:
        raise argparse.ArgumentTypeError(f"须为 <宽>x<高>：{text}") from None
    if min(size) < 1:
        raise argparse.ArgumentTypeError(f"宽与高须为正数：{text}")
    return size


def _data_dir(value: Path | None) -> Path:
    if value is not None:
        if not value.is_absolute():
            raise LocalDataError(f"--data-dir 须为绝对路径：{value}")
        return value
    return local_data_dir()


def record_main(argv: Sequence[str] | None = None) -> int:
    """`uv run record <适配包 id>`。"""
    parser = argparse.ArgumentParser(
        prog="record",
        description="Record frames and game audio of an adapter's window into the local data "
        "directory. Press Enter or Ctrl+C to stop.",
    )
    parser.add_argument("game", help="adapter id (a directory under games/)")
    parser.add_argument("--name", help="name of the recording: letters, digits, - and _")
    parser.add_argument("--seconds", type=float, help="stop after this many seconds")
    parser.add_argument("--max-rate", type=float, default=DEFAULT_MAX_RATE)
    parser.add_argument("--idle-interval", type=float, default=DEFAULT_IDLE_INTERVAL)
    parser.add_argument(
        "--max-size", type=_size, help="frame ring size WIDTHxHEIGHT (default: the adapter's)"
    )
    parser.add_argument(
        "--record-only", action="store_true", help="record only, without the perception plugins"
    )
    parser.add_argument("--core", type=Path, help="an already built realtime-core executable")
    parser.add_argument("--data-dir", type=Path, help="local data directory (absolute)")
    parser.add_argument("--verbose", action="store_true", help="log at INFO level")
    args = parser.parse_args(argv)
    _setup_output(args.verbose)
    try:
        adapter = load_adapter(GAMES_DIR / args.game)
        quota = quota_bytes(adapter)
        data_dir = _data_dir(args.data_dir)
        policy = AdmissionPolicy(args.max_rate, args.idle_interval)
        if args.name is not None:
            check_name(args.name)
    except (AdapterError, QuotaRefusedError, LocalDataError, ValueError) as error:
        _err(f"不能录制：{error}")
        return 2
    core: Path | None = args.core
    if core is None:
        _out("正在编译 realtime-core（release）……")
        try:
            core = build_realtime_core()
        except RuntimeError as error:
            _err(str(error))
            return 2
    options = LiveOptions(
        link=CoreLinkOptions(core),
        adapter=adapter,
        adapter_sha256=adapter_sha256(adapter.id),
        capture=capture_config(adapter, args.max_size),
        audio=audio_config(adapter),
        data_dir=data_dir,
        tool=tool_info(),
        name=args.name,
        policy=policy,
        perception=not args.record_only,
    )
    try:
        return asyncio.run(record(options, quota=quota, seconds=args.seconds))
    except KeyboardInterrupt:
        _err("已强制退出：录制停在 recording 状态，不完整")
        return 130


def _report_delete(stored: StoredRecording) -> None:
    _out(f"超出录制配额，删除最旧的录制：{stored.path}（{stored.state}，{_gb(stored.size)}）")


def _report_stuck(stored: StoredRecording) -> None:
    _err(f"没能删干净 {stored.path}，剩下 {_gb(stored.size)} 仍计入用量")


async def record(options: LiveOptions, *, quota: int, seconds: float | None = None) -> int:
    """录制到按回车、Ctrl+C、`seconds` 到时、录满配额或出错为止，然后收尾。返回退出码。"""
    adapter = options.adapter
    root = recordings_root(options.data_dir, adapter.id)
    keeper = QuotaKeeper(root, quota)
    status = await asyncio.to_thread(keeper.check, _report_delete, failed=_report_stuck)
    session = LiveSession(options)
    try:
        await session.start()
    except (CoreStartError, RuntimeError) as error:
        _err(f"没能开始录制：{error}")
        return 1
    keeper.set_current(session.path)
    _out(f"录制 {adapter.id}（{adapter.name} {adapter.version}）到 {session.path}")
    _out(
        f"配额 {_gb(quota)}，已用 {_gb(status.used)}；"
        f"帧环 {options.capture.max_width}×{options.capture.max_height}；"
        "按回车或 Ctrl+C 停止。"
    )
    reason = "error"
    try:
        reason = await _wait_for_stop(session, keeper, seconds)
        _out(f"正在收尾（{reason}）……")
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None:
            task.uncancel()
        reason = "interrupt"
        _out("")
        _out("收到 Ctrl+C，正在收尾（再按一次即强制退出，录制将不完整）……")
    finally:
        # 等待中出了别的错也照样收尾，再让错误传出去。
        result = await session.stop(reason)
    meta = result.meta
    audio = meta.audio
    final = keeper.check(_report_delete, failed=_report_stuck)
    duration = 0.0
    if meta.frames.first_capture_ns is not None and meta.frames.last_capture_ns is not None:
        duration = (meta.frames.last_capture_ns - meta.frames.first_capture_ns) / 1e9
    _out(
        f"已停止（{meta.stop_reason}）：录制 {meta.state}，时长 {_clock(duration)}，"
        f"帧 {meta.frames.count}（关键帧 {meta.frames.keyframes}），"
        + (f"声音 {audio.blocks} 块（空缺 {audio.gaps}），" if audio is not None else "")
        + f"体积 {_gb(final.current)}，剩余配额 {_gb(final.remaining)}"
    )
    if meta.reader_overflows:
        _out(f"读者积压超限 {meta.reader_overflows} 次：录制时的实时结果与回放不一致")
    if not result.drained:
        _out("读者没有在时限内处理完已接纳的帧：live/ 里的实时输出不完整")
    _out(f"录制目录：{result.path}")
    _out(f"实时核：{result.core.stage.value}，退出码 {result.core.returncode}")
    return 0 if meta.state == "complete" else 1


async def _wait_for_stop(session: LiveSession, keeper: QuotaKeeper, seconds: float | None) -> str:
    loop = asyncio.get_running_loop()
    enter: asyncio.Future[None] = loop.create_future()

    def read_stdin() -> None:
        try:
            line = sys.stdin.readline()
        except (OSError, ValueError):
            return
        if line:  # 读到 EOF（stdin 不是终端）不当作回车
            loop.call_soon_threadsafe(lambda: enter.done() or enter.set_result(None))

    threading.Thread(target=read_stdin, name="录制的回车", daemon=True).start()
    started = time.monotonic()
    next_quota = started
    next_status = started + STATUS_INTERVAL
    previous = (started, session.status())
    pump = session.frames.done
    quota: QuotaStatus | None = None
    while True:
        now = time.monotonic()
        if enter.done():
            return "enter"
        if seconds is not None and now - started >= seconds:
            return "seconds"
        if pump is not None and pump.done():
            return "error"
        audio = session.audio_task
        if audio is not None and audio.done():
            return "error"
        if not session.link.running:
            _err("实时核已退出")
            return "core-exited"
        if now >= next_quota:
            next_quota = now + QUOTA_INTERVAL
            quota = await asyncio.to_thread(keeper.check, _report_delete, failed=_report_stuck)
            if quota.over:
                _out(f"已录满配额 {_gb(quota.quota)}，停止录制")
                return "quota"
        if now >= next_status:
            next_status = now + STATUS_INTERVAL
            current = session.status()
            _out(_status_line(previous, (now, current), quota))
            previous = (now, current)
        await asyncio.wait((enter,), timeout=0.2)


def _status_line(
    previous: tuple[float, SessionStatus],
    current: tuple[float, SessionStatus],
    quota: QuotaStatus | None,
) -> str:
    (then, before), (now, status) = previous, current
    elapsed = max(now - then, 1e-9)
    admitted = (status.admitted - before.admitted) / elapsed
    ring = (status.ring_latest - before.ring_latest) / elapsed
    capture = "—" if status.capture is None else status.capture.value
    audio = "—" if status.audio is None else status.audio.value
    size = "—" if quota is None else f"{_gb(quota.current)}，剩余配额 {_gb(quota.remaining)}"
    return (
        f"[{_clock(status.duration)}] 画面 {capture} · 声音 {audio} · "
        f"接纳 {admitted:.1f} 帧/秒（帧环 {ring:.1f}）· 在途 {status.pending} · "
        f"{status.frames} 帧、{status.audio_blocks} 块 · {size} · "
        f"积压超限 {status.overflows}"
    )


# 回放


def _resolve(argument: str, data_dir: Path) -> Path:
    """录制的写法：目录的路径、`<适配包 id>/<录制目录名>`，或 `<适配包 id>`（最新的完整录制）。"""
    path = Path(argument)
    if path.is_dir():
        return path.resolve()
    game, _, name = argument.replace("\\", "/").partition("/")
    root = recordings_root(data_dir, game)
    if name:
        return root / name
    complete = [s for s in list_recordings(root, sizes=False) if s.state == "complete"]
    if not complete:
        raise RecordingError(f"{root} 下没有完整的录制")
    return complete[-1].path


def replay_main(argv: Sequence[str] | None = None) -> int:
    """`uv run replay <录制>`。"""
    parser = argparse.ArgumentParser(
        prog="replay",
        description="Replay a recording through the same plugins (M0) and compare with the "
        "live results recorded alongside it.",
    )
    parser.add_argument("recording", help="recording directory, GAME/NAME, or GAME (latest)")
    parser.add_argument("--out", type=Path, help="output directory (default: <recording>/replay)")
    parser.add_argument("--data-dir", type=Path, help="local data directory (absolute)")
    parser.add_argument("--verbose", action="store_true", help="log at INFO level")
    args = parser.parse_args(argv)
    _setup_output(args.verbose)
    try:
        data_dir = _data_dir(args.data_dir)
        recording = Recording.open(_resolve(args.recording, data_dir))
    except (RecordingError, LocalDataError, ValueError) as error:
        _err(f"不能回放：{error}")
        return 2

    def progress(done: int, total: int) -> None:
        _out(f"回放 {done}/{total} 帧")

    try:
        result = asyncio.run(
            replay(recording, data_dir=data_dir, output=args.out, progress=progress)
        )
    except RecordingError as error:
        _err(f"回放失败：{error}")
        return 1
    _out(f"回放了 {result.frames} 帧、{result.blocks} 块，输出在 {result.output}")
    if result.adapter_changed:
        _out("注意：适配包与录制时不同")
    return 0 if _report_comparisons(result.comparisons) else 1


def _report_comparisons(comparisons: dict[str, Comparison]) -> bool:
    """打印各结果文件的比对；全部逐条相同（或没有实时输出）为真。"""
    if not comparisons:
        _out("录制里没有实时输出，不比对")
        return True
    identical = True
    for name, comparison in comparisons.items():
        if comparison.identical:
            _out(f"{name}：与录制时的实时输出逐条相同，{comparison.same} 条")
            continue
        identical = False
        _out(
            f"{name}：与录制时的实时输出不同，前 {comparison.same} 条相同，"
            f"实时 {comparison.live_lines} 条，回放 {comparison.replay_lines} 条"
        )
        difference = comparison.first_difference
        if difference is not None:
            _out(f"  第 {difference.line} 条：")
            _out(f"    实时：{json.dumps(difference.live, ensure_ascii=False)}")
            _out(f"    回放：{json.dumps(difference.replay, ensure_ascii=False)}")
    return identical


# 列出、导出与核对


def recording_main(argv: Sequence[str] | None = None) -> int:
    """`uv run recording <list|export-frames|export-audio|check>`。"""
    parser = argparse.ArgumentParser(
        prog="recording", description="List, export and check recordings."
    )
    parser.add_argument("--data-dir", type=Path, help="local data directory (absolute)")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="list recordings with sizes and the quota")
    listing.add_argument("game", nargs="?", help="adapter id (default: every game)")
    frames = commands.add_parser("export-frames", help="export frames as PNG")
    frames.add_argument("recording")
    choice = frames.add_mutually_exclusive_group(required=True)
    choice.add_argument("--frames", help="comma-separated frame numbers")
    choice.add_argument("--every", type=float, help="one frame every N seconds")
    choice.add_argument("--stable", action="store_true", help="first frame of each stable stretch")
    choice.add_argument("--all", action="store_true", help="every frame")
    for command in (frames,):
        command.add_argument("--start", type=float, help="seconds after the first frame")
        command.add_argument("--end", type=float, help="seconds after the first frame")
    audio = commands.add_parser("export-audio", help="export game audio as WAV")
    audio.add_argument("recording")
    audio.add_argument("--start", type=float, help="seconds after the first frame")
    audio.add_argument("--end", type=float, help="seconds after the first frame")
    audio.add_argument("--pcm16", action="store_true", help="16-bit integer instead of float")
    check = commands.add_parser("check", help="decode every frame and check the annotations")
    check.add_argument("recording")
    args = parser.parse_args(argv)
    _setup_output(False)
    try:
        data_dir = _data_dir(args.data_dir)
        if args.command == "list":
            return _list(data_dir, args.game)
        recording = Recording.open(_resolve(args.recording, data_dir))
        exports = exports_root(data_dir, recording.meta.game.id, recording.name)
        if args.command == "export-frames":
            numbers = () if args.frames is None else _numbers(args.frames)
            selection = FrameSelection(
                frames=numbers,
                every=args.every,
                stable=args.stable,
                all=args.all,
                start=args.start,
                end=args.end,
            )
            written = export_frames(recording, exports, select_frames(recording, selection))
            _out(f"导出了 {len(written)} 帧到 {exports / 'frames'}")
            return 0
        if args.command == "export-audio":
            exported = export_audio(
                recording, exports, start=args.start, end=args.end, pcm16=args.pcm16
            )
            _out(f"导出了 {exported.samples} 个采样点的声音到 {exported.path}")
            return 0
        return _check(recording)
    except (RecordingError, LocalDataError, ValueError) as error:
        _err(str(error))
        return 1


def _numbers(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in text.split(",") if part.strip())
    except ValueError:
        raise ValueError(f"--frames 须为逗号分隔的帧号：{text}") from None


def _list(data_dir: Path, game: str | None) -> int:
    games = (
        [game]
        if game is not None
        else sorted(p.name for p in (data_dir / RECORDINGS_DIR).glob("*") if p.is_dir())
    )
    for name in games:
        stored = list_recordings(recordings_root(data_dir, name))
        used = sum(s.size for s in stored)
        quota = _quota_text(name)
        _out(f"{name}：{len(stored)} 段，{_gb(used)}{quota}")
        for item in stored:
            meta = item.meta
            if meta is None:
                _out(f"  {item.name}  unreadable  {_gb(item.size)}")
                continue
            frames = meta.frames
            duration = 0.0
            if frames.first_capture_ns is not None and frames.last_capture_ns is not None:
                duration = (frames.last_capture_ns - frames.first_capture_ns) / 1e9
            _out(
                f"  {item.name}  {meta.state}  {_clock(duration)}  {frames.count} 帧  "
                f"{_gb(item.size)}"
            )
    return 0


def _quota_text(game: str) -> str:
    try:
        adapter: Adapter = load_adapter(GAMES_DIR / game)
    except AdapterError:
        return ""
    return f"，上限 {adapter.compliance.recording_quota_gb} GB"


def _check(recording: Recording) -> int:
    problems: list[str] = []
    if not recording.complete:
        problems.append(f"录制不完整：{recording.meta.state}")
    for index in range(len(recording.frames)):
        try:
            recording.frame(index)
        except RecordingError as error:
            problems.append(str(error))
    try:
        recording.blocks(0, len(recording.audio))
    except RecordingError as error:
        problems.append(str(error))
    try:
        annotations = load_annotations(recording.path)
        adapter = load_adapter(GAMES_DIR / recording.meta.game.id)
    except (ValueError, AdapterError) as error:
        problems.append(f"标注或适配包读不出：{error}")
    else:
        end = recording.audio_end_ns()
        audio_range = None if end is None else (recording.audio[0].capture_ns, end)
        problems += check_annotations(
            annotations,
            frame_numbers=recording.frame_numbers,
            adapter=adapter,
            audio_range=audio_range,
        )
    for problem in problems:
        _err(f"FAIL {problem}")
    if not problems:
        _out(
            f"ok   {recording.path}：{len(recording.frames)} 帧、"
            f"{len(recording.audio)} 块都能读出，标注合格式"
        )
    return 1 if problems else 0
