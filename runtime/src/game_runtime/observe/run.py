"""实时运行的运行循环，录制工具（`uv run record`）与只读运行入口（`uv run observe`）共用：开始，等到
按回车、Ctrl+C、到时、录满配额、实时核退出或出错，然后按顺序收尾。终端上显示什么由 `Monitor` 决定。

设计见 `docs/modules/offline-tools.md` 的“录制工具”与“只读运行”。Ctrl+C 以取消主任务的方式到达，
照常按顺序收尾；实时核在自己的进程组里，由 `LiveSession.stop` 按 R-06 停。
"""

import asyncio
import sys
import threading
import time
from datetime import timedelta
from typing import Final, Protocol

from game_runtime.core_link import CoreStartError
from game_runtime.observe.display import size
from game_runtime.recording import (
    LiveOptions,
    LiveSession,
    QuotaKeeper,
    QuotaStatus,
    SessionResult,
    SessionStatus,
    StoredRecording,
    recordings_root,
)

STATUS_INTERVAL: Final = 2.0
"""录制工具的状态行间隔（秒）。"""
QUOTA_INTERVAL: Final = 5.0
"""录制期间核对配额的间隔（秒）。"""
NORMAL_STOPS: Final = frozenset({"enter", "interrupt", "seconds"})
"""不录制时退出码为 0 的停止原因。"""

type Sample = tuple[float, SessionStatus]
"""（`time.monotonic()`，此刻的状态）。"""


def out(line: str = "") -> None:
    print(line, flush=True)


def err(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


class Monitor(Protocol):
    """终端上显示什么。"""

    def started(self, session: LiveSession, quota: QuotaStatus | None) -> None:
        """开始之后打印开始的几行；`quota` 是录制开始时的配额（不录制为 None）。"""
        ...

    def status(self, previous: Sample, current: Sample, quota: QuotaStatus | None) -> None:
        """打印一行状态；`quota` 是最近一次核对的配额。"""
        ...

    def stopping(self) -> None:
        """开始收尾。"""
        ...

    def finished(self, result: SessionResult) -> None:
        """收尾之后、打印录制与实时核的结果之前。"""
        ...


def report_delete(stored: StoredRecording) -> None:
    out(f"超出录制配额，删除最旧的录制：{stored.path}（{stored.state}，{size(stored.size)}）")


def report_stuck(stored: StoredRecording) -> None:
    err(f"没能删干净 {stored.path}，剩下 {size(stored.size)} 仍计入用量")


async def run_live(
    options: LiveOptions,
    *,
    monitor: Monitor,
    quota: int | None,
    seconds: float | None = None,
    status_interval: float = STATUS_INTERVAL,
) -> int:
    """运行到停止为止，然后收尾，返回退出码。录制时 `quota` 是配额（字节）。"""
    keeper: QuotaKeeper | None = None
    initial: QuotaStatus | None = None
    if options.record:
        if quota is None:
            raise ValueError("录制须给出配额")
        keeper = QuotaKeeper(recordings_root(options.data_dir, options.adapter.id), quota)
        initial = await asyncio.to_thread(keeper.check, report_delete, failed=report_stuck)
    session = LiveSession(options)
    try:
        await session.start()
    except (CoreStartError, RuntimeError) as error:
        err(f"没能开始{'录制' if options.record else '只读运行'}：{error}")
        return 1
    if keeper is not None:
        keeper.set_current(session.path)
    monitor.started(session, initial)
    reason = "error"
    try:
        reason = await _wait_for_stop(session, keeper, seconds, monitor, status_interval)
        out(f"正在收尾（{reason}）……")
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None:
            task.uncancel()
        reason = "interrupt"
        out("")
        tail = "，录制将不完整" if options.record else ""
        out(f"收到 Ctrl+C，正在收尾（再按一次即强制退出{tail}）……")
    finally:
        # 等待中出了别的错也照样收尾，再让错误传出去。
        monitor.stopping()
        result = await session.stop(reason)
    monitor.finished(result)
    code = _report(result, keeper, reason)
    out(f"实时核：{result.core.stage.value}，退出码 {result.core.returncode}")
    return code


def _report(result: SessionResult, keeper: QuotaKeeper | None, reason: str) -> int:
    """录制的结果几行（不录制时一行），返回退出码。"""
    meta = result.meta
    if meta is None or keeper is None or result.path is None:
        out(f"已停止（{reason}）")
        if not result.drained:
            out("插件没有在时限内处理完已接纳的帧：结果不完整")
        return 0 if reason in NORMAL_STOPS else 1
    audio = meta.audio
    final = keeper.check(report_delete, failed=report_stuck)
    duration = 0.0
    if meta.frames.first_capture_ns is not None and meta.frames.last_capture_ns is not None:
        duration = (meta.frames.last_capture_ns - meta.frames.first_capture_ns) / 1e9
    out(
        f"已停止（{meta.stop_reason}）：录制 {meta.state}，时长 {_hms(duration)}，"
        f"帧 {meta.frames.count}（关键帧 {meta.frames.keyframes}），"
        + (f"声音 {audio.blocks} 块（空缺 {audio.gaps}），" if audio is not None else "")
        + f"体积 {size(final.current)}，剩余配额 {size(final.remaining)}"
    )
    if meta.reader_overflows:
        out(f"读者积压超限 {meta.reader_overflows} 次：录制时的实时结果与回放不一致")
    if not result.drained:
        out("读者没有在时限内处理完已接纳的帧：live/ 里的实时输出不完整")
    out(f"录制目录：{result.path}")
    return 0 if meta.state == "complete" else 1


def _hms(seconds: float) -> str:
    return str(timedelta(seconds=round(seconds)))


async def _wait_for_stop(
    session: LiveSession,
    keeper: QuotaKeeper | None,
    seconds: float | None,
    monitor: Monitor,
    interval: float,
) -> str:
    loop = asyncio.get_running_loop()
    enter: asyncio.Future[None] = loop.create_future()

    def read_stdin() -> None:
        try:
            line = sys.stdin.readline()
        except (OSError, ValueError):
            return
        if line:  # 读到 EOF（stdin 不是终端）不当作回车
            loop.call_soon_threadsafe(lambda: enter.done() or enter.set_result(None))

    threading.Thread(target=read_stdin, name="停止的回车", daemon=True).start()
    started = time.monotonic()
    next_quota = started
    next_status = started + interval
    previous: Sample = (started, session.status())
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
            err("实时核已退出")
            return "core-exited"
        if keeper is not None and now >= next_quota:
            next_quota = now + QUOTA_INTERVAL
            quota = await asyncio.to_thread(keeper.check, report_delete, failed=report_stuck)
            if quota.over:
                out(f"已录满配额 {size(quota.quota)}，停止录制")
                return "quota"
        if now >= next_status:
            next_status = now + interval
            current: Sample = (now, session.status())
            monitor.status(previous, current, quota)
            previous = current
        await asyncio.wait((enter,), timeout=0.2)
