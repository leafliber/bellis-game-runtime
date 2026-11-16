"""模型只提议观察和动作，执行由 `policy@1` 的控制器把关。"""

from dataclasses import asdict
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from core_protocol.models import MouseButton
from game_runtime.adapter import Adapter, NotCalibratedError, ScreenType
from game_runtime.models import ImageTransform, ModelCallError, ModelClient, ModelImage, Purpose
from game_runtime.scene import ScreenObservation, Stability
from game_runtime.step_control.contracts import (
    Category,
    Click,
    GoalJudgment,
    Plan,
    PolicyInput,
    Press,
    Scroll,
    StepProposal,
    TextTarget,
)
from game_runtime.vision_agent.effects import EffectReply

_INSTRUCTION = (
    "Describe the screen, interactable elements and visible change in one short observation. "
    "Propose up to the requested number of safe steps using the declared inputs. Use actual OCR "
    "text and calibrated regions when known. For a click on part of a line, set `text` to the "
    "complete source line, `phrase` to the exact part, and `roi` to its calibrated region. "
    "Points are relative to the supplied image and follow its coordinate convention. In a choice "
    "screen propose opening a decision, with no input. "
    "For decision evidence, rank its listed option indices, with no input. For a goal explicitly "
    "marked for agent judgment, report completion only when it is visible in the current image. "
    "Put that proof in goal_evidence with no steps or decision. Use only this run."
)
_CHOICES = frozenset({ScreenType.CHOICE, ScreenType.TIMED_CHOICE})


class _Reply(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Target(_Reply):
    text: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    roi: str | None = None
    point: tuple[float, float] | None = None
    phrase: Annotated[str, Field(min_length=1, max_length=160)] | None = None

    def convert(self, adapter: Adapter, transform: ImageTransform) -> TextTarget:
        if self.roi is not None:
            adapter.roi(self.roi)
        if self.phrase is not None:
            return TextTarget(text=self.text, roi=self.roi, phrase=self.phrase)
        point = None if self.point is None else transform.client_point(*self.point)
        return TextTarget(self.text, self.roi, point)


class _Click(_Reply):
    kind: Literal["click"]
    target: _Target
    button: MouseButton = MouseButton.LEFT


class _Press(_Reply):
    kind: Literal["press"]
    scan_code: Annotated[int, Field(ge=1, le=65535)]


class _Scroll(_Reply):
    kind: Literal["scroll"]
    target: _Target
    notches: Annotated[int, Field(ge=-16, le=16)]


class _Step(_Reply):
    category: Literal["advance", "navigate"]
    action: Annotated[_Click | _Press | _Scroll, Field(discriminator="kind")]
    expected: EffectReply
    confidence: Annotated[float, Field(ge=0, le=1)]

    def convert(self, adapter: Adapter, transform: ImageTransform) -> StepProposal:
        action = self.action
        match action:
            case _Click():
                if action.button not in adapter.whitelist.click_buttons:
                    raise ValueError("鼠标键不在动作白名单")
                converted = Click(action.target.convert(adapter, transform), action.button)
            case _Press():
                if action.scan_code not in adapter.whitelist.press_keys:
                    raise ValueError("短按不在动作白名单")
                converted = Press(action.scan_code)
            case _Scroll():
                if not adapter.whitelist.scroll or not action.notches:
                    raise ValueError("滚动不在动作白名单")
                converted = Scroll(action.target.convert(adapter, transform), action.notches)
        return StepProposal(
            Category(self.category), converted, self.expected.expected(adapter), self.confidence
        )


class VisionReply(_Reply):
    observation: ScreenObservation
    description: Annotated[str, Field(min_length=1, max_length=600)]
    steps: Annotated[tuple[_Step, ...], Field(max_length=3)] = ()
    open_decision: bool = False
    ranked_options: Annotated[tuple[int, ...], Field(max_length=32)] = ()
    goal_evidence: Annotated[str, Field(min_length=1, max_length=200)] | None = None


class VisionPolicy:
    """宿主将此 `Policy` 实例交给逐步控制器。"""

    def __init__(self, adapter: Adapter, model: ModelClient, *, max_steps: int = 3) -> None:
        if not 1 <= max_steps <= 3:
            raise ValueError("视觉智能体每次至多提议 3 步")
        self._adapter = adapter
        self._model = model
        self._max_steps = max_steps

    async def propose(self, inputs: PolicyInput) -> Plan:
        scene = inputs.scene
        if scene.state.playing or (
            scene.state.stability is not Stability.STABLE and inputs.decision is None
        ):
            return Plan()
        if inputs.decision is None and scene.screen_state.restrictions & _CHOICES:
            return Plan(open_decision=True)
        try:
            result = await self._model.complete(
                Purpose.AGENT,
                instruction=_INSTRUCTION,
                data={
                    "screen": scene.screen_state.classification.screen_type.value,
                    "goal": None
                    if inputs.goal is None
                    else {
                        "text": inputs.goal.text,
                        "completion": None
                        if inputs.goal.completion is None
                        else asdict(inputs.goal.completion),
                        "completion_by_agent": inputs.goal.completion_by_agent,
                    },
                    "notes": inputs.notes[:6000],
                    "client_size": [scene.frame.width, scene.frame.height],
                    "ocr": [
                        {
                            "text": line.text[:300],
                            "client_box": list(line.box),
                            "confidence": line.confidence,
                        }
                        for line in inputs.ocr[:32]
                    ],
                    "recent": [
                        {"status": result.status.value, "detail": result.detail[:100]}
                        for result in inputs.recent[-6:]
                    ],
                    "inputs": {
                        "buttons": [item.value for item in self._adapter.whitelist.click_buttons],
                        "press_keys": list(self._adapter.whitelist.press_keys),
                        "scroll": self._adapter.whitelist.scroll,
                        "click_regions": list(self._adapter.whitelist.click_regions),
                    },
                    "rois": [
                        name for name, roi in self._adapter.rois.items() if roi.rect is not None
                    ],
                    "signatures": [item.id for item in self._adapter.signatures],
                    "decision": None
                    if inputs.decision is None
                    else {
                        "prompt": inputs.decision.prompt[:600],
                        "options": [
                            {"index": option.index, "text": option.text[:600]}
                            for option in inputs.decision.options
                        ],
                    },
                    "max_steps": self._max_steps,
                },
                output=VisionReply,
                images=(ModelImage(scene.frame),),
            )
        except ModelCallError as error:
            return Plan(report=f"visual-{error.reason.value}")
        if len(result.transforms) != 1:
            return Plan(report="visual-invalid-image")
        transform = result.transforms[0]
        if (transform.frame_number, transform.transform_version) != (
            scene.frame.frame_number,
            scene.frame.transform_version,
        ):
            return Plan(report="visual-invalid-image")
        reply = result.value
        text = reply.description.strip() or None
        if reply.goal_evidence is not None:
            goal = inputs.goal
            evidence = reply.goal_evidence.strip()
            if (
                goal is not None
                and goal.completion_by_agent
                and goal.completion is None
                and evidence
                and inputs.decision is None
                and not reply.steps
                and not reply.open_decision
                and not reply.ranked_options
                and reply.observation.screen_type not in _CHOICES
            ):
                judgment = GoalJudgment(
                    True,
                    evidence,
                    goal.generation,
                    scene.frame.frame_number,
                    scene.frame.transform_version,
                )
                return Plan(
                    observation=reply.observation, observation_text=text, goal_judgment=judgment
                )
            return Plan(
                observation=reply.observation,
                observation_text=text,
                report="visual-invalid-goal-judgment",
            )
        if inputs.decision is not None:
            indices = set(reply.ranked_options)
            actual = {option.index for option in inputs.decision.options}
            ranked = (
                reply.ranked_options
                if indices == actual and len(indices) == len(reply.ranked_options)
                else ()
            )
            return Plan(observation=reply.observation, observation_text=text, ranked_options=ranked)
        if reply.open_decision or reply.observation.screen_type in _CHOICES:
            return Plan(observation=reply.observation, observation_text=text, open_decision=True)
        try:
            if len(reply.steps) > self._max_steps:
                raise ValueError("步数超过该策略上限")
            steps = tuple(item.convert(self._adapter, transform) for item in reply.steps)
        except (LookupError, NotCalibratedError, ValueError):
            return Plan(
                observation=reply.observation, observation_text=text, report="visual-invalid-step"
            )
        return Plan(steps=steps, observation=reply.observation, observation_text=text)
