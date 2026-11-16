"""由维护者指定的本地任务配置。"""

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from game_runtime.adapter.schema import Ident
from game_runtime.game_bridge.session import TaskAuthorityConfig
from game_runtime.game_bridge.task_ledger import TaskLimits


class _Limits(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    calls: int
    inputs: int
    no_progress_ms: int


class _TaskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    control_target_id: str
    adapter_id: Ident
    ledger_path: str
    limits: _Limits


def read_task_config(path: Path) -> TaskAuthorityConfig:
    """核对启动入口显式选择的任务配置，不打开账库。"""
    config = _TaskConfig.model_validate_json(path.read_bytes())
    ledger_path = Path(config.ledger_path)
    if not ledger_path.is_absolute():
        raise ValueError("ledger_path 须为绝对路径")
    return TaskAuthorityConfig(
        control_target_id=config.control_target_id,
        adapter_id=config.adapter_id,
        ledger_path=ledger_path.resolve(),
        limits=TaskLimits(
            calls=config.limits.calls,
            inputs=config.limits.inputs,
            no_progress_ns=config.limits.no_progress_ms * 1_000_000,
        ),
    )
