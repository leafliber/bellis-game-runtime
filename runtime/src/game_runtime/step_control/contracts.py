"""逐步模式的策略契约。提议与观察没有输入权限。"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from core_protocol.models import MouseButton
from game_runtime.adapter import ScreenType
from game_runtime.kernel import ServiceToken
from game_runtime.ocr import OcrLine
from game_runtime.operator.messages import DecisionOption
from game_runtime.scene import SceneUpdate, ScreenObservation


class Category(StrEnum):
    ADVANCE = "advance"
    NAVIGATE = "navigate"
    SELECT = "select"


class EffectKind(StrEnum):
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    SIGNATURE = "signature"
    SCREEN = "screen"
    TEXT_APPEARED = "text-appeared"
    TEXT_DISAPPEARED = "text-disappeared"


@dataclass(frozen=True, slots=True)
class ExpectedEffect:
    kind: EffectKind
    text: str | None = None
    roi: str | None = None
    signature: str | None = None
    screen_type: ScreenType | None = None

    def __post_init__(self) -> None:
        match self.kind:
            case EffectKind.TEXT_APPEARED | EffectKind.TEXT_DISAPPEARED:
                if not self.text or not self.roi:
                    raise ValueError("文字效果须指定文字与 ROI")
            case EffectKind.SIGNATURE:
                if not self.signature:
                    raise ValueError("签名效果须指定签名")
            case EffectKind.SCREEN:
                if self.screen_type is None:
                    raise ValueError("画面效果须指定通用类型")
            case EffectKind.CHANGED | EffectKind.UNCHANGED:
                pass


@dataclass(frozen=True, slots=True)
class TextTarget:
    text: str | None = None
    roi: str | None = None
    point: tuple[int, int] | None = None
    """模型坐标由调用方先还原为本帧的客户区物理像素。"""

    def __post_init__(self) -> None:
        if self.roi is None and self.point is None:
            raise ValueError("定位须给出 ROI 或坐标")


@dataclass(frozen=True, slots=True)
class Click:
    target: TextTarget
    button: MouseButton = MouseButton.LEFT


@dataclass(frozen=True, slots=True)
class Press:
    scan_code: int


@dataclass(frozen=True, slots=True)
class Scroll:
    target: TextTarget
    notches: int


type Input = Click | Press | Scroll


@dataclass(frozen=True, slots=True)
class StepProposal:
    category: Category
    action: Input
    expected: ExpectedEffect
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("置信度须在 [0, 1] 范围内")


@dataclass(frozen=True, slots=True)
class DecisionView:
    prompt: str
    options: tuple[DecisionOption, ...]
    deadline_ns: int | None


class StepStatus(StrEnum):
    WAITING = "waiting"
    VERIFIED = "verified"
    REJECTED = "rejected"
    NO_EFFECT = "no-effect"
    UNKNOWN = "unknown"
    STUCK = "stuck"


@dataclass(frozen=True, slots=True)
class StepResult:
    status: StepStatus
    detail: str
    frame_number: int | None = None
    category: Category | None = None
    elapsed_ns: int | None = None


@dataclass(frozen=True, slots=True)
class Goal:
    text: str
    completion: ExpectedEffect | None = None
    deadline_ns: int | None = None
    generation: int = 0
    max_steps: int | None = None
    completion_by_agent: bool = False

    def __post_init__(self) -> None:
        if self.generation < 0 or (self.max_steps is not None and self.max_steps < 1):
            raise ValueError("目标代次或步数上限无效")
        if self.completion is not None and self.completion_by_agent:
            raise ValueError("确定性完成条件与智能体判断互斥")


@dataclass(frozen=True, slots=True)
class PolicyInput:
    scene: SceneUpdate
    ocr: tuple[OcrLine, ...]
    goal: Goal | None
    notes: str
    recent: tuple[StepResult, ...]
    decision: DecisionView | None = None


@dataclass(frozen=True, slots=True)
class GoalJudgment:
    """完成判断。策略从 PolicyInput 填入目标代次和原帧标识。"""

    completed: bool
    evidence: str
    generation: int
    frame_number: int
    transform_version: int

    def __post_init__(self) -> None:
        if type(self.completed) is not bool or not 0 < len(self.evidence.strip()) <= 200:
            raise ValueError("完成判断须附 1–200 字的短证据")
        if self.generation < 0 or self.frame_number < 1 or self.transform_version < 0:
            raise ValueError("完成判断的目标代次或帧标识无效")


@dataclass(frozen=True, slots=True)
class Plan:
    steps: tuple[StepProposal, ...] = ()
    observation: ScreenObservation | None = None
    observation_text: str | None = None
    open_decision: bool = False
    ranked_options: tuple[int, ...] = ()
    report: str | None = None
    goal_judgment: GoalJudgment | None = None

    def __post_init__(self) -> None:
        if len(self.steps) > 3:
            raise ValueError("一次计划至多 3 步")
        if self.open_decision and self.steps:
            raise ValueError("打开决策点时不能同时提议输入")
        if self.observation_text is not None and (
            self.observation is None or not 0 < len(self.observation_text.strip()) <= 600
        ):
            raise ValueError("观察文字须与分类一起提供，长度为 1–600 字")
        if (
            self.goal_judgment is not None
            and self.goal_judgment.completed
            and (self.observation is None or self.steps or self.open_decision)
        ):
            raise ValueError("完成判断须附画面观察，且不能同时提议输入或打开决策点")


class Policy(Protocol):
    async def propose(self, inputs: PolicyInput) -> Plan: ...


class PolicyService(Policy, Protocol):
    @property
    def recent(self) -> tuple[StepResult, ...]: ...


POLICY = ServiceToken[PolicyService]("policy", 1)


@dataclass(frozen=True, slots=True)
class PlannerContext:
    goal: Goal | None
    notes: str = ""
    paused: bool = False
    pause_reason: str | None = None
    planning: bool = False
    restarting: bool = False


@dataclass(frozen=True, slots=True)
class DecisionEvent:
    session_id: str
    instance_id: str
    phase: Literal["preview", "opened", "confirmed", "unknown", "withdrawn"]
    view: DecisionView
    frame_number: int
    index: int | None = None
    by: Literal["operator", "default"] | None = None
    executed_at_ns: int | None = None


@dataclass(frozen=True, slots=True)
class GoalEvent:
    generation: int
    kind: Literal["completed", "step-limit", "timeout", "rejected"]
    steps: int
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ObservationReceipt:
    frame_number: int
    transform_version: int
    capture_ns: int
    text: str
    observation: ScreenObservation


class PlannerPort(Protocol):
    """下一批 planner@1 的同步入口。模型任务不在这些回调中等待。"""

    def current(self) -> PlannerContext: ...
    def submit_goal(self, goal: Goal | None) -> None: ...
    def candidates(self, session_id: str, instance_id: str) -> tuple[int, ...]: ...
    def on_decision(self, event: DecisionEvent) -> None: ...
    def on_goal(self, event: GoalEvent) -> None: ...
    def on_observation(self, event: ObservationReceipt) -> None: ...


PLANNER = ServiceToken[PlannerPort]("planner", 1)
