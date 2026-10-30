"""非提权端到端：非提权一侧对一个**非提权运行**的提权一侧程序，连接、握手、做 RPC、写帧都成功，
完整性级别报为非提权（Windows 上有 SID 与 RID，Linux 上为 unsupported）。

这证明工具链本身正确，不是 GS-03 的提权结论。真正的跨完整性级别由维护者在提权下跑。
Windows 走命名管道，Linux 容器走 UDS。
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from gs03_probe.build import build_probe
from gs03_probe.host import Host

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="session")
def probe_binary() -> Path:
    return build_probe(REPO_ROOT)


def test_nonelevated_core_probe_connects_writes_and_reports_identity(probe_binary: Path) -> None:
    async def main() -> None:
        async with Host(frames=5, write_cred=False) as host:
            env = {**os.environ, "GS03_CONN_INFO": json.dumps(host.conn_info())}
            process = subprocess.Popen(
                [os.fspath(probe_binary), "--conn-info-env", "GS03_CONN_INFO"],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
            )
            try:
                outcome = await host.wait(30)
                stdout, stderr = await asyncio.to_thread(process.communicate, None, 30)
            finally:
                if process.poll() is None:
                    process.kill()

        assert process.returncode == 0, stderr
        assert stdout.splitlines()[-1] == "ok", stdout
        assert outcome.connected
        assert outcome.role_matches
        assert outcome.hello_received
        assert outcome.frames_verified, outcome.frames_detail
        assert outcome.latest_frame_number == 5
        assert outcome.elevated_pid == process.pid
        assert outcome.errors == []

        identity = outcome.elevated_identity
        if sys.platform == "win32":
            # 非提权运行：与非提权一侧同一用户，完整性级别有 RID（通常为 medium）。
            assert identity["user_sid"] == outcome.host_user_sid
            assert identity["integrity_rid"] is not None
            assert identity["integrity_level"] != "unsupported"
        else:
            assert identity["integrity_level"] == "unsupported"

    asyncio.run(main())
