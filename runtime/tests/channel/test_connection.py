"""真实传输上的握手与拒绝：Windows 上是命名管道，其它平台是 UDS。与 Bellis 的
`packages/channel/test/connection.test.ts` 对应，另加重放、读超时与协议标识不符。"""

import asyncio
import contextlib
import json
import stat
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from game_runtime.channel import (
    BinaryMessage,
    ChannelCredentials,
    ChannelError,
    ChannelServer,
    Connection,
    FrameDecoder,
    FrameKind,
    JsonMessage,
    connect,
    create_session_key,
    encode_frame,
    listen,
)
from game_runtime.channel.frame import Frame
from game_runtime.channel.handshake import b64url, mac
from game_runtime.channel.transport import open_stream, start_listener

type Body = dict[str, Any] | bytes

ROLE = "test.client"


@dataclass
class Served:
    """一个监听中的服务端，以及它交出的连接与拒绝。"""

    server: ChannelServer
    accepted: list[Connection]
    rejected: asyncio.Queue[ChannelError]
    errors: list[BaseException]

    async def next_connection(self, timeout: float = 2.0) -> Connection:
        async with asyncio.timeout(timeout):
            while not self.accepted:
                await asyncio.sleep(0.005)
        return self.accepted.pop(0)

    async def next_rejection(self, timeout: float = 2.0) -> ChannelError:
        async with asyncio.timeout(timeout):
            return await self.rejected.get()


type Serve = Callable[..., Awaitable[Served]]


@pytest.fixture
def serve(credentials: ChannelCredentials) -> Serve:
    """在 `credentials` 的地址上监听；测试结束前须 `await served.server.close()`。"""

    async def start(**options: Any) -> Served:
        accepted: list[Connection] = []
        rejected: asyncio.Queue[ChannelError] = asyncio.Queue()
        errors: list[BaseException] = []
        server = await listen(
            credentials.path,
            credentials.key,
            on_connection=accepted.append,
            on_rejected=rejected.put_nowait,
            on_error=errors.append,
            **options,
        )
        return Served(server, accepted, rejected, errors)

    return start


@dataclass
class Pair:
    client: Connection
    server: Connection
    served: Served

    async def close(self) -> None:
        await self.client.close()
        await self.served.server.close()


type ConnectPair = Callable[..., Awaitable[Pair]]


@pytest.fixture
def connected_pair(credentials: ChannelCredentials, serve: Serve) -> ConnectPair:
    """一对已认证的连接；测试结束前须 `await pair.close()`。"""

    async def start(**options: Any) -> Pair:
        served = await serve(**options)
        client_options = {k: v for k, v in options.items() if k != "max_pending_handshakes"}
        client = await connect(credentials.path, ROLE, credentials.key, **client_options)
        return Pair(client, await served.next_connection(), served)

    return start


class RawPeer:
    """不做握手的原始对端，用来构造攻击和异常输入。"""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self._decoder = FrameDecoder(1 << 30)

    @classmethod
    async def connect(cls, path: str) -> "RawPeer":
        return cls(*await open_stream(path))

    def send(self, kind: FrameKind, body: Body) -> None:
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.writer.write(encode_frame(kind, payload))

    async def frame(self, timeout: float = 2.0) -> Frame | None:
        """下一帧；对端关闭时返回 None。"""
        async with asyncio.timeout(timeout):
            while (frame := self._decoder.read()) is None:
                try:
                    chunk = await self.reader.read(65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    return None
                self._decoder.push(chunk)
            return frame

    async def json(self) -> dict[str, Any]:
        frame = await self.frame()
        assert frame is not None, "对端已关闭"
        return json.loads(frame.payload)

    async def frames_until_closed(self) -> int:
        count = 0
        while await self.frame() is not None:
            count += 1
        return count

    async def close(self) -> None:
        self.writer.close()
        with contextlib.suppress(OSError):
            await self.writer.wait_closed()


def nonce() -> str:
    return b64url(create_session_key())


def hello(client_nonce: str, role: str = "test.raw", **extra: object) -> dict[str, Any]:
    return {"v": 1, "type": "hello", "role": role, "nonce": client_nonce, **extra}


async def raw_handshake(
    raw: RawPeer, key: bytes, role: str = "test.raw", domain: str = "bellis-channel/1"
) -> tuple[bytes, bytes]:
    """以原始对端完成握手，返回发出的 hello 帧与 proof 帧。"""
    client_nonce = nonce()
    hello_frame = encode_frame(FrameKind.HANDSHAKE, json.dumps(hello(client_nonce, role)).encode())
    raw.writer.write(hello_frame)
    challenge = await raw.json()
    proof = b64url(mac(key, "client", role, client_nonce, challenge["nonce"], domain=domain))
    proof_frame = encode_frame(
        FrameKind.HANDSHAKE, json.dumps({"v": 1, "type": "proof", "proof": proof}).encode()
    )
    raw.writer.write(proof_frame)
    return hello_frame, proof_frame


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def test_authenticated_json_and_binary(connected_pair: ConnectPair) -> None:
    async def main() -> None:
        pair = await connected_pair()
        try:
            assert pair.server.role == ROLE
            pair.client.send({"text": "你好"})
            assert await pair.server.receive(timeout=2) == JsonMessage({"text": "你好"})
            pair.server.send_binary(b"\x01\x02\x03")
            assert await pair.client.receive(timeout=2) == BinaryMessage(b"\x01\x02\x03")
            await pair.client.close()
            with pytest.raises(ChannelError) as raised:
                await pair.server.receive(timeout=2)
            assert raised.value.code == "closed"
        finally:
            await pair.close()

    run(main())


@pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 上")
def test_windows_uses_named_pipes(credentials: ChannelCredentials) -> None:
    assert credentials.path.startswith("\\\\.\\pipe\\bellis-t")


@pytest.mark.skipif(sys.platform == "win32", reason="只在 POSIX 上")
def test_posix_socket_lives_in_a_private_directory(credentials: ChannelCredentials) -> None:
    directory = Path(credentials.path).parent
    assert directory.parent == Path(tempfile.gettempdir())
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert Path(credentials.path).name.startswith("bellis-t")


def test_client_rejects_server_without_the_key(
    credentials: ChannelCredentials, serve: Serve
) -> None:
    async def main() -> None:
        served = await serve()
        try:
            with pytest.raises(ChannelError) as raised:
                await connect(credentials.path, ROLE, create_session_key())
            assert raised.value.code == "auth-failed"
            await served.next_rejection()
            assert served.accepted == []
        finally:
            await served.server.close()

    run(main())


def test_client_sends_nothing_more_to_a_squatter(credentials: ChannelCredentials) -> None:
    async def main() -> None:
        received: asyncio.Queue[int] = asyncio.Queue()

        async def squat(raw: RawPeer) -> None:
            await raw.frame()
            raw.send(
                FrameKind.HANDSHAKE,
                {"v": 1, "type": "challenge", "nonce": nonce(), "proof": nonce()},
            )
            await received.put(1 + await raw.frames_until_closed())
            await raw.close()

        tasks: set[asyncio.Task[None]] = set()

        def on_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            tasks.add(asyncio.get_running_loop().create_task(squat(RawPeer(reader, writer))))

        squatter = await start_listener(credentials.path, on_stream)
        try:
            with pytest.raises(ChannelError) as raised:
                await connect(credentials.path, ROLE, credentials.key)
            assert raised.value.code == "auth-failed"
            assert await asyncio.wait_for(received.get(), 2) == 1  # 只有 hello
        finally:
            await squatter.close()
            await asyncio.gather(*tasks)

    run(main())


def test_server_rejects_wrong_client_proof(credentials: ChannelCredentials, serve: Serve) -> None:
    async def main() -> None:
        served = await serve()
        raw = await RawPeer.connect(credentials.path)
        try:
            raw.send(FrameKind.HANDSHAKE, hello(nonce()))
            assert (await raw.json())["type"] == "challenge"
            raw.send(FrameKind.HANDSHAKE, {"v": 1, "type": "proof", "proof": nonce()})
            assert (await served.next_rejection()).code == "auth-failed"
            assert await raw.frames_until_closed() == 0  # 没有 accept
        finally:
            await raw.close()
            await served.server.close()

    run(main())


def test_server_rejects_reflected_proof(credentials: ChannelCredentials, serve: Serve) -> None:
    async def main() -> None:
        served = await serve()
        raw = await RawPeer.connect(credentials.path)
        try:
            raw.send(FrameKind.HANDSHAKE, hello(nonce()))
            challenge = await raw.json()
            raw.send(FrameKind.HANDSHAKE, {"v": 1, "type": "proof", "proof": challenge["proof"]})
            assert (await served.next_rejection()).code == "auth-failed"
        finally:
            await raw.close()
            await served.server.close()

    run(main())


def test_server_rejects_replayed_handshake(credentials: ChannelCredentials, serve: Serve) -> None:
    async def main() -> None:
        served = await serve()
        try:
            first = await RawPeer.connect(credentials.path)
            recorded = await raw_handshake(first, credentials.key)
            assert (await first.json()) == {"v": 1, "type": "accept"}
            await (await served.next_connection()).close()
            await first.close()
            # 原样重放上一次连接的 hello 与 proof：服务端的随机数已变，证明对不上。
            replay = await RawPeer.connect(credentials.path)
            replay.writer.write(recorded[0])
            assert (await replay.json())["type"] == "challenge"
            replay.writer.write(recorded[1])
            assert (await served.next_rejection()).code == "auth-failed"
            assert await replay.frames_until_closed() == 0
            await replay.close()
            assert served.accepted == []
        finally:
            await served.server.close()

    run(main())


def test_data_before_authentication_is_rejected(
    credentials: ChannelCredentials, serve: Serve
) -> None:
    async def main() -> None:
        served = await serve()
        raw = await RawPeer.connect(credentials.path)
        try:
            raw.send(FrameKind.HANDSHAKE, hello(nonce()))
            raw.send(FrameKind.JSON, {"jsonrpc": "2.0", "method": "x"})
            assert (await served.next_rejection()).code == "protocol-error"
            assert await raw.frames_until_closed() == 1  # 只有 challenge
        finally:
            await raw.close()
            await served.server.close()

    run(main())


def test_handshake_timeout(credentials: ChannelCredentials, serve: Serve) -> None:
    async def main() -> None:
        served = await serve(handshake_timeout=0.1)
        raw = await RawPeer.connect(credentials.path)
        try:
            loop = asyncio.get_running_loop()
            started = loop.time()
            assert (await served.next_rejection()).code == "handshake-timeout"
            assert loop.time() - started >= 0.09
            assert await raw.frames_until_closed() == 0
        finally:
            await raw.close()
            await served.server.close()

    run(main())


def test_client_handshake_timeout(credentials: ChannelCredentials) -> None:
    async def main() -> None:
        tasks: set[asyncio.Task[int]] = set()

        async def silent_peer(raw: RawPeer) -> int:
            # 收下连接但一言不发，直到客户端断开。
            count = await raw.frames_until_closed()
            await raw.close()
            return count

        def on_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            tasks.add(asyncio.get_running_loop().create_task(silent_peer(RawPeer(reader, writer))))

        silent = await start_listener(credentials.path, on_stream)
        try:
            with pytest.raises(ChannelError) as raised:
                await connect(credentials.path, ROLE, credentials.key, handshake_timeout=0.1)
            assert raised.value.code == "handshake-timeout"
        finally:
            await silent.close()
            assert await asyncio.gather(*tasks) == [1]  # 收到 hello 后连接被断开

    run(main())


def test_oversize_frame_before_authentication(
    credentials: ChannelCredentials, serve: Serve
) -> None:
    async def main() -> None:
        served = await serve()
        raw = await RawPeer.connect(credentials.path)
        try:
            raw.send(FrameKind.HANDSHAKE, bytes(4096))
            assert (await served.next_rejection()).code == "frame-too-large"
        finally:
            await raw.close()
            await served.server.close()

    run(main())


def test_sender_rejects_oversize_message(connected_pair: ConnectPair) -> None:
    async def main() -> None:
        pair = await connected_pair(max_frame_bytes=64)
        try:
            with pytest.raises(ChannelError) as raised:
                pair.client.send({"text": "x" * 100})
            assert raised.value.code == "frame-too-large"
            assert pair.client.is_open
        finally:
            await pair.close()

    run(main())


def test_receiver_closes_on_oversize_frame(credentials: ChannelCredentials, serve: Serve) -> None:
    async def main() -> None:
        served = await serve(max_frame_bytes=16)
        client = await connect(credentials.path, ROLE, credentials.key)
        try:
            server = await served.next_connection()
            client.send({"text": "x" * 64})
            with pytest.raises(ChannelError) as raised:
                await server.receive(timeout=2)
            assert raised.value.code == "frame-too-large"
            assert not server.is_open
            with pytest.raises(ChannelError) as raised:
                await client.receive(timeout=2)
            assert raised.value.code == "closed"
        finally:
            await client.close()
            await served.server.close()

    run(main())


def test_read_timeout_keeps_the_connection_usable(
    credentials: ChannelCredentials, serve: Serve
) -> None:
    async def main() -> None:
        served = await serve()
        raw = await RawPeer.connect(credentials.path)
        try:
            await raw_handshake(raw, credentials.key)
            assert (await raw.json())["type"] == "accept"
            server = await served.next_connection()
            with pytest.raises(ChannelError) as raised:
                await server.receive(timeout=0.05)
            assert raised.value.code == "timeout"
            frame = encode_frame(FrameKind.JSON, b'{"n":1}')
            raw.writer.write(frame[:7])  # 半帧：读超时后不失步
            with pytest.raises(ChannelError) as raised:
                await server.receive(timeout=0.05)
            assert raised.value.code == "timeout"
            raw.writer.write(frame[7:])
            assert await server.receive(timeout=2) == JsonMessage({"n": 1})
            await server.close()
        finally:
            await raw.close()
            await served.server.close()

    run(main())


def test_protocol_version_mismatch(credentials: ChannelCredentials, serve: Serve) -> None:
    async def main() -> None:
        served = await serve()
        raw = await RawPeer.connect(credentials.path)
        try:
            raw.send(FrameKind.HANDSHAKE, {**hello(nonce()), "v": 2})
            assert (await served.next_rejection()).code == "protocol-error"
            assert await raw.frames_until_closed() == 0
        finally:
            await raw.close()
            await served.server.close()

    run(main())


def test_protocol_identifier_mismatch(credentials: ChannelCredentials, serve: Serve) -> None:
    async def main() -> None:
        served = await serve()
        raw = await RawPeer.connect(credentials.path)
        try:
            await raw_handshake(raw, credentials.key, domain="bellis-channel/2")
            assert (await served.next_rejection()).code == "auth-failed"
        finally:
            await raw.close()
            await served.server.close()

    run(main())


def test_data_right_after_the_handshake_is_not_lost(
    credentials: ChannelCredentials, serve: Serve
) -> None:
    async def main() -> None:
        served = await serve()
        raw = await RawPeer.connect(credentials.path)
        try:
            client_nonce = nonce()
            raw.send(FrameKind.HANDSHAKE, hello(client_nonce))
            challenge = await raw.json()
            proof = b64url(
                mac(credentials.key, "client", "test.raw", client_nonce, challenge["nonce"])
            )
            # 证明与第一条业务消息在同一次写入中到达。
            raw.writer.write(
                encode_frame(
                    FrameKind.HANDSHAKE,
                    json.dumps({"v": 1, "type": "proof", "proof": proof}).encode(),
                )
                + encode_frame(FrameKind.JSON, b'{"n":1}')
            )
            server = await served.next_connection()
            assert await server.receive(timeout=2) == JsonMessage({"n": 1})
            await server.close()
        finally:
            await raw.close()
            await served.server.close()

    run(main())


def test_address_in_use(credentials: ChannelCredentials, serve: Serve) -> None:
    async def main() -> None:
        served = await serve()
        try:
            with pytest.raises(ChannelError) as raised:
                await serve()
            assert raised.value.code == "address-in-use"
            client = await connect(credentials.path, ROLE, credentials.key)
            await client.close()
        finally:
            await served.server.close()

    run(main())


def test_connect_failed(credentials: ChannelCredentials) -> None:
    async def main() -> None:
        with pytest.raises(ChannelError) as raised:
            await connect(credentials.path, ROLE, credentials.key)
        assert raised.value.code == "connect-failed"

    run(main())


def test_invalid_role_is_not_sent(credentials: ChannelCredentials, serve: Serve) -> None:
    async def main() -> None:
        served = await serve()
        try:
            with pytest.raises(ChannelError) as raised:
                await connect(credentials.path, "Bad Role", credentials.key)
            assert raised.value.code == "protocol-error"
            # 角色名在连接之前就校验，服务端看不到这次连接。
            await asyncio.sleep(0.05)
            assert served.rejected.empty()
            assert served.accepted == []
        finally:
            await served.server.close()

    run(main())


def test_pending_handshakes_are_limited(credentials: ChannelCredentials, serve: Serve) -> None:
    async def main() -> None:
        served = await serve(max_pending_handshakes=2)
        try:
            idle = [await RawPeer.connect(credentials.path) for _ in range(2)]
            await asyncio.sleep(0.05)
            third = await RawPeer.connect(credentials.path)
            assert (await served.next_rejection()).code == "busy"
            assert await third.frames_until_closed() == 0
            await third.close()
            for peer in idle:
                await peer.close()
            for _ in idle:
                assert (await served.next_rejection()).code == "closed"
            client = await connect(credentials.path, ROLE, credentials.key)
            await client.close()
        finally:
            await served.server.close()

    run(main())


def test_failing_on_connection_closes_the_connection(credentials: ChannelCredentials) -> None:
    async def main() -> None:
        errors: asyncio.Queue[BaseException] = asyncio.Queue()

        def on_connection(_: Connection) -> None:
            raise RuntimeError("装配失败")

        server = await listen(
            credentials.path,
            credentials.key,
            on_connection=on_connection,
            on_rejected=lambda _: None,
            on_error=errors.put_nowait,
        )
        try:
            client = await connect(credentials.path, ROLE, credentials.key)
            assert "装配失败" in str(await asyncio.wait_for(errors.get(), 2))
            with pytest.raises(ChannelError) as raised:
                await client.receive(timeout=2)
            assert raised.value.code == "closed"
            await client.close()
        finally:
            await server.close()

    run(main())


def test_server_close_disconnects_all_connections(connected_pair: ConnectPair) -> None:
    async def main() -> None:
        pair = await connected_pair()
        await pair.served.server.close()
        with pytest.raises(ChannelError) as raised:
            await pair.client.receive(timeout=2)
        assert raised.value.code == "closed"
        assert not pair.client.is_open
        with pytest.raises(ChannelError) as raised:
            pair.client.send({})
        assert raised.value.code == "closed"
        await pair.close()

    run(main())


def test_session_key_length_is_checked(credentials: ChannelCredentials) -> None:
    async def main() -> None:
        with pytest.raises(ChannelError) as raised:
            await listen(
                credentials.path,
                bytes(16),
                on_connection=lambda _: None,
                on_rejected=lambda _: None,
                on_error=lambda _: None,
            )
        assert raised.value.code == "invalid-key"
        with pytest.raises(ChannelError) as raised:
            await connect(credentials.path, ROLE, bytes(31))
        assert raised.value.code == "invalid-key"

    run(main())


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    from game_runtime.channel._winpipe import current_user_sid

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _k32.CreateNamedPipeW.restype = wintypes.HANDLE
    _k32.CreateNamedPipeW.argtypes = (wintypes.LPCWSTR, *([wintypes.DWORD] * 6), wintypes.LPVOID)
    _k32.LocalFree.argtypes = (wintypes.HLOCAL,)
    _adv.GetSecurityInfo.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID),
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID),
    )
    _adv.GetSecurityInfo.restype = wintypes.DWORD
    _adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = (
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        wintypes.LPVOID,
    )
    _INVALID = ctypes.c_void_p(-1).value
    _GENERIC_RW = 0xC0000000
    _READ_CONTROL = 0x00020000
    _OPEN_EXISTING = 3

    def _open(path: str, access: int) -> tuple[int | None, int]:
        handle = _k32.CreateFileW(path, access, 0, None, _OPEN_EXISTING, 0, None)
        error = ctypes.get_last_error()
        return (None if handle in (None, _INVALID) else handle), error

    def _dacl(handle: int) -> str:
        dacl = wintypes.LPVOID()
        descriptor = wintypes.LPVOID()
        # SE_KERNEL_OBJECT = 6，DACL_SECURITY_INFORMATION = 4
        status = _adv.GetSecurityInfo(
            handle, 6, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor)
        )
        assert status == 0, status
        text = wintypes.LPWSTR()
        ok = _adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, 1, 4, ctypes.byref(text), None
        )
        assert ok, ctypes.get_last_error()
        value = text.value
        _k32.LocalFree(ctypes.cast(text, wintypes.HLOCAL))
        _k32.LocalFree(descriptor)
        assert value is not None
        return value

    def test_pipe_dacl_allows_only_the_current_user(
        credentials: ChannelCredentials, serve: Serve
    ) -> None:
        async def main() -> None:
            served = await serve()
            try:
                handle, error = _open(credentials.path, _READ_CONTROL)
                assert handle is not None, error
                try:
                    # GA 在文件对象上读回为 FA。
                    assert _dacl(handle) == f"D:P(A;;FA;;;{current_user_sid()})"
                finally:
                    _k32.CloseHandle(handle)
            finally:
                await served.server.close()

        run(main())

    def test_pipe_rejects_remote_clients(credentials: ChannelCredentials, serve: Serve) -> None:
        # 对照：不带 PIPE_REJECT_REMOTE_CLIENTS 的管道经 SMB 回环（\\localhost\pipe\…）能连上。
        name = Path(credentials.path).name + "-control"
        control = _k32.CreateNamedPipeW("\\\\.\\pipe\\" + name, 3, 0, 1, 4096, 4096, 0, None)
        assert control not in (None, _INVALID)
        try:
            handle, error = _open("\\\\localhost\\pipe\\" + name, _GENERIC_RW)
            if handle is None:
                pytest.skip(f"本机 SMB 回环不可用（错误 {error}），无法对照")
            _k32.CloseHandle(handle)
        finally:
            _k32.CloseHandle(control)

        async def main() -> None:
            served = await serve()
            try:
                remote = "\\\\localhost\\pipe\\" + credentials.path.removeprefix("\\\\.\\pipe\\")
                handle, error = _open(remote, _GENERIC_RW)
                assert handle is None
                assert error == 5  # ERROR_ACCESS_DENIED
                assert served.accepted == []
            finally:
                await served.server.close()

        run(main())
