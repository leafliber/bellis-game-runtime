"""主进程插件内核（GR-02）：按依赖激活，注册的一切都能撤销，控制产出者停用前先等输入释放。

设计见 `docs/modules/plugin-kernel.md`。全部状态变化都在一个串行的调和任务里进行，每一轮：

1. 续完已收到输入已释放确认的 QUIESCING；
2. 停用：按 profile 逆序，被停用或条件不再满足的 ACTIVE 条目经 QUIESCING 卸载，
   依赖它所提供服务的条目先卸载；
3. 激活：按 profile 顺序，加载条件满足的 WAITING 条目。
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, JsonValue, ValidationError

from game_runtime.kernel.clock import Clock, MonotonicClock, finished_within, run_with_deadline
from game_runtime.kernel.control import CONTROL_SUBMIT, ControlGrant, CoreControl
from game_runtime.kernel.manifest import Manifest, parse_ref
from game_runtime.kernel.plugin import Catalog, Disposer, Plugin, PluginContext, Registrations
from game_runtime.kernel.profile import ResolvedEntry
from game_runtime.kernel.service import ServiceRegistry
from game_runtime.kernel.status import EntryStatus, Reason, ReasonCode, State, Transition

logger = logging.getLogger(__name__)

_HOLDS_INSTANCE = frozenset({State.LOADING, State.ACTIVE, State.QUIESCING, State.UNLOADING})


@dataclass(frozen=True, slots=True)
class KernelOptions:
    """时限与退避，单位为秒。缺省值的依据见模块文档。"""

    activate_timeout: float = 10.0
    """激活的截止时间，超时按激活失败处理。"""
    quiesce_timeout: float = 5.0
    """插件 `quiesce` 的截止时间，超时记录后继续停用。"""
    release_timeout: float = 5.0
    """控制产出者从请求作废代次到收到输入已释放确认的上报时限；超时保持 QUIESCING 并继续等。"""
    disposer_timeout: float = 2.0
    """单个异步撤销函数的截止时间，超时记录后继续撤销其余注册。"""
    retry_delays: tuple[float, ...] = (1.0, 2.0, 4.0)
    """激活失败后每次重试前的等待；个数即重试次数，用尽后停在 FAILED。"""

    def __post_init__(self) -> None:
        timeouts = (
            self.activate_timeout,
            self.quiesce_timeout,
            self.release_timeout,
            self.disposer_timeout,
        )
        if any(t <= 0 for t in timeouts):
            raise ValueError("时限必须为正")
        if any(d < 0 for d in self.retry_delays):
            raise ValueError("重试间隔不能为负")


@dataclass(eq=False, slots=True)
class _Instance:
    id: str
    plugin: Plugin[Any]
    context: PluginContext[Any]
    registrations: Registrations
    grant: ControlGrant | None
    release: asyncio.Task[None] | None = None
    """控制产出者的“请求作废代次并等待输入已释放的确认”。"""


@dataclass(eq=False, slots=True)
class _Entry:
    id: str
    plugin: type[Plugin[Any]]
    config: dict[str, JsonValue]
    grant: frozenset[str]
    enabled: bool
    state: State
    reasons: tuple[Reason, ...] = ()
    instance: _Instance | None = None
    attempts: int = 0
    retry: asyncio.Task[None] | None = None
    retry_due: bool = False

    @property
    def manifest(self) -> Manifest:
        return self.plugin.manifest


def _disabled(entry: _Entry) -> Reason:
    return Reason(ReasonCode.DISABLED, entry.id, "已停用")


class Kernel:
    """主进程插件内核。"""

    def __init__(
        self,
        *,
        catalog: Catalog,
        core: CoreControl,
        clock: Clock | None = None,
        options: KernelOptions | None = None,
        on_transition: Callable[[Transition], None] | None = None,
    ) -> None:
        self._catalog = catalog
        self._core = core
        self._clock = clock or MonotonicClock()
        self._options = options or KernelOptions()
        self._on_transition = on_transition
        self._registry = ServiceRegistry()
        self._entries: dict[str, _Entry] = {}
        self._producer: str | None = None
        self._generation = 0
        self._instances = 0
        self._dirty = False
        self._loop: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    # 操作员与宿主的接口。插件拿不到内核，只能经 PluginContext 操作自己的注册。

    def add(self, entries: Sequence[ResolvedEntry]) -> None:
        """按顺序加入 profile 展开后的条目；之后 `await settled()` 等调和完成。

        先校验整批，全部合格才加入。
        """
        ids = set(self._entries)
        batch: list[_Entry] = []
        for resolved in entries:
            if resolved.id in ids:
                raise ValueError(f"条目 {resolved.id} 重复")
            ids.add(resolved.id)
            plugin = self._catalog.get(resolved.plugin)
            if plugin is None:
                raise ValueError(f"条目 {resolved.id} 引用了未知插件 {resolved.plugin}")
            entry = _Entry(
                id=resolved.id,
                plugin=plugin,
                config=dict(resolved.config),
                grant=resolved.grant,
                enabled=not resolved.disabled,
                state=State.DISPOSED if resolved.disabled else State.WAITING,
            )
            if resolved.disabled:
                entry.reasons = (_disabled(entry),)
            batch.append(entry)
        for entry in batch:
            self._entries[entry.id] = entry
        self._request()

    async def settled(self) -> None:
        """等调和任务结束。控制产出者等输入释放的确认超时后不阻塞这里。"""
        while self._loop is not None and not self._loop.done():
            await asyncio.shield(self._loop)

    async def enable(self, entry_id: str) -> None:
        """启用条目。停用后再启用时重试计数清零。"""
        self._entry(entry_id).enabled = True
        self._request()
        await self.settled()

    async def disable(self, entry_id: str) -> None:
        """停用条目：经 QUIESCING 卸载，保留条目。"""
        self._entry(entry_id).enabled = False
        self._request()
        await self.settled()

    async def set_control_producer(self, entry_id: str | None) -> None:
        """指定当前控制产出者；None 表示不要控制产出者。

        旧的控制产出者先走完 QUIESCING，新的才激活；旧的等确认超时时新的停在 WAITING。
        """
        if entry_id is not None:
            entry = self._entry(entry_id)
            if not entry.manifest.is_control_producer:
                raise ValueError(f"{entry_id} 的插件没有申请 {CONTROL_SUBMIT}，不能作为控制产出者")
            if CONTROL_SUBMIT not in entry.grant:
                raise ValueError(f"profile 没有授予 {entry_id} {CONTROL_SUBMIT}")
        self._producer = entry_id
        self._request()
        await self.settled()

    @property
    def control_producer(self) -> str | None:
        """被指定为控制产出者的条目 id；它可能还没有激活。"""
        return self._producer

    @property
    def active_control(self) -> ControlGrant | None:
        """当前有效的提交资格：控制产出者处于 ACTIVE 时才有。"""
        for entry in self._entries.values():
            instance = entry.instance
            if instance is not None and instance.grant is not None and instance.grant.is_open:
                return instance.grant
        return None

    def status(self, entry_id: str) -> EntryStatus:
        return self._status(self._entry(entry_id))

    def snapshot(self) -> list[EntryStatus]:
        return [self._status(entry) for entry in self._entries.values()]

    # 调和

    def _request(self) -> None:
        self._dirty = True
        if self._loop is None or self._loop.done():
            self._loop = asyncio.get_running_loop().create_task(self._run())
            self._loop.add_done_callback(_log_loop_failure)

    async def _run(self) -> None:
        while self._dirty:
            self._dirty = False
            await self._pass()

    async def _pass(self) -> None:
        entries = list(self._entries.values())

        for entry in reversed(entries):
            instance = entry.instance
            if (
                entry.state is State.QUIESCING
                and instance is not None
                and instance.release is not None
                and instance.release.done()
            ):
                await self._after_release(entry, instance)

        for entry in reversed(entries):
            if entry.state is not State.ACTIVE:
                continue
            if not entry.enabled:
                await self._unload(entry, (_disabled(entry),))
                continue
            blockers = self._blockers(entry)
            if blockers:
                await self._unload(entry, blockers)
            else:
                self._set(entry, State.ACTIVE, ())

        for entry in entries:
            if not entry.enabled:
                if entry.state in (State.WAITING, State.FAILED):
                    self._cancel_retry(entry)
                    self._set(entry, State.DISPOSED, (_disabled(entry),))
                continue
            if entry.state is State.DISPOSED:
                entry.attempts = 0
                self._set(entry, State.WAITING, ())
            if entry.state is State.FAILED and entry.retry_due:
                entry.retry_due = False
                self._set(entry, State.WAITING, ())
            if entry.state is not State.WAITING:
                continue
            config = self._config(entry)
            if isinstance(config, Reason):
                # 配置不合法，重试也不会好：直接停在 FAILED。
                self._set(entry, State.FAILED, (config,))
                continue
            blockers = self._blockers(entry)
            if blockers:
                self._set(entry, State.WAITING, blockers)
                continue
            await self._load(entry, config)
            self._dirty = True

    def _blockers(self, entry: _Entry) -> tuple[Reason, ...]:
        """WAITING 的原因：缺少或不兼容的服务、未授予的权限、控制产出者的指定与交接。"""
        manifest = entry.manifest
        reasons: list[Reason] = []
        for ref in manifest.inject:
            missing = self._registry.missing(*parse_ref(ref))
            if missing is not None:
                reasons.append(missing)
        for permission in manifest.permissions:
            if permission not in entry.grant:
                reasons.append(
                    Reason(
                        ReasonCode.PERMISSION_NOT_GRANTED,
                        permission,
                        f"profile 未授予权限 {permission}",
                    )
                )
        if manifest.is_control_producer and CONTROL_SUBMIT in entry.grant:
            if self._producer != entry.id:
                reasons.append(
                    Reason(
                        ReasonCode.CONTROL_NOT_DESIGNATED,
                        CONTROL_SUBMIT,
                        f"{CONTROL_SUBMIT} 只授予当前控制产出者，"
                        f"当前控制产出者：{self._producer or '未指定'}",
                    )
                )
            else:
                reasons.extend(
                    Reason(
                        ReasonCode.CONTROL_HANDOVER_PENDING,
                        other.id,
                        f"上一个控制产出者 {other.id} 尚未完成 QUIESCING",
                    )
                    for other in self._entries.values()
                    if other is not entry
                    and other.manifest.is_control_producer
                    and other.state in _HOLDS_INSTANCE
                )
        return tuple(reasons)

    def _config(self, entry: _Entry) -> BaseModel | Reason:
        """按插件的配置模型补齐缺省值并校验。"""
        try:
            return entry.manifest.config.model_validate(entry.config)
        except ValidationError as error:
            first = error.errors()[0]
            where = "/".join(str(part) for part in first["loc"]) or "/"
            return Reason(
                ReasonCode.CONFIG_INVALID, entry.id, f"配置不合法：{where} {first['msg']}"
            )

    # 激活

    async def _load(self, entry: _Entry, config: BaseModel) -> None:
        self._instances += 1
        instance_id = f"{entry.id}#{self._instances}"

        def revoke_late(disposer: Disposer) -> None:
            self._spawn(self._revoke(disposer, instance_id))

        registrations = Registrations(revoke_late)
        grant: ControlGrant | None = None
        if entry.manifest.is_control_producer:
            self._generation += 1
            grant = ControlGrant(
                entry.id,
                instance_id,
                self._generation,
                lambda: (
                    entry.state is State.ACTIVE
                    and entry.instance is not None
                    and entry.instance.id == instance_id
                ),
            )
        context = PluginContext(
            entry_id=entry.id,
            instance_id=instance_id,
            manifest=entry.manifest,
            config=config,
            control=grant,
            registry=self._registry,
            registrations=registrations,
        )
        self._set(entry, State.LOADING, ())
        try:
            plugin = entry.plugin()
            await run_with_deadline(
                self._clock, plugin.activate(context), self._options.activate_timeout
            )
            self._check_provides(entry, registrations)
            self._publish(instance_id, registrations)
        except Exception as error:
            await self._dispose(registrations, instance_id)
            self._fail(entry, error)
            return
        entry.instance = _Instance(instance_id, plugin, context, registrations, grant)
        entry.attempts = 0
        self._set(entry, State.ACTIVE, ())

    def _check_provides(self, entry: _Entry, registrations: Registrations) -> None:
        for ref in entry.manifest.provides:
            name, major = parse_ref(ref)
            staged = registrations.staged.get(name)
            if staged is None or staged[0] != major:
                raise RuntimeError(f"激活后没有提供 {ref}")

    def _publish(self, instance_id: str, registrations: Registrations) -> None:
        """发布激活期间提供的服务。服务最后注册，停用时最先撤回。"""
        added: list[Callable[[], None]] = []
        try:
            for name, (major, impl) in registrations.staged.items():
                added.append(self._registry.add(name, major, impl, instance_id))
        except Exception:
            for remove in reversed(added):
                remove()
            raise
        registrations.disposers.extend(added)
        registrations.published = True

    def _fail(self, entry: _Entry, error: Exception) -> None:
        detail = str(error) or type(error).__name__
        logger.warning("%s 激活失败：%s", entry.id, detail)
        delays = self._options.retry_delays
        entry.attempts += 1
        if entry.attempts <= len(delays):
            delay = delays[entry.attempts - 1]
            reason = Reason(
                ReasonCode.ACTIVATION_FAILED,
                entry.id,
                f"激活失败：{detail}；{delay:g} 秒后第 {entry.attempts} 次重试"
                f"（共 {len(delays)} 次）",
            )
            self._set(entry, State.FAILED, (reason,))
            entry.retry = self._spawn(self._retry_later(entry, delay))
        else:
            reason = Reason(
                ReasonCode.RETRIES_EXHAUSTED,
                entry.id,
                f"激活失败：{detail}；{len(delays)} 次重试已用尽，停在 FAILED，"
                "操作员停用再启用可重新尝试",
            )
            self._set(entry, State.FAILED, (reason,))

    async def _retry_later(self, entry: _Entry, delay: float) -> None:
        await self._clock.sleep(delay)
        entry.retry = None
        entry.retry_due = True
        self._request()

    def _cancel_retry(self, entry: _Entry) -> None:
        if entry.retry is not None:
            entry.retry.cancel()
            entry.retry = None
        entry.retry_due = False

    # 停用

    async def _unload(self, entry: _Entry, reasons: tuple[Reason, ...]) -> None:
        """ACTIVE → QUIESCING → UNLOADING → WAITING 或 DISPOSED。

        控制产出者的 QUIESCING：停止提交动作块 → 实时核作废其代次 → 等到输入已释放的确认
        → 撤销注册。等确认超时即保持 QUIESCING 并上报，不撤销注册，收到确认后再继续。
        """
        instance = entry.instance
        if instance is None:
            return
        stalled = await self._unload_consumers(entry, instance)
        if stalled:
            # 依赖它的条目还在等确认：它的注册可能还在用这里的服务，提供方先不停。
            self._set(
                entry,
                State.ACTIVE,
                tuple(
                    Reason(
                        ReasonCode.DEPENDENT_QUIESCING,
                        other.id,
                        f"依赖它的 {other.id} 尚未完成 QUIESCING，完成后再停用",
                    )
                    for other in stalled
                ),
            )
            return

        # 离开 ACTIVE 的同时提交资格失效：停止提交动作块。
        self._set(entry, State.QUIESCING, reasons)
        grant = instance.grant
        if grant is None:
            await self._run_quiesce(instance)
            await self._finish_unload(entry, instance)
            return

        # 立即请求作废代次，插件的 quiesce 与等确认同时进行，不推迟释放输入。
        instance.release = self._spawn(self._invalidate_and_wait(grant))
        timeout = self._options.release_timeout
        _, released = await asyncio.gather(
            self._run_quiesce(instance),
            finished_within(self._clock, instance.release, timeout),
        )
        if not released:
            logger.warning(
                "%s 未在 %g 秒内收到代次 %d 输入已释放的确认，保持 QUIESCING",
                instance.id,
                timeout,
                grant.generation,
            )
            self._set(
                entry,
                State.QUIESCING,
                (
                    Reason(
                        ReasonCode.RELEASE_TIMEOUT,
                        entry.id,
                        f"未在 {timeout:g} 秒内收到代次 {grant.generation} 输入已释放的确认；"
                        "保持 QUIESCING、不撤销注册，收到确认后继续",
                    ),
                ),
            )
            instance.release.add_done_callback(self._release_done)
            return
        await self._after_release(entry, instance)

    async def _unload_consumers(self, entry: _Entry, instance: _Instance) -> list[_Entry]:
        """先卸载依赖本实例所提供服务的条目，返回仍未卸载完的。"""
        names = self._registry.provided_by(instance.id)
        if not names:
            return []
        stalled: list[_Entry] = []
        for other in reversed(list(self._entries.values())):
            if other is entry or other.instance is None:
                continue
            if not any(parse_ref(ref)[0] in names for ref in other.manifest.inject):
                continue
            if other.state is State.ACTIVE:
                reason = Reason(
                    ReasonCode.PROVIDER_STOPPING, entry.id, f"依赖的提供方 {entry.id} 正在停用"
                )
                await self._unload(other, (reason,))
            # 卸载完成即回到 WAITING 或 DISPOSED；还在等确认或推迟停用的仍持有实例。
            if other.state in _HOLDS_INSTANCE:
                stalled.append(other)
        return stalled

    async def _run_quiesce(self, instance: _Instance) -> None:
        try:
            await run_with_deadline(
                self._clock,
                instance.plugin.quiesce(instance.context),
                self._options.quiesce_timeout,
            )
        except Exception:
            # 提交资格已由内核收回，插件收尾失败不影响后续步骤。
            logger.warning("%s 的 quiesce 失败或超时，继续停用", instance.id, exc_info=True)

    async def _invalidate_and_wait(self, grant: ControlGrant) -> None:
        try:
            await self._core.invalidate_generation(grant)
        except Exception:
            logger.exception(
                "请求实时核作废 %s 的代次 %d 失败，仍等待输入已释放的确认",
                grant.instance_id,
                grant.generation,
            )
        await self._core.wait_released(grant)

    def _release_done(self, _task: asyncio.Task[None]) -> None:
        self._request()

    async def _after_release(self, entry: _Entry, instance: _Instance) -> None:
        task = instance.release
        grant = instance.grant
        if task is None or grant is None:
            return
        error = "已取消" if task.cancelled() else task.exception()
        if error is not None:
            reason = Reason(
                ReasonCode.RELEASE_FAILED,
                entry.id,
                f"等待代次 {grant.generation} 输入已释放的确认失败：{error}；"
                "保持 QUIESCING、不撤销注册，需要操作员处理",
            )
            if entry.reasons != (reason,):
                logger.error("%s %s", instance.id, reason.message)
            self._set(entry, State.QUIESCING, (reason,))
            return
        await self._finish_unload(entry, instance)

    async def _finish_unload(self, entry: _Entry, instance: _Instance) -> None:
        self._set(entry, State.UNLOADING, entry.reasons)
        await self._dispose(instance.registrations, instance.id)
        entry.instance = None
        if entry.enabled:
            self._set(entry, State.WAITING, self._blockers(entry))
        else:
            self._set(entry, State.DISPOSED, (_disabled(entry),))

    async def _dispose(self, registrations: Registrations, owner: str) -> None:
        """按后注册先撤销的顺序运行撤销函数；单个失败或超时记录后继续撤销其余注册。"""
        registrations.closed = True
        while registrations.disposers:
            await self._revoke(registrations.disposers.pop(), owner)

    async def _revoke(self, disposer: Disposer, owner: str) -> None:
        try:
            result = disposer()
            if result is not None:
                await run_with_deadline(self._clock, result, self._options.disposer_timeout)
        except Exception:
            logger.exception("%s 的一项注册撤销失败，继续撤销其余注册", owner)

    # 其它

    def _set(self, entry: _Entry, state: State, reasons: tuple[Reason, ...]) -> None:
        if entry.state is state and entry.reasons == reasons:
            return
        before = entry.state
        entry.state = state
        entry.reasons = reasons
        if self._on_transition is not None:
            try:
                self._on_transition(Transition(entry.id, before, state, reasons))
            except Exception:
                logger.exception("状态变化的通知失败")

    def _spawn(self, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.get_running_loop().create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _entry(self, entry_id: str) -> _Entry:
        entry = self._entries.get(entry_id)
        if entry is None:
            raise KeyError(f"没有条目 {entry_id}")
        return entry

    @staticmethod
    def _status(entry: _Entry) -> EntryStatus:
        manifest = entry.manifest
        return EntryStatus(
            id=entry.id,
            plugin=manifest.id,
            version=manifest.version,
            tier=manifest.tier,
            state=entry.state,
            reasons=entry.reasons,
            instance_id=entry.instance.id if entry.instance is not None else None,
            enabled=entry.enabled,
            attempts=entry.attempts,
        )


def _log_loop_failure(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and (error := task.exception()) is not None:
        logger.error("插件内核的调和任务异常退出", exc_info=error)
