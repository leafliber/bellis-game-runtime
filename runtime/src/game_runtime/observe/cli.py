"""只读运行入口：`uv run observe <适配包 id> [--record [--name …]]`。

用法见 `docs/modules/offline-tools.md` 的“只读运行”。这是维护者在自己终端里运行的程序：按回车或
Ctrl+C 停止，Ctrl+C 以取消主任务的方式到达，照常按顺序收尾。
"""

import argparse
import asyncio
import dataclasses
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from game_runtime.adapter import GAMES_DIR, AdapterError, load_adapter
from game_runtime.core_link import CoreLinkOptions
from game_runtime.frames import DEFAULT_IDLE_INTERVAL, DEFAULT_MAX_RATE, AdmissionPolicy
from game_runtime.local_data import LocalDataError
from game_runtime.observe.display import Formatter, Recording, banner, status_line, summary
from game_runtime.observe.hub import Event, ObserveHub, PluginEvent
from game_runtime.observe.plugin import OBSERVER_ENTRY, observe_catalog
from game_runtime.observe.run import Sample, err, out, run_live
from game_runtime.recording import (
    LiveOptions,
    LiveSession,
    QuotaRefusedError,
    QuotaStatus,
    SessionResult,
    adapter_sha256,
    audio_config,
    capture_config,
    narrative_problem,
    quota_bytes,
)
from game_runtime.recording.cli import (
    build_realtime_core,
    data_dir_option,
    setup_output,
    size_option,
    tool_info,
)
from game_runtime.recording.layout import check_name

STATUS_INTERVAL: Final = 5.0
"""状态行的缺省间隔（秒）。"""


class ObserveMonitor:
    """只读运行的显示：事件逐行写出，开始、状态行与摘要见 `game_runtime.observe.display`。

    `hub` 交给 `observer` 插件；`events` 依次记下显示过的事件，供测试核对。
    """

    def __init__(self, write: Callable[[str], None] = out, *, origin_ns: int | None = None) -> None:
        self.write = write
        self.hub = ObserveHub(self._event, origin_ns=origin_ns)
        self.formatter = Formatter(self.hub)
        self.events: list[Event] = []
        self.session: LiveSession | None = None
        self._stopping = False

    def _event(self, event: Event) -> None:
        if isinstance(event, PluginEvent) and (self._stopping or self.session is None):
            # 开始时的状态由开始的几行列出；停止期间各条目依次停用，不逐个显示。
            return
        self.events.append(event)
        self.write(self.formatter.event(event))

    def started(self, session: LiveSession, quota: QuotaStatus | None) -> None:
        self.session = session
        options = session.options
        path = session.recording_path
        recording = (
            None if path is None or quota is None else Recording(path, quota.quota, quota.used)
        )
        for line in banner(
            self.formatter,
            options.adapter,
            frame_size=(options.capture.max_width, options.capture.max_height),
            recording=recording,
            plugins=session.snapshot(),
            narrative_problem=narrative_problem(options.adapter),
        ):
            self.write(line)

    def status(self, previous: Sample, current: Sample, quota: QuotaStatus | None) -> None:
        session = self.session
        scene = self.hub.scene
        queued = 0
        if session is not None:
            queued = session.frames.queued(0 if scene is None else scene.frame_number)
        recording_size: int | None = None
        if session is not None and session.options.record:
            recording_size = current[1].frame_bytes if quota is None else quota.current
        self.write(
            status_line(
                self.formatter, previous, current, queued=queued, recording_size=recording_size
            )
        )

    def stopping(self) -> None:
        self._stopping = True

    def finished(self, result: SessionResult) -> None:
        del result
        adapter = self.hub.adapter
        problem = None if adapter is None else narrative_problem(adapter)
        for line in summary(self.formatter, narrative_problem=problem):
            self.write(line)


async def observe(
    options: LiveOptions,
    *,
    quota: int | None,
    seconds: float | None = None,
    status_interval: float = STATUS_INTERVAL,
    monitor: ObserveMonitor | None = None,
) -> int:
    """只读运行到停止为止，返回退出码。`options.record` 为真时同时录制，`quota` 是配额；
    `options.plugins` 是感知插件的目录（缺省 `perception_catalog`），这里加上 `observer`。"""
    monitor = monitor or ObserveMonitor()
    live = dataclasses.replace(
        options,
        plugins=observe_catalog(monitor.hub, options.plugins),
        extra_entries=(*options.extra_entries, OBSERVER_ENTRY),
        on_transition=monitor.hub.transition,
    )
    return await run_live(
        live, monitor=monitor, quota=quota, seconds=seconds, status_interval=status_interval
    )


def _interval(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"须为秒数：{text}") from None
    if not 0 < value <= 3600:
        raise argparse.ArgumentTypeError(f"须在 0 到 3600 秒之间：{text}")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    """`uv run observe <适配包 id>`。"""
    origin = time.perf_counter_ns()
    parser = argparse.ArgumentParser(
        prog="observe",
        description="Run the realtime core and the perception plugins read-only on an adapter's "
        "window and show scenes, voice activity and the narrative record live. Sends no input "
        "and does not connect to Bellis. Press Enter or Ctrl+C to stop.",
    )
    parser.add_argument("game", help="adapter id (a directory under games/)")
    parser.add_argument("--record", action="store_true", help="record at the same time")
    parser.add_argument("--name", help="name of the recording (with --record)")
    parser.add_argument("--seconds", type=float, help="stop after this many seconds")
    parser.add_argument(
        "--status-interval",
        type=_interval,
        default=STATUS_INTERVAL,
        help=f"seconds between status lines (default {STATUS_INTERVAL:g})",
    )
    parser.add_argument("--max-rate", type=float, default=DEFAULT_MAX_RATE)
    parser.add_argument("--idle-interval", type=float, default=DEFAULT_IDLE_INTERVAL)
    parser.add_argument(
        "--max-size",
        type=size_option,
        help="frame ring size WIDTHxHEIGHT (default: the adapter's)",
    )
    parser.add_argument("--core", type=Path, help="an already built realtime-core executable")
    parser.add_argument("--data-dir", type=Path, help="local data directory (absolute)")
    parser.add_argument("--verbose", action="store_true", help="log at INFO level")
    args = parser.parse_args(argv)
    if args.name is not None and not args.record:
        parser.error("--name 只与 --record 一起用")
    setup_output(args.verbose)
    record: bool = args.record
    try:
        adapter = load_adapter(GAMES_DIR / args.game)
        quota = quota_bytes(adapter) if record else None
        data_dir = data_dir_option(args.data_dir)
        policy = AdmissionPolicy(args.max_rate, args.idle_interval)
        if args.name is not None:
            check_name(args.name)
    except (AdapterError, QuotaRefusedError, LocalDataError, ValueError) as error:
        err(f"不能只读运行：{error}")
        return 2
    core: Path | None = args.core
    if core is None:
        out("正在编译 realtime-core（release）……")
        try:
            core = build_realtime_core()
        except RuntimeError as error:
            err(str(error))
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
        record=record,
    )
    monitor = ObserveMonitor(origin_ns=origin)
    try:
        return asyncio.run(
            observe(
                options,
                quota=quota,
                seconds=args.seconds,
                status_interval=args.status_interval,
                monitor=monitor,
            )
        )
    except KeyboardInterrupt:
        err("已强制退出" + ("：录制停在 recording 状态，不完整" if record else ""))
        return 130
