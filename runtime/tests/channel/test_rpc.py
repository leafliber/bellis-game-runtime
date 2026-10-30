"""JSON-RPC 2.0 对端，与 Bellis 的 `packages/channel/test/rpc.test.ts` 对应。

传输与这里无关，两端以 socketpair 相连并完成真实的握手。
"""

import asyncio
import socket
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

import pytest
from game_runtime.channel import (
    ChannelError,
    Connection,
    JsonMessage,
    RpcError,
    RpcErrorCode,
    RpcPeer,
    create_session_key,
)


@dataclass
class Connected:
    client: Connection
    server: Connection

    async def close(self) -> None:
        await self.client.close()
        await self.server.close()


async def connected(**server_options: Any) -> Connected:
    a, b = socket.socketpair()
    server_reader, server_writer = await asyncio.open_connection(sock=a)
    client_reader, client_writer = await asyncio.open_connection(sock=b)
    key = create_session_key()
    accepting = asyncio.create_task(
        Connection.accept(server_reader, server_writer, key, **server_options)
    )
    client = await Connection.open(client_reader, client_writer, "test.client", key)
    return Connected(client, await accepting)


def run(main: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(main())


async def rpc_pair(**server_options: Any) -> tuple[RpcPeer, RpcPeer, Connected]:
    pair = await connected()
    client = RpcPeer(pair.client, timeout=1.0)
    server = RpcPeer(pair.server, timeout=1.0, **server_options)
    return client, server, pair


async def close(*peers: RpcPeer) -> None:
    for peer in peers:
        await peer.close()


def test_requests_in_both_directions() -> None:
    async def main() -> None:
        client, server, _ = await rpc_pair()
        server.handle("math.add", lambda params: params["a"] + params["b"])
        client.handle("host.ping", lambda _: None)
        try:
            assert await client.request("math.add", {"a": 1, "b": 2}) == 3
            assert await server.request("host.ping") is None
        finally:
            await close(client, server)

    run(main)


def test_unknown_method() -> None:
    async def main() -> None:
        client, server, _ = await rpc_pair()
        try:
            with pytest.raises(RpcError) as raised:
                await client.request("no.such")
            assert raised.value.code == RpcErrorCode.METHOD_NOT_FOUND
        finally:
            await close(client, server)

    run(main)


def test_handler_errors() -> None:
    async def main() -> None:
        client, server, _ = await rpc_pair()

        def fail(_: dict[str, Any]) -> object:
            raise RpcError(1001, "不允许", {"reason": "x"})

        def crash(_: dict[str, Any]) -> object:
            raise RuntimeError("内部细节")

        server.handle("a.fail", fail)
        server.handle("a.crash", crash)
        try:
            with pytest.raises(RpcError) as raised:
                await client.request("a.fail")
            assert (raised.value.code, raised.value.message, raised.value.data) == (
                1001,
                "不允许",
                {"reason": "x"},
            )
            with pytest.raises(RpcError) as raised:
                await client.request("a.crash")
            assert raised.value.code == RpcErrorCode.INTERNAL_ERROR
            assert "内部细节" not in raised.value.message
        finally:
            await close(client, server)

    run(main)


def test_timeout_cancels_the_handler() -> None:
    async def main() -> None:
        client, server, _ = await rpc_pair()
        cancelled = asyncio.Event()

        async def hang(_: dict[str, Any]) -> object:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return None

        server.handle("a.hang", hang)
        try:
            with pytest.raises(ChannelError) as raised:
                await client.request("a.hang", {}, timeout=0.05)
            assert raised.value.code == "timeout"
            await asyncio.wait_for(cancelled.wait(), 2)
        finally:
            await close(client, server)

    run(main)


def test_caller_can_cancel() -> None:
    async def main() -> None:
        client, server, _ = await rpc_pair()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def hang(_: dict[str, Any]) -> object:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return None

        server.handle("a.hang", hang)
        try:
            pending = asyncio.create_task(client.request("a.hang"))
            await asyncio.wait_for(started.wait(), 2)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            await asyncio.wait_for(cancelled.wait(), 2)
        finally:
            await close(client, server)

    run(main)


def test_close_fails_pending_and_cancels_inflight() -> None:
    async def main() -> None:
        client, server, _ = await rpc_pair()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def hang(_: dict[str, Any]) -> object:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return None

        server.handle("a.hang", hang)
        pending = asyncio.create_task(client.request("a.hang"))
        await asyncio.wait_for(started.wait(), 2)
        await server.close()
        with pytest.raises(ChannelError) as raised:
            await pending
        assert raised.value.code == "closed"
        await asyncio.wait_for(cancelled.wait(), 2)
        assert await asyncio.wait_for(client.closed, 2) is None
        await client.close()

    run(main)


def test_notifications() -> None:
    async def main() -> None:
        client, server, _ = await rpc_pair()
        got: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        server.on_notification("host.heartbeat", got.put_nowait)
        try:
            client.notify("host.heartbeat", {"seq": 7})
            client.notify("host.heartbeat")
            assert await asyncio.wait_for(got.get(), 2) == {"seq": 7}
            assert await asyncio.wait_for(got.get(), 2) == {}
        finally:
            await close(client, server)

    run(main)


def test_invalid_message_gets_invalid_request() -> None:
    async def main() -> None:
        pair = await connected()
        server = RpcPeer(pair.server, timeout=1.0)
        try:
            pair.client.send({"jsonrpc": "2.0", "method": "Bad Name", "id": 1})
            assert await pair.client.receive(timeout=2) == JsonMessage(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": RpcErrorCode.INVALID_REQUEST, "message": "Invalid Request"},
                }
            )
        finally:
            await server.close()
            await pair.close()

    run(main)


def test_duplicate_request_id_and_busy() -> None:
    async def main() -> None:
        pair = await connected()
        server = RpcPeer(pair.server, timeout=1.0, max_inflight=2)

        async def hang(_: dict[str, Any]) -> object:
            await asyncio.Event().wait()
            return None

        server.handle("a.hang", hang)
        try:
            for request_id in (1, 1, 2, 3):
                pair.client.send({"jsonrpc": "2.0", "id": request_id, "method": "a.hang"})
            first = await pair.client.receive(timeout=2)
            second = await pair.client.receive(timeout=2)
            assert first == JsonMessage(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {
                        "code": RpcErrorCode.INVALID_REQUEST,
                        "message": "Duplicate request id",
                    },
                }
            )
            assert second == JsonMessage(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "error": {"code": RpcErrorCode.SERVER_BUSY, "message": "Server busy"},
                }
            )
        finally:
            await server.close()
            await pair.close()

    run(main)


def test_oversize_result_becomes_internal_error() -> None:
    async def main() -> None:
        pair = await connected(max_frame_bytes=128)
        client = RpcPeer(pair.client, timeout=1.0)
        server = RpcPeer(pair.server, timeout=1.0)
        server.handle("a.big", lambda _: "x" * 1000)
        try:
            with pytest.raises(RpcError) as raised:
                await client.request("a.big")
            assert raised.value.code == RpcErrorCode.INTERNAL_ERROR
        finally:
            await close(client, server)

    run(main)


def test_binary_frames_are_ignored() -> None:
    async def main() -> None:
        client, server, pair = await rpc_pair()
        server.handle("math.add", lambda params: params["a"] + params["b"])
        try:
            pair.client.send_binary(b"\x00\x01")
            assert await client.request("math.add", {"a": 2, "b": 2}) == 4
        finally:
            await close(client, server)

    run(main)


def test_handler_registration() -> None:
    async def main() -> None:
        client, server, _ = await rpc_pair()
        try:
            dispose = server.handle("a.b", lambda _: 1)
            with pytest.raises(ValueError, match="已有处理函数"):
                server.handle("a.b", lambda _: 2)
            dispose()
            server.handle("a.b", lambda _: 2)
            assert await client.request("a.b") == 2
            with pytest.raises(ValueError, match="保留"):
                server.handle("rpc.x", lambda _: 0)
            with pytest.raises(ValueError, match="保留"):
                server.on_notification("rpc.x", lambda _: None)
        finally:
            await close(client, server)

    run(main)
