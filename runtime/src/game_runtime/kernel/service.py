"""服务目录：按 `名字@主版本` 匹配，同一个名字同一时刻只有一个提供方。"""

from collections.abc import Callable
from dataclasses import dataclass

from game_runtime.kernel.manifest import parse_ref
from game_runtime.kernel.status import Reason, ReasonCode


@dataclass(frozen=True, slots=True)
class ServiceToken[T]:
    """带类型的服务标识：清单里写 `ref`，代码里用标识取得或提供服务。"""

    name: str
    major: int

    def __post_init__(self) -> None:
        parse_ref(self.ref)

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.major}"


@dataclass(frozen=True, slots=True)
class _Provision:
    major: int
    impl: object
    provider: str


class ServiceRegistry:
    """服务目录。只由内核写入：插件在激活期间提供的服务，激活成功后才登记。"""

    def __init__(self) -> None:
        self._services: dict[str, _Provision] = {}

    def add(self, name: str, major: int, impl: object, provider: str) -> Callable[[], None]:
        """登记服务，返回撤销函数。名字已被占用时抛错。"""
        existing = self._services.get(name)
        if existing is not None:
            raise ValueError(f"服务 {name} 已由 {existing.provider} 提供")
        provision = _Provision(major, impl, provider)
        self._services[name] = provision

        def remove() -> None:
            if self._services.get(name) is provision:
                del self._services[name]

        return remove

    def missing(self, name: str, major: int) -> Reason | None:
        """`name@major` 不可用的原因；可用时为 None。"""
        ref = f"{name}@{major}"
        provision = self._services.get(name)
        if provision is None:
            return Reason(ReasonCode.SERVICE_MISSING, ref, f"缺少服务 {ref}")
        if provision.major != major:
            return Reason(
                ReasonCode.SERVICE_MAJOR_MISMATCH,
                ref,
                f"服务 {name} 主版本不兼容：需要 @{major}，"
                f"现有 @{provision.major}（由 {provision.provider} 提供）",
            )
        return None

    def get(self, name: str, major: int) -> object:
        """取得服务实现；不可用时抛 LookupError。"""
        reason = self.missing(name, major)
        if reason is not None:
            raise LookupError(reason.message)
        return self._services[name].impl

    def provided_by(self, provider: str) -> frozenset[str]:
        """某个插件实例提供的服务名。"""
        return frozenset(name for name, p in self._services.items() if p.provider == provider)
