"""Python 与 Rust 互通：pytest 编译并拉起 `audio-ring` 的示例 `audio_ring_interop`（命令见
`core/crates/audio-ring/examples/audio_ring_interop.rs`）。

- Python 创建音频环，Rust 写入方挂接、写块，Python 读到的与写入的一致；实时核重启后块号续写。
- 头部与参数不符时 Rust 一方拒绝。
- 并发：一方高速写、另一方并发读，读到的块要么完整一致、要么报撕裂或已被覆盖。两个方向都测。

Windows 上跑命名文件映射，Linux 容器里跑 POSIX 共享内存。
"""

import json
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from core_protocol import models
from game_runtime.audio_ring import AudioBlock, AudioRing, AudioRingTornError
from game_runtime.audio_ring.fake import FakeAudioOutput

CORE = Path(__file__).resolve().parents[3] / "core"


@pytest.fixture(scope="session")
def interop() -> Path:
    """编译示例并返回可执行文件的路径；尊重 CARGO_TARGET_DIR（由 cargo 自己读取）。"""
    cargo = shutil.which("cargo")
    assert cargo is not None, "PATH 上没有 cargo"
    argv = [cargo, "build", "--locked", "-p", "audio-ring", "--example", "audio_ring_interop"]
    with subprocess.Popen(
        [*argv, "--message-format=json"],
        cwd=CORE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    ) as process:
        stdout, stderr = process.communicate(timeout=600)
    assert process.returncode == 0, stderr
    for line in stdout.splitlines():
        message = json.loads(line)
        if (
            message.get("reason") == "compiler-artifact"
            and message["target"]["name"] == "audio_ring_interop"
            and message.get("executable")
        ):
            return Path(message["executable"])
    raise AssertionError("cargo 没有给出 audio_ring_interop 的路径")


@pytest.fixture
def processes() -> Iterator[list[subprocess.Popen[str]]]:
    """测试结束时还在运行的示例一律杀掉。"""
    started: list[subprocess.Popen[str]] = []
    yield started
    for process in started:
        if process.poll() is None:
            process.kill()
        process.wait()


def start(
    binary: Path, processes: list[subprocess.Popen[str]], *args: str
) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [os.fspath(binary), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )
    processes.append(process)
    return process


def run(binary: Path, processes: list[subprocess.Popen[str]], *args: str) -> tuple[int, str]:
    """运行到结束，返回退出码与标准输出（去掉结尾的换行）。"""
    process = start(binary, processes, *args)
    stdout, _ = process.communicate(timeout=60)
    return process.returncode, stdout.strip()


def ring_json(ring: AudioRing) -> str:
    """`audio_ring.attach` 的 params。"""
    return ring.attach_params().model_dump_json()


def target_json(output: FakeAudioOutput) -> str:
    """`audio.set_target` 的 params，`fake` 分支。"""
    params = models.AudioSetTargetParams(target=models.AudioTarget(root=output.target))
    return params.model_dump_json()


def pattern(number: int, capacity: int, channels: int) -> tuple[int, int, bytes]:
    """块号为 `number` 的块的图案：采样点数、采集时刻与样本值。

    与 `audio_ring_interop.rs` 的 `pattern` 相同：读方只凭元数据就能核对整块，撕裂的块一定能被发现。
    """
    word = ((number * 0x9E3779B1) & 0xFFFFFFFF).to_bytes(4, "little")
    sample_count = capacity - number % 7
    return sample_count, number * 1000 + 7, word * (sample_count * channels)


def check_pattern(block: AudioBlock, capacity: int, channels: int) -> None:
    sample_count, capture_ns, samples = pattern(block.block_number, capacity, channels)
    assert (block.sample_count, block.capture_ns) == (sample_count, capture_ns)
    assert block.samples == samples, f"block {block.block_number} is torn"
    assert not block.discontinuity


def test_python_reads_what_rust_writes_and_numbers_resume_after_a_restart(
    interop: Path, processes: list[subprocess.Popen[str]]
) -> None:
    with AudioRing.create(48_000, 2, block_capacity=480, block_count=8) as ring:
        params = ring_json(ring)
        assert run(interop, processes, "attach", params) == (0, "ok 0")
        assert run(interop, processes, "write", params, "5") == (0, "ok 5")
        read = ring.read_recent(10**9)
        assert [block.block_number for block in read.blocks] == [1, 2, 3, 4, 5]
        for block in read.blocks:
            check_pattern(block, 480, 2)
        # 实时核重启：新的写入方从头部的 write_seq 续写。
        assert run(interop, processes, "write", params, "3") == (0, "ok 8")
        read = ring.read_recent(10**9, after=5)
        assert [block.block_number for block in read.blocks] == [6, 7, 8]
        for block in read.blocks:
            check_pattern(block, 480, 2)


def changed(params: str, **fields: object) -> str:
    return json.dumps({**json.loads(params), **fields})


def test_rust_refuses_a_ring_that_does_not_match_the_parameters(
    interop: Path, processes: list[subprocess.Popen[str]]
) -> None:
    # 另一个音频环更大：共享内存放得下参数要的大小，于是头部不符。
    with (
        AudioRing.create(48_000, 2, block_capacity=480, block_count=8) as ring,
        AudioRing.create(48_000, 2, block_capacity=960, block_count=8) as other,
    ):
        params = ring_json(ring)
        cases = {
            "sample_rate": (changed(params, sample_rate=44_100), "header sample_rate is 48000"),
            "channels": (changed(params, channels=1), "header channels is 2"),
            "block_count": (changed(params, block_count=4), "header block_count is 8"),
            "another ring": (changed(params, name=other.name), "header block_stride"),
            "missing": (changed(params, name=ring.name + "x"), "cannot open shared memory"),
            "too small": (
                changed(params, size_bytes=ring.size_bytes + (1 << 20)),
                "cannot open shared memory",
            ),
            "layout version": (changed(params, layout_version=2), "invalid audio ring parameters"),
            "sample format": (changed(params, sample_format=2), "invalid audio ring parameters"),
        }
        for case, (bad, message) in cases.items():
            returncode, stdout = run(interop, processes, "attach", bad)
            assert returncode == 1, case
            assert stdout.startswith("error "), (case, stdout)
            assert message in stdout, (case, stdout)
        assert ring.latest_block_number() == 0


def test_python_never_returns_a_torn_block_from_a_fast_rust_writer(
    interop: Path, processes: list[subprocess.Popen[str]]
) -> None:
    count = 20_000
    with AudioRing.create(48_000, 2, block_capacity=64, block_count=3) as ring:
        writer = start(interop, processes, "write", ring_json(ring), str(count))
        reads = torn = 0
        while writer.poll() is None:
            try:
                read = ring.read_recent(10**9, max_attempts=1)
            except AudioRingTornError:
                torn += 1
                continue
            numbers = [block.block_number for block in read.blocks]
            if numbers:
                assert numbers == list(range(numbers[0], numbers[0] + len(numbers)))
            for block in read.blocks:
                check_pattern(block, 64, 2)
            reads += bool(read.blocks)
        stdout, _ = writer.communicate(timeout=60)
        assert (writer.returncode, stdout.strip()) == (0, f"ok {count}")
        assert reads > 0, f"the reader never got a block ({torn} torn reads)"
        last = ring.read_recent(0)
        assert [block.block_number for block in last.blocks] == [count]


def test_rust_fake_source_never_returns_a_torn_block_from_a_fast_python_output(
    interop: Path, processes: list[subprocess.Popen[str]]
) -> None:
    # Rust 一方经假音频源逐块读到最后一块并逐块核对；这期间 Python 一方不停地写。
    last = 5_000
    with FakeAudioOutput.create(16_000, 1, block_capacity=64, block_count=4) as output:
        reader = start(interop, processes, "read", target_json(output), str(last))
        stdout = reader.stdout
        assert stdout is not None
        assert stdout.readline().strip() == "ready"
        deadline = time.monotonic() + 30
        for number in range(1, last + 1):
            assert time.monotonic() < deadline
            _, capture_ns, samples = pattern(number, 64, 1)
            output.play(samples, capture_ns=capture_ns)
        rest, stderr = reader.communicate(timeout=60)
        assert reader.returncode == 0, (rest, stderr)
        kind, got, skipped = rest.split()
        assert kind == "ok"
        # 读到的块与跳过的块合起来正好是写出的全部。
        assert int(got) + int(skipped) == last
        assert int(got) > 0
