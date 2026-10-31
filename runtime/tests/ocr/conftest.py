"""OCR 测试的共用夹具：适配包与本地数据目录里的真实模型（没有时跳过）。"""

import pytest

from game_runtime.adapter import GAMES_DIR, Adapter, load_adapter
from game_runtime.artifacts import ArtifactError
from game_runtime.local_data import local_data_dir
from game_runtime.ocr import DEFAULT_MODEL_SET, MODEL_SETS, EngineOptions, OcrEngine


@pytest.fixture(scope="session")
def synthetic_adapter() -> Adapter:
    return load_adapter(GAMES_DIR / "synthetic-game")


@pytest.fixture(scope="session")
def witch_trial_adapter() -> Adapter:
    return load_adapter(GAMES_DIR / "witch-trial")


@pytest.fixture(scope="session")
def real_engine() -> OcrEngine:
    """缺省模型组的真实引擎；本地数据目录里没有模型或摘要不符时跳过。"""
    try:
        engine = OcrEngine.load(MODEL_SETS[DEFAULT_MODEL_SET], local_data_dir(), EngineOptions())
    except ArtifactError as error:
        pytest.skip(f"OCR 模型不可用：{error}")
    return engine
