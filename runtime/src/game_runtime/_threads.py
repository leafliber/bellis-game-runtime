"""把阻塞的工作放进工作线程，不阻塞事件循环。"""

import asyncio
from collections.abc import Callable


async def in_thread[T](work: Callable[[], T]) -> T:
    """在缺省的线程池里运行 `work` 并等它的结果。

    线程不能中断：被取消时先等 `work` 做完再交出取消，这样调用方停下时不会留下还在读帧环等共享
    资源的线程。`work` 的耗时就是取消被推迟的上限。
    """
    future = asyncio.get_running_loop().run_in_executor(None, work)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        while not future.done():
            try:
                await asyncio.wait((future,))
            except asyncio.CancelledError:
                continue
        if not future.cancelled():
            future.exception()  # 已取消就不要结果了，取走异常以免事件循环报告未取走的异常
        raise
