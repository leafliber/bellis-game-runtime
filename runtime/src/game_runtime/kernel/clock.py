"""时钟：内核的全部计时（截止时间、退避）都经过它，测试注入手动推进的时钟，不真睡。"""

import asyncio
from collections.abc import Awaitable
from typing import Protocol


class Clock(Protocol):
    async def sleep(self, seconds: float) -> None:
        """在单调时钟上等待 `seconds` 秒。"""
        ...


class MonotonicClock:
    """真实时钟：asyncio 事件循环的单调时钟。"""

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


async def finished_within[T](clock: Clock, task: asyncio.Future[T], seconds: float) -> bool:
    """等 `task` 至多 `seconds` 秒，返回它是否已结束。到时不取消 `task`。"""
    if task.done():
        return True
    timer = asyncio.ensure_future(clock.sleep(seconds))
    try:
        await asyncio.wait((task, timer), return_when=asyncio.FIRST_COMPLETED)
    finally:
        timer.cancel()
    return task.done()


async def run_with_deadline[T](clock: Clock, work: Awaitable[T], seconds: float) -> T:
    """运行 `work`，超过 `seconds` 秒即取消它并抛 TimeoutError。"""
    task = asyncio.ensure_future(work)
    try:
        finished = await finished_within(clock, task, seconds)
    except asyncio.CancelledError:
        task.cancel()
        raise
    if not finished:
        task.cancel()
        # 插件吞掉取消时它会在后台跑完，取走结果以免事件循环报告未取走的异常。
        task.add_done_callback(_consume)
        raise TimeoutError(f"未在 {seconds:g} 秒内完成")
    return task.result()


def _consume[T](task: asyncio.Future[T]) -> None:
    if not task.cancelled():
        task.exception()
