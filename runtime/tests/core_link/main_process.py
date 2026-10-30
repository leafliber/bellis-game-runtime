"""跑主进程一侧代码的子进程，供“真的杀掉主进程”的测试使用（`test_link.py`、`test_actions.py`）。

用法：`python main_process.py <realtime-core 可执行文件> [<假显示面的 FakeTarget JSON>]`

以 `CoreLink` 拉起实时核，让一个控制产出者的心跳到达实时核，然后在标准输出打印一行
`core <实时核的进程号>`，再按标准输入的命令行事，直到被测试强制终止：

- `drop-job`：只关闭本进程持有的 job 句柄（主进程崩溃时系统也会关闭它），再等实时核发来 3 条
  `core.health`，打印 `core alive`；实时核在此期间退出则打印 `core dead`。
- `hold`：提交一块按住 W 键（扫描码 0x11）30 秒的动作块，等实时核报告持有输入后打印 `held`。
  需要给出假显示面，并在环境变量 `GAME_RUNTIME_TEST_DEVICE_PATH`、`..._KEY` 里给出测试设备
  （由测试进程运行），实时核才以假输入后端启动、有许可。

实时核继承本进程的 stderr，日志一并写在那里。
"""

import asyncio
import os
import sys
from pathlib import Path

from core_protocol import models
from core_protocol.models import CoreHealthParams, Producer
from game_runtime.channel import ChannelCredentials
from game_runtime.core_link import (
    ENV_TEST_DEVICE_KEY,
    ENV_TEST_DEVICE_PATH,
    CaptureConfig,
    CoreLink,
    CoreLinkOptions,
)
from game_runtime.kernel import ControlGrant

HOLD_SCAN_CODE = 0x11


def options() -> CoreLinkOptions:
    capture = None
    if len(sys.argv) > 2:
        target = models.FakeTarget.model_validate_json(sys.argv[2])
        surface = target.surface
        capture = CaptureConfig(
            target=target, max_width=surface.max_width, max_height=surface.max_height
        )
    path, key = os.environ.get(ENV_TEST_DEVICE_PATH), os.environ.get(ENV_TEST_DEVICE_KEY)
    device = None if path is None or key is None else ChannelCredentials.decode(path, key)
    return CoreLinkOptions(
        Path(sys.argv[1]),
        health_interval=0.05,
        health_timeout=1.0,
        capture=capture,
        test_device=device,
    )


async def main() -> None:
    producing = asyncio.Event()
    holding = asyncio.Event()
    healths: asyncio.Queue[CoreHealthParams] = asyncio.Queue()

    def on_health(health: CoreHealthParams) -> None:
        healths.put_nowait(health)
        if health.producer == Producer(id="step", generation=1):
            producing.set()
        if health.inputs_held:
            holding.set()

    link = CoreLink(options(), on_health=on_health)
    # 只在这个测试进程里用的提交资格：代次 1 一直有效，直到进程被杀。
    grant = ControlGrant("step", "step#1", 1, lambda: True, link.submit)
    link.bind_control(lambda: grant)
    await link.start()
    await asyncio.wait_for(producing.wait(), 10)
    print(f"core {link.pid}", flush=True)

    loop = asyncio.get_running_loop()
    while True:
        command = (await loop.run_in_executor(None, sys.stdin.readline)).strip()
        if command == "drop-job":
            instance = link._instance  # pyright: ignore[reportPrivateUsage]
            assert instance is not None
            instance.process._job.close()  # pyright: ignore[reportPrivateUsage, reportOptionalMemberAccess]
            while not healths.empty():
                healths.get_nowait()
            try:
                for _ in range(3):
                    await asyncio.wait_for(healths.get(), 1)
                alive = link.running
            except TimeoutError:
                alive = False
            print("core alive" if alive else "core dead", flush=True)
        elif command == "hold":
            ring = link.frames
            assert ring is not None
            frame = ring.read_latest()
            assert frame is not None
            held = models.HeldInput(root=models.HeldKey(scan_code=HOLD_SCAN_CODE))
            step = models.ControlStep(offset_ns=0, held=[held], actions=[])
            submission = await grant.submit([step], base_frame=frame.frame_number, valid_for=30)
            assert isinstance(submission.result, models.Accepted), submission.result
            await asyncio.wait_for(holding.wait(), 10)
            print("held", flush=True)
        elif not command:
            await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
