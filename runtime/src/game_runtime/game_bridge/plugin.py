"""普通插件拥有语义连接及重连任务。"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from game_runtime.adapter import GAME_ADAPTER
from game_runtime.channel import ChannelError, RpcPeer, connect
from game_runtime.core_link import CoreLink
from game_runtime.game_bridge.projection import Projection
from game_runtime.game_bridge.session import BellisSession
from game_runtime.game_bridge.wire import decode_request
from game_runtime.kernel import Manifest, NoConfig, Plugin, PluginContext, Tier
from game_runtime.narrative import NARRATIVE
from game_runtime.scene import SCENE
from game_runtime.step_control.contracts import PLANNER
from game_runtime.voice_activity import VOICE_ACTIVITY

logger = logging.getLogger(__name__)
ROLE = "game-runtime.main"
REPORT_INTERVAL = 0.5


async def _run(session: BellisSession, select: Callable[[bool], Awaitable[None]]) -> None:
    credentials = session.info.credentials()
    try:
        while session.same_session():
            peer = None
            cleanup: tuple[Callable[[], None], ...] = ()
            try:
                conn = await connect(credentials.path, ROLE, credentials.key)
                peer = RpcPeer(conn, timeout=2.0, json_decoder=decode_request)
                cleanup = session.attach(peer)
                selected = False
                while not peer.closed.done() and session.same_session():
                    if session.live:
                        if not selected:
                            await select(True)
                            selected = True
                        session.tick()
                        session.publish_point()
                        session.notify("health.reported", session.health())
                        project = session.project
                        if project is not None:
                            project.publish(session)
                    await asyncio.wait({peer.closed}, timeout=REPORT_INTERVAL)
            except ChannelError as error:
                logger.info("Bellis 语义连接不可用 %s", error.code)
            finally:
                described = session.described_ns is not None
                session.detach()
                for dispose in cleanup:
                    dispose()
                if peer is not None:
                    peer.connection.abort()
                    await peer.close()
            if described:
                await select(False)
            await asyncio.sleep(REPORT_INTERVAL)
        session.signals.on_end("bellis-session-changed")
    except Exception as error:
        logger.error("Bellis 语义桥停止 %s", type(error).__name__)
        session.signals.on_end("bellis-bridge-failed")
    finally:
        session.detach()


def game_bridge_for(
    session: BellisSession,
    core: CoreLink,
    max_frame_age_ns: int,
    select: Callable[[bool], Awaitable[None]],
) -> type[Plugin[NoConfig]]:
    class GameBridgePlugin(Plugin[NoConfig]):
        manifest = Manifest(
            id="game-bridge",
            version="1",
            tier=Tier.ORDINARY,
            inject=(
                GAME_ADAPTER.ref,
                SCENE.ref,
                f"{PLANNER.ref}?",
                f"{VOICE_ACTIVITY.ref}?",
                f"{NARRATIVE.ref}?",
            ),
            config=NoConfig,
        )

        async def activate(self, ctx: PluginContext[NoConfig]) -> None:
            project = Projection(
                ctx.service(GAME_ADAPTER),
                ctx.service(SCENE),
                core,
                lambda: ctx.optional(PLANNER),
                lambda: ctx.optional(VOICE_ACTIVITY),
                lambda: ctx.optional(NARRATIVE),
                max_frame_age_ns,
            )
            session.project = project
            task = asyncio.create_task(_run(session, select), name=ctx.instance_id)

            async def close() -> None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                session.project = None

            ctx.register(close)

    return GameBridgePlugin
