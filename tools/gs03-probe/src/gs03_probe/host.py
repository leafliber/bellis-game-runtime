"""GS-03 探针的非提权一侧：开真实的 `bellis-channel/1` 服务端与真实的帧环，等提权一侧连上，
核对握手、一次 RPC 与它写进帧环的帧（GS-03 的第 1 项“写入”、第 2、3 项的核对侧）。

非提权一侧创建全部进程间对象（管道与共享内存，GD-02），提权一侧只连接与写入。
"""

import asyncio
import contextlib
import json
import os
import sys
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self

from game_runtime.channel import (
    ChannelCredentials,
    Connection,
    RpcPeer,
    listen,
    session_credentials,
)
from game_runtime.frame_ring import FrameRing

from . import secret
from .conn import DEFAULT_ROLE, build_conn_info
from .pattern import check_frame

#: 帧环的缺省最大尺寸（至少 7×5 才放得下图案）。
DEFAULT_MAX_WIDTH = 64
DEFAULT_MAX_HEIGHT = 48
#: 缺省写入的帧数。
DEFAULT_FRAMES = 5


def _current_user_sid() -> str:
    """当前用户 SID；非 Windows 上无此概念。"""
    if sys.platform != "win32":
        return "unsupported"
    from game_runtime._winsecurity import current_user_sid

    return current_user_sid()


@dataclass
class HostOutcome:
    """提权一侧与非提权一侧交互的结果，写进报告。"""

    connected: bool = False
    connection_role: str | None = None
    role_matches: bool = False
    hello_received: bool = False
    elevated_identity: dict[str, Any] = field(default_factory=dict[str, Any])
    elevated_pid: int | None = None
    frames_expected: int = 0
    frames_verified: bool = False
    frames_detail: str = "未收到提权一侧的帧回报"
    latest_frame_number: int | None = None
    host_pid: int = field(default_factory=os.getpid)
    host_user_sid: str = ""
    errors: list[str] = field(default_factory=list[str])

    def to_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "connection_role": self.connection_role,
            "role_matches": self.role_matches,
            "hello_received": self.hello_received,
            "elevated_identity": self.elevated_identity,
            "elevated_pid": self.elevated_pid,
            "frames_expected": self.frames_expected,
            "frames_verified": self.frames_verified,
            "frames_detail": self.frames_detail,
            "latest_frame_number": self.latest_frame_number,
            "host_pid": self.host_pid,
            "host_user_sid": self.host_user_sid,
            "errors": self.errors,
        }


class Host:
    """非提权一侧。用作异步上下文管理器：进入时建帧环、开监听、（可选）写凭据；退出时收拾干净。"""

    def __init__(
        self,
        *,
        max_width: int = DEFAULT_MAX_WIDTH,
        max_height: int = DEFAULT_MAX_HEIGHT,
        frames: int = DEFAULT_FRAMES,
        role: str = DEFAULT_ROLE,
        session_id: str = "gs03",
        write_cred: bool = True,
        cred_service: str = secret.DEFAULT_SERVICE,
        cred_name: str = secret.DEFAULT_NAME,
    ) -> None:
        if frames < 1:
            raise ValueError("帧数须为正数")
        self._max_width = max_width
        self._max_height = max_height
        self._frames = frames
        self._role = role
        self._session_id = session_id
        self._write_cred = write_cred
        self._cred_service = cred_service
        self._cred_name = cred_name
        self._stack = contextlib.ExitStack()
        self._ring: FrameRing | None = None
        self._credentials: ChannelCredentials | None = None
        self._server: Any = None
        self._peers: list[RpcPeer] = []
        self._cred_written = False
        self.outcome = HostOutcome(frames_expected=frames, host_user_sid=_current_user_sid())
        self._done: asyncio.Event = asyncio.Event()

    @property
    def credentials(self) -> ChannelCredentials:
        if self._credentials is None:
            raise RuntimeError("Host 尚未进入上下文")
        return self._credentials

    @property
    def ring(self) -> FrameRing:
        if self._ring is None:
            raise RuntimeError("Host 尚未进入上下文")
        return self._ring

    def conn_info(self) -> dict[str, object]:
        return build_conn_info(self.credentials, self.ring, role=self._role, frames=self._frames)

    async def __aenter__(self) -> Self:
        self._ring = self._stack.enter_context(FrameRing.create(self._max_width, self._max_height))
        self._credentials = self._stack.enter_context(session_credentials(self._session_id))
        self._server = await listen(
            self._credentials.path,
            self._credentials.key,
            on_connection=self._on_connection,
            on_rejected=lambda error: self.outcome.errors.append(f"握手被拒：{error}"),
            on_error=lambda error: self.outcome.errors.append(f"服务端错误：{error}"),
        )
        if self._write_cred:
            secret.write_secret(self._cred_service, self._cred_name, json.dumps(self.conn_info()))
            self._cred_written = True
        return self

    def _on_connection(self, connection: Connection) -> None:
        self.outcome.connected = True
        self.outcome.connection_role = connection.role
        self.outcome.role_matches = connection.role == self._role
        peer = RpcPeer(connection, timeout=10.0)
        peer.handle("gs03.hello", self._on_hello)
        peer.handle("gs03.frames", self._on_frames)
        self._peers.append(peer)

    def _on_hello(self, params: dict[str, Any]) -> object:
        self.outcome.hello_received = True
        identity = params.get("identity")
        if isinstance(identity, dict):
            self.outcome.elevated_identity = identity
        pid = params.get("pid")
        if isinstance(pid, int):
            self.outcome.elevated_pid = pid
        return {"server_pid": self.outcome.host_pid, "server_sid": self.outcome.host_user_sid}

    def _on_frames(self, params: dict[str, Any]) -> object:
        try:
            last = int(params["last_frame_number"])
            count = int(params["count"])
            self.outcome.latest_frame_number = last
            problem = self._verify_frames(last, count)
            self.outcome.frames_verified = problem is None
            self.outcome.frames_detail = problem or f"帧环里的第 {last} 帧与图案一致"
            return {"verified": problem is None, "detail": self.outcome.frames_detail}
        except Exception as error:  # 核对出错也要回报，不让 RPC 悬空
            self.outcome.frames_detail = f"核对出错：{error}"
            self.outcome.errors.append(self.outcome.frames_detail)
            return {"verified": False, "detail": self.outcome.frames_detail}
        finally:
            self._done.set()

    def _verify_frames(self, last: int, count: int) -> str | None:
        if count != self._frames:
            return f"提权一侧写了 {count} 帧，应为 {self._frames}"
        if last != count:
            return f"最新帧号为 {last}，应为 {count}（全新帧环续写基数为 0）"
        frame = self.ring.read_latest()
        if frame is None:
            return "帧环里还没有帧"
        if frame.frame_number != last:
            return f"帧环最新是第 {frame.frame_number} 帧，提权一侧说写到第 {last} 帧"
        return check_frame(frame, self._max_width, self._max_height)

    async def wait(self, timeout: float) -> HostOutcome:
        """等提权一侧完成（握手 + 两次 RPC）。超时即记录并返回当前结果。"""
        try:
            await asyncio.wait_for(self._done.wait(), timeout)
        except TimeoutError:
            self.outcome.errors.append(f"等待 {timeout}s 内提权一侧没有完成")
        return self.outcome

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        for peer in self._peers:
            with contextlib.suppress(Exception):
                await peer.close()
        if self._server is not None:
            await self._server.close()
        if self._cred_written:
            with contextlib.suppress(OSError):
                secret.delete_secret(self._cred_service, self._cred_name)
        self._stack.close()
