"""`uv run gs02 <适配包 id>`：对已经在跑的 `uv run operate <id>` 控制台，以真实输入后端向游戏点击
一个 ROI 的中心 N 次，量生效率与首个变化帧的时延（`docs/plan/spikes.md` 的 GS-02）。

它是主进程一侧的探针：从 SecretStore 读控制台本次会话的连接信息（`OperatorClient`），以真实输入
后端（SendInput）拉起实时核，用适配包的窗口采集与输入策略；等 `may_autoplay`，然后只发左键点击、
从不按键，逐次记下回执、约 1 秒内画面是否变化与首个变化帧的时延，以及每条
`input.released`/`input.residual`。报告写进不入库的 `reports/gs02/`。

先决条件：在另一个终端里 `uv run operate <id>`，逐条确认前置条件与“不直播”，把游戏窗口切到前台，按
`start` 开始（控制台倒数 3 秒后授予）。真实输入会移动鼠标并点击游戏窗口。
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from core_protocol import models
from game_runtime.adapter import GAMES_DIR, Adapter, AdapterError, NotCalibratedError, load_adapter
from game_runtime.channel import ChannelError
from game_runtime.core_link import CaptureConfig, CoreLink, CoreLinkOptions
from game_runtime.kernel import ControlGrant
from game_runtime.local_data import LocalDataError, local_data_dir
from game_runtime.operator import OperatorClient, OperatorError
from game_runtime.recording.cli import build_realtime_core
from game_runtime.secret_store import CredentialManagerStore, SecretStoreError

logger = logging.getLogger(__name__)

REPORTS_DIR = Path("reports/gs02")
"""报告目录，不入库。"""


def out(line: str = "") -> None:
    print(line, flush=True)


def err(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


@dataclass
class ClickResult:
    """一次点击的结果。"""

    index: int
    x: int
    y: int
    base_frame: int
    outcome: str
    """回执：`executed`、`uncertain` 或 `dropped:<原因>`，或 `rejected:<原因>`。"""
    changed: bool
    """约 `change_timeout` 秒内画面是否变化。"""
    latency_ms: float | None
    """从提交到首个变化帧的毫秒数；没变化时为 None。"""

    def as_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "x": self.x,
            "y": self.y,
            "base_frame": self.base_frame,
            "outcome": self.outcome,
            "changed": self.changed,
            "latency_ms": None if self.latency_ms is None else round(self.latency_ms, 1),
        }


@dataclass
class Probe:
    adapter: Adapter
    roi: str
    count: int
    interval: float
    change_timeout: float
    link: CoreLink
    client: OperatorClient
    grant: ControlGrant
    clicks: list[ClickResult] = field(default_factory=list[ClickResult])
    released: list[models.InputReleasedParams] = field(
        default_factory=list[models.InputReleasedParams]
    )
    residuals: list[models.InputResidualParams] = field(
        default_factory=list[models.InputResidualParams]
    )
    _action_id: int = 0

    def _next_id(self) -> int:
        self._action_id += 1
        return self._action_id

    def _frame(self) -> tuple[int, int, int, bytes] | None:
        ring = self.link.frames
        if ring is None:
            return None
        frame = ring.read_latest()
        if frame is None:
            return None
        return frame.frame_number, frame.width, frame.height, frame.pixels

    def _center(self, width: int, height: int) -> tuple[int, int]:
        rect = self.adapter.roi(self.roi)
        x = round((float(rect.left) + float(rect.right)) / 2 * width)
        y = round((float(rect.top) + float(rect.bottom)) / 2 * height)
        return max(0, min(width - 1, x)), max(0, min(height - 1, y))

    async def run(self) -> None:
        for index in range(1, self.count + 1):
            await self._click(index)
            if index < self.count:
                await asyncio.sleep(self.interval)

    async def _click(self, index: int) -> None:
        frame = self._frame()
        if frame is None:
            self.clicks.append(ClickResult(index, 0, 0, 0, "no_frame", False, None))
            return
        base_frame, width, height, before = frame
        x, y = self._center(width, height)
        action = models.Action(
            root=models.Click(id=self._next_id(), x=x, y=y, button=models.MouseButton.LEFT)
        )
        submitted = time.perf_counter_ns()
        submission = await self.grant.submit(
            [models.ControlStep(offset_ns=0, held=[], actions=[action])],
            base_frame=base_frame,
            valid_for=self.change_timeout + 0.5,
        )
        if isinstance(submission.result, models.Rejected):
            outcome = f"rejected:{submission.result.reason.value}"
        else:
            got = (await submission.outcomes()).get(action.root.id)
            outcome = _describe_outcome(got)
        changed, latency = await self._wait_changed(before, width, height, submitted)
        self.clicks.append(ClickResult(index, x, y, base_frame, outcome, changed, latency))
        out(
            f"点击 {index}/{self.count} @({x},{y})：{outcome}；"
            + ("未变化" if not changed else f"变化，时延 {latency:.1f} ms")
        )

    async def _wait_changed(
        self, before: bytes, width: int, height: int, submitted_ns: int
    ) -> tuple[bool, float | None]:
        deadline = time.monotonic() + self.change_timeout
        while time.monotonic() < deadline:
            frame = self._frame()
            if (
                frame is not None
                and frame[1] == width
                and frame[2] == height
                and frame[3] != before
            ):
                return True, (time.perf_counter_ns() - submitted_ns) / 1e6
            await asyncio.sleep(0.005)
        return False, None

    def report(self) -> dict[str, object]:
        executed = sum(1 for c in self.clicks if c.outcome == "executed")
        changed = sum(1 for c in self.clicks if c.changed)
        latencies = [c.latency_ms for c in self.clicks if c.latency_ms is not None]
        return {
            "adapter": self.adapter.id,
            "roi": self.roi,
            "count": self.count,
            "interval_s": self.interval,
            "executed": executed,
            "changed": changed,
            "effect_rate": round(changed / self.count, 3) if self.count else 0.0,
            "median_latency_ms": round(_median(latencies), 1) if latencies else None,
            "clicks": [c.as_dict() for c in self.clicks],
            "released": [r.model_dump(mode="json") for r in self.released],
            "residual": [r.model_dump(mode="json") for r in self.residuals],
        }


def _describe_outcome(outcome: object) -> str:
    if isinstance(outcome, models.Executed):
        return "executed"
    if isinstance(outcome, models.Uncertain):
        return "uncertain"
    if isinstance(outcome, models.Dropped):
        return f"dropped:{outcome.reason.value}"
    return "no_receipt"


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _write_report(probe: "Probe", report: dict[str, object]) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS_DIR / f"{probe.adapter.id}-{stem}"
    path.with_suffix(".json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        f"# GS-02 {probe.adapter.id} {stem}",
        "",
        f"- ROI：{probe.roi}",
        f"- 点击：{report['count']} 次，间隔 {report['interval_s']} 秒",
        f"- executed：{report['executed']}；画面变化：{report['changed']}"
        f"（生效率 {report['effect_rate']}）",
        f"- 变化时延中位数：{report['median_latency_ms']} ms",
        "",
        "| # | 落点 | 回执 | 变化 | 时延 ms |",
        "| --- | --- | --- | --- | --- |",
    ]
    for click in probe.clicks:
        latency = "" if click.latency_ms is None else f"{click.latency_ms:.1f}"
        changed = "是" if click.changed else "否"
        lines.append(
            f"| {click.index} | ({click.x},{click.y}) | {click.outcome} | {changed} | {latency} |"
        )
    lines += [
        "",
        f"- input.released：{len(probe.released)} 条",
        f"- input.residual：{len(probe.residuals)} 条",
    ]
    path.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


async def probe(
    adapter: Adapter,
    *,
    roi: str,
    count: int,
    interval: float,
    change_timeout: float,
    core: Path,
) -> int:
    store = CredentialManagerStore()
    client = OperatorClient(adapter, store=store)
    try:
        await client.connect()
    except OperatorError as error:
        err(f"连不上操作员控制台：{error}。先在另一个终端运行 `uv run operate {adapter.id}`。")
        return 1
    resolution = adapter.resolution
    link: CoreLink | None = None
    probe_state: Probe | None = None
    try:
        link = CoreLink(
            CoreLinkOptions(
                core,
                real_input=True,
                operator=client.credentials,
                capture=CaptureConfig(
                    target=adapter.window_rule(),
                    max_width=resolution.width,
                    max_height=resolution.height,
                ),
                input_policy=adapter.input_policy(),
            ),
            on_released=lambda r: probe_state.released.append(r) if probe_state else None,
            on_residual=lambda r: probe_state.residuals.append(r) if probe_state else None,
        )
        grant = ControlGrant("gs02", "gs02#1", 1, lambda: True, link.submit)
        link.bind_control(lambda: grant)
        probe_state = Probe(adapter, roi, count, interval, change_timeout, link, client, grant)
        out("正在拉起实时核（真实输入后端）……")
        await link.start()
        out("等待操作员在控制台上开始（may_autoplay）……")
        if not await _wait(lambda: client.may_autoplay, timeout=600.0):
            err("等不到会话进入运行（在控制台上确认前置条件并 start）。")
            return 1
        # 等第一帧进入帧环。
        if not await _wait(
            lambda: link.frames is not None and link.frames.read_latest() is not None
        ):
            err("帧环里还没有帧：确认游戏窗口在前台且采集正常。")
            return 1
        out(f"开始点击 ROI {roi} 的中心 {count} 次……")
        await probe_state.run()
    except (ChannelError, OSError) as error:
        err(f"探测失败：{error}")
        return 1
    finally:
        if link is not None:
            await link.stop()
        await client.close()
    report = probe_state.report()
    path = _write_report(probe_state, report)
    out("")
    out(
        f"生效率 {report['effect_rate']}（{report['changed']}/{report['count']}），"
        f"变化时延中位数 {report['median_latency_ms']} ms；"
        f"input.residual {len(probe_state.residuals)} 条"
    )
    out(f"报告：{path.with_suffix('.json')}")
    return 0


async def _wait(check: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        await asyncio.sleep(0.02)
    return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gs02",
        description="GS-02 probe: click the centre of a ROI with the real input backend against a "
        "running `uv run operate <id>` console, and measure the click effect rate and the latency "
        "to the first changed frame. Only left clicks, never keys. It moves the real mouse.",
    )
    parser.add_argument("game", help="adapter id (a directory under games/)")
    parser.add_argument("--roi", required=True, help="the ROI whose centre to click")
    parser.add_argument("--count", type=int, default=10, help="how many clicks (default 10)")
    parser.add_argument(
        "--interval", type=float, default=1.0, help="seconds between clicks (default 1.0)"
    )
    parser.add_argument(
        "--change-timeout",
        type=float,
        default=1.0,
        help="seconds to wait for the frame to change after each click (default 1.0)",
    )
    parser.add_argument("--core", type=Path, help="an already built realtime-core executable")
    parser.add_argument("--verbose", action="store_true", help="log at INFO level")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.count <= 0 or args.interval < 0 or args.change_timeout <= 0:
        err("count 须为正，interval 非负，change-timeout 为正。")
        return 2
    try:
        adapter = load_adapter(GAMES_DIR / args.game)
        local_data_dir()
        core = args.core
        if core is None:
            out("正在编译 realtime-core（release）……")
            core = build_realtime_core()
        _ = adapter.input_policy()  # 提前核对 ROI 已标定
    except (AdapterError, LocalDataError, SecretStoreError, NotCalibratedError, RuntimeError) as e:
        err(f"不能运行 GS-02 探针：{e}")
        return 2
    return asyncio.run(
        probe(
            adapter,
            roi=args.roi,
            count=args.count,
            interval=args.interval,
            change_timeout=args.change_timeout,
            core=core,
        )
    )
