"""从现有服务投影语义事实，画面和模型输出均只作数据。"""

import re
from collections.abc import Callable
from hashlib import sha256
from typing import Literal

from core_protocol.models import CaptureState
from game_runtime.adapter import Adapter, ScreenType
from game_runtime.core_link import CoreLink
from game_runtime.game_bridge.session import BellisSession
from game_runtime.game_bridge.wire import ms, selection_source, v1
from game_runtime.narrative import NarrativeService, compare_key
from game_runtime.planner import Candidate, Planner, Status
from game_runtime.scene import SceneService, ScreenSource
from game_runtime.step_control.contracts import PlannerPort
from game_runtime.step_control.controller import StepController
from game_runtime.voice_activity import VoiceActivityService, VoiceState, VoiceStatus

_MAX_U32 = 2**32 - 1


def voice_activity_at(
    status: VoiceStatus, capture_ns: int, now_ns: int, max_age_ns: int
) -> Literal["busy", "idle", "unknown"]:
    at = status.processed_ns
    if at is None or not capture_ns <= at <= now_ns or now_ns - at > max_age_ns:
        return "unknown"
    if status.state is VoiceState.BUSY:
        return "busy"
    return "idle" if status.state is VoiceState.IDLE else "unknown"


class Projection:
    def __init__(
        self,
        adapter: Adapter,
        scene: SceneService,
        core: CoreLink,
        planner: Callable[[], PlannerPort | None],
        voice: Callable[[], VoiceActivityService | None],
        narrative: Callable[[], NarrativeService | None],
        max_frame_age_ns: int,
    ) -> None:
        self.adapter, self.scene, self.core = adapter, scene, core
        self._planner, self._voice, self._narrative = planner, voice, narrative
        self._max_frame_age = max_frame_age_ns

    def frame_age(self, now: int) -> int | None:
        update = self.scene.latest
        return None if update is None else min(_MAX_U32, ms(max(0, now - update.frame.capture_ns)))

    def capture_ready(self, now: int) -> bool:
        health = self.core.last_health
        return (
            self.core.healthy
            and health is not None
            and health.capture is CaptureState.CAPTURING
            and health.latest_frame is not None
            and 0 <= now - health.latest_frame.capture_ns <= self._max_frame_age
        )

    def action_ready(self, controller: StepController | None, now: int) -> bool:
        update = self.scene.latest
        return update is not None and self._ready(controller, now, update.analyzed_capture_ns)

    def health_ready(self, controller: StepController | None, now: int) -> bool:
        analyzed = self.scene.last_analyzed_capture_ns
        return self._ready(controller, now, analyzed) or (
            analyzed is not None
            and self.scene.latest is None
            and controller is not None
            and controller.awaiting_effect(now)
            and self.capture_ready(now)
        )

    def _ready(self, controller: StepController | None, now: int, capture_ns: int | None) -> bool:
        return (
            controller is not None
            and controller.grant.is_open
            and self.capture_ready(now)
            and capture_ns is not None
            and 0 <= now - capture_ns <= self._max_frame_age
        )

    def ranks(self, session_id: str, decision_id: str) -> tuple[Candidate, ...]:
        planner = self._planner()
        return planner.ranked(session_id, decision_id) if isinstance(planner, Planner) else ()

    def publish(self, session: BellisSession) -> None:
        update, described = self.scene.latest, session.described_ns
        now = session.clock.now_ns()
        if (
            update is None
            or described is None
            or not (
                described <= update.frame.capture_ns <= now
                and now - update.frame.capture_ns <= self._max_frame_age
            )
        ):
            return
        classification = update.screen_state.classification
        screen_type = classification.screen_type
        planner = self._planner()
        situation = planner.situation if isinstance(planner, Planner) else None
        controller = session.signals.controller
        if (
            session.unknown is not None
            or (situation is not None and situation.status == Status.STUCK)
            or (controller is not None and controller.stuck)
        ):
            screen_type = ScreenType.STUCK
        extension = None
        if (
            classification.source is ScreenSource.SIGNATURE
            and screen_type is classification.screen_type
        ):
            name = next(
                (
                    screen.type
                    for screen in self.adapter.screens
                    if screen.id == update.state.screen
                ),
                None,
            )
            if any(item.name == name for item in self.adapter.screen_types):
                extension = name

        lookpoints: list[v1.Lookpoint] = []
        if situation is not None:
            for kind, text, importance, at in situation.highlights:
                key = sha256(f"{kind}\0{compare_key(text)}".encode()).hexdigest()
                if (
                    key not in session.reported_lookpoints
                    and text.strip()
                    and 0 <= at <= update.frame.capture_ns
                    and importance in ("high", "medium")
                    and re.fullmatch(r"[a-z][a-z0-9-]{0,47}", kind)
                ):
                    lookpoints.append(
                        v1.Lookpoint(
                            id=key,
                            kind=kind,
                            summary=text.strip()[:240],
                            importance=importance,
                            atMs=ms(at),
                        )
                    )
        failure = None
        if session.unknown is not None:
            choice = session.unknown.choice
            failure = v1.Failure(
                source=None if choice is None else choice.source, summary="上一次输入的结果尚未核验"
            )
        elif (
            screen_type is ScreenType.BAD_ENDING
            and situation is not None
            and situation.ending is not None
        ):
            failure = v1.Failure(
                source=selection_source(situation.ending.source),
                summary=situation.ending.summary[:240],
            )
        elif screen_type is ScreenType.STUCK:
            failure = v1.Failure(source=None, summary="当前目标达到推进限制")

        line = None
        narrative = self._narrative()
        record = None if narrative is None else narrative.latest
        if (
            record is not None
            and not record.closed
            and record.screen in (None, update.state.screen)
            and record.last_frame <= update.frame.frame_number
            and 0 <= now - record.last_ns <= self._max_frame_age
            and screen_type in (ScreenType.DIALOGUE, ScreenType.CUTSCENE)
        ):
            line = record.text.strip()[:240] or None

        # 优先用与当前画面范围关联的观察摘要。没有摘要时只报已判定的通用类型。
        summary = planner.observed_text(update) if isinstance(planner, Planner) else None
        voice = self._voice()
        wire = v1.GameSituationObserved.model_validate(
            {
                "atMs": ms(update.frame.capture_ns),
                "screenType": screen_type.value,
                "screenTypeExtension": extension,
                "summary": (summary or f"当前画面为{screen_type.value}。")[:600],
                "currentLine": line,
                "interactables": list(session.interactables)
                if screen_type in (ScreenType.CHOICE, ScreenType.TIMED_CHOICE)
                else [],
                "lookpoints": lookpoints[:16],
                "voiceActivity": "unknown"
                if voice is None
                else voice_activity_at(
                    voice.status, update.frame.capture_ns, now, self._max_frame_age
                ),
                "rereading": line is not None and situation is not None and situation.rereading,
                "failure": failure,
                "traceparent": session.traceparent,
            }
        )
        if session.notify("situation.observed", wire):
            session.reported_lookpoints.update(point.id for point in wire.lookpoints)
