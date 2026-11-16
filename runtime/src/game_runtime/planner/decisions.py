"""决策实例绑定、经过核验的尝试与坏结局记忆。"""

from collections.abc import Mapping
from dataclasses import dataclass

from game_runtime.narrative import NarrativeRecord, compare_key
from game_runtime.planner.output import DecisionReply
from game_runtime.step_control.contracts import DecisionEvent


@dataclass(frozen=True, slots=True)
class Knowledge:
    summary: str = ""
    notes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class Candidate:
    index: int
    reason: str
    quotes: tuple[tuple[int, str], ...] = ()
    failed_outcome: str | None = None
    failed_source: str | None = None


type DecisionKey = tuple[str, str, tuple[str, ...], str]


@dataclass(slots=True)
class Decision:
    event: DecisionEvent
    key: DecisionKey | None
    scope: int
    checkpoint: Knowledge
    ranked: tuple[Candidate, ...] = ()
    requested: bool = False

    def matches(self, event: DecisionEvent) -> bool:
        return (self.event.session_id, self.event.instance_id) == (
            event.session_id,
            event.instance_id,
        )


@dataclass(frozen=True, slots=True)
class Attempt:
    key: DecisionKey | None
    instance_id: str
    option_key: str
    option_text: str
    source: str
    scope: int
    checkpoint: Knowledge


class DecisionHistory:
    def __init__(self) -> None:
        self.current: Decision | None = None
        self.last_attempt: Attempt | None = None
        self._failures: dict[DecisionKey, dict[str, tuple[str, str]]] = {}
        self._last_failure: DecisionKey | None = None
        self._consecutive = 0

    def observe(
        self, event: DecisionEvent, screen: str, prior: str, scope: int, checkpoint: Knowledge
    ) -> Decision | None:
        options = tuple(compare_key(option.text) for option in event.view.options)
        prompt, context = compare_key(event.view.prompt), compare_key(prior)
        key = (
            (screen, prompt[:240], tuple(sorted(options)), context[-240:])
            if options
            and all(options)
            and len(set(options)) == len(options)
            and (prompt or context)
            else None
        )
        previous = self.current
        if (
            previous is not None
            and previous.matches(event)
            and previous.event.view.options == event.view.options
            and previous.event.view.prompt == event.view.prompt
        ):
            previous.event = event
            return None
        self.current = Decision(event, key, scope, checkpoint)
        return self.current

    def ranks(
        self, decision: Decision, reply: DecisionReply, evidence: Mapping[int, NarrativeRecord]
    ) -> tuple[Candidate, ...]:
        options = {option.index: compare_key(option.text) for option in decision.event.view.options}
        order = tuple(row.index for row in reply.ranked)
        if len(order) != len(options) or set(order) != options.keys():
            raise ValueError("排行须恰好覆盖实际选项")
        tried = {} if decision.key is None else self._failures.get(decision.key, {})
        result: list[Candidate] = []
        for row in reply.ranked:
            if any(item not in evidence for item in row.quotes):
                raise ValueError("引用须来自本次调用的剧情记录")
            quotes = tuple((item, evidence[item].text[:48]) for item in dict.fromkeys(row.quotes))
            failure = tried.get(options[row.index])
            outcome, source = (None, None) if failure is None else failure
            result.append(Candidate(row.index, row.reason, quotes, outcome, source))
        return tuple(sorted(result, key=lambda candidate: candidate.failed_outcome is not None))

    def tried(self, decision: Decision) -> dict[int, str]:
        if decision.key is None:
            return {}
        failures = self._failures.get(decision.key, {})
        return {
            option.index: failures[key][0]
            for option in decision.event.view.options
            if (key := compare_key(option.text)) in failures
        }

    def receipt(self, event: DecisionEvent) -> None:
        decision = self.current
        if decision is None or not decision.matches(event):
            return
        self.current = None
        self.last_attempt = None
        if (
            event.phase != "confirmed"
            or event.by is None
            or event.view.options != decision.event.view.options
        ):
            return
        selected = next(
            (option for option in event.view.options if option.index == event.index), None
        )
        if selected is not None:
            self.last_attempt = Attempt(
                decision.key,
                event.instance_id,
                compare_key(selected.text),
                selected.text,
                event.authorized_source or event.by,
                decision.scope,
                decision.checkpoint,
            )

    def ending(self, scope: int) -> tuple[Attempt | None, int]:
        attempt, self.last_attempt = self.last_attempt, None
        self.current = None
        if attempt is None or attempt.scope != scope:
            self._last_failure = None
            self._consecutive = 0
            return None, 0
        if attempt.key is not None:
            self._consecutive = self._consecutive + 1 if attempt.key == self._last_failure else 1
            self._last_failure = attempt.key
            self.annotate(attempt, "导致坏结局")
        else:
            self._last_failure = None
            self._consecutive = 0
        return attempt, self._consecutive

    def annotate(self, attempt: Attempt, result: str) -> None:
        if attempt.key is not None:
            self._failures.setdefault(attempt.key, {})[attempt.option_key] = result, attempt.source
