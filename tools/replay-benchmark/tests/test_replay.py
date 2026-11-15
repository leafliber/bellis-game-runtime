"""以合成像素走已授权的 VisionPolicy、控制器和假设备。"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from functools import partial
from pathlib import Path

import numpy as np
import pytest
from pydantic import BaseModel, JsonValue

from game_runtime.adapter import GAMES_DIR, Adapter, PixelRect, ScreenType, load_adapter
from game_runtime.frame_ring import RingFrame
from game_runtime.kernel import ResolvedEntry, read_profile, resolve_profile
from game_runtime.models import (
    CallMetrics,
    ImageTransform,
    ModelClient,
    ModelImage,
    ModelResult,
    PreparedKeyframe,
    Purpose,
    VlmConfig,
)
from game_runtime.ocr.engine import Image, RecognizedLine
from game_runtime.ocr.fake import ScriptedRecognizer, scripted_line
from game_runtime.recording.cli import build_realtime_core
from game_runtime.recording.layout import AdmissionInfo, GameInfo, Resolution, ToolInfo
from game_runtime.recording.reader import Recording, RecordingError
from game_runtime.recording.replay import adapter_sha256
from game_runtime.recording.writer import RecordingWriter
from game_runtime.scene import store_template
from game_runtime.step_control import (
    Category,
    Click,
    EffectKind,
    ExpectedEffect,
    Plan,
    PolicyInput,
    StepConfig,
    StepProposal,
    StepStatus,
    TextTarget,
)
from replay_benchmark.labels import Box, ClickTarget, FrameSpan, Labels
from replay_benchmark.runner import replay_case
from replay_benchmark.score import Trace, score
from replay_benchmark.timeline import Timeline
from synthetic_game import Click as SyntheticClick
from synthetic_game import GameConfig, ManualClock, SyntheticGame
from synthetic_game.anchors import anchor_templates

WIDTH, HEIGHT = 640, 360
MARKER = bytes((13, 251, 37, 255))
DIALOGUE_MARKER = bytes((37, 213, 19, 255))
DIALOGUE_LINE = "雨停了，钟楼的指针停在七点。"
MOTION_BYTE = 4 * (HEIGHT * WIDTH - 2)
MICRO_CHANGE_INDEX = 5
DESTINATION_INDEX = 11


@pytest.fixture(scope="session")
def benchmark_core() -> Path:
    return build_realtime_core(release=False)


def _step() -> StepConfig:
    return StepConfig(policy_timeout=0.65, receipt_timeout=0.6, effect_timeout=1.2, fast_paths=True)


def _profile() -> list[ResolvedEntry]:
    entries = resolve_profile(read_profile(GAMES_DIR.parent / "profiles/supervised.example.json"))
    config = _step().model_dump(mode="json")
    return [
        replace(entry, config=config) if entry.plugin == "step-control" else entry
        for entry in entries
    ]


def _center(rect: PixelRect) -> tuple[int, int]:
    return (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2


async def _recording(root: Path, variant: str) -> tuple[Recording, Labels, Path]:
    script = (GAMES_DIR / "synthetic-game" / "adapter.toml").read_text(encoding="utf-8")
    # 菜单语言核对使用有合成像素标记的“开始”。
    start, stop = (
        script.index('[[language.fixed_texts]]\nscreen = "title-menu"\nroi = "menu-shop"'),
        script.index("[compliance]"),
    )
    script = script[:start] + script[stop:]
    script = script.replace(
        "[risk]\n",
        '[risk]\npreconditions = [{ id = "fixture", text = "合成环境已确认" }]\n',
    )
    games = root / "games"
    package = games / "synthetic-game"
    package.mkdir(parents=True)
    (package / "adapter.toml").write_text(script, encoding="utf-8")
    adapter = load_adapter(package)
    for template in anchor_templates():
        store_template(root, adapter.id, template.png)
    clock = ManualClock()
    game = SyntheticGame(GameConfig(WIDTH, HEIGHT), clock)
    menu_start = adapter.roi("menu-start").to_pixels(WIDTH, HEIGHT)
    title = game.frame().data
    game.handle(SyntheticClick(*_center(menu_start)))
    after = game.frame().data
    dialogue = variant.startswith("dialogue")
    target = adapter.roi("dialogue").to_pixels(WIDTH, HEIGHT) if dialogue else menu_start
    clock.advance(1.0)
    marked = bytearray(game.frame().data if dialogue else title)
    x, y = _center(target)
    offset = 4 * (y * WIDTH + x)
    marked[offset : offset + 4] = DIALOGUE_MARKER if dialogue else MARKER
    writer = RecordingWriter.create(
        root / "recording",
        game=GameInfo(
            id=adapter.id,
            name=adapter.name,
            version=adapter.version,
            adapter_sha256=adapter_sha256(adapter.id, games),
            resolution=Resolution(width=WIDTH, height=HEIGHT),
        ),
        name=None,
        admission=AdmissionInfo(max_rate=30, idle_interval=0.08, backlog=32),
        audio_format=None,
        tool=ToolInfo(commit=None, dirty=None),
    )
    frames = 34 if dialogue else 17
    for index in range(frames):
        pixels = bytes(marked)
        if index >= DESTINATION_INDEX and variant != "ignored" and not dialogue:
            pixels = after
        elif variant.startswith("motion") or (variant == "micro" and index >= MICRO_CHANGE_INDEX):
            copy = bytearray(pixels)
            copy[MOTION_BYTE] ^= index % 2 if variant.startswith("motion") else 1
            pixels = bytes(copy)
        # 录制帧号有空隙，确保评分使用原帧号。
        number = 10 + index * 2
        await writer.write_frame(
            RingFrame(
                number, 30_000_000_000 + index * 100_000_000, 1, WIDTH, HEIGHT, WIDTH * 4, pixels
            )
        )
    await writer.finish(stop_reason="fixture")
    labels = Labels(
        targets=(
            ClickTarget(
                first=10,
                last=10 + (frames - 1) * 2 if dialogue else 30,
                box=Box(left=target.left, top=target.top, right=target.right, bottom=target.bottom),
            ),
        ),
        completion=None if dialogue or variant == "ignored" else FrameSpan(first=32, last=42),
    )
    recording = Recording.open(writer.path)
    labels.check(recording)
    return recording, labels, games


async def _commands() -> AsyncIterator[str]:
    yield "start"
    yield "y"
    yield "y"
    yield "start"
    await asyncio.Future[None]()


def test_timeline_rejects_pixels_outside_recording(tmp_path: Path) -> None:
    recording, _, _ = asyncio.run(_recording(tmp_path, "static"))
    extra = recording.frames[0].model_copy(update={"file": "../outside.png"})
    replaced = Recording(
        recording.path, recording.meta, (extra, *recording.frames[1:]), recording.audio
    )
    with pytest.raises(RecordingError, match="像素路径不合样本格式"):
        Timeline(replaced, Trace())


@pytest.mark.parametrize(
    "variant",
    [
        "static",
        "micro",
        "motion-fast",
        "motion-delayed",
        "ignored",
        "policy",
        "dialogue-default",
        "dialogue-fast-path",
    ],
)
def test_recorded_replay_exposes_evidence_expiry_and_ignored_clicks(
    benchmark_core: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    dialogue = variant.startswith("dialogue")

    class PixelModel(ModelClient):
        def __init__(
            self, config: VlmConfig, *, adapter: Adapter, permissions: frozenset[str]
        ) -> None:
            super().__init__(config, adapter=adapter, permissions=permissions)

        async def complete[T: BaseModel](
            self,
            purpose: Purpose,
            *,
            instruction: str,
            data: JsonValue,
            output: type[T],
            images: tuple[ModelImage | PreparedKeyframe, ...] = (),
        ) -> ModelResult[T]:
            transforms: tuple[ImageTransform, ...] = ()
            if purpose is Purpose.REASONING:
                subgoal: dict[str, JsonValue] = {"text": "继续阅读" if dialogue else "打开入口"}
                if dialogue:
                    subgoal["completion"] = {"kind": "screen", "screen_type": ScreenType.CHOICE}
                reply = {"subgoals": [subgoal]}
            elif purpose is Purpose.AGENT:
                assert isinstance(data, dict)
                assert not {"labels", "targets", "completion_frame", "truth"} & data.keys()
                if variant in {"static", "micro", "motion-delayed"}:
                    await asyncio.sleep(0.4)
                (image,) = images
                assert isinstance(image, ModelImage)
                frame = image.frame
                pixel = frame.pixels.find(MARKER) // 4
                transforms = (
                    ImageTransform(
                        frame.frame_number,
                        frame.transform_version,
                        PixelRect(0, 0, frame.width, frame.height),
                        frame.width,
                        frame.height,
                        "pixels",
                    ),
                )
                steps: list[dict[str, JsonValue]] = []
                if pixel >= 0:
                    steps.append(
                        {
                            "category": "navigate",
                            "confidence": 1.0,
                            "action": {
                                "kind": "click",
                                "target": {"point": [pixel % frame.width, pixel // frame.width]},
                            },
                            "expected": {
                                "kind": "screen",
                                "screen_type": ScreenType.DIALOGUE.value,
                            },
                        }
                    )
                elif DIALOGUE_MARKER in frame.pixels:
                    steps.append(
                        {
                            "category": "advance",
                            "confidence": 1.0,
                            "action": {"kind": "press", "scan_code": 0x1C},
                            "expected": {"kind": "changed"},
                        }
                    )
                reply = {
                    "observation": {
                        "screen_type": (
                            ScreenType.MENU if pixel >= 0 else ScreenType.DIALOGUE
                        ).value,
                        "confidence": 1.0,
                    },
                    "description": "识别合成画面",
                    "steps": steps,
                }
            else:
                assert purpose is Purpose.SUMMARY
                reply = {"summary": "合成模型替身的摘要"}
            result = output.model_validate_json(json.dumps(reply))
            metrics = CallMetrics(purpose, "fake", "fake", 0.0, None, None, None, None)
            logging.getLogger("game_runtime.models.client").info(
                "benchmark fixture model.call", extra={"model_call": metrics}
            )
            return ModelResult(result, transforms, metrics)

    def visible_text(image: Image) -> list[RecognizedLine]:
        # 合成 OCR 只看交给它的裁片。标注中的范围和框不会进入识别或策略。
        for marker, text in ((MARKER, "开始"), (DIALOGUE_MARKER, DIALOGUE_LINE)):
            color = np.frombuffer(marker, dtype=np.uint8)[:3]
            rows, columns = np.nonzero((image == color).all(axis=2))
            if rows.size == 1:
                x, y = int(columns[0]), int(rows[0])
                return [scripted_line(text, (x, y, x + 1, y + 1))]
        return []

    def pixel_ocr(*_arguments: object) -> ScriptedRecognizer:
        return ScriptedRecognizer(answer=visible_text)

    class PixelPolicy:
        async def propose(self, inputs: PolicyInput) -> Plan:
            frame = inputs.scene.frame
            pixel = frame.pixels.find(MARKER) // 4
            if pixel < 0:
                return Plan()
            point = (pixel % frame.width, pixel // frame.width)
            return Plan(
                steps=(
                    StepProposal(
                        Category.NAVIGATE,
                        Click(TextTarget(point=point)),
                        ExpectedEffect(EffectKind.SCREEN, screen_type=ScreenType.DIALOGUE),
                    ),
                )
            )

    monkeypatch.setattr("game_runtime.ocr.plugin.load_engine", pixel_ocr)
    monkeypatch.setattr("game_runtime.models.plugin.ModelClient", PixelModel)

    async def scenario() -> None:
        recording, labels, games = await _recording(tmp_path, variant)
        terminal: list[str] = []
        custom = variant == "policy"
        fast_paths = variant == "dialogue-fast-path"
        replay = partial(replay_case, fast_paths=True) if fast_paths else replay_case
        trace = await replay(
            recording,
            core=benchmark_core,
            data_dir=tmp_path,
            games_dir=games,
            goal="继续阅读对话" if dialogue else "进入合成游戏",
            profile=None if custom else _profile(),
            policy=PixelPolicy() if custom else None,
            step=_step() if custom else None,
            commands=_commands(),
            write=terminal.append,
        )
        result = score(labels, trace, backend="policy" if custom else "model", signatures=0)
        assert any("还有确认项没有回答" in line for line in terminal)
        assert result.finished
        assert result.frames_presented == len(recording.frames)
        assert result.scene_stability.get("stable", 0) > 0
        if custom:
            assert result.model.calls_logged == 0
        elif fast_paths:
            assert result.model.by_purpose.get("agent", 0) == 0
        else:
            assert result.model.by_purpose["agent"] > 0
        assert result.model.status == "PENDING"
        assert result.closed_loop_goal == "PENDING"
        assert result.generality.replay_alignment_rate is None
        assert result.generality.fast_path_status == ("enabled" if fast_paths else "disabled")
        if dialogue:
            executed = {
                action.kind
                for action in trace.actions
                if action.result == "executed" and action.device_evidence is not None
            }
            assert executed == ({"click"} if fast_paths else {"key_press"})
            return
        if variant in {"micro", "motion-delayed"}:
            assert result.rejection_reasons.get("scene-changed", 0) > 0
            before = MICRO_CHANGE_INDEX if variant == "micro" else DESTINATION_INDEX
            assert any(
                step.result.status is StepStatus.REJECTED
                and step.result.detail == "scene-changed"
                and step.result.frame_number is not None
                and (frame := trace.frames.get(step.result.frame_number)) is not None
                and frame.stability == "stable"
                and frame.original < recording.frame_numbers[before]
                for step in trace.steps
            )
        if variant == "motion-delayed":
            assert result.submitted_coordinate_hits == 0
            assert result.replay_goal_steps is None
        elif variant != "micro":
            assert result.submitted_coordinate_hits >= 1
            assert any(
                item.receipt == "executed"
                and (evidence := item.device_evidence) is not None
                and evidence.match == "timed-press"
                and evidence.reported_point is None
                for item in result.submitted_coordinates
            )
            if variant == "ignored":
                assert result.replay_goal_steps is None
                assert result.effect_results.get("verified", 0) == 0
                assert result.effect_results.get("no-effect", 0) > 0
            else:
                assert result.replay_goal_steps == 1

    asyncio.run(asyncio.wait_for(scenario(), 25))
