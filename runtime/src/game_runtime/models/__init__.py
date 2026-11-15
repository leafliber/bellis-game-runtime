"""按用途调用模型的客户端和 `vlm@1`，见 `docs/modules/models.md`。"""

from game_runtime.models.client import CallMetrics, ModelClient, ModelResult
from game_runtime.models.config import Purpose, VlmConfig
from game_runtime.models.errors import ModelCallError, ModelFailure
from game_runtime.models.images import ImageTransform, ModelImage, PreparedKeyframe
from game_runtime.models.plugin import VLM, VlmPlugin, vlm_plugin_for
from game_runtime.models.stream import Usage

__all__ = [
    "VLM",
    "CallMetrics",
    "ImageTransform",
    "ModelCallError",
    "ModelClient",
    "ModelFailure",
    "ModelImage",
    "ModelResult",
    "PreparedKeyframe",
    "Purpose",
    "Usage",
    "VlmConfig",
    "VlmPlugin",
    "vlm_plugin_for",
]
