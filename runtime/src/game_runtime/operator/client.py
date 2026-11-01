"""主进程一侧的操作员连接：连到操作员控制台，跟踪测试会话，交出决策点与已提交的动作。

设计见 `docs/modules/game-bridge.md` 的“操作员连接的消息”。连接信息从 SecretStore 的
`operator-session` 条目读取；同一地址与密钥（`credentials`）再经 `CoreLinkOptions.operator` 交给
实时核，许可由控制台直接给实时核，不经主进程。

- 核对 `operator.session` 的 `adapter` 与自己加载的适配包相同，不同即断开并报错；
- `may_autoplay` 只在会话为 `running` 且本适配包的前置条件全部已确认时为真（动作风险的第四道防线）；
- 连接断开即按会话结束处理。

这里只有连接与消息；逐步控制器（何时提议动作、决策点怎样执行）在 G2 第 5 步。
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from game_runtime.adapter import Adapter
from game_runtime.channel import ChannelCredentials, ChannelError, RpcPeer, connect
from game_runtime.operator import messages
from game_runtime.operator.console import MAIN_ROLE
from game_runtime.operator.messages import SessionState
from game_runtime.secret_store import SecretStore, read_session_info

logger = logging.getLogger(__name__)


class OperatorError(Exception):
    """连不上操作员控制台，或控制台的会话与本进程不符。"""


class OperatorUnavailableError(OperatorError):
    """SecretStore 里没有控制台的连接信息，或连不上它。"""


class OperatorAdapterMismatchError(OperatorError):
    """控制台的会话是另一个适配包的；已断开。"""


class OperatorClient:
    """操作员连接的客户端。`await connect()` 连上并等到第一条 `operator.session`；`close()` 断开。

    `on_decision_resolved` 收到操作员的选择；`on_session` 收到每条会话状态；`on_ended` 在会话结束、
    连接断开或适配包不符时调用一次。这些回调在事件循环里调用，须很快返回。
    """

    def __init__(
        self,
        adapter: Adapter,
        *,
        store: SecretStore,
        on_decision_resolved: Callable[[messages.DecisionResolved], None] | None = None,
        on_session: Callable[[messages.Session], None] | None = None,
        on_ended: Callable[[str], None] | None = None,
        timeout: float = 2.0,
    ) -> None:
        self.adapter = adapter
        self._store = store
        self._on_decision_resolved = on_decision_resolved
        self._on_session = on_session
        self._on_ended = on_ended
        self._timeout = timeout
        self._credentials: ChannelCredentials | None = None
        self._peer: RpcPeer | None = None
        self._session: messages.Session | None = None
        self._first: asyncio.Future[messages.Session] | None = None
        self._ended: str | None = None

    # 状态

    @property
    def credentials(self) -> ChannelCredentials:
        """控制台本次会话的地址与密钥，交给 `CoreLinkOptions.operator`。`connect` 之后才有。"""
        if self._credentials is None:
            raise RuntimeError("还没有连上操作员控制台")
        return self._credentials

    @property
    def session(self) -> messages.Session | None:
        """最近一条 `operator.session`。"""
        return self._session

    @property
    def connected(self) -> bool:
        return self._peer is not None and self._ended is None

    @property
    def ended(self) -> str | None:
        """会话按结束处理的原因：`ended`（控制台报会话结束）、`disconnected`、`adapter_mismatch`、
        `closed`（本端断开）；还没结束时为 None。"""
        return self._ended

    @property
    def may_autoplay(self) -> bool:
        """会话在运行，且本适配包的前置条件全部已确认：只有这时进入自动游玩、提议动作。"""
        session = self._session
        if not self.connected or session is None or session.state is not SessionState.RUNNING:
            return False
        required = {precondition.id for precondition in self.adapter.risk.preconditions}
        return required <= set(session.confirmed)

    # 连接

    async def connect(self) -> None:
        """读取连接信息、以 `game-runtime.main` 连上控制台，等到第一条 `operator.session`，
        核对适配包。

        没有连接信息或连不上时抛 `OperatorUnavailableError`；适配包不符时断开并抛
        `OperatorAdapterMismatchError`。
        """
        if self._peer is not None:
            raise RuntimeError("已连上操作员控制台")
        info = read_session_info(self._store)
        if info is None:
            raise OperatorUnavailableError(
                "SecretStore 里没有操作员控制台的连接信息：先在另一个终端运行 "
                f"`uv run operate {self.adapter.id}`"
            )
        credentials = info.credentials()
        try:
            connection = await connect(credentials.path, MAIN_ROLE, credentials.key)
        except ChannelError as error:
            raise OperatorUnavailableError(f"连不上操作员控制台：{error}") from error
        loop = asyncio.get_running_loop()
        self._first = loop.create_future()
        peer = RpcPeer(connection, timeout=self._timeout)
        peer.on_notification(messages.SESSION, self._session_message)
        peer.on_notification(messages.DECISION_RESOLVED, self._resolved_message)
        self._peer = peer
        self._credentials = credentials
        peer.closed.add_done_callback(lambda _: self._finish("disconnected"))
        try:
            async with asyncio.timeout(self._timeout):
                await asyncio.wait({self._first, peer.closed}, return_when=asyncio.FIRST_COMPLETED)
        except TimeoutError:
            await self.close()
            raise OperatorUnavailableError("操作员控制台没有发来会话状态") from None
        if self._ended == "adapter_mismatch":
            await peer.close()
            raise OperatorAdapterMismatchError(self._mismatch())
        if not self._first.done():
            await peer.close()
            raise OperatorUnavailableError("操作员控制台断开了连接")

    async def close(self) -> None:
        """断开；之后按会话结束处理。"""
        peer = self._peer
        self._finish("closed")
        if peer is not None:
            await peer.close()

    # 发出

    def open_decision(self, decision: messages.DecisionOpened) -> None:
        """打开一个决策点，由操作员给出选择。"""
        self._notify(messages.DECISION_OPENED, decision.params())

    def close_decision(self, closed: messages.DecisionClosed) -> None:
        """报告决策点已关闭：按操作员的选择、按排第一的候选，或画面已变而撤回。"""
        self._notify(messages.DECISION_CLOSED, closed.params())

    def report_action(self, action: messages.Action) -> None:
        """报告已提交的一个动作，计入会话记录。"""
        self._notify(messages.ACTION, action.params())

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        peer = self._peer
        if peer is None or self._ended is not None:
            raise ChannelError("closed", "没有连着操作员控制台")
        peer.notify(method, params)

    # 收到

    def _session_message(self, params: dict[str, Any]) -> None:
        try:
            session = messages.Session.model_validate(params)
        except ValidationError as error:
            logger.warning("忽略了不合法的 %s：%s", messages.SESSION, error)
            return
        if session.adapter != self.adapter.id:
            self._session = session
            logger.error("%s", self._mismatch())
            self._finish("adapter_mismatch")
            return
        self._session = session
        if self._first is not None and not self._first.done():
            self._first.set_result(session)
        if self._on_session is not None:
            self._on_session(session)
        if session.state is SessionState.ENDED:
            self._finish("ended")

    def _resolved_message(self, params: dict[str, Any]) -> None:
        if self._ended is not None:
            return
        try:
            resolved = messages.DecisionResolved.model_validate(params)
        except ValidationError as error:
            logger.warning("忽略了不合法的 %s：%s", messages.DECISION_RESOLVED, error)
            return
        if self._on_decision_resolved is not None:
            self._on_decision_resolved(resolved)

    def _mismatch(self) -> str:
        other = None if self._session is None else self._session.adapter
        return f"操作员控制台的会话是适配包 {other} 的，本进程加载的是 {self.adapter.id}"

    def _finish(self, reason: str) -> None:
        """按会话结束处理，只第一次算数；适配包不符时断开。"""
        if self._ended is not None:
            return
        self._ended = reason
        peer = self._peer
        if reason == "adapter_mismatch" and peer is not None:
            task = asyncio.get_running_loop().create_task(peer.close())
            _background.add(task)
            task.add_done_callback(_background.discard)
        if self._on_ended is not None:
            try:
                self._on_ended(reason)
            except Exception:
                logger.exception("on_ended 出错")


_background: set[asyncio.Task[None]] = set()
