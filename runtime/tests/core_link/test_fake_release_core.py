"""实际 Rust 核通过独立假设备的状态核验，投影到 Bellis 控制连接。"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from game_runtime.channel import RpcError

from .test_bellis_control import HELD, Rig, describe, hold, runtime, wire_now_ms
from .test_core import WAIT
from .test_operator import policy_for_tests


async def release(rig: Rig, reason: str, token: int | None) -> dict[str, Any]:
    async with asyncio.timeout(WAIT):
        while True:
            report = await rig.released.get()
            if report["reason"] == reason and report["token"] == token:
                return report


@pytest.mark.parametrize("drop_lift", [False, True])
def test_core_uses_independent_held_state_for_release(
    core_exe: Path, tmp_path: Path, drop_lift: bool
) -> None:
    async def run() -> None:
        async with runtime(
            core_exe,
            tmp_path,
            verify_release=True,
            drop_lifts=frozenset({HELD}) if drop_lift else frozenset(),
        ) as rig:
            peer = await rig.connect()
            try:
                await rig.main.started()
                await describe(peer)
                assert (await release(rig, "core_start", None))["verification"] == "verified"
                assert (
                    await rig.main.request(
                        "input.configure", policy_for_tests().model_dump(mode="json")
                    )
                    == {}
                )
                await hold(rig, peer, 9)
                assert HELD in rig.device.held
                await peer.request("input.stop", {"throughToken": 9, "reason": "test"})
                result = await release(rig, "lease_revoked", 9)
                assert result["verification"] == ("residual" if drop_lift else "verified")
                assert (HELD in rig.device.held) is drop_lift
                records = rig.device.verifications()
                assert bool(records[-1]["held"]) is drop_lift
                if drop_lift:
                    with pytest.raises(RpcError):
                        await peer.request(
                            "lease.renew", {"token": 10, "expiresAtMs": wire_now_ms() + 1000}
                        )
                else:
                    assert (
                        await peer.request(
                            "lease.renew", {"token": 10, "expiresAtMs": wire_now_ms() + 1000}
                        )
                        == {}
                    )
            finally:
                await peer.close()

    asyncio.run(run())


def test_timed_out_device_never_opens_the_core_gate(core_exe: Path, tmp_path: Path) -> None:
    async def run() -> None:
        async with runtime(core_exe, tmp_path, verify_release=True, verification_delay=0.3) as rig:
            peer = await rig.connect()
            try:
                await rig.main.started()
                await describe(peer)
                assert (await release(rig, "core_start", None))["verification"] == "unverified"
                with pytest.raises(RpcError):
                    await peer.request(
                        "lease.renew", {"token": 1, "expiresAtMs": wire_now_ms() + 1000}
                    )
                assert rig.device.verifications() == []
                assert not rig.device.held
            finally:
                await peer.close()

    asyncio.run(run())
