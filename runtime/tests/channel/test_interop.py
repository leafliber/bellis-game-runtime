"""Python 服务端与 Rust 客户端互通：pytest 拉起 `bellis-channel` 的 `interop_client` 示例。

Windows 上走命名管道，其它平台走 UDS。示例的脚本见
`core/crates/bellis-channel/examples/interop_client.rs`。
"""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from game_runtime.channel import (
    ChannelCredentials,
    ChannelError,
    Connection,
    RpcPeer,
    create_session_key,
    encode_session_key,
    listen,
)

CORE = Path(__file__).resolve().parents[3] / "core"
CORE_ROLE = "game-runtime.core"


@pytest.fixture(scope="session")
def interop_client() -> Path:
    """编译示例并返回可执行文件的路径；尊重 CARGO_TARGET_DIR（由 cargo 自己读取）。"""
    cargo = shutil.which("cargo")
    assert cargo is not None, "PATH 上没有 cargo"
    argv = [
        cargo,
        "build",
        "--locked",
        "-p",
        "bellis-channel",
        "--example",
        "interop_client",
        "--message-format=json",
    ]
    with subprocess.Popen(
        argv,
        cwd=CORE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    ) as process:
        stdout, stderr = process.communicate(timeout=600)
    assert process.returncode == 0, stderr
    for line in stdout.splitlines():
        message = json.loads(line)
        if (
            message.get("reason") == "compiler-artifact"
            and message["target"]["name"] == "interop_client"
            and message.get("executable")
        ):
            return Path(message["executable"])
    raise AssertionError("cargo 没有给出 interop_client 的路径")


def run_client(binary: Path, path: str, key: bytes) -> "subprocess.Popen[str]":
    env = {**os.environ, "BELLIS_CHANNEL_PATH": path, "BELLIS_CHANNEL_KEY": encode_session_key(key)}
    return subprocess.Popen(
        [os.fspath(binary), "session"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )


async def finish(process: "subprocess.Popen[str]") -> tuple[int, list[str], str]:
    stdout, stderr = await asyncio.to_thread(process.communicate, None, 30)
    return process.returncode, stdout.splitlines(), stderr


def test_python_server_and_rust_client(
    interop_client: Path, credentials: ChannelCredentials
) -> None:
    async def main() -> None:
        loop = asyncio.get_running_loop()
        connected: asyncio.Future[Connection] = loop.create_future()
        hello: asyncio.Future[dict[str, Any]] = loop.create_future()
        done: asyncio.Future[dict[str, Any]] = loop.create_future()
        peers: list[RpcPeer] = []
        problems: list[BaseException] = []

        def on_connection(connection: Connection) -> None:
            peer = RpcPeer(connection, timeout=5.0)
            peer.handle("interop.add", lambda params: params["a"] + params["b"])

            def on_done(params: dict[str, Any]) -> object:
                done.set_result(params)
                return {"received": True}

            peer.handle("interop.done", on_done)
            peer.on_notification("interop.hello", hello.set_result)
            peers.append(peer)
            connected.set_result(connection)

        server = await listen(
            credentials.path,
            credentials.key,
            on_connection=on_connection,
            on_rejected=problems.append,
            on_error=problems.append,
        )
        process = run_client(interop_client, credentials.path, credentials.key)
        try:
            connection = await asyncio.wait_for(connected, 10)
            assert connection.role == CORE_ROLE
            assert await asyncio.wait_for(hello, 10) == {"from": "rust"}
            # 服务端发起请求与通知，Rust 一侧应答与接收。
            peer = peers[0]
            echoed = await peer.request("interop.echo", {"x": [1, "二"], "y": None})
            assert echoed == {"x": [1, "二"], "y": None}
            peer.notify("interop.note", {"seq": 7})
            assert await asyncio.wait_for(done, 10) == {"note": {"seq": 7}}
            returncode, lines, stderr = await finish(process)
            assert returncode == 0, stderr
            assert lines == [
                f"open {CORE_ROLE}",
                "add 5",
                'note {"seq":7}',
                'done {"received":true}',
                "ok",
            ]
            assert await asyncio.wait_for(peer.closed, 5) is None
            assert problems == []
        finally:
            if process.poll() is None:
                process.kill()
                await finish(process)
            for peer in peers:
                await peer.close()
            await server.close()

    asyncio.run(main())


def test_rust_client_rejects_wrong_key(
    interop_client: Path, credentials: ChannelCredentials
) -> None:
    async def main() -> None:
        accepted: list[Connection] = []
        rejected: asyncio.Queue[ChannelError] = asyncio.Queue()
        errors: list[BaseException] = []
        server = await listen(
            credentials.path,
            credentials.key,
            on_connection=accepted.append,
            on_rejected=rejected.put_nowait,
            on_error=errors.append,
        )
        process = run_client(interop_client, credentials.path, create_session_key())
        try:
            returncode, lines, _ = await finish(process)
            assert returncode == 1
            assert lines == ["error auth-failed"]
            # 客户端核验服务端失败后直接断开，不回送证明；服务端不交出连接。
            assert (await asyncio.wait_for(rejected.get(), 5)).code == "closed"
            assert accepted == []
            assert errors == []
        finally:
            if process.poll() is None:
                process.kill()
                await finish(process)
            await server.close()

    asyncio.run(main())
