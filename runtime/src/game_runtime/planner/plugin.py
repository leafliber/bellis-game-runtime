"""普通插件 `planner` 与控制器共享同一个 `planner@1` 契约。"""

from collections.abc import Callable

from game_runtime.adapter import GAME_ADAPTER
from game_runtime.kernel import Manifest, Plugin, PluginContext, Tier
from game_runtime.models import VLM
from game_runtime.narrative import NARRATIVE, NarrativeService
from game_runtime.planner.service import Planner, PlannerConfig
from game_runtime.scene import SCENE, SceneUpdate
from game_runtime.step_control.contracts import PLANNER


class PlannerPlugin(Plugin[PlannerConfig]):
    manifest = Manifest(
        id="planner",
        version="1",
        tier=Tier.ORDINARY,
        provides=(PLANNER.ref,),
        inject=(GAME_ADAPTER.ref, SCENE.ref, VLM.ref, f"{NARRATIVE.ref}?"),
        config=PlannerConfig,
    )

    async def activate(self, ctx: PluginContext[PlannerConfig]) -> None:
        planner = Planner(
            ctx.service(GAME_ADAPTER),
            ctx.service(VLM),
            ctx.config,
            narrative=lambda: ctx.optional(NARRATIVE),
        )
        scene = ctx.service(SCENE)
        followed: NarrativeService | None = None
        releases: list[Callable[[], None]] = []

        def release() -> None:
            for unsubscribe in releases:
                unsubscribe()
            releases.clear()

        def refresh() -> None:
            nonlocal followed
            current = ctx.optional(NARRATIVE)
            if current is followed:
                return
            release()
            followed = current
            if current is None:
                planner.narrative_unavailable()
                return
            releases.extend(
                (
                    current.subscribe(planner.on_record),
                    current.subscribe_cutscenes(planner.on_cutscene),
                )
            )
            latest = current.latest
            if latest is not None:
                for record in current.records(max(0, latest.occurrence - ctx.config.batch_size)):
                    if record.scope == latest.scope:
                        planner.on_record(record)

        def on_scene(update: SceneUpdate) -> None:
            refresh()
            planner.on_scene(update)

        ctx.register(planner.close)
        ctx.register(release)
        refresh()
        ctx.register(scene.subscribe(on_scene))
        if scene.latest is not None:
            planner.on_scene(scene.latest)
        planner.start()
        ctx.provide(PLANNER, planner)
