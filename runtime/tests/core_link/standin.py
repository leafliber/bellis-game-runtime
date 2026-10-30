"""替身子进程：只用于验证主进程一侧的停止顺序与孙进程的清理，不连接核连接、不应答任何请求。

用法：`python standin.py <状态文件> [--ignore-stdin] [--grandchild]`

- 就绪后把 `{"pid": 本进程号, "grandchild": 孙进程号或 null}` 写进状态文件；
- 缺省读 stdin 直到 EOF 后以 0 退出；`--ignore-stdin` 时读到 EOF 也不退出，只能被强制终止；
- `--grandchild` 时先拉起一个一直睡眠的孙进程，自己退出时不管它。
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    state = Path(sys.argv[1])
    flags = set(sys.argv[2:])
    grandchild = None
    if "--grandchild" in flags:
        grandchild = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"], stdin=subprocess.DEVNULL
        ).pid
    partial = state.with_suffix(".partial")
    partial.write_text(json.dumps({"pid": os.getpid(), "grandchild": grandchild}))
    partial.replace(state)
    sys.stdin.buffer.read()
    if "--ignore-stdin" in flags:
        while True:
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
