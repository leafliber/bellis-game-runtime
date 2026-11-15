"""逐步控制产出者的注册和撤销。核内的代次与释放协议不经策略实现。"""

import asyncio
from collections.abc import Callable
from typing import Final, cast

from core_protocol.models import InputReleasedParams, ReleaseReason
from game_runtime.adapter import GAME_ADAPTER
from game_runtime.kernel import CONTROL_SUBMIT, Manifest, Plugin, PluginContext, Tier
from game_runtime.models import VLM
from game_runtime.ocr import OCR
from game_runtime.operator import Clock, OperatorClient, messages
from game_runtime.scene import SCENE
from game_runtime.step_control.contracts import PLANNER, POLICY, PlannerPort, Policy
from game_runtime.step_control.controller import StepConfig, StepController
from game_runtime.step_control.supervision import OperatorGoalRelay, PlannerBinding, SessionProgress

_INTERRUPTS: Final = frozenset(
    {
        ReleaseReason.CORE_START,
        ReleaseReason.HEARTBEAT_TIMEOUT,
        ReleaseReason.FRAME_STALE,
        ReleaseReason.LEASE_EXPIRED,
        ReleaseReason.LEASE_REVOKED,
        ReleaseReason.LEASE_SOURCE_LOST,
        ReleaseReason.NO_LEASE,
        ReleaseReason.FOCUS_LOST,
        ReleaseReason.TAKEOVER,
        ReleaseReason.EMERGENCY_STOP,
        ReleaseReason.INJECTION_BLOCKED,
        ReleaseReason.HOOK_LOST,
        ReleaseReason.STOP,
    }
)


class SessionSignals:
    """将宿主的操作员和实时核回调交给当前插件实例。"""

    def __init__(self) -> None:
        self.controller: StepController | None = None
        self.ended = asyncio.Event()
        self.progress = SessionProgress()
        self.operator_goal = OperatorGoalRelay()
        self.planner_binding = PlannerBinding()

    def session(self, message: messages.Session) -> None:
        if self.controller is not None:
            self.controller.on_session(message)

    def resolved(self, message: messages.DecisionResolved) -> None:
        if self.controller is not None:
            self.controller.on_resolved(message)

    def on_end(self, _reason: str) -> None:
        self.ended.set()
        if self.controller is not None:
            self.controller.stop()

    def became_ready(self) -> None:
        if self.controller is not None:
            self.controller.wake()

    def restart(self, _attempt: int) -> None:
        if self.controller is not None:
            self.controller.interrupt()

    def released(self, message: InputReleasedParams) -> None:
        if self.controller is not None and message.reason in _INTERRUPTS:
            self.controller.on_release(message.reason)


def step_plugin_for(
    operator: OperatorClient,
    policy: Policy | None,
    signals: SessionSignals,
    clock: Clock,
    core_ready: Callable[[], bool],
    *,
    model_policy: Callable[[PluginContext[StepConfig]], Policy] | None = None,
) -> type[Plugin[StepConfig]]:
    if (policy is None) == (model_policy is None):
        raise ValueError("须指定固定策略或模型策略工厂，二选一")
    policy_dependencies = (f"{PLANNER.ref}?",) if model_policy is None else (VLM.ref, PLANNER.ref)

    class StepPlugin(Plugin[StepConfig]):
        manifest = Manifest(
            id="step-control",
            version="1",
            tier=Tier.CRITICAL,
            provides=(POLICY.ref,),
            inject=(GAME_ADAPTER.ref, SCENE.ref, OCR.ref, *policy_dependencies),
            permissions=(CONTROL_SUBMIT,),
            config=StepConfig,
        )

        def __init__(self) -> None:
            self.controller: StepController | None = None

        async def activate(self, ctx: PluginContext[StepConfig]) -> None:
            grant = ctx.control
            if grant is None:
                raise RuntimeError("逐步控制需要内核选定控制产出者")
            scene = ctx.service(SCENE)

            def planner() -> PlannerPort | None:
                return ctx.optional(PLANNER) if model_policy is None else ctx.service(PLANNER)

            controller = StepController(
                ctx.service(GAME_ADAPTER),
                scene,
                ctx.service(OCR),
                grant,
                operator,
                cast(Policy, policy) if model_policy is None else model_policy(ctx),
                clock,
                ctx.config,
                core_ready,
                planner,
                signals.progress,
                signals.operator_goal,
                signals.planner_binding,
            )
            self.controller = controller
            signals.controller = controller

            def detach() -> None:
                if signals.controller is controller:
                    signals.controller = None

            ctx.register(detach)
            ctx.register(scene.subscribe(controller.observe))
            if operator.session is not None:
                controller.on_session(operator.session)
            if scene.latest is not None:
                controller.observe(scene.latest)
            task = asyncio.create_task(controller.run(), name=ctx.instance_id)

            async def stop() -> None:
                controller.stop()
                await task

            ctx.register(stop)
            ctx.provide(POLICY, controller)

        async def quiesce(self, ctx: PluginContext[StepConfig]) -> None:
            if self.controller is not None:
                self.controller.stop()

    return StepPlugin
