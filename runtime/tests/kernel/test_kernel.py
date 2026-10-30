"""插件内核：覆盖模块文档验收表 G0 一行，另加注册的撤销顺序与单一控制产出者。

时钟与实时核都用假实现：时钟由测试手动推进，不真睡；假实时核由测试决定何时确认输入已释放。
"""

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping, Sequence
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core_protocol import models
from game_runtime.kernel import (
    CONTROL_SUBMIT,
    ControlGrant,
    ControlRevokedError,
    Kernel,
    KernelOptions,
    Manifest,
    NoConfig,
    Plugin,
    PluginContext,
    Profile,
    ProfilePatch,
    Reason,
    ReasonCode,
    ResolvedEntry,
    ServiceToken,
    State,
    Submission,
    Tier,
    Transition,
    catalog,
    resolve_profile,
)

GAME = ServiceToken[str]("game-adapter", 1)

type Hook = Callable[[PluginContext[Any]], Awaitable[None]]


def run[**P](test: Callable[P, Coroutine[Any, Any, None]]) -> Callable[P, None]:
    """在新的事件循环里跑异步测试。"""

    @functools.wraps(test)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        asyncio.run(test(*args, **kwargs))

    return wrapper


async def _yield_to_loop() -> None:
    # 让其它任务跑到下一个等待点。内核一次调和中的步骤远少于这个次数。
    for _ in range(100):
        await asyncio.sleep(0)


class ManualClock:
    """手动推进的时钟：`advance` 按到期先后唤醒等待者。"""

    def __init__(self) -> None:
        self.now = 0.0
        self._timers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._seq = 0

    async def sleep(self, seconds: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._seq += 1
        timer = (self.now + seconds, self._seq, future)
        self._timers.append(timer)
        try:
            await future
        finally:
            if timer in self._timers:
                self._timers.remove(timer)

    async def advance(self, seconds: float) -> None:
        target = self.now + seconds
        await _yield_to_loop()
        while due := [timer for timer in self._timers if timer[0] <= target]:
            timer = min(due, key=lambda t: (t[0], t[1]))
            self._timers.remove(timer)
            self.now = timer[0]
            if not timer[2].done():
                timer[2].set_result(None)
            await _yield_to_loop()
        self.now = target
        await _yield_to_loop()

    @property
    def pending(self) -> int:
        return len(self._timers)


class FakeCore:
    """假实时核：记录作废请求；`auto_release` 为假时由测试调用 `release` 确认输入已释放。"""

    def __init__(self, log: list[str], *, auto_release: bool) -> None:
        self.log = log
        self.auto_release = auto_release
        self.fail_invalidate = False
        self.fail_wait = False
        self._released: dict[int, asyncio.Event] = {}

    def release(self, generation: int) -> None:
        self._event(generation).set()

    async def invalidate_generation(self, grant: ControlGrant) -> None:
        state = "有效" if grant.is_open else "已失效"
        self.log.append(f"作废代次 {grant.generation}（提交资格{state}）")
        if self.fail_invalidate:
            raise ConnectionError("核连接已断开")
        if self.auto_release:
            self.release(grant.generation)

    async def wait_released(self, grant: ControlGrant) -> None:
        if self.fail_wait:
            raise ConnectionError("核连接已断开")
        await self._event(grant.generation).wait()
        self.log.append(f"输入已释放 {grant.generation}")

    async def submit(
        self,
        grant: ControlGrant,
        steps: Sequence[models.ControlStep],
        *,
        base_frame: int,
        valid_for: float,
        start_delay: float = 0.0,
    ) -> Submission:
        """记下提交；真实的提交见 `CoreLink.submit` 与 `test_actions.py`。"""
        self.log.append(
            f"提交 {grant.producer}#{grant.generation}：{len(steps)} 步，基准帧 {base_frame}"
        )
        block = models.ActionBlock(
            producer=models.Producer(id=grant.producer, generation=grant.generation),
            seq=1,
            base_frame=base_frame,
            start_ns=0,
            deadline_ns=round((start_delay + valid_for) * 1e9),
            steps=list(steps),
        )
        return _Accepted(block)

    def _event(self, generation: int) -> asyncio.Event:
        return self._released.setdefault(generation, asyncio.Event())


class _Accepted:
    """`FakeCore.submit` 的结果：总是接受，没有一次性动作的结果。"""

    def __init__(self, block: models.ActionBlock) -> None:
        self.block = block
        self.result: models.Accepted | models.Rejected = models.Accepted()

    async def outcomes(self) -> Mapping[int, models.Executed | models.Dropped]:
        return {}


def make_plugin(
    plugin_id: str,
    *,
    tier: Tier = Tier.ORDINARY,
    provides: Iterable[str] = (),
    inject: Iterable[str] = (),
    permissions: Iterable[str] = (),
    config: type[BaseModel] = NoConfig,
    on_activate: Hook | None = None,
    on_quiesce: Hook | None = None,
) -> type[Plugin[Any]]:
    the_manifest = Manifest(
        id=plugin_id,
        version="1.0.0",
        tier=tier,
        provides=tuple(provides),
        inject=tuple(inject),
        permissions=tuple(permissions),
        config=config,
    )

    class TestPlugin(Plugin[Any]):
        manifest = the_manifest

        async def activate(self, ctx: PluginContext[Any]) -> None:
            if on_activate is not None:
                await on_activate(ctx)

        async def quiesce(self, ctx: PluginContext[Any]) -> None:
            if on_quiesce is not None:
                await on_quiesce(ctx)

    return TestPlugin


def adapter(log: list[str], *, major: int = 1) -> type[Plugin[Any]]:
    """游戏适配包的替身：提供 `game-adapter`。"""

    async def on_activate(ctx: PluginContext[Any]) -> None:
        log.append(f"激活 {ctx.entry_id}")
        ctx.provide(ServiceToken[str]("game-adapter", major), "窗口识别")
        ctx.register(lambda: log.append(f"撤销 {ctx.entry_id}"))

    return make_plugin(
        "adapter", tier=Tier.CRITICAL, provides=[f"game-adapter@{major}"], on_activate=on_activate
    )


def consumer(log: list[str]) -> type[Plugin[Any]]:
    """依赖 `game-adapter@1` 的普通插件。"""

    async def on_activate(ctx: PluginContext[Any]) -> None:
        log.append(f"激活 {ctx.entry_id}：拿到{ctx.service(GAME)}")
        ctx.register(lambda: log.append(f"撤销 {ctx.entry_id}"))

    return make_plugin("consumer", inject=[GAME.ref], on_activate=on_activate)


def producer(plugin_id: str, log: list[str], *, inject: Iterable[str] = ()) -> type[Plugin[Any]]:
    """控制产出者：激活与收尾时记下提交资格是否有效。"""

    async def on_activate(ctx: PluginContext[Any]) -> None:
        grant = ctx.control
        assert grant is not None
        state = "有效" if grant.is_open else "未生效"
        log.append(f"激活 {ctx.entry_id}：代次 {grant.generation}，提交资格{state}")
        ctx.register(lambda: log.append(f"撤销 {ctx.entry_id} 注册1"))
        ctx.register(lambda: log.append(f"撤销 {ctx.entry_id} 注册2"))

    async def on_quiesce(ctx: PluginContext[Any]) -> None:
        assert ctx.control is not None
        state = "有效" if ctx.control.is_open else "已失效"
        log.append(f"收尾 {ctx.entry_id}：提交资格{state}")

    return make_plugin(
        plugin_id,
        tier=Tier.CRITICAL,
        inject=inject,
        permissions=[CONTROL_SUBMIT],
        on_activate=on_activate,
        on_quiesce=on_quiesce,
    )


def entry(
    entry_id: str,
    plugin: str | None = None,
    *,
    grant: Iterable[str] = (),
    config: dict[str, Any] | None = None,
    disabled: bool = False,
) -> ResolvedEntry:
    return ResolvedEntry(
        id=entry_id,
        plugin=plugin or entry_id,
        config=config or {},
        disabled=disabled,
        grant=frozenset(grant),
    )


class Harness:
    def __init__(
        self,
        log: list[str],
        *plugins: type[Plugin[Any]],
        auto_release: bool = True,
        options: KernelOptions | None = None,
    ) -> None:
        self.log = log
        self.clock = ManualClock()
        self.core = FakeCore(log, auto_release=auto_release)
        self.transitions: list[Transition] = []
        self.producer_overlaps: list[list[str]] = []
        self._producers = {p.manifest.id for p in plugins if p.manifest.is_control_producer}
        self.kernel = Kernel(
            catalog=catalog(*plugins),
            core=self.core,
            clock=self.clock,
            options=options,
            on_transition=self._record,
        )

    def _record(self, transition: Transition) -> None:
        self.transitions.append(transition)
        holding = [
            s.id
            for s in self.kernel.snapshot()
            if s.plugin in self._producers
            and s.state in (State.LOADING, State.ACTIVE, State.QUIESCING, State.UNLOADING)
        ]
        if len(holding) > 1:
            self.producer_overlaps.append(holding)

    def state(self, entry_id: str) -> State:
        return self.kernel.status(entry_id).state

    def codes(self, entry_id: str) -> list[ReasonCode]:
        return [reason.code for reason in self.kernel.status(entry_id).reasons]

    def path(self, entry_id: str) -> list[tuple[State, State]]:
        """状态的变化，不含只改原因的通知。"""
        return [
            (t.before, t.after)
            for t in self.transitions
            if t.entry_id == entry_id and t.before is not t.after
        ]


# 依赖与 WAITING 原因


@run
async def test_missing_dependency_waits_with_reason_then_activates() -> None:
    log: list[str] = []
    h = Harness(log, adapter(log), consumer(log))
    # 消费方排在提供方前面，提供方还被停用。
    h.kernel.add([entry("consumer"), entry("adapter", disabled=True)])
    await h.kernel.settled()

    status = h.kernel.status("consumer")
    assert status.state is State.WAITING
    assert status.reasons == (
        Reason(ReasonCode.SERVICE_MISSING, "game-adapter@1", "缺少服务 game-adapter@1"),
    )
    assert log == []

    await h.kernel.enable("adapter")
    assert h.state("consumer") is State.ACTIVE
    assert h.kernel.status("consumer").reasons == ()
    assert log == ["激活 adapter", "激活 consumer：拿到窗口识别"]


@run
async def test_major_version_mismatch_is_explained() -> None:
    log: list[str] = []
    h = Harness(log, adapter(log, major=2), consumer(log))
    h.kernel.add([entry("adapter"), entry("consumer")])
    await h.kernel.settled()

    [reason] = h.kernel.status("consumer").reasons
    assert reason.code is ReasonCode.SERVICE_MAJOR_MISMATCH
    assert reason.subject == "game-adapter@1"
    assert "需要 @1，现有 @2" in reason.message
    assert log == ["激活 adapter"]


@run
async def test_lost_dependency_returns_consumer_to_waiting_then_reactivates() -> None:
    log: list[str] = []
    h = Harness(log, adapter(log), consumer(log))
    h.kernel.add([entry("adapter"), entry("consumer")])
    await h.kernel.settled()
    first_instance = h.kernel.status("consumer").instance_id
    log.clear()

    await h.kernel.disable("adapter")
    # 消费方先于提供方停用。
    assert log == ["撤销 consumer", "撤销 adapter"]
    assert h.state("adapter") is State.DISPOSED
    assert h.codes("consumer") == [ReasonCode.SERVICE_MISSING]
    assert h.path("consumer")[-3:] == [
        (State.ACTIVE, State.QUIESCING),
        (State.QUIESCING, State.UNLOADING),
        (State.UNLOADING, State.WAITING),
    ]
    quiescing = next(t for t in h.transitions if t.after is State.QUIESCING)
    assert [r.code for r in quiescing.reasons] == [ReasonCode.PROVIDER_STOPPING]

    await h.kernel.enable("adapter")
    assert h.state("consumer") is State.ACTIVE
    assert h.kernel.status("consumer").instance_id != first_instance


@run
async def test_plugin_only_reaches_declared_services() -> None:
    errors: list[str] = []

    async def nosy_activate(ctx: PluginContext[Any]) -> None:
        for attempt in (lambda: ctx.service(GAME), lambda: ctx.provide(GAME, "冒充")):
            try:
                attempt()
            except LookupError as error:
                errors.append(str(error))

    log: list[str] = []
    nosy = make_plugin("nosy", on_activate=nosy_activate)
    h = Harness(log, adapter(log), nosy)
    h.kernel.add([entry("adapter"), entry("nosy")])
    await h.kernel.settled()
    assert errors == ["nosy 没有声明依赖 game-adapter@1", "nosy 没有声明提供 game-adapter@1"]


@run
async def test_missing_declared_service_fails_activation() -> None:
    liar = make_plugin("liar", provides=[GAME.ref])
    h = Harness([], liar, options=KernelOptions(retry_delays=()))
    h.kernel.add([entry("liar")])
    await h.kernel.settled()
    [reason] = h.kernel.status("liar").reasons
    assert reason.code is ReasonCode.RETRIES_EXHAUSTED
    assert "激活后没有提供 game-adapter@1" in reason.message


# 权限


@run
async def test_ungranted_permission_keeps_plugin_waiting() -> None:
    log: list[str] = []
    seen: list[frozenset[str]] = []

    async def on_activate(ctx: PluginContext[Any]) -> None:
        log.append(f"激活 {ctx.entry_id}")
        seen.append(ctx.permissions)

    ocr = make_plugin("ocr", permissions=["frames.read"], on_activate=on_activate)
    h = Harness(log, ocr)
    h.kernel.add([entry("ocr-a", "ocr"), entry("ocr-b", "ocr", grant=["frames.read", "x.y"])])
    await h.kernel.settled()

    assert h.kernel.status("ocr-a").reasons == (
        Reason(ReasonCode.PERMISSION_NOT_GRANTED, "frames.read", "profile 未授予权限 frames.read"),
    )
    assert h.state("ocr-a") is State.WAITING
    assert h.state("ocr-b") is State.ACTIVE
    assert log == ["激活 ocr-b"]
    # 插件只看到自己申请且已授予的权限。
    assert seen == [frozenset({"frames.read"})]


@run
async def test_control_submit_goes_only_to_designated_producer() -> None:
    log: list[str] = []
    h = Harness(log, producer("step", log), producer("vla", log), consumer(log))
    h.kernel.add(
        [
            entry("step", grant=[CONTROL_SUBMIT]),
            entry("vla", grant=[CONTROL_SUBMIT]),
            entry("ungranted", "step"),
            entry("consumer"),
        ]
    )
    await h.kernel.settled()
    assert h.codes("step") == [ReasonCode.CONTROL_NOT_DESIGNATED]
    assert "当前控制产出者：未指定" in h.kernel.status("step").reasons[0].message
    assert h.kernel.active_control is None

    await h.kernel.set_control_producer("step")
    assert h.state("step") is State.ACTIVE
    assert h.codes("vla") == [ReasonCode.CONTROL_NOT_DESIGNATED]
    assert "当前控制产出者：step" in h.kernel.status("vla").reasons[0].message
    assert h.codes("ungranted") == [ReasonCode.PERMISSION_NOT_GRANTED]
    grant = h.kernel.active_control
    assert grant is not None
    assert (grant.producer, grant.generation) == ("step", 1)
    grant.check()

    with pytest.raises(ValueError, match=r"没有授予 ungranted control\.submit"):
        await h.kernel.set_control_producer("ungranted")
    with pytest.raises(ValueError, match=r"没有申请 control\.submit"):
        await h.kernel.set_control_producer("consumer")

    await h.kernel.set_control_producer(None)
    assert h.codes("step") == [ReasonCode.CONTROL_NOT_DESIGNATED]
    assert h.kernel.active_control is None
    with pytest.raises(ControlRevokedError):
        grant.check()
    assert h.producer_overlaps == []


@run
async def test_the_grant_submits_through_the_core_only_while_active() -> None:
    log: list[str] = []
    h = Harness(log, producer("step", log))
    h.kernel.add([entry("step", grant=[CONTROL_SUBMIT])])
    await h.kernel.set_control_producer("step")
    grant = h.kernel.active_control
    assert grant is not None
    steps = [models.ControlStep(offset_ns=0, held=[], actions=[])]

    submission = await grant.submit(steps, base_frame=7, valid_for=0.5)
    assert isinstance(submission.result, models.Accepted)
    assert submission.block.producer == models.Producer(id="step", generation=1)
    assert log[-1] == "提交 step#1：1 步，基准帧 7"

    # 资格收回后什么也不发。
    await h.kernel.set_control_producer(None)
    sent = len(log)
    with pytest.raises(ControlRevokedError):
        await grant.submit(steps, base_frame=7, valid_for=0.5)
    assert not any(line.startswith("提交") for line in log[sent:])
    # 没有接到实时核的资格（只在测试里手工造出）不能提交。
    detached = ControlGrant("step", "step#9", 9, lambda: True)
    with pytest.raises(RuntimeError, match="没有接到实时核"):
        await detached.submit(steps, base_frame=7, valid_for=0.5)


def test_manifest_rules() -> None:
    def manifest(**fields: Any) -> Manifest:
        return Manifest.model_validate({"id": "p", "version": "1", "tier": "ordinary", **fields})

    assert manifest(provides=["a@1"], inject=["b.c@2"]).inject == ("b.c@2",)
    with pytest.raises(ValidationError, match="必须是关键平面"):
        manifest(permissions=[CONTROL_SUBMIT])
    assert manifest(tier="critical", permissions=[CONTROL_SUBMIT]).is_control_producer
    with pytest.raises(ValidationError, match="provides 中服务名重复：a"):
        manifest(provides=["a@1", "a@2"])
    with pytest.raises(ValidationError, match="inject 中服务名重复：b"):
        manifest(inject=["b@1", "b@1"])
    for bad in (
        {"inject": ["a"]},
        {"inject": ["A@1"]},
        {"permissions": ["a b"]},
        {"tier": "core"},
        {"process": "sidecar"},
    ):
        with pytest.raises(ValidationError):
            manifest(**bad)


# 配置


class OcrConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lang: str = "zh"
    rate: float = Field(default=2.0, gt=0)


@run
async def test_config_gets_defaults_then_is_validated() -> None:
    received: dict[str, BaseModel] = {}

    async def on_activate(ctx: PluginContext[Any]) -> None:
        received[ctx.entry_id] = ctx.config

    ocr = make_plugin("ocr", config=OcrConfig, on_activate=on_activate)
    h = Harness([], ocr)
    h.kernel.add(
        [
            entry("defaults", "ocr"),
            entry("partial", "ocr", config={"rate": 5}),
            entry("invalid", "ocr", config={"rate": 0}),
            entry("unknown", "ocr", config={"lagn": "ja"}),
        ]
    )
    await h.kernel.settled()

    assert received == {"defaults": OcrConfig(), "partial": OcrConfig(lang="zh", rate=5)}
    for entry_id, where in (("invalid", "rate"), ("unknown", "lagn")):
        [reason] = h.kernel.status(entry_id).reasons
        assert h.state(entry_id) is State.FAILED
        assert reason.code is ReasonCode.CONFIG_INVALID
        assert reason.message.startswith(f"配置不合法：{where} ")
    # 配置不合法不重试。
    assert h.clock.pending == 0
    await h.clock.advance(60)
    await h.kernel.settled()
    assert set(received) == {"defaults", "partial"}


# FAILED 与退避重试


@run
async def test_activation_failure_retries_with_backoff_then_stops() -> None:
    log: list[str] = []
    attempts = 0

    async def on_activate(ctx: PluginContext[Any]) -> None:
        nonlocal attempts
        attempts += 1
        n = attempts
        ctx.register(lambda: log.append(f"撤销第 {n} 次激活的注册"))
        raise RuntimeError("设备未就绪")

    h = Harness(log, make_plugin("flaky", on_activate=on_activate))
    h.kernel.add([entry("flaky")])
    await h.kernel.settled()

    def reason() -> Reason:
        [only] = h.kernel.status("flaky").reasons
        return only

    assert (h.state("flaky"), attempts) == (State.FAILED, 1)
    assert reason() == Reason(
        ReasonCode.ACTIVATION_FAILED,
        "flaky",
        "激活失败：设备未就绪；1 秒后第 1 次重试（共 3 次）",
    )

    # 缺省退避 1、2、4 秒：未到时不重试。
    for delay, retry in ((1, 1), (2, 2), (4, 3)):
        await h.clock.advance(delay - 0.001)
        await h.kernel.settled()
        assert attempts == retry
        await h.clock.advance(0.001)
        await h.kernel.settled()
        assert attempts == retry + 1

    assert h.state("flaky") is State.FAILED
    assert reason().code is ReasonCode.RETRIES_EXHAUSTED
    assert "3 次重试已用尽" in reason().message
    assert h.kernel.status("flaky").attempts == 4
    assert h.clock.pending == 0
    await h.clock.advance(3600)
    await h.kernel.settled()
    assert attempts == 4
    # 每次失败都撤销了那次激活注册的副作用。
    assert log == [f"撤销第 {n} 次激活的注册" for n in range(1, 5)]

    # 操作员停用再启用：计数清零，重新尝试。
    await h.kernel.disable("flaky")
    await h.kernel.enable("flaky")
    assert attempts == 5
    assert reason().code is ReasonCode.ACTIVATION_FAILED


@run
async def test_retry_settings_are_configurable_and_success_resets_count() -> None:
    attempts = 0

    async def on_activate(ctx: PluginContext[Any]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("还没好")

    h = Harness(
        [],
        make_plugin("flaky", on_activate=on_activate),
        options=KernelOptions(retry_delays=(0.5, 0.5)),
    )
    h.kernel.add([entry("flaky")])
    await h.kernel.settled()
    assert "0.5 秒后第 1 次重试（共 2 次）" in h.kernel.status("flaky").reasons[0].message
    await h.clock.advance(0.5)
    await h.kernel.settled()
    assert "0.5 秒后第 2 次重试（共 2 次）" in h.kernel.status("flaky").reasons[0].message
    await h.clock.advance(0.5)
    await h.kernel.settled()
    status = h.kernel.status("flaky")
    assert (status.state, status.attempts, attempts) == (State.ACTIVE, 0, 3)


@run
async def test_activation_timeout_is_a_failure() -> None:
    log: list[str] = []

    async def on_activate(ctx: PluginContext[Any]) -> None:
        ctx.register(lambda: log.append("撤销激活中的注册"))
        await asyncio.Event().wait()

    h = Harness(log, make_plugin("slow", on_activate=on_activate))
    h.kernel.add([entry("slow")])
    await h.clock.advance(9.999)
    assert h.state("slow") is State.LOADING
    await h.clock.advance(0.001)
    await h.kernel.settled()
    assert h.state("slow") is State.FAILED
    assert "未在 10 秒内完成" in h.kernel.status("slow").reasons[0].message
    assert log == ["撤销激活中的注册"]


# 可撤销注册


@run
async def test_registrations_are_revoked_last_in_first_out_despite_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    log: list[str] = []

    async def on_activate(ctx: PluginContext[Any]) -> None:
        def broken() -> None:
            raise RuntimeError("撤销出错")

        async def hangs() -> None:
            log.append("撤销 4 开始")
            await asyncio.Event().wait()

        async def undo_async() -> None:
            log.append("撤销 5")

        ctx.register(lambda: log.append("撤销 1"))
        ctx.register(broken)
        ctx.register(lambda: log.append("撤销 3"))
        ctx.register(hangs)
        ctx.register(undo_async)
        ctx.register(lambda: log.append("撤销 6"))

    h = Harness(log, make_plugin("p", on_activate=on_activate))
    h.kernel.add([entry("p")])
    await h.kernel.settled()

    disabling = asyncio.create_task(h.kernel.disable("p"))
    await h.clock.advance(1.999)
    assert log == ["撤销 6", "撤销 5", "撤销 4 开始"]
    await h.clock.advance(0.001)
    await disabling

    assert log == ["撤销 6", "撤销 5", "撤销 4 开始", "撤销 3", "撤销 1"]
    assert h.state("p") is State.DISPOSED
    failures = [r for r in caplog.records if "撤销失败" in r.getMessage()]
    assert len(failures) == 2
    assert all(r.levelno == logging.ERROR for r in failures)


# 控制产出者的 QUIESCING


@run
async def test_producer_quiescing_runs_in_order() -> None:
    log: list[str] = []
    h = Harness(log, producer("step", log), auto_release=False)
    h.kernel.add([entry("step", grant=[CONTROL_SUBMIT])])
    await h.kernel.set_control_producer("step")
    assert log == ["激活 step：代次 1，提交资格未生效"]
    grant = h.kernel.active_control
    assert grant is not None
    grant.check()
    log.clear()

    disabling = asyncio.create_task(h.kernel.disable("step"))
    await h.clock.advance(1)
    # 提交资格先失效，再请求作废代次，插件随后收尾；还没收到确认，不撤销注册。
    assert log == ["作废代次 1（提交资格已失效）", "收尾 step：提交资格已失效"]
    assert h.state("step") is State.QUIESCING
    assert h.kernel.active_control is None
    with pytest.raises(ControlRevokedError):
        grant.check()

    h.core.release(1)
    await disabling
    assert log == [
        "作废代次 1（提交资格已失效）",
        "收尾 step：提交资格已失效",
        "输入已释放 1",
        "撤销 step 注册2",
        "撤销 step 注册1",
    ]
    assert h.state("step") is State.DISPOSED
    assert h.path("step")[-3:] == [
        (State.ACTIVE, State.QUIESCING),
        (State.QUIESCING, State.UNLOADING),
        (State.UNLOADING, State.DISPOSED),
    ]


@run
async def test_release_timeout_keeps_quiescing_and_reports(
    caplog: pytest.LogCaptureFixture,
) -> None:
    log: list[str] = []
    h = Harness(log, producer("step", log), auto_release=False)
    h.kernel.add([entry("step", grant=[CONTROL_SUBMIT])])
    await h.kernel.set_control_producer("step")
    log.clear()

    disabling = asyncio.create_task(h.kernel.disable("step"))
    await h.clock.advance(4.999)
    assert not disabling.done()
    await h.clock.advance(0.001)
    await disabling

    status = h.kernel.status("step")
    assert status.state is State.QUIESCING
    [reason] = status.reasons
    assert reason.code is ReasonCode.RELEASE_TIMEOUT
    assert "未在 5 秒内收到代次 1 输入已释放的确认" in reason.message
    # 上报：状态变化的通知与告警日志。
    assert h.transitions[-1] == Transition("step", State.QUIESCING, State.QUIESCING, (reason,))
    assert any(
        r.levelno == logging.WARNING and "保持 QUIESCING" in r.getMessage() for r in caplog.records
    )

    # 不因超时放弃等待，也不撤销注册。
    await h.clock.advance(3600)
    await h.kernel.settled()
    assert h.state("step") is State.QUIESCING
    assert not any(line.startswith("撤销") for line in log)

    h.core.release(1)
    await h.clock.advance(0)
    await h.kernel.settled()
    assert log[-3:] == ["输入已释放 1", "撤销 step 注册2", "撤销 step 注册1"]
    assert h.state("step") is State.DISPOSED


@run
async def test_failed_invalidation_still_waits_for_release() -> None:
    log: list[str] = []
    h = Harness(log, producer("step", log), auto_release=False)
    h.kernel.add([entry("step", grant=[CONTROL_SUBMIT])])
    await h.kernel.set_control_producer("step")
    h.core.fail_invalidate = True
    log.clear()

    disabling = asyncio.create_task(h.kernel.disable("step"))
    await h.clock.advance(1)
    assert h.state("step") is State.QUIESCING
    assert not any(line.startswith("撤销") for line in log)
    # 例如实时核按生命线自行释放后，实现据此确认。
    h.core.release(1)
    await disabling
    assert log[-3:] == ["输入已释放 1", "撤销 step 注册2", "撤销 step 注册1"]


@run
async def test_failed_release_wait_keeps_quiescing() -> None:
    log: list[str] = []
    h = Harness(log, producer("step", log), auto_release=False)
    h.kernel.add([entry("step", grant=[CONTROL_SUBMIT])])
    await h.kernel.set_control_producer("step")
    h.core.fail_wait = True

    await h.kernel.disable("step")
    assert h.state("step") is State.QUIESCING
    [reason] = h.kernel.status("step").reasons
    assert reason.code is ReasonCode.RELEASE_FAILED
    assert "核连接已断开" in reason.message
    assert not any(line.startswith("撤销") for line in log)


@run
async def test_switching_producer_finishes_old_quiescing_first() -> None:
    log: list[str] = []
    h = Harness(log, producer("step", log), producer("vla", log), auto_release=False)
    h.kernel.add([entry("step", grant=[CONTROL_SUBMIT]), entry("vla", grant=[CONTROL_SUBMIT])])
    await h.kernel.set_control_producer("step")
    log.clear()

    switching = asyncio.create_task(h.kernel.set_control_producer("vla"))
    await h.clock.advance(1)
    assert h.state("step") is State.QUIESCING
    assert h.state("vla") is State.WAITING
    h.core.release(1)
    await switching

    assert log == [
        "作废代次 1（提交资格已失效）",
        "收尾 step：提交资格已失效",
        "输入已释放 1",
        "撤销 step 注册2",
        "撤销 step 注册1",
        "激活 vla：代次 2，提交资格未生效",
    ]
    assert h.state("vla") is State.ACTIVE
    assert h.codes("step") == [ReasonCode.CONTROL_NOT_DESIGNATED]
    grant = h.kernel.active_control
    assert grant is not None
    assert (grant.producer, grant.generation) == ("vla", 2)
    assert h.producer_overlaps == []


@run
async def test_new_producer_waits_while_old_one_is_stuck() -> None:
    log: list[str] = []
    h = Harness(log, producer("step", log), producer("vla", log), auto_release=False)
    h.kernel.add([entry("step", grant=[CONTROL_SUBMIT]), entry("vla", grant=[CONTROL_SUBMIT])])
    await h.kernel.set_control_producer("step")

    switching = asyncio.create_task(h.kernel.set_control_producer("vla"))
    await h.clock.advance(5)
    await switching
    assert h.codes("step") == [ReasonCode.RELEASE_TIMEOUT]
    assert h.kernel.status("vla").reasons == (
        Reason(
            ReasonCode.CONTROL_HANDOVER_PENDING, "step", "上一个控制产出者 step 尚未完成 QUIESCING"
        ),
    )
    await h.clock.advance(3600)
    await h.kernel.settled()
    assert h.state("vla") is State.WAITING
    assert h.kernel.active_control is None

    h.core.release(1)
    await h.clock.advance(0)
    await h.kernel.settled()
    assert h.state("step") is State.WAITING
    assert h.state("vla") is State.ACTIVE
    assert h.producer_overlaps == []


@run
async def test_provider_waits_for_stuck_producer_before_stopping() -> None:
    log: list[str] = []
    h = Harness(log, adapter(log), producer("step", log, inject=[GAME.ref]), auto_release=False)
    h.kernel.add([entry("adapter"), entry("step", grant=[CONTROL_SUBMIT])])
    await h.kernel.set_control_producer("step")
    assert h.state("step") is State.ACTIVE
    log.clear()

    disabling = asyncio.create_task(h.kernel.disable("adapter"))
    await h.clock.advance(5)
    await disabling
    # 依赖消失时控制产出者同样走 QUIESCING；它等确认期间，提供方不停。
    assert h.codes("step") == [ReasonCode.RELEASE_TIMEOUT]
    assert h.state("adapter") is State.ACTIVE
    assert h.kernel.status("adapter").reasons == (
        Reason(
            ReasonCode.DEPENDENT_QUIESCING, "step", "依赖它的 step 尚未完成 QUIESCING，完成后再停用"
        ),
    )
    assert not any(line.startswith("撤销") for line in log)

    h.core.release(1)
    await h.clock.advance(0)
    await h.kernel.settled()
    assert log[-4:] == ["输入已释放 1", "撤销 step 注册2", "撤销 step 注册1", "撤销 adapter"]
    assert h.state("adapter") is State.DISPOSED
    assert h.codes("step") == [ReasonCode.SERVICE_MISSING]


# 停止内核


@run
async def test_shutdown_stops_entries_in_reverse_profile_order() -> None:
    log: list[str] = []
    h = Harness(log, adapter(log), consumer(log), producer("step", log, inject=[GAME.ref]))
    h.kernel.add([entry("adapter"), entry("consumer"), entry("step", grant=[CONTROL_SUBMIT])])
    await h.kernel.set_control_producer("step")
    log.clear()

    await h.kernel.shutdown()
    assert log == [
        "作废代次 1（提交资格已失效）",
        "输入已释放 1",
        "收尾 step：提交资格已失效",
        "撤销 step 注册2",
        "撤销 step 注册1",
        "撤销 consumer",
        "撤销 adapter",
    ]
    for entry_id in ("adapter", "consumer", "step"):
        status = h.kernel.status(entry_id)
        assert status.state is State.DISPOSED
        assert status.reasons == (Reason(ReasonCode.DISABLED, entry_id, "内核已停止"),)
        # 停止不改条目的启用开关。
        assert status.enabled
    # 停止之后不再激活，也不再加入条目。
    await h.kernel.enable("step")
    assert h.state("step") is State.DISPOSED
    with pytest.raises(RuntimeError, match="内核已停止"):
        h.kernel.add([entry("other", "consumer")])


@run
async def test_shutdown_reports_release_timeout_then_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    log: list[str] = []
    h = Harness(log, adapter(log), producer("step", log, inject=[GAME.ref]), auto_release=False)
    h.kernel.add([entry("adapter"), entry("step", grant=[CONTROL_SUBMIT])])
    await h.kernel.set_control_producer("step")
    log.clear()

    stopping = asyncio.create_task(h.kernel.shutdown())
    await h.clock.advance(4.999)
    # 停止时照常等确认，时限内不撤销注册。
    assert not stopping.done()
    assert not any(line.startswith("撤销") for line in log)
    await h.clock.advance(0.001)
    await stopping

    # 等确认超时照常上报，然后撤销注册继续停止，提供方随后停用。
    reported = [t for t in h.transitions if t.entry_id == "step" and t.after is State.QUIESCING]
    assert reported[-1].reasons[0].code is ReasonCode.RELEASE_TIMEOUT
    assert h.path("step")[-3:] == [
        (State.ACTIVE, State.QUIESCING),
        (State.QUIESCING, State.UNLOADING),
        (State.UNLOADING, State.DISPOSED),
    ]
    assert log[-3:] == ["撤销 step 注册2", "撤销 step 注册1", "撤销 adapter"]
    assert h.state("adapter") is State.DISPOSED
    assert any(
        r.levelno == logging.WARNING and "撤销其注册继续停止" in r.getMessage()
        for r in caplog.records
    )
    # 迟到的确认不再改变什么。
    h.core.release(1)
    await h.clock.advance(0)
    await h.kernel.settled()
    assert h.state("step") is State.DISPOSED


@run
async def test_shutdown_does_not_wait_again_for_a_stuck_producer() -> None:
    log: list[str] = []
    h = Harness(log, producer("step", log), auto_release=False)
    h.kernel.add([entry("step", grant=[CONTROL_SUBMIT])])
    await h.kernel.set_control_producer("step")
    disabling = asyncio.create_task(h.kernel.disable("step"))
    await h.clock.advance(5)
    await disabling
    assert h.codes("step") == [ReasonCode.RELEASE_TIMEOUT]

    # 已经卡在 QUIESCING 的控制产出者在停止时直接撤销注册，不再计时。
    await h.kernel.shutdown()
    assert h.state("step") is State.DISPOSED
    assert log[-2:] == ["撤销 step 注册2", "撤销 step 注册1"]


@run
async def test_shutdown_continues_after_a_failed_release_wait() -> None:
    log: list[str] = []
    h = Harness(log, producer("step", log), auto_release=False)
    h.kernel.add([entry("step", grant=[CONTROL_SUBMIT])])
    await h.kernel.set_control_producer("step")
    h.core.fail_wait = True

    await h.kernel.shutdown()
    assert h.state("step") is State.DISPOSED
    reported = [t for t in h.transitions if t.after is State.QUIESCING]
    assert reported[-1].reasons[0].code is ReasonCode.RELEASE_FAILED


# profile 交给内核


@run
async def test_profile_and_patch_drive_the_kernel() -> None:
    log: list[str] = []

    async def ocr_activate(ctx: PluginContext[Any]) -> None:
        log.append(f"激活 {ctx.entry_id}：{ctx.config}")

    ocr = make_plugin(
        "ocr", config=OcrConfig, permissions=["frames.read"], on_activate=ocr_activate
    )
    h = Harness(log, adapter(log), ocr)
    profile = Profile.model_validate(
        {
            "name": "sim",
            "packs": [
                {
                    "name": "core",
                    "entries": [
                        {"id": "adapter", "plugin": "adapter"},
                        {
                            "id": "ocr",
                            "plugin": "ocr",
                            "disabled": True,
                            "grant": ["frames.read"],
                            "config": {"lang": "ja"},
                        },
                    ],
                }
            ],
        }
    )
    patch = ProfilePatch.model_validate({"name": "ocr", "ops": [{"op": "enable", "id": "ocr"}]})

    h.kernel.add(resolve_profile(profile, [patch]))
    await h.kernel.settled()
    assert log == ["激活 adapter", "激活 ocr：lang='ja' rate=2.0"]
