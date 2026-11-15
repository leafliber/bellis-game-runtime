"""按实时核回执、控制器结果和独立标注评分。"""

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from game_runtime.models import CallMetrics, Purpose
from game_runtime.step_control import StepResult, StepStatus
from replay_benchmark.labels import Labels, Prices

type InputResult = Literal["unknown", "executed", "dropped", "uncertain", "rejected"]
type Verdict = Literal["hit", "wrong", "unscored", "not-executed", "missing-device-evidence"]


@dataclass(frozen=True, slots=True)
class ObservedFrame:
    original: int
    capture_ns: int
    admitted_ns: int
    stability: str


@dataclass(frozen=True, slots=True)
class ObservedStep:
    at_ns: int
    result: StepResult


@dataclass(frozen=True, slots=True)
class DeviceEvidence:
    match: Literal["action-id", "timed-press"]
    event_type: str
    connection: int
    seq: int
    at_ns: int
    reported_point: tuple[int, int] | None


@dataclass(slots=True)
class ObservedAction:
    frame: int
    kind: str
    submitted_ns: int
    submitted_point: tuple[int, int] | None
    action_id: int
    control: int | str | None = None
    result: InputResult = "unknown"
    executed_ns: int | None = None
    device_evidence: DeviceEvidence | None = None


@dataclass(slots=True)
class Trace:
    frames: dict[int, ObservedFrame] = field(default_factory=dict[int, ObservedFrame])
    steps: list[ObservedStep] = field(default_factory=list[ObservedStep])
    actions: list[ObservedAction] = field(default_factory=list[ObservedAction])
    calls: list[CallMetrics] = field(default_factory=list[CallMetrics])
    device_events: int = 0
    presented: int = 0
    finished: bool = False
    fast_paths_enabled: bool = False


@dataclass(frozen=True, slots=True)
class SubmittedCoordinateResult:
    action_id: int
    original_frame: int | None
    kind: str
    submitted_point: tuple[int, int]
    receipt: InputResult
    executed_ns: int | None
    device_evidence: DeviceEvidence | None
    verdict: Verdict


@dataclass(frozen=True, slots=True)
class ModelScore:
    calls_logged: int
    by_purpose: dict[str, int]
    status: Literal["measured", "PENDING"]
    estimated_cost: str | None
    currency: str | None
    cost_status: Literal["estimated", "PENDING"]


@dataclass(frozen=True, slots=True)
class Generality:
    fast_path_status: Literal["enabled", "disabled"]
    replay_alignment_rate: float | None
    closed_loop_success_rate: None
    signatures: int
    fast_paths: int | None
    hooks: int | None
    adaptation_hours: float | None


@dataclass(frozen=True, slots=True)
class Score:
    backend: Literal["model", "policy"]
    finished: bool
    frames_presented: int
    frames_observed: int
    scene_stability: dict[str, int]
    submitted_coordinate_hits: int
    submitted_coordinate_wrong: int
    rejected: int
    rejection_reasons: dict[str, int]
    submitted_coordinates: tuple[SubmittedCoordinateResult, ...]
    input_results: dict[str, int]
    device_events: int
    effect_results: dict[str, int]
    replay_goal_steps: int | None
    closed_loop_goal: Literal["PENDING"]
    model: ModelScore
    generality: Generality


def _visible_response(call: CallMetrics) -> bool:
    return call.first_output_s is not None or call.usage is not None


def _model_score(trace: Trace, prices: Prices | None, *, configured_model: bool) -> ModelScore:
    calls = trace.calls if configured_model else []
    measured = any(_visible_response(call) or call.http_status is not None for call in calls)
    estimated: Decimal | None = None
    if measured and prices is not None:
        rates = {(rate.vendor, rate.model): rate for rate in prices.models}
        estimated = Decimal(0)
        for call in calls:
            rate = rates.get((call.vendor, call.model))
            usage = call.usage
            if (
                rate is None
                or usage is None
                or (usage.prompt_tokens is None or usage.completion_tokens is None)
            ):
                estimated = None
                break
            estimated += (
                rate.input_per_million * usage.prompt_tokens
                + rate.output_per_million * usage.completion_tokens
            ) / 1_000_000
    return ModelScore(
        len(trace.calls),
        dict(Counter(call.purpose.value for call in trace.calls)),
        "measured" if measured else "PENDING",
        None if estimated is None else format(estimated, "f"),
        None if prices is None else prices.currency,
        "PENDING" if estimated is None else "estimated",
    )


def _goal_verified(action_index: int, trace: Trace, labels: Labels) -> bool:
    completion = labels.completion
    action = trace.actions[action_index]
    executed = action.executed_ns
    if completion is None or executed is None:
        return False
    next_index = action_index + 1
    next_submission = (
        None if len(trace.actions) <= next_index else trace.actions[next_index].submitted_ns
    )
    evidence = min(
        (
            frame.admitted_ns
            for frame in trace.frames.values()
            if frame.capture_ns > executed and completion.contains(frame.original)
        ),
        default=None,
    )
    return evidence is not None and any(
        step.result.status is StepStatus.VERIFIED
        and step.result.frame_number == action.frame
        and step.at_ns >= evidence
        and (next_submission is None or step.at_ns < next_submission)
        for step in trace.steps
    )


def score(
    labels: Labels,
    trace: Trace,
    *,
    backend: Literal["model", "policy"],
    signatures: int,
    fast_paths: int | None = None,
    hooks: int | None = None,
    prices: Prices | None = None,
) -> Score:
    coordinates: list[SubmittedCoordinateResult] = []
    goal_steps: int | None = None
    matched = 0
    for index, action in enumerate(trace.actions):
        point = action.submitted_point
        if point is None:
            continue
        observed = trace.frames.get(action.frame)
        original = None if observed is None else observed.original
        if action.result != "executed":
            verdict: Verdict = "not-executed"
        elif action.device_evidence is None:
            verdict = "missing-device-evidence"
        else:
            verdict = "unscored"
            if original is not None and action.kind == "click":
                active = {
                    key: target
                    for key, target in enumerate(labels.targets)
                    if target.contains(original)
                }
                hits = {key for key, target in active.items() if target.box.contains(*point)}
                if active:
                    verdict = "hit" if hits else "wrong"
                if matched in hits:
                    matched += 1
                    if matched == len(labels.targets) and _goal_verified(index, trace, labels):
                        goal_steps = index + 1
        coordinates.append(
            SubmittedCoordinateResult(
                action.action_id,
                original,
                action.kind,
                point,
                action.result,
                action.executed_ns,
                action.device_evidence,
                verdict,
            )
        )

    rejections = Counter(
        step.result.detail for step in trace.steps if step.result.status is StepStatus.REJECTED
    )
    effects = Counter(
        step.result.status.value
        for step in trace.steps
        if step.result.status in (StepStatus.VERIFIED, StepStatus.NO_EFFECT, StepStatus.UNKNOWN)
    )
    model = _model_score(trace, prices, configured_model=backend == "model")
    measure_alignment = (
        backend == "model"
        and any(call.purpose is Purpose.AGENT and _visible_response(call) for call in trace.calls)
        and trace.finished
        and not trace.fast_paths_enabled
        and bool(labels.targets)
        and labels.completion is not None
    )
    return Score(
        backend,
        trace.finished,
        trace.presented,
        len(trace.frames),
        dict(Counter(frame.stability for frame in trace.frames.values())),
        sum(item.verdict == "hit" for item in coordinates),
        sum(item.verdict == "wrong" for item in coordinates),
        sum(rejections.values()),
        dict(rejections),
        tuple(coordinates),
        dict(Counter(action.result for action in trace.actions)),
        trace.device_events,
        dict(effects),
        goal_steps,
        "PENDING",
        model,
        Generality(
            "enabled" if trace.fast_paths_enabled else "disabled",
            float(goal_steps is not None) if measure_alignment else None,
            None,
            signatures,
            fast_paths,
            hooks,
            labels.adaptation_hours,
        ),
    )
