"""独立假设备的释放水位核验；真实认证通道，不访问 SendInput。"""

import asyncio
import time
from typing import Any

import pytest

from game_runtime.channel import RpcError, RpcPeer, connect
from game_runtime.core_link import CORE_ROLE
from game_runtime.core_link.fake_device import FakeDevice


async def peer_for(device: FakeDevice) -> RpcPeer:
    credentials = device.credentials
    peer = RpcPeer(await connect(credentials.path, CORE_ROLE, credentials.key), timeout=1)
    await device.until_connected()
    return peer


def event(peer: RpcPeer, seq: int, kind: str, **values: object) -> None:
    peer.notify(
        "fake_input.event",
        {"seq": seq, "at_ns": time.perf_counter_ns(), "event": {"type": kind, **values}},
    )


async def verify(peer: RpcPeer, seq: int, release: int = 1, **overrides: Any) -> object:
    return await peer.request(
        "fake_input.verify_release",
        {
            "release_id": release,
            "through_seq": seq,
            "deadline_ns": time.perf_counter_ns() + 500_000_000,
            **overrides,
        },
    )


def test_release_barrier_does_not_erase_held_state() -> None:
    async def main() -> None:
        with FakeDevice(verify_release=True) as device:
            peer = await peer_for(device)
            try:
                event(peer, 1, "key_down", scan_code=17)
                event(peer, 2, "release_all", reason="stop")
                assert await verify(peer, 2) == {
                    "release_id": 1,
                    "through_seq": 2,
                    "held": [{"type": "key", "scan_code": 17}],
                }
                records = device.verifications()
                records[0]["held"].clear()
                assert device.verifications()[0]["held"]
                with pytest.raises(RpcError):
                    await verify(peer, 2, 2)
                event(peer, 3, "key_up", scan_code=17)
                event(peer, 4, "release_all", reason="stop")
                assert await verify(peer, 4, 2) == {"release_id": 2, "through_seq": 4, "held": []}
                with pytest.raises(RpcError):
                    await verify(peer, 4, 2)
                with pytest.raises(RpcError):
                    await verify(peer, 4, 3)
                event(peer, 5, "release_all", reason="stop")
                assert await verify(peer, 5, 3) == {"release_id": 3, "through_seq": 5, "held": []}
            finally:
                await peer.close()

    asyncio.run(main())


def test_dropped_lift_survives_new_core_connection() -> None:
    async def main() -> None:
        with FakeDevice(verify_release=True, drop_lifts=frozenset({("key", 17)})) as device:
            peer = await peer_for(device)
            event(peer, 1, "key_down", scan_code=17)
            event(peer, 2, "key_up", scan_code=17)
            event(peer, 3, "release_all", reason="stop")
            assert (await verify(peer, 3)) == {
                "release_id": 1,
                "through_seq": 3,
                "held": [{"type": "key", "scan_code": 17}],
            }
            await device.disconnect()
            await peer.close()
            second = await peer_for(device)
            try:
                event(second, 1, "release_all", reason="core_start")
                assert (await verify(second, 1)) == {
                    "release_id": 1,
                    "through_seq": 1,
                    "held": [{"type": "key", "scan_code": 17}],
                }
                assert device.connections == 2
            finally:
                await second.close()

    asyncio.run(main())


@pytest.mark.parametrize("fault", ["gap", "duplicate", "bad_event", "watermark", "expired"])
def test_uncertain_or_stale_history_cannot_verify(fault: str) -> None:
    async def main() -> None:
        with FakeDevice(verify_release=True) as device:
            peer = await peer_for(device)
            try:
                if fault == "gap":
                    event(peer, 2, "release_all", reason="stop")
                elif fault == "duplicate":
                    event(peer, 1, "release_all", reason="stop")
                    event(peer, 1, "release_all", reason="stop")
                elif fault == "bad_event":
                    peer.notify("fake_input.event", {"nonsense": True})
                    event(peer, 1, "release_all", reason="stop")
                else:
                    event(peer, 1, "release_all", reason="stop")
                with pytest.raises(RpcError):
                    await verify(
                        peer,
                        2 if fault in {"gap", "watermark"} else 1,
                        **({"deadline_ns": 1} if fault == "expired" else {}),
                    )
                assert device.verifications() == []
            finally:
                await peer.close()

    asyncio.run(main())


def test_default_device_never_claims_verified() -> None:
    async def main() -> None:
        with FakeDevice() as device:
            peer = await peer_for(device)
            try:
                event(peer, 1, "release_all", reason="stop")
                with pytest.raises(RpcError) as error:
                    await verify(peer, 1)
                assert error.value.code == -32601
                assert device.verifications() == []
            finally:
                await peer.close()

    asyncio.run(main())


def test_delayed_verification_cannot_answer_past_deadline() -> None:
    async def main() -> None:
        with FakeDevice(verify_release=True, verification_delay=0.04) as device:
            peer = await peer_for(device)
            try:
                event(peer, 1, "release_all", reason="stop")
                with pytest.raises(RpcError):
                    await verify(peer, 1, deadline_ns=time.perf_counter_ns() + 10_000_000)
                assert device.verifications() == []
            finally:
                await peer.close()

    asyncio.run(main())
