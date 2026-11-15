"""真实 Rust 实时核和 Bellis 控制连接的假后端联调。"""

import asyncio
import contextlib
import os
import secrets
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from core_protocol import models
from core_protocol.methods import ACTION_SUBMIT, CAPTURE_SET_TARGET, CORE_STOP, FRAME_RING_ATTACH
from game_runtime.channel import (
    ChannelCredentials,
    ChannelError,
    Connection,
    JsonMessage,
    RpcError,
    RpcErrorCode,
    RpcPeer,
    listen,
    session_credentials,
)
from game_runtime.core_link import CORE_ROLE, ENV_TEST_DEVICE_KEY, ENV_TEST_DEVICE_PATH
from game_runtime.core_link.fake_device import FakeDevice
from game_runtime.frame_ring import FrameRing
from game_runtime.frame_ring.fake import FakeDisplay
from game_runtime.secret_store import SessionInfo

from .test_core import WAIT, FakeMain, attach_params, target_params

RUNTIME_ID = "runtime-01"
LEASE_REFUSED = -32004
HELD = ("key", 0x11)
MS = 1_000_000


def wire_now_ms() -> int:
    return time.perf_counter_ns() // MS


@dataclass
class Rig:
    main: FakeMain
    device: FakeDevice
    connections: asyncio.Queue[Connection]
    released: asyncio.Queue[dict[str, Any]]
    health: asyncio.Queue[dict[str, Any]]
    session_file: Path
    resources: contextlib.ExitStack

    async def connect(self) -> RpcPeer:
        connection = await asyncio.wait_for(self.connections.get(), WAIT)
        assert connection.role == CORE_ROLE
        peer = RpcPeer(connection, timeout=WAIT)
        peer.on_notification("input.released", self.released.put_nowait)
        peer.on_notification("health.reported", self.health.put_nowait)
        return peer

    async def release(self, reason: str, token: int | None) -> dict[str, Any]:
        async with asyncio.timeout(WAIT):
            while True:
                report = await self.released.get()
                if report["reason"] == reason and report["token"] == token:
                    assert report["verification"] == "unverified"
                    return report


@contextlib.asynccontextmanager
async def runtime(
    exe: Path,
    folder: Path,
    *,
    wrong_key: bool = False,
    wrong_session: bool = False,
) -> AsyncGenerator[Rig]:
    session_id = f"b{secrets.token_hex(4)}"
    with (
        session_credentials(session_id) as credentials,
        FakeDevice() as device,
        contextlib.ExitStack() as resources,
    ):
        connections: asyncio.Queue[Connection] = asyncio.Queue()
        server = await listen(
            credentials.path,
            credentials.key,
            on_connection=connections.put_nowait,
            on_rejected=lambda _: None,
            on_error=lambda error: pytest.fail(str(error)),
        )
        try:
            client_credentials = (
                ChannelCredentials(credentials.path, secrets.token_bytes(32))
                if wrong_key
                else credentials
            )
            info = SessionInfo.create(
                client_credentials,
                session_id,
                launcher_pid=os.getpid(),
                started_at=datetime.now(UTC),
            )
            session_file = folder / "session.json"
            session_file.write_text(info.to_json(), encoding="utf-8")
            fake = FakeMain(exe, folder / "core.log", None)
            async with fake.running(
                extra_args=[
                    "--input-backend",
                    "fake",
                    "--bellis-control",
                    RUNTIME_ID,
                    "--heartbeat-timeout-ms",
                    "3000",
                    "--max-frame-age-ms",
                    "3000",
                ],
                extra_env={
                    ENV_TEST_DEVICE_PATH: device.credentials.path,
                    ENV_TEST_DEVICE_KEY: device.credentials.encoded_key,
                    "BELLIS_SESSION_ID": "wrong" if wrong_session else session_id,
                    "GAME_RUNTIME_TEST_BELLIS_SESSION_FILE": str(session_file),
                },
            ):
                yield Rig(
                    fake,
                    device,
                    connections,
                    asyncio.Queue(),
                    asyncio.Queue(),
                    session_file,
                    resources,
                )
        finally:
            await server.close()


async def describe(peer: RpcPeer, *, major: int = 1) -> None:
    before = wire_now_ms()
    result = cast(dict[str, object], await peer.request("game.describe", {"major": major}))
    after = wire_now_ms()
    assert set(result) == {"major", "runtimeId", "nowMs"}
    assert result["major"] == 1
    assert result["runtimeId"] == RUNTIME_ID
    now = result["nowMs"]
    assert isinstance(now, int)
    assert before <= now <= after


async def hold(rig: Rig, peer: RpcPeer, token: int) -> None:
    assert (
        await peer.request(
            "lease.renew",
            {
                "token": token,
                "expiresAtMs": wire_now_ms() + 3000,
            },
        )
        == {}
    )
    ring = rig.resources.enter_context(FrameRing.create(16, 12))
    display = rig.resources.enter_context(FakeDisplay.create(16, 12))
    await rig.main.request(FRAME_RING_ATTACH.name, attach_params(ring))
    await rig.main.request(CAPTURE_SET_TARGET.name, target_params(display.target))
    display.present(bytes(16 * 12 * 4), 16, 12, capture_ns=time.perf_counter_ns())
    health = await rig.main.until_health(lambda health: health.latest_frame is not None)
    assert health.latest_frame is not None
    rig.main.heartbeat("fixture", 1, 1)
    now = time.perf_counter_ns()
    block = models.ActionBlock(
        producer=models.Producer(id="fixture", generation=1),
        seq=1,
        base_frame=health.latest_frame.frame_number,
        start_ns=now,
        deadline_ns=now + 3000 * MS,
        steps=[
            models.ControlStep(
                offset_ns=0,
                held=[models.HeldInput(root=models.HeldKey(scan_code=HELD[1]))],
                actions=[],
            )
        ],
    )
    assert await rig.main.request(ACTION_SUBMIT.name, block.model_dump(mode="json")) == {
        "type": "accepted"
    }
    await rig.device.until(lambda device: True if HELD in device.held else None)


@pytest.mark.parametrize("fault", ["key", "session"])
def test_bellis_boot_rejects_wrong_secret_or_session(
    core_exe: Path, tmp_path: Path, fault: str
) -> None:
    async def run() -> None:
        async with runtime(
            core_exe, tmp_path, wrong_key=fault == "key", wrong_session=fault == "session"
        ) as rig:
            assert await rig.main.exit_code() == 1
            assert rig.connections.empty()
            assert rig.main.released.empty()
            assert not rig.device.held

    asyncio.run(run())


@pytest.mark.parametrize("major", [None, 2])
def test_renewal_needs_a_compatible_describe(
    core_exe: Path, tmp_path: Path, major: int | None
) -> None:
    async def run() -> None:
        async with runtime(core_exe, tmp_path) as rig:
            peer = await rig.connect()
            try:
                await rig.main.started()
                if major is not None:
                    await describe(peer, major=major)
                with pytest.raises(ChannelError) as refusal:
                    await peer.request(
                        "lease.renew", {"token": 1, "expiresAtMs": wire_now_ms() + 1000}
                    )
                assert refusal.value.code == "closed"
                await rig.main.until_health(lambda h: h.lease is models.LeaseStatus.SOURCE_LOST)
                assert not rig.device.held
                assert rig.health.empty()
            finally:
                await peer.close()

    asyncio.run(run())


def test_bellis_is_only_source_and_stop_releases_by_cutoff(core_exe: Path, tmp_path: Path) -> None:
    async def run() -> None:
        async with runtime(core_exe, tmp_path) as rig:
            peer = await rig.connect()
            try:
                await rig.main.started()
                await rig.device.until_connected()
                await describe(peer)
                reported = await asyncio.wait_for(rig.health.get(), WAIT)
                assert reported["leaseState"] == "not_granted"
                assert reported["frameAgeMs"] is None
                assert reported["queueDepth"] == 0

                # 通用取消消息只属于 RPC，不改变许可状态，也不中断合法控制连接。
                peer.notify("rpc.cancel", {"id": "previous-request"})
                sent = {"token": 8, "expiresAtMs": wire_now_ms() + 1000}
                assert (
                    await rig.main.refused("lease.renew", sent)
                ).code == RpcErrorCode.METHOD_NOT_FOUND
                with pytest.raises(RpcError) as test_source:
                    await rig.device.grant_raw(8, time.perf_counter_ns() + 1000 * MS)
                assert test_source.value.code == RpcErrorCode.METHOD_NOT_FOUND

                # 首份许可前也允许停止。wire 的 0 是通过水位，释放事件以 null 表示没有令牌。
                assert (
                    await peer.request("input.stop", {"throughToken": 0, "reason": "before grant"})
                    == {}
                )
                await rig.release("lease_revoked", None)

                await hold(rig, peer, 9)
                assert (
                    await peer.request("input.stop", {"throughToken": 8, "reason": "old stop"})
                    == {}
                )
                assert (await rig.main.snapshot()).inputs_held
                assert HELD in rig.device.held

                before = wire_now_ms()
                assert await peer.request("input.stop", {"throughToken": 9, "reason": "stop"}) == {}
                first = await rig.release("lease_revoked", 9)
                assert first["atMs"] >= before
                await rig.device.until(lambda device: True if not device.held else None)
                assert not (await rig.main.snapshot()).inputs_held

                # 另一个请求须产生自己的释放证据，即使前一次已完成。
                await asyncio.sleep(0.005)
                before = wire_now_ms()
                assert (
                    await peer.request("input.stop", {"throughToken": 9, "reason": "stop again"})
                    == {}
                )
                second = await rig.release("lease_revoked", 9)
                assert second["atMs"] >= before
                assert second["atMs"] > first["atMs"]
                with pytest.raises(RpcError) as revoked:
                    await peer.request(
                        "lease.renew", {"token": 9, "expiresAtMs": wire_now_ms() + 1000}
                    )
                assert revoked.value.code == LEASE_REFUSED
                assert await rig.main.request(CORE_STOP.name, {}) == {}
                assert await rig.main.exit_code() == 0
            finally:
                await peer.close()

    asyncio.run(run())


def test_absolute_deadline_and_raw_integer_spelling(core_exe: Path, tmp_path: Path) -> None:
    async def run() -> None:
        async with runtime(core_exe, tmp_path) as rig:
            peer = await rig.connect()
            try:
                await rig.main.started()
                await describe(peer)
                expires_at = wire_now_ms() + 50
                await asyncio.sleep(0.07)
                with pytest.raises(RpcError) as late:
                    await peer.request("lease.renew", {"token": 3, "expiresAtMs": expires_at})
                assert late.value.code == LEASE_REFUSED
                assert (await rig.main.snapshot()).lease is models.LeaseStatus.NOT_GRANTED
                assert (
                    await peer.request(
                        "lease.renew",
                        {"token": 4, "expiresAtMs": wire_now_ms() + 1000},
                    )
                    == {}
                )
                with pytest.raises(ChannelError) as bad_number:
                    await peer.request(
                        "lease.renew", {"token": 4.0, "expiresAtMs": wire_now_ms() + 1000}
                    )
                assert bad_number.value.code == "closed"
                await rig.main.until_health(lambda h: h.lease is models.LeaseStatus.SOURCE_LOST)
            finally:
                await peer.close()

    asyncio.run(run())


@pytest.mark.parametrize("cause", ["control", "session_replace", "session_delete", "core"])
def test_control_invalidation_releases_held_input(
    core_exe: Path, tmp_path: Path, cause: str
) -> None:
    async def run() -> None:
        async with runtime(core_exe, tmp_path) as rig:
            peer = await rig.connect()
            try:
                await rig.main.started()
                await describe(peer)
                await hold(rig, peer, 12)
                if cause == "control":
                    await peer.close()
                elif cause == "session_replace":
                    # 覆盖凭据时旧连接仍在。只能由实时核的会话核对发现变化。
                    old = SessionInfo.model_validate_json(rig.session_file.read_bytes())
                    newer = SessionInfo.create(
                        old.credentials(),
                        "newsession",
                        launcher_pid=old.launcher_pid,
                        started_at=datetime.now(UTC),
                    )
                    replacement = rig.session_file.with_suffix(".replacement")
                    replacement.write_text(newer.to_json(), encoding="utf-8")
                    replacement.replace(rig.session_file)
                    await asyncio.wait_for(asyncio.shield(peer.closed), WAIT)
                elif cause == "session_delete":
                    rig.session_file.unlink()
                    await asyncio.wait_for(asyncio.shield(peer.closed), WAIT)
                else:
                    assert rig.main.peer is not None
                    await rig.main.peer.close()
                    await rig.release("core_connection_lost", 12)
                    assert await rig.main.exit_code() == 0
                await rig.device.until(lambda device: True if not device.held else None)
                if cause != "core":
                    await rig.main.until_health(lambda h: h.lease is models.LeaseStatus.SOURCE_LOST)
                    with pytest.raises(ChannelError):
                        await peer.request(
                            "lease.renew", {"token": 13, "expiresAtMs": wire_now_ms() + 1000}
                        )
            finally:
                await peer.close()

    asyncio.run(run())


def test_core_stop_finishes_when_control_peer_does_not_read(core_exe: Path, tmp_path: Path) -> None:
    async def run() -> None:
        async with runtime(core_exe, tmp_path) as rig:
            control = await asyncio.wait_for(rig.connections.get(), WAIT)
            try:
                await rig.main.started()
                assert await rig.main.request(CORE_STOP.name, {}) == {}
                assert await rig.main.exit_code() == 0
            finally:
                control.abort()

    asyncio.run(run())


def test_ready_control_loses_authority_when_writes_cannot_finish(
    core_exe: Path, tmp_path: Path
) -> None:
    async def run() -> None:
        async with runtime(core_exe, tmp_path) as rig:
            connection = await asyncio.wait_for(rig.connections.get(), WAIT)

            async def request(request_id: int, method: str, params: dict[str, object]) -> object:
                connection.send(
                    {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
                )
                async with asyncio.timeout(WAIT):
                    while True:
                        message = await connection.receive()
                        if (
                            isinstance(message, JsonMessage)
                            and message.value.get("id") == request_id
                        ):
                            return message.value["result"]

            try:
                await rig.main.started()
                description = cast(
                    dict[str, object], await request(1, "game.describe", {"major": 1})
                )
                assert description["major"] == 1
                assert description["runtimeId"] == RUNTIME_ID
                assert isinstance(description["nowMs"], int)
                assert (
                    await request(
                        2, "lease.renew", {"token": 7, "expiresAtMs": wire_now_ms() + 20_000}
                    )
                    == {}
                )
                await rig.main.until_health(lambda health: health.lease is models.LeaseStatus.VALID)
                transport = cast(asyncio.Transport, connection._writer.transport)  # pyright: ignore[reportPrivateUsage]
                transport.pause_reading()
                # 握手和授予都完成后，停止从真实管道/UDS 读取。
                # 旧停止每次都要应答，且不撤销 token 7。
                for number in range(8192):
                    connection.send(
                        {
                            "jsonrpc": "2.0",
                            "id": f"{number:0128d}",
                            "method": "input.stop",
                            "params": {"throughToken": 0, "reason": "test the blocked write"},
                        }
                    )
                await rig.main.until_health(
                    lambda health: health.lease is models.LeaseStatus.SOURCE_LOST
                )
                assert "Bellis control connection closed: timeout" in rig.main.log()
            finally:
                connection.abort()

    asyncio.run(run())
