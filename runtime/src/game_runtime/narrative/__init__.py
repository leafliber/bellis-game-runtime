"""剧情记录：普通插件 `narrative` 与它提供的 `narrative@1`，设计见
`docs/modules/narrative-planner.md` 的“剧情记录”。

完整日志只存本地数据目录里的 SQLite（`game_runtime.narrative.store`），不进版本库，也不出本机。
"""

from game_runtime.narrative.plugin import (
    NARRATIVE,
    NARRATIVE_WRITE,
    SCOPE_TYPES,
    NarrativeConfig,
    NarrativeListener,
    NarrativePlugin,
    NarrativeService,
    Recorder,
    RoiPixels,
    crop_roi,
)
from game_runtime.narrative.record import (
    REVISION_TOLERANCE,
    Completeness,
    Discontinuity,
    LowConfidenceLine,
    NarrativeRecord,
    Scope,
    compare_key,
    continues,
)
from game_runtime.narrative.store import (
    NARRATIVE_DIR,
    NarrativeStore,
    NarrativeStoreError,
    read_discontinuities,
    read_records,
    read_scopes,
    read_sessions,
    store_path,
)
from game_runtime.narrative.tracker import (
    BoxFrame,
    Changes,
    Gap,
    Observation,
    OtherScreen,
    Reading,
    SentenceTracker,
    TrackerParams,
    Unjudged,
)

__all__ = [
    "NARRATIVE",
    "NARRATIVE_DIR",
    "NARRATIVE_WRITE",
    "REVISION_TOLERANCE",
    "SCOPE_TYPES",
    "BoxFrame",
    "Changes",
    "Completeness",
    "Discontinuity",
    "Gap",
    "LowConfidenceLine",
    "NarrativeConfig",
    "NarrativeListener",
    "NarrativePlugin",
    "NarrativeRecord",
    "NarrativeService",
    "NarrativeStore",
    "NarrativeStoreError",
    "Observation",
    "OtherScreen",
    "Reading",
    "Recorder",
    "RoiPixels",
    "Scope",
    "SentenceTracker",
    "TrackerParams",
    "Unjudged",
    "compare_key",
    "continues",
    "crop_roi",
    "read_discontinuities",
    "read_records",
    "read_scopes",
    "read_sessions",
    "store_path",
]
