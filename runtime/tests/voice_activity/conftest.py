"""配音检测测试的共用夹具：本地数据目录里的真实模型（没有时跳过），与 SAPI 合成的一句台词（不在
Windows 或没有语音时跳过）。"""

from pathlib import Path

import pytest

from game_runtime.artifacts import ArtifactError
from game_runtime.local_data import local_data_dir
from game_runtime.voice_activity import SileroVad
from gs06_eval import sapi
from gs06_eval.wav import Audio, read_wav

LINE = "今天的风有点大，我们早点回去吧。"
"""为测试原创的台词。"""


@pytest.fixture
def silero() -> SileroVad:
    """候选模型；本地数据目录里没有或摘要不符时跳过（先运行 uv run fetch-models）。"""
    try:
        return SileroVad.load(local_data_dir())
    except ArtifactError as error:
        pytest.skip(f"语音检测模型不可用：{error}")


@pytest.fixture(scope="session")
def sapi_line(tmp_path_factory: pytest.TempPathFactory) -> Audio:
    """SAPI 合成的一句台词，48 kHz 单声道。"""
    if not sapi.available():
        pytest.skip("没有可用的 SAPI 语音（只在 Windows 上）")
    path: Path = tmp_path_factory.mktemp("sapi") / "line.wav"
    sapi.synthesize([(LINE, path)], sample_rate=48_000)
    return read_wav(path)
