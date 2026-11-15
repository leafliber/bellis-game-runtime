"""按用途配置 Chat Completions，见 `docs/modules/models.md`。"""

from enum import StrEnum
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, StrictStr, StringConstraints, field_validator

from game_runtime.adapter import ARK
from game_runtime.adapter.schema import Ident
from game_runtime.secret_store import check_name

ARK_HOST = "ark.cn-beijing.volces.com"
_ARK_ENTRY = "ark"
_ARK_ORIGIN = f"https://{ARK_HOST}"
_PLAN_ENDPOINT = f"{_ARK_ORIGIN}/api/plan/v3/chat/completions"
_ARK_ENDPOINTS = frozenset({_PLAN_ENDPOINT, f"{_ARK_ORIGIN}/api/v3/chat/completions"})


class Purpose(StrEnum):
    AGENT = "agent"
    REASONING = "reasoning"
    SUMMARY = "summary"
    KEYFRAME = "keyframe"
    CALIBRATION = "calibration"


type Coordinates = Literal["pixels", "normalized"]


class ImageSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    max_long_edge: Annotated[int, Field(ge=1, le=2048)] = 1024
    roi: Ident | None = None
    coordinates: Coordinates = "pixels"


class RouteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    vendor: Ident = ARK
    model: Annotated[
        StrictStr, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    ] = "glm-5-3-flash-260828"
    endpoint: str = _PLAN_ENDPOINT
    secret: str = _ARK_ENTRY
    thinking: Literal["enabled", "disabled", "auto"] | None = None
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = "low"
    response_format: Literal["json_object", "json_schema"] = "json_object"
    timeout_s: Annotated[float, Field(gt=0, le=300)] = 30.0
    max_tokens: Annotated[int, Field(ge=1, le=65536)] = 4096
    concurrency: Annotated[int, Field(ge=1, le=8)] = 1
    requests_per_minute: Annotated[int, Field(ge=1, le=600)] = 30

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, value: str) -> str:
        if value not in _ARK_ENDPOINTS:
            raise ValueError("须使用批准的方舟 HTTPS Chat Completions 地址")
        return value

    @field_validator("secret")
    @classmethod
    def _check_secret(cls, value: str) -> str:
        try:
            check_name(value)
        except ValueError:
            raise ValueError("凭据条目名不合法") from None
        if "_" in value:
            raise ValueError("插件权限中的凭据条目名不能含下划线")
        return value

    @property
    def path(self) -> str:
        return self.endpoint.removeprefix(_ARK_ORIGIN)

    @property
    def permissions(self) -> tuple[str, str]:
        return f"net.egress:{ARK_HOST}", f"secret:{self.secret}"


class VisionRouteConfig(RouteConfig):
    image: ImageSpec = ImageSpec()


class VlmConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    agent: VisionRouteConfig = VisionRouteConfig()
    reasoning: RouteConfig = RouteConfig()
    summary: RouteConfig = RouteConfig()
    keyframe: VisionRouteConfig = VisionRouteConfig()
    calibration: VisionRouteConfig = VisionRouteConfig()

    def route(self, purpose: Purpose) -> RouteConfig:
        return cast(RouteConfig, getattr(self, purpose.value))

    @property
    def permissions(self) -> tuple[str, ...]:
        return tuple(
            sorted({name for purpose in Purpose for name in self.route(purpose).permissions})
        )
