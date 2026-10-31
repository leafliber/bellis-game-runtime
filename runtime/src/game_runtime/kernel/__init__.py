"""主进程插件内核（GR-02），设计见 `docs/modules/plugin-kernel.md`。"""

from game_runtime.kernel.clock import Clock, MonotonicClock
from game_runtime.kernel.control import (
    CONTROL_SUBMIT,
    ControlGrant,
    ControlRevokedError,
    CoreControl,
    Submission,
    SubmitFn,
)
from game_runtime.kernel.kernel import Kernel, KernelOptions
from game_runtime.kernel.manifest import Manifest, NoConfig, Tier
from game_runtime.kernel.plugin import (
    AUDIO_READ,
    FRAMES_READ,
    Catalog,
    Disposer,
    Plugin,
    PluginContext,
    catalog,
)
from game_runtime.kernel.profile import (
    Profile,
    ProfilePatch,
    ResolvedEntry,
    read_patch,
    read_profile,
    resolve_profile,
)
from game_runtime.kernel.service import ServiceToken
from game_runtime.kernel.status import EntryStatus, Reason, ReasonCode, State, Transition

__all__ = [
    "AUDIO_READ",
    "CONTROL_SUBMIT",
    "FRAMES_READ",
    "Catalog",
    "Clock",
    "ControlGrant",
    "ControlRevokedError",
    "CoreControl",
    "Disposer",
    "EntryStatus",
    "Kernel",
    "KernelOptions",
    "Manifest",
    "MonotonicClock",
    "NoConfig",
    "Plugin",
    "PluginContext",
    "Profile",
    "ProfilePatch",
    "Reason",
    "ReasonCode",
    "ResolvedEntry",
    "ServiceToken",
    "State",
    "Submission",
    "SubmitFn",
    "Tier",
    "Transition",
    "catalog",
    "read_patch",
    "read_profile",
    "resolve_profile",
]
