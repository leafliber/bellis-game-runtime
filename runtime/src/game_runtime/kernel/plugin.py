"""插件与插件拿到的收窄上下文：取得依赖、提供服务、注册副作用。"""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, ClassVar, cast

from pydantic import BaseModel

from game_runtime.kernel.control import ControlGrant
from game_runtime.kernel.manifest import Manifest, parse_ref
from game_runtime.kernel.service import ServiceRegistry, ServiceToken

type Disposer = Callable[[], Awaitable[None] | None]
"""撤销函数：撤回一项副作用。可以是同步函数，也可以返回可等待对象。"""


class Registrations:
    """一个插件实例的全部注册。由内核创建并在停用时撤销，插件经 `PluginContext` 写入。"""

    def __init__(self, late: Callable[[Disposer], None]) -> None:
        self.disposers: list[Disposer] = []
        """撤销函数，按注册顺序排列，撤销时从尾部开始。"""
        self.staged: dict[str, tuple[int, object]] = {}
        """激活期间提供的服务，激活成功后才发布。"""
        self.published = False
        self.closed = False
        self._late = late

    def add(self, disposer: Disposer) -> None:
        if self.closed:
            # 实例已卸载（例如激活超时后插件仍在运行）：副作用立即撤销。
            self._late(disposer)
        else:
            self.disposers.append(disposer)


class PluginContext[C: BaseModel]:
    """插件拿到的上下文：只能取得清单声明的依赖，只能提供清单声明的服务。"""

    def __init__(
        self,
        *,
        entry_id: str,
        instance_id: str,
        manifest: Manifest,
        config: C,
        control: ControlGrant | None,
        registry: ServiceRegistry,
        registrations: Registrations,
    ) -> None:
        self.entry_id = entry_id
        """profile 条目 id。"""
        self.instance_id = instance_id
        """插件实例 id，每次激活都不同。"""
        self.config = config
        """补齐缺省值并校验过的配置。"""
        self.permissions = frozenset(manifest.permissions)
        """已授予的权限：激活时清单申请的权限都已授予。"""
        self.control = control
        """控制产出者的提交资格；不是控制产出者时为 None。"""
        self._inject = frozenset(parse_ref(ref) for ref in manifest.inject)
        self._provides = frozenset(parse_ref(ref) for ref in manifest.provides)
        self._registry = registry
        self._registrations = registrations

    def service[T](self, token: ServiceToken[T]) -> T:
        """取得清单 `inject` 中声明的服务。"""
        self._alive()
        if (token.name, token.major) not in self._inject:
            raise LookupError(f"{self.entry_id} 没有声明依赖 {token.ref}")
        return cast(T, self._registry.get(token.name, token.major))

    def provide[T](self, token: ServiceToken[T], impl: T) -> None:
        """提供清单 `provides` 中声明的服务；只能在激活期间调用，激活成功后才发布。"""
        self._alive()
        if (token.name, token.major) not in self._provides:
            raise LookupError(f"{self.entry_id} 没有声明提供 {token.ref}")
        if self._registrations.published:
            raise RuntimeError("只能在激活期间提供服务")
        if token.name in self._registrations.staged:
            raise RuntimeError(f"{token.ref} 已提供过")
        self._registrations.staged[token.name] = (token.major, impl)

    def register(self, disposer: Disposer) -> None:
        """注册一项副作用的撤销函数；停用时按后注册先撤销的顺序运行。"""
        self._registrations.add(disposer)

    def _alive(self) -> None:
        if self._registrations.closed:
            raise RuntimeError(f"{self.instance_id} 已卸载")


class Plugin[C: BaseModel](ABC):
    """插件。每次激活新建一个实例；类属性 `manifest` 是它的清单。"""

    manifest: ClassVar[Manifest]

    @abstractmethod
    async def activate(self, ctx: PluginContext[C]) -> None:
        """激活：取得依赖、提供服务、注册副作用。

        全部副作用都经 `ctx` 注册，以便撤销；抛错或超时即激活失败，已注册的副作用随即撤销。
        """

    async def quiesce(self, ctx: PluginContext[C]) -> None:
        """进入 QUIESCING 时调用：停止新工作、取消在途任务。缺省什么也不做。

        控制产出者调用时提交资格已经失效。
        """
        del ctx


type Catalog = Mapping[str, type[Plugin[Any]]]
"""插件目录：插件 id → 插件类。"""


def catalog(*plugins: type[Plugin[Any]]) -> dict[str, type[Plugin[Any]]]:
    """按清单的 id 建插件目录。"""
    result: dict[str, type[Plugin[Any]]] = {}
    for plugin in plugins:
        manifest: object = getattr(plugin, "manifest", None)
        if not isinstance(manifest, Manifest):
            raise TypeError(f"{plugin.__name__} 没有清单")
        if manifest.id in result:
            raise ValueError(f"插件 {manifest.id} 重复")
        result[manifest.id] = plugin
    return result
