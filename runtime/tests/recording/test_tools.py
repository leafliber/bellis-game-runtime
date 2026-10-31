"""留存、标注、导出与结果的比对：配额为 0 时拒绝、超出配额删最旧的而不删正在录的、标注的读写与
校验、导出的 PNG 与 WAV（GS-06 的评测工具能读）、结果逐条写出与比对、录制工具的拒绝。
"""

import asyncio
import dataclasses
import json
from collections.abc import Callable, Sequence
from pathlib import Path

import cv2
import numpy as np
import pytest
from pydantic import ValidationError

from game_runtime.adapter import Adapter
from game_runtime.audio_ring import AudioBlock, AudioRead
from game_runtime.frame_ring import RingFrame
from game_runtime.recording import (
    Annotations,
    FrameSelection,
    LineTruth,
    QuotaKeeper,
    QuotaRefusedError,
    Recording,
    RecordingWriter,
    ScreenSpan,
    StoredRecording,
    VoiceSegment,
    check_annotations,
    compare_results,
    export_audio,
    export_frames,
    list_recordings,
    load_annotations,
    mark_line,
    parse_mark,
    quota_bytes,
    result_line,
    save_annotations,
    select_frames,
)
from game_runtime.recording import cli as cli_module
from game_runtime.recording.layout import read_meta
from game_runtime.voice_activity import (
    FeedEnd,
    FeedInterrupt,
    FeedSkip,
    UnknownReason,
    VoiceChange,
    VoiceState,
)
from gs06_eval.wav import decode_wav

MS = 1_000_000

type NewWriter = Callable[..., RecordingWriter]


def finished(writer: RecordingWriter, frames: Sequence[RingFrame] = ()) -> Path:
    async def scenario() -> None:
        for frame in frames:
            await writer.write_frame(frame)
        await writer.finish(stop_reason="enter")

    asyncio.run(scenario())
    return writer.path


# 留存


def test_a_zero_quota_refuses_recording(
    synthetic: Adapter, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert quota_bytes(synthetic) == synthetic.compliance.recording_quota_gb * 10**9
    refused = synthetic.model_copy(
        update={"compliance": synthetic.compliance.model_copy(update={"recording_quota_gb": 0})}
    )
    with pytest.raises(QuotaRefusedError, match="不允许录制"):
        quota_bytes(refused)

    def load(directory: Path) -> Adapter:
        return refused

    monkeypatch.setattr(cli_module, "load_adapter", load)
    assert cli_module.record_main(["synthetic-game", "--core", "unused"]) == 2
    assert "不能录制" in capsys.readouterr().err


def test_over_the_quota_the_oldest_recordings_are_deleted_but_not_the_current(
    new_writer: NewWriter, frames: list[RingFrame], root: Path
) -> None:
    paths = [finished(new_writer(name=f"take-{i}"), frames[:2]) for i in range(3)]
    stray = paths[0].parent / "not-a-recording"
    stray.mkdir()
    stored = list_recordings(paths[0].parent)
    assert [s.path for s in stored] == paths  # 名字不合格式的目录不计入
    sizes = [s.size for s in stored]
    assert all(s.state == "complete" for s in stored)

    current = new_writer(name="current")
    keeper = QuotaKeeper(paths[0].parent, sum(sizes[1:]) + 4096, current=current.path)
    reported: list[StoredRecording] = []
    status = keeper.check(reported.append)
    assert [s.path for s in reported] == [paths[0]]
    assert not paths[0].exists()
    assert all(path.exists() for path in paths[1:])
    assert not status.over
    finished(current, frames)  # 正在录的这段变大了
    status = keeper.check(reported.append)
    assert [s.path for s in reported] == paths
    assert current.path.exists()
    assert status.over  # 只剩正在录的一段仍超出：须停止
    assert stray.exists()


# 标注


def test_annotations_round_trip_and_are_checked_against_the_recording(
    new_writer: NewWriter, frames: list[RingFrame], synthetic: Adapter
) -> None:
    path = finished(new_writer(), frames)
    assert load_annotations(path) == Annotations()
    numbers = [f.frame_number for f in frames]
    annotations = Annotations(
        screens=(
            ScreenSpan(screen="title-menu", first=numbers[0], last=numbers[0]),
            ScreenSpan(screen="dialogue", first=numbers[1], last=numbers[-1]),
        ),
        lines=(
            LineTruth(
                speaker="钟楼守",
                text="这是一句台词。",
                first=numbers[1],
                complete=numbers[5],
                last=numbers[-1],
            ),
            LineTruth(speaker=None, text="旁白", first=numbers[2], complete=None, last=numbers[3]),
        ),
        voice=(VoiceSegment(start_ns=10, end_ns=20),),
    )
    save_annotations(path, annotations)
    assert load_annotations(path) == annotations
    problems = check_annotations(
        annotations, frame_numbers=numbers, adapter=synthetic, audio_range=(0, 100)
    )
    assert problems == []
    wrong = annotations.model_copy(
        update={"screens": (ScreenSpan(screen="nowhere", first=1, last=numbers[0]),)}
    )
    problems = check_annotations(wrong, frame_numbers=numbers, adapter=synthetic, audio_range=None)
    assert problems == [
        "screens[0].screen：适配包没有画面 nowhere",
        "screens[0]：帧 1 不在帧索引里",
        "voice[0]：录制里没有音频",
    ]


def test_bad_annotations_are_rejected() -> None:
    with pytest.raises(ValidationError, match="first 9 大于 last 3"):
        ScreenSpan(screen="dialogue", first=9, last=3)
    with pytest.raises(ValidationError, match="first ≤ complete ≤ last"):
        LineTruth(speaker=None, text="一句", first=1, complete=9, last=5)
    with pytest.raises(ValidationError):
        LineTruth(speaker=None, text=" 首尾有空白", first=1, complete=None, last=5)
    with pytest.raises(ValidationError, match="重叠"):
        Annotations(
            screens=(
                ScreenSpan(screen="a", first=1, last=5),
                ScreenSpan(screen="b", first=5, last=9),
            )
        )
    with pytest.raises(ValidationError, match="start_ns"):
        VoiceSegment(start_ns=5, end_ns=5)
    with pytest.raises(ValidationError):
        Annotations.model_validate_json('{"format": 2}')


# 导出


def test_frames_are_exported_as_png_with_the_recorded_pixels(
    new_writer: NewWriter, frames: list[RingFrame], root: Path
) -> None:
    # 末尾画面不再变化，每 100 ms 接纳一帧。
    last = frames[-1]
    still = [
        dataclasses.replace(
            last, frame_number=last.frame_number + k, capture_ns=last.capture_ns + k * 100 * MS
        )
        for k in range(1, 6)
    ]
    frames = [*frames, *still]
    recording = Recording.open(finished(new_writer(), frames))
    numbers = [f.frame_number for f in frames]
    picks = select_frames(recording, FrameSelection(frames=(numbers[4], numbers[1])))
    assert picks == [1, 4]
    assert select_frames(recording, FrameSelection(all=True, start=0.1, end=0.2)) == [2, 3, 4]
    every = select_frames(recording, FrameSelection(every=0.2))
    assert every == [0, 4, 8, 12, 15, 17, 19]
    # 逐字显示完、不再变化 0.25 秒之后的第一帧，只有一段稳定。
    [stable] = select_frames(recording, FrameSelection(stable=True))
    assert frames[stable].pixels == frames[stable - 1].pixels == frames[-1].pixels
    changed = max(i for i in range(1, len(frames)) if frames[i].pixels != frames[i - 1].pixels)
    assert frames[stable].capture_ns - frames[changed].capture_ns >= 250 * MS
    assert frames[stable - 1].capture_ns - frames[changed].capture_ns < 250 * MS
    with pytest.raises(ValueError, match="恰好"):
        FrameSelection(all=True, stable=True)

    out = root / "exports"
    written = export_frames(recording, out, picks)
    assert [p.name for p in written] == [f"{numbers[1]:08d}.png", f"{numbers[4]:08d}.png"]
    for path, index in zip(written, picks, strict=True):
        image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        assert image is not None
        assert np.asarray(image).tobytes() == frames[index].pixels
    listed = [json.loads(line) for line in (out / "frames.jsonl").read_text().splitlines()]
    assert [entry["frame_number"] for entry in listed] == [numbers[1], numbers[4]]
    assert listed[1]["seconds"] == pytest.approx(0.2)


def test_audio_is_exported_as_wav_that_the_gs06_tool_reads(
    new_writer: NewWriter, frames: list[RingFrame], root: Path
) -> None:
    writer = new_writer(audio=(48_000, 2))
    rng = np.random.default_rng(7)
    blocks: list[AudioBlock] = []
    for number in [1, 2, 3, 6, 7]:  # 4、5 被覆盖了：按时差补零
        samples = rng.uniform(-0.5, 0.5, 480 * 2).astype(np.float32)
        blocks.append(AudioBlock(number, number * 10 * MS, 480, False, samples.tobytes()))
    writer.write_audio(AudioRead(48_000, 2, tuple(blocks)))
    recording = Recording.open(finished(writer, frames[:1]))

    exported = export_audio(recording, root / "exports")
    assert exported.path.name == "audio-10-80ms.wav"
    assert exported.inserted == ((1440, 960),)
    audio = decode_wav(exported.path.read_bytes())
    assert (audio.sample_rate, audio.channels, audio.length) == (48_000, 2, 480 * 5 + 960)
    expected = np.concatenate(
        [
            *(np.frombuffer(b.samples, np.float32) for b in blocks[:3]),
            np.zeros(960 * 2, np.float32),
            *(np.frombuffer(b.samples, np.float32) for b in blocks[3:]),
        ]
    ).reshape(-1, 2)
    assert np.array_equal(audio.samples, expected)
    sidecar = json.loads(exported.path.with_suffix(".json").read_text())
    assert sidecar["first_capture_ns"] == 10 * MS
    assert sidecar["inserted"] == [{"at_sample": 1440, "samples": 960}]

    part = export_audio(recording, root / "pcm", start=0.035, end=0.06, pcm16=True)
    pcm = decode_wav(part.path.read_bytes())
    # 与范围有重叠的是第 3 块与第 6 块，其间补零。
    assert pcm.length == 480 * 2 + 960
    assert np.allclose(pcm.samples[:480], expected[480 * 2 : 480 * 3], atol=1 / 32767)
    assert not pcm.samples[480 : 480 + 960].any()


# 结果


def test_results_are_written_exactly_and_compared_line_by_line(root: Path) -> None:
    change = VoiceChange(VoiceState.BUSY, 123, 100, None)
    assert json.loads(result_line(change)) == {
        "state": "busy",
        "at_ns": 123,
        "since_ns": 100,
        "reason": None,
    }
    assert json.loads(result_line({"x": 0.1 + 0.2}))["x"] == 0.1 + 0.2
    for mark in (FeedSkip(0, 3), FeedInterrupt(7, UnknownReason.STALLED, 9), FeedEnd(12)):
        assert parse_mark(mark_line(mark)) == mark
    with pytest.raises(ValueError, match="不合格式"):
        parse_mark('{"type": "skip"}')
    with pytest.raises(ValueError, match="不认识"):
        parse_mark('{"type": "other", "after": 1}')

    live, replay = root / "live.jsonl", root / "replay.jsonl"
    live.write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
    replay.write_text('{"a": 1}\n{"a": 2}\n', encoding="utf-8")
    assert compare_results(live, replay).identical
    replay.write_text('{"a":1}\n{"a":3}\n{"a":4}\n', encoding="utf-8")
    comparison = compare_results(live, replay)
    assert not comparison.identical
    assert comparison.same == 1
    assert comparison.first_difference is not None
    assert comparison.first_difference.line == 2
    replay.write_text('{"a":1}\n{"a":2}\n{"a":3}\n', encoding="utf-8")
    longer = compare_results(live, replay)
    assert (longer.same, longer.identical) == (2, False)
    assert longer.first_difference is not None
    assert longer.first_difference.live is None


def test_the_recording_command_lists_exports_and_checks(
    new_writer: NewWriter, frames: list[RingFrame], root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = finished(new_writer(name="take"), frames)
    data = root / "data"
    assert cli_module.recording_main(["--data-dir", str(data), "list"]) == 0
    listed = capsys.readouterr().out
    assert "synthetic-game：1 段" in listed
    assert path.name in listed
    assert cli_module.recording_main(["--data-dir", str(data), "check", "synthetic-game"]) == 0
    assert "都能读出" in capsys.readouterr().out
    argv = ["--data-dir", str(data), "export-frames", f"synthetic-game/{path.name}", "--every", "1"]
    assert cli_module.recording_main(argv) == 0
    exported = data / "exports" / "synthetic-game" / path.name / "frames"
    assert sorted(p.name for p in exported.iterdir()) == [f"{frames[0].frame_number:08d}.png"]
    assert read_meta(path).state == "complete"
