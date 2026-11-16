"""用合成命令验证 Runtime span、导出隐私和内核登记。"""

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from re import fullmatch
from threading import Event, Thread, current_thread
from time import monotonic_ns
from typing import TypedDict, cast

import pytest
from pydantic import ValidationError

from core_protocol.models import ControlStep
from game_runtime.kernel import (
    CONTROL_SUBMIT,
    ControlGrant,
    Kernel,
    Manifest,
    NoConfig,
    Plugin,
    PluginContext,
    ResolvedEntry,
    State,
    Submission,
    Tier,
    catalog,
)
from game_runtime.telemetry import (
    TELEMETRY,
    RuntimeTelemetry,
    SpanContext,
    TelemetryConfig,
    TelemetryPlugin,
    TelemetrySpan,
    exporter,
    service,
)
from game_runtime.telemetry.consumer import recorded_traceparent
from game_runtime.telemetry.exporter import SpanRecord
from game_runtime.telemetry.exporter import _RotatingFiles as RotatingFiles  # pyright: ignore[reportPrivateUsage]

HOST_TRACE = "12" * 16
HOST_PARENT = "34" * 8
PARENT = f"00-{HOST_TRACE}-{HOST_PARENT}-00"


class _Scope(TypedDict):
    spans: list[SpanRecord]


class _Resource(TypedDict):
    scopeSpans: list[_Scope]


def _records(directory: Path) -> list[SpanRecord]:
    result: list[SpanRecord] = []
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_bytes().splitlines():
            request = cast(dict[str, list[_Resource]], json.loads(line))
            for resource in request["resourceSpans"]:
                for scope in resource["scopeSpans"]:
                    result.extend(scope["spans"])
    return result


def _require(span: TelemetrySpan | None) -> TelemetrySpan:
    assert span is not None
    assert fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-01", span.traceparent)
    return span


def _outcome(span: SpanRecord) -> str:
    return next(
        attr["value"]["stringValue"]
        for attr in span["attributes"]
        if attr["key"] == "bellis.game.outcome"
    )


@pytest.mark.parametrize(
    "directory", [None, "./traces", "https://invalid.test", r"\\server\traces"]
)
def test_directory_must_be_explicit_and_local(directory: str | None) -> None:
    with pytest.raises(ValidationError):
        TelemetryConfig.model_validate({} if directory is None else {"directory": directory})


def test_step_reparent_keeps_original_start_and_deferred_stage_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    epoch = 1_710_000_000_000_000_000
    now = 100
    monkeypatch.setattr(service, "time_ns", lambda: epoch)
    monkeypatch.setattr(service, "perf_counter_ns", lambda: now)
    new_trace, new_parent = "56" * 16, "78" * 8
    parent = f"00-{new_trace}-{new_parent}-01"

    async def run() -> str:
        nonlocal now
        telemetry = RuntimeTelemetry(TelemetryConfig(directory=tmp_path))
        try:
            step = _require(telemetry.step(SpanContext(1, PARENT, goal_id="g"), "select"))
            span_id = step.traceparent.split("-")[2]
            now += 40
            assert telemetry.reparent_step(step, parent)
            assert step.traceparent == f"00-{new_trace}-{span_id}-01"
            step.event("located")
            assert not telemetry.reparent_step(step, PARENT)
            now += 100
            step.event("submitted", at_monotonic_ns=150)
            step.event("executed", at_monotonic_ns=160)
            step.event("effect_checked", at_monotonic_ns=159)
            step.event("effect_checked")
            step.end("verified")
            assert not telemetry.records(step, (new_trace, span_id))
            return span_id
        finally:
            await telemetry.aclose()

    span_id = asyncio.run(run())
    (step,) = _records(tmp_path)
    assert step["spanId"] == span_id
    assert step["traceId"] == new_trace
    assert step.get("parentSpanId") == new_parent
    assert step["startTimeUnixNano"] == str(epoch)
    assert [int(event["timeUnixNano"]) - epoch for event in step.get("events", [])] == [
        40,
        50,
        60,
        140,
    ]


@pytest.mark.parametrize(
    "malformed",
    [
        "broken",
        f"00-{'0' * 32}-{HOST_PARENT}-01",
        f"00-{HOST_TRACE}-{'0' * 16}-01",
        f"00-{HOST_TRACE}-{'a' * 16}-01",
    ],
)
def test_wire_context_checks_advertised_identity_and_current_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, malformed: str
) -> None:
    async def run() -> None:
        telemetry = RuntimeTelemetry(TelemetryConfig(directory=tmp_path))
        try:
            step = _require(telemetry.step(SpanContext(1, PARENT, goal_id="g"), "advance"))

            def lookup() -> RuntimeTelemetry:
                return telemetry

            assert recorded_traceparent(step, telemetry, PARENT, lookup) == step.traceparent
            assert recorded_traceparent(step, telemetry, PARENT, lambda: None) is None
            monkeypatch.setattr(step, "_traceparent", malformed)
            assert recorded_traceparent(step, telemetry, PARENT, lookup) is None
        finally:
            await telemetry.aclose()

    asyncio.run(run())


def test_connected_spans_use_a_monotonic_timeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    epoch = 1_710_000_000_000_000_000
    wall, monotonic = epoch, 100
    monkeypatch.setattr(service, "time_ns", lambda: wall)
    monkeypatch.setattr(service, "perf_counter_ns", lambda: monotonic)

    async def run() -> str:
        nonlocal wall, monotonic
        telemetry = RuntimeTelemetry(TelemetryConfig(directory=tmp_path))
        try:
            command = _require(
                telemetry.command_received(SpanContext(7, PARENT, goal_id="goal-a"), "goal.submit")
            )
            monotonic += 10
            command.end("accepted")
            child = SpanContext(7, command.traceparent, goal_id="goal-a", decision_id="decision-a")
            step = _require(telemetry.step(child, "select"))
            for stage in ("located", "submitted", "executed", "effect_checked"):
                monotonic += 10
                step.event(stage)
                step.event(stage)
            step.event("located")
            wall -= 20_000_000_000
            monotonic += 10
            step.end("verified")
            step.end("rejected")
            step.event("effect_checked")
            decision = _require(telemetry.decision_opened(child, "timed"))
            decision.event("located")
            decision.end("opened")
            return step.traceparent
        finally:
            await telemetry.aclose()

    step_parent = asyncio.run(run())
    command, step, decision = _records(tmp_path)
    assert [span["name"] for span in (command, step, decision)] == [
        "game.command",
        "game.step",
        "game.decision",
    ]
    assert {span["traceId"] for span in (command, step, decision)} == {HOST_TRACE}
    assert command.get("parentSpanId") == HOST_PARENT
    assert step.get("parentSpanId") == decision.get("parentSpanId") == command["spanId"]
    assert step_parent == f"00-{HOST_TRACE}-{step['spanId']}-01"
    assert step["startTimeUnixNano"] == str(epoch + 10)
    assert step["endTimeUnixNano"] == str(epoch + 60)
    assert step.get("events") == [
        {"name": f"game.step.{stage}", "timeUnixNano": str(epoch + timestamp)}
        for stage, timestamp in zip(
            ("located", "submitted", "executed", "effect_checked"), (20, 30, 40, 50), strict=True
        )
    ]
    assert "events" not in decision
    assert [_outcome(span) for span in (command, step, decision)] == [
        "accepted",
        "verified",
        "opened",
    ]


@pytest.mark.parametrize(
    "parent",
    [
        None,
        "synthetic private text",
        f"00-{'0' * 32}-{HOST_PARENT}-01",
        f"00-{HOST_TRACE}-{'0' * 16}-01",
        f"{PARENT}\n",
    ],
)
def test_missing_or_invalid_parent_creates_a_root(tmp_path: Path, parent: str | None) -> None:
    async def run() -> str:
        telemetry = RuntimeTelemetry(TelemetryConfig(directory=tmp_path))
        try:
            span = _require(telemetry.step(SpanContext(0, parent, goal_id="local-goal"), "advance"))
            span.end("verified")
            return span.traceparent
        finally:
            await telemetry.aclose()

    traceparent = asyncio.run(run())
    (root,) = _records(tmp_path)
    assert "parentSpanId" not in root
    assert root["traceId"] not in {HOST_TRACE, "0" * 32}
    assert root["spanId"] != "0" * 16
    assert traceparent == f"00-{root['traceId']}-{root['spanId']}-01"


def test_scoped_invalidation_capacity_and_field_allowlists(tmp_path: Path) -> None:
    async def run() -> dict[str, str]:
        telemetry = RuntimeTelemetry(TelemetryConfig(directory=tmp_path, max_active_spans=3))
        goal = SpanContext(4, PARENT, goal_id="private-goal")
        decision = SpanContext(4, PARENT, decision_id="private-decision")
        next_goal = SpanContext(5, PARENT, goal_id="private-goal")
        try:
            assert telemetry.command_received(goal, "private-prompt") is None
            assert telemetry.command_received(goal, "decision.resolve") is None
            assert telemetry.step(goal, "private-ocr") is None
            assert telemetry.decision_opened(decision, "private-token") is None
            old_step = _require(telemetry.step(goal, "navigate"))
            old_decision = _require(telemetry.command_received(decision, "decision.resolve"))
            current = _require(telemetry.step(next_goal, "select"))
            assert telemetry.step(next_goal, "advance") is None
            telemetry.invalidate(4, goal_id="irrelevant-goal")
            assert telemetry.step(next_goal, "advance") is None
            telemetry.invalidate(4, goal_id="private-goal")
            old_step.end("verified")
            new_step = _require(telemetry.step(next_goal, "advance"))
            telemetry.invalidate(4, decision_id="private-decision")
            old_decision.end("accepted")
            current.event("private-coordinates")
            current.end("private-exception")
            # 关闭时取消仍活动的新步骤，不把上个代次的失效传到新代次。
            return {
                span.traceparent.split("-")[2]: outcome
                for span, outcome in (
                    (old_step, "invalidated"),
                    (old_decision, "invalidated"),
                    (current, "unknown"),
                    (new_step, "cancelled"),
                )
            }
        finally:
            await telemetry.aclose()
            assert telemetry.step(next_goal, "advance") is None

    expected = asyncio.run(run())
    records = _records(tmp_path)
    assert len(records) == len(expected)
    assert {span["spanId"]: _outcome(span) for span in records} == expected
    assert all("events" not in span for span in records)
    assert "private-" not in "".join(path.read_text("utf-8") for path in tmp_path.glob("*.jsonl"))


class _NoCore:
    async def invalidate_generation(self, grant: ControlGrant) -> None:
        raise AssertionError("遥测插件不持有控制资格")

    async def wait_released(self, grant: ControlGrant) -> None:
        raise AssertionError("遥测插件不持有控制资格")

    async def submit(
        self,
        grant: ControlGrant,
        steps: Sequence[ControlStep],
        *,
        base_frame: int,
        valid_for: float,
        start_delay: float = 0.0,
    ) -> Submission:
        raise AssertionError("遥测插件不持有控制资格")


def _probe(provided: list[RuntimeTelemetry]) -> type[Plugin[NoConfig]]:
    class Probe(Plugin[NoConfig]):
        manifest = Manifest(
            id="telemetry-probe", version="1", tier=Tier.ORDINARY, inject=(TELEMETRY.ref,)
        )

        async def activate(self, ctx: PluginContext[NoConfig]) -> None:
            provided.append(ctx.service(TELEMETRY))

    return Probe


def test_kernel_provides_and_revokes_the_ordinary_service(tmp_path: Path) -> None:
    provided: list[RuntimeTelemetry] = []

    async def run() -> None:
        kernel = Kernel(catalog=catalog(TelemetryPlugin, _probe(provided)), core=_NoCore())
        assert TelemetryPlugin.manifest.tier is Tier.ORDINARY
        assert not TelemetryPlugin.manifest.permissions
        kernel.add(
            [
                ResolvedEntry(
                    "telemetry", "telemetry", {"directory": str(tmp_path)}, False, frozenset()
                ),
                ResolvedEntry("probe", "telemetry-probe", {}, False, frozenset()),
            ]
        )
        try:
            await kernel.settled()
            assert kernel.status("probe").state is State.ACTIVE
            (telemetry,) = provided
            context = SpanContext(0, decision_id="decision-a")
            span = _require(telemetry.decision_opened(context, "untimed"))
            await kernel.disable("telemetry")
            assert kernel.status("probe").state is State.WAITING
            assert telemetry.decision_opened(context, "untimed") is None
            span.end("opened")
            await telemetry.aclose()
        finally:
            await kernel.shutdown()

    asyncio.run(run())
    (record,) = _records(tmp_path)
    assert _outcome(record) == "cancelled"


def test_queue_keeps_newest_and_recovers_after_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, resume = Event(), Event()
    write = RotatingFiles.write

    def fail_first(files: RotatingFiles, data: bytes) -> None:
        if not started.is_set():
            started.set()
            assert resume.wait(5)
            raise OSError("合成写入失败")
        write(files, data)

    monkeypatch.setattr(RotatingFiles, "write", fail_first)

    async def run() -> list[str]:
        telemetry = RuntimeTelemetry(
            TelemetryConfig(directory=tmp_path, max_queue=2, flush_interval_ms=1)
        )
        context = SpanContext(1, PARENT, goal_id="goal-a")
        try:
            _require(telemetry.step(context, "navigate")).end("verified")
            assert await asyncio.to_thread(started.wait, 2)
            ids: list[str] = []
            for _ in range(5):
                span = _require(telemetry.step(context, "select"))
                span.end("verified")
                ids.append(span.traceparent.split("-")[2])
            return ids[-2:]
        finally:
            resume.set()
            await telemetry.aclose()

    newest = asyncio.run(run())
    (path,) = tmp_path.glob("*.jsonl")
    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1
    assert b"\r" not in raw
    assert [span["spanId"] for span in _records(tmp_path)] == newest


def test_stalled_file_io_cannot_hold_control_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, resume = Event(), Event()
    workers: list[Thread] = []
    provided: list[RuntimeTelemetry] = []
    advance_ns = 0
    write = RotatingFiles.write

    def stall(files: RotatingFiles, data: bytes) -> None:
        workers.append(current_thread())
        started.set()
        assert resume.wait(5)
        write(files, data)

    monkeypatch.setattr(RotatingFiles, "write", stall)
    monkeypatch.setattr(exporter, "_STOP_NS", 3_000_000_000)
    monkeypatch.setattr(exporter, "monotonic_ns", lambda: monotonic_ns() + advance_ns)

    class ReleaseProbe(_NoCore):
        invalidated: int | None = None
        released: int | None = None

        async def invalidate_generation(self, grant: ControlGrant) -> None:
            assert not grant.is_open
            self.invalidated = grant.generation

        async def wait_released(self, grant: ControlGrant) -> None:
            assert self.invalidated == grant.generation
            assert not resume.is_set()
            self.released = grant.generation

    class ControlProducer(Plugin[NoConfig]):
        manifest = Manifest(
            id="control-producer", version="1", tier=Tier.CRITICAL, permissions=(CONTROL_SUBMIT,)
        )

        async def activate(self, ctx: PluginContext[NoConfig]) -> None:
            assert ctx.control is not None

    async def run() -> str:
        nonlocal advance_ns
        core = ReleaseProbe()
        kernel = Kernel(
            catalog=catalog(ControlProducer, TelemetryPlugin, _probe(provided)), core=core
        )
        context = SpanContext(1, PARENT, goal_id="goal-a")
        try:
            kernel.add(
                [
                    ResolvedEntry(
                        "control", "control-producer", {}, False, frozenset({CONTROL_SUBMIT})
                    ),
                    ResolvedEntry(
                        "telemetry",
                        "telemetry",
                        {"directory": str(tmp_path), "flush_interval_ms": 1},
                        False,
                        frozenset(),
                    ),
                    ResolvedEntry("probe", "telemetry-probe", {}, False, frozenset()),
                ]
            )
            await kernel.set_control_producer("control")
            grant = kernel.active_control
            assert grant is not None
            (telemetry,) = provided
            first = _require(telemetry.step(context, "advance"))
            first.end("verified")
            assert await asyncio.to_thread(started.wait, 2)
            _require(telemetry.step(context, "navigate")).end("verified")
            # 逆序停用会先处理遥测。一个等待排空的撤销函数会耗尽这里的时限。
            await asyncio.wait_for(kernel.shutdown(), 1)
            assert core.released == grant.generation
            (worker,) = workers
            assert worker.is_alive()
            assert worker.daemon
            replacement = RuntimeTelemetry(TelemetryConfig(directory=tmp_path))
            try:
                assert replacement.step(context, "advance") is None
            finally:
                await replacement.aclose()
            return first.traceparent.split("-")[2]
        finally:
            advance_ns += 4_000_000_000
            resume.set()
            for worker in workers[:]:
                await asyncio.to_thread(worker.join, 2)
                assert not worker.is_alive()
            await kernel.shutdown()

    first_id = asyncio.run(run())
    (record,) = _records(tmp_path)
    assert record["spanId"] == first_id


def test_rotation_uses_size_and_monotonic_age_and_preserves_foreign_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monotonic = 100
    monkeypatch.setattr(exporter, "monotonic_ns", lambda: monotonic)
    foreign = tmp_path / "game-runtime-111-222-000000000000.jsonl"
    foreign.write_bytes(b"{}\n")
    files = RotatingFiles(
        TelemetryConfig(
            directory=tmp_path, max_files=2, max_file_bytes=65_536, max_file_age_ms=1000
        )
    )
    big = b" " * 35_000 + b"{}\n"
    try:
        files.write(big)
        (first,) = set(tmp_path.glob("*.jsonl")) - {foreign}
        files.write(big)
        (second,) = set(tmp_path.glob("*.jsonl")) - {foreign, first}
        assert first.stat().st_size == second.stat().st_size == len(big)
        monotonic += 1_000_000_000
        files.write(b"{}\n")
    finally:
        files.close()

    own = set(tmp_path.glob("*.jsonl")) - {foreign}
    assert len(own) == 2
    assert first not in own
    assert second in own
    (latest,) = own - {second}
    assert latest.read_bytes() == foreign.read_bytes() == b"{}\n"
