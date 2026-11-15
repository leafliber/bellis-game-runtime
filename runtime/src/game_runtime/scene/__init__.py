"""普通插件 `scene@1`：通用的变化与稳定检测与画面签名，设计见 `docs/modules/perception.md` 的
“画面变化与签名”。锚点模板文件的布局见 `docs/modules/game-adapter.md` 的“模板文件”。
"""

from game_runtime.scene.analyzer import SceneAnalyzer, SceneState, SceneUpdate
from game_runtime.scene.change import (
    Change,
    ChangeDetector,
    ChangeParams,
    Stability,
    bgra_view,
)
from game_runtime.scene.plugin import (
    SCENE,
    SceneConfig,
    SceneHub,
    SceneListener,
    ScenePlugin,
    SceneService,
)
from game_runtime.scene.signatures import (
    ASPECT_TOLERANCE,
    OCR,
    AnchorScore,
    Judgement,
    Outcome,
    SignatureJudge,
    SignatureResult,
    TextReader,
    Verdict,
    decide,
    resolution_problem,
)
from game_runtime.scene.state import (
    ScreenClassification,
    ScreenObservation,
    ScreenSource,
    ScreenState,
)
from game_runtime.scene.templates import (
    TEMPLATES_DIR,
    TemplateError,
    load_template,
    store_template,
    template_path,
)

__all__ = [
    "ASPECT_TOLERANCE",
    "OCR",
    "SCENE",
    "TEMPLATES_DIR",
    "AnchorScore",
    "Change",
    "ChangeDetector",
    "ChangeParams",
    "Judgement",
    "Outcome",
    "SceneAnalyzer",
    "SceneConfig",
    "SceneHub",
    "SceneListener",
    "ScenePlugin",
    "SceneService",
    "SceneState",
    "SceneUpdate",
    "ScreenClassification",
    "ScreenObservation",
    "ScreenSource",
    "ScreenState",
    "SignatureJudge",
    "SignatureResult",
    "Stability",
    "TemplateError",
    "TextReader",
    "Verdict",
    "bgra_view",
    "decide",
    "load_template",
    "resolution_problem",
    "store_template",
    "template_path",
]
