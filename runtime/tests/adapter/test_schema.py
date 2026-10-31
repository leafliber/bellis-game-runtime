"""适配包声明的 Schema 与读取：合法的声明、尚未标定的项，以及每条校验规则的反例。

规则见 `docs/modules/game-adapter.md` 的“格式”。声明都是测试里写的合成数据。
"""

import math
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core_protocol.models import MouseButton
from game_runtime.adapter import (
    ADAPTER_FILE,
    Adapter,
    AdapterError,
    NormalizedRect,
    NotCalibratedError,
    PixelRect,
    ScreenType,
    c2_allows,
    load_adapter,
)
from game_runtime.adapter.load import describe
from game_runtime.core_link import WindowRule

BASE = """
id = "sample"
name = "样例"
version = "1"

[window]
process_name = "sample.exe"
title = "Sample Game"

[resolution]
width = 640
height = 360

[language]
tag = "zh-Hans"
fixed_texts = [{ screen = "menu", roi = "start", text = "开始" }]

[compliance]
basis = "测试用的合成声明"
text_source = "screen"
training = false
demonstrations = false
recording_quota_gb = 1
models = [{ vendor = "volcengine-ark", model = "glm-5-3-flash-260828" }]

[whitelist]
click_buttons = ["left"]
press_keys = [0x1C]
scroll = false
click_regions = ["client"]
press_duration = 0.05
max_rate = 4
hold_keys = []
hold_buttons = []

[[screens]]
id = "menu"
title = "菜单"
type = "菜单"

[risk]

[rois]
client = { title = "客户区", rect = [0, 0, 1, 1] }
start = { title = "开始按钮", rect = [0.25, 0.5, 0.75, 0.75] }
"""

FULL = """
[[screen_types]]
name = "审判发言"
base = "计时选择"

[[screens]]
id = "trial"
title = "审判"
type = "审判发言"

[pacing]
min_action_interval = 0.5
read_base = 1
read_per_char = 0.05
voice_unknown_wait = 6

[[signatures]]
id = "menu"
screen = "menu"
priority = 1
anchors = [{ roi = "start", template_sha256 = "%s", min_score = 0.9 }]
pixels = [{ at = [0.5, 0.25], color = "#1A2b3C", tolerance = 12 }]
texts = [{ roi = "start", text = "开始" }]

[[notes]]
id = "characters"
title = "人物"
description = "出场人物与关系"
""" % ("0123456789abcdef" * 4)

type Data = dict[str, Any]
type Mutation = Callable[[Data], None]


def declaration(extra: str = "") -> Data:
    return tomllib.loads(BASE + extra)


def problems(data: Data) -> list[str]:
    """校验 `data`，返回逐条的问题；合法时测试失败。"""
    with pytest.raises(ValidationError) as caught:
        Adapter.model_validate(data)
    return describe(caught.value)


def _parent(data: Data, path: str) -> tuple[Any, str | int]:
    parts: list[str | int] = [int(p) if p.isdigit() else p for p in path.split(".")]
    node: Any = data
    for part in parts[:-1]:
        node = node[part]
    return node, parts[-1]


def put(path: str, value: object) -> Mutation:
    def mutate(data: Data) -> None:
        node, key = _parent(data, path)
        node[key] = value

    return mutate


def drop(path: str) -> Mutation:
    def mutate(data: Data) -> None:
        node, key = _parent(data, path)
        del node[key]

    return mutate


def test_minimal_declaration_is_valid() -> None:
    adapter = Adapter.model_validate(declaration())

    assert adapter.id == "sample"
    assert adapter.whitelist.click_buttons == (MouseButton.LEFT,)
    assert adapter.whitelist.press_keys == (0x1C,)
    assert not adapter.whitelist.allows_hold
    assert adapter.roi("start") == NormalizedRect(0.25, 0.5, 0.75, 0.75)
    assert isinstance(adapter.roi("client").right, float)
    assert adapter.uncalibrated() == ("阅读节奏",)


def test_full_declaration_with_extension_signature_pacing_and_notes_is_valid() -> None:
    adapter = Adapter.model_validate(declaration(FULL))

    assert adapter.screens[1].type == "审判发言"
    assert adapter.screen_types[0].base is ScreenType.TIMED_CHOICE
    signature = adapter.signatures[0]
    assert signature.calibrated
    assert signature.pixels[0].at == (0.5, 0.25)
    pacing = adapter.require_pacing()
    # 重读的阅读时间缺省与首读相同。
    assert (pacing.reread_base, pacing.reread_per_char) == (1.0, 0.05)
    assert adapter.notes[0].title == "人物"
    assert adapter.uncalibrated() == ()


def test_window_converts_to_the_capture_rule() -> None:
    data = declaration()
    data["window"]["window_class"] = "SampleClass"

    rule = Adapter.model_validate(data).window_rule()

    assert rule == WindowRule(
        process_name="sample.exe", window_class="SampleClass", title="Sample Game"
    )


def test_roi_converts_to_client_pixels() -> None:
    rect = NormalizedRect(0.25, 0.5, 0.75, 0.75)

    assert rect.to_pixels(640, 360) == PixelRect(160, 180, 480, 270)
    assert rect.to_pixels(2560, 1440) == PixelRect(640, 720, 1920, 1080)


def test_uncalibrated_items_are_listed_and_using_them_raises() -> None:
    data = declaration()
    data["rois"]["speaker"] = {"title": "名字栏"}
    data["language"]["fixed_texts"] = []
    data["signatures"] = [{"id": "menu", "screen": "menu"}]

    adapter = Adapter.model_validate(data)

    assert adapter.uncalibrated() == (
        "ROI speaker（名字栏）",
        "签名 menu",
        "语言核对的固定文字",
        "阅读节奏",
    )
    assert not adapter.signatures[0].calibrated
    for use, item in (
        (lambda: adapter.roi("speaker"), "ROI speaker（名字栏）"),
        (adapter.require_pacing, "阅读节奏"),
        (adapter.language_check, "语言核对的固定文字"),
    ):
        with pytest.raises(NotCalibratedError) as caught:
            use()
        assert (caught.value.game, caught.value.item) == ("sample", item)
        assert str(caught.value) == f"适配包 sample 的{item}尚未标定"
    with pytest.raises(LookupError, match="没有声明 ROI missing"):
        adapter.roi("missing")


def test_declaration_is_read_only() -> None:
    adapter = Adapter.model_validate(declaration())

    with pytest.raises(ValidationError):
        adapter.id = "other"


@pytest.mark.parametrize(
    ("vendor", "model", "allowed"),
    [
        ("volcengine-ark", "glm-5-3-flash-260828", True),
        ("volcengine-ark", "doubao-seed-1-6-250615", True),
        ("volcengine-ark", "doubao-", False),
        # 文档里的 glm-5-3-flash 只是简写，清单按接入点的完整值核对。
        ("volcengine-ark", "glm-5-3-flash", False),
        ("volcengine-ark", "glm-4-plus", False),
        ("openai", "doubao-seed-1-6-250615", False),
        ("openai", "glm-5-3-flash-260828", False),
    ],
)
def test_c2_allowlist(vendor: str, model: str, allowed: bool) -> None:
    assert c2_allows(vendor, model) is allowed


SHA = "0123456789abcdef" * 4

REJECTED: list[tuple[str, Mutation, str]] = [
    ("unknown top-level field", put("hooks", []), "hooks：Extra inputs are not permitted"),
    ("unknown nested field", put("whitelist.axes", []), "whitelist.axes：Extra inputs"),
    ("arbitration rules are unsupported", put("arbitration", {}), "arbitration：Extra inputs"),
    ("risk levels are unsupported", put("risk.levels", []), "risk.levels：Extra inputs"),
    ("required table missing", drop("risk"), "risk：Field required"),
    ("screens must be written", drop("screens"), "screens：Field required"),
    ("id pattern", put("id", "Sample"), "id：String should match pattern"),
    ("version too long", put("version", "1" * 33), "version："),
    ("window without class or title", drop("window.title"), "window_class 与 title 至少给出一个"),
    ("process name with a directory", put("window.process_name", "bin/sample.exe"), "不含目录"),
    ("empty title", put("window.title", ""), "window.title：String should match pattern"),
    ("zero resolution", put("resolution.width", 0), "resolution.width：Input should be greater"),
    ("float resolution", put("resolution.height", 360.0), "resolution.height：Input should be"),
    ("language tag", put("language.tag", "zh_CN"), "language.tag：String should match pattern"),
    ("fixed texts must be written", drop("language.fixed_texts"), "language.fixed_texts：Field"),
    ("integer for a boolean", put("compliance.training", 0), "compliance.training：Input should"),
    (
        "string for a boolean",
        put("compliance.demonstrations", "false"),
        "compliance.demonstrations",
    ),
    ("string for an integer", put("compliance.recording_quota_gb", "1"), "recording_quota_gb"),
    ("negative quota", put("compliance.recording_quota_gb", -1), "recording_quota_gb：Input"),
    ("text from game data", put("compliance.text_source", "game-data"), "compliance.text_source"),
    (
        "model outside C-2",
        put("compliance.models", [{"vendor": "volcengine-ark", "model": "glm-4-plus"}]),
        "compliance.models[0]：volcengine-ark 上的 glm-4-plus 不在 C-2 的允许清单内",
    ),
    (
        "vendor outside C-2",
        put("compliance.models", [{"vendor": "openai", "model": "doubao-seed-1-6"}]),
        "不在 C-2 的允许清单内",
    ),
    (
        "duplicate model",
        put(
            "compliance.models",
            [{"vendor": "volcengine-ark", "model": "glm-5-3-flash-260828"}] * 2,
        ),
        "compliance：models：volcengine-ark 上的 glm-5-3-flash-260828 重复",
    ),
    ("break code", put("whitelist.press_keys", [0x9C]), "0x009c 不是 Set 1 的按下扫描码"),
    ("wrong prefix", put("whitelist.press_keys", [0xE11D]), "0xe11d 不是 Set 1 的按下扫描码"),
    ("zero scan code", put("whitelist.press_keys", [0]), "press_keys[0]：Input should be greater"),
    ("duplicate key", put("whitelist.press_keys", [0x1C, 0x1C]), "press_keys：28 重复"),
    ("unknown button", put("whitelist.click_buttons", ["x1"]), "whitelist.click_buttons[0]："),
    ("long press duration", put("whitelist.press_duration", 1.0), "press_duration：Input should"),
    ("zero rate", put("whitelist.max_rate", 0), "whitelist.max_rate：Input should be greater"),
    ("hold without limit", put("whitelist.hold_keys", [0x2A]), "允许按住时必须写 max_hold"),
    ("limit without hold", put("whitelist.max_hold", 1.0), "不能写 max_hold"),
    ("clicks without regions", put("whitelist.click_regions", []), "click_regions 不能为空"),
    ("whitelist field missing", drop("whitelist.scroll"), "whitelist.scroll：Field required"),
    (
        "region not declared",
        put("whitelist.click_regions", ["board"]),
        "whitelist.click_regions[0]：没有声明 ROI board",
    ),
    ("rect out of range", put("rois.start.rect", [0, 0, 1.5, 1]), "rois.start.rect[2]：Input"),
    ("rect inverted", put("rois.start.rect", [0.5, 0, 0.25, 1]), "rois.start：rect 须满足"),
    ("rect too short", put("rois.start.rect", [0, 0, 1]), "rois.start.rect[3]：Field required"),
    ("rect not a number", put("rois.start.rect", [0, 0, 1, math.nan]), "rois.start.rect[3]："),
    ("roi without title", drop("rois.start.title"), "rois.start.title：Field required"),
    ("roi name pattern", put("rois.Start", {"title": "x"}), "rois.Start（键）：String should"),
    (
        "screen maps to an undeclared type",
        put("screens.0.type", "弹窗"),
        "screens[0].type：弹窗 不是通用画面类型，也没有在 screen_types 中声明",
    ),
    (
        "screen maps to unknown",
        put("screens.0.type", "未知"),
        "screens[0].type：未知 由运行时给出，不能作为映射目标",
    ),
    (
        "screen maps to stuck",
        put("screens.0.type", "卡住"),
        "screens[0].type：卡住 由运行时给出",
    ),
    (
        "extension shadows a generic type",
        put("screen_types", [{"name": "对话", "base": "对话"}]),
        "screen_types[0].name：对话 是通用画面类型",
    ),
    (
        "extension based on a runtime-only type",
        put("screen_types", [{"name": "审判发言", "base": "未知"}]),
        "screen_types[0].base：未知 由运行时给出",
    ),
    (
        "extension based on an extension",
        put("screen_types", [{"name": "a", "base": "b"}]),
        "screen_types[0].base：Input should be",
    ),
    (
        "duplicate screen",
        put("screens", [{"id": "menu", "title": "菜单", "type": "菜单"}] * 2),
        "screens：menu 重复",
    ),
    (
        "fixed text on an undeclared screen",
        put("language.fixed_texts.0.screen", "title"),
        "language.fixed_texts[0].screen：没有声明画面 title",
    ),
    (
        "fixed text in an undeclared roi",
        put("language.fixed_texts.0.roi", "title"),
        "language.fixed_texts[0].roi：没有声明 ROI title",
    ),
    (
        "reference to an uncalibrated roi",
        drop("rois.start.rect"),
        "language.fixed_texts[0].roi：ROI start 尚未标定，不能引用",
    ),
    (
        "forbidden zone not declared",
        put("risk.forbidden_zones", ["shop"]),
        "risk.forbidden_zones[0]：没有声明 ROI shop",
    ),
    (
        "duplicate precondition",
        put("risk.preconditions", [{"id": "no-payment", "text": "关闭免密支付"}] * 2),
        "risk：preconditions：no-payment 重复",
    ),
    ("duplicate forbidden word", put("risk.forbidden_words", ["充值"] * 2), "充值 重复"),
    (
        "signature on an undeclared screen",
        put("signatures", [{"id": "s", "screen": "shop"}]),
        "signatures[0].screen：没有声明画面 shop",
    ),
    (
        "anchor in an undeclared roi",
        put(
            "signatures",
            [
                {
                    "id": "s",
                    "screen": "menu",
                    "anchors": [{"roi": "logo", "template_sha256": SHA, "min_score": 0.9}],
                }
            ],
        ),
        "signatures[0].anchors[0].roi：没有声明 ROI logo",
    ),
    (
        "anchor digest",
        put(
            "signatures",
            [
                {
                    "id": "s",
                    "screen": "menu",
                    "anchors": [{"roi": "start", "template_sha256": "AB" * 32, "min_score": 0.9}],
                }
            ],
        ),
        "signatures[0].anchors[0].template_sha256：String should match pattern",
    ),
    (
        "zero anchor score",
        put(
            "signatures",
            [
                {
                    "id": "s",
                    "screen": "menu",
                    "anchors": [{"roi": "start", "template_sha256": SHA, "min_score": 0}],
                }
            ],
        ),
        "signatures[0].anchors[0].min_score：Input should be greater",
    ),
    (
        "pixel color",
        put(
            "signatures",
            [
                {
                    "id": "s",
                    "screen": "menu",
                    "pixels": [{"at": [0.5, 0.5], "color": "#12345", "tolerance": 0}],
                }
            ],
        ),
        "signatures[0].pixels[0].color：String should match pattern",
    ),
    (
        "pixel tolerance",
        put(
            "signatures",
            [
                {
                    "id": "s",
                    "screen": "menu",
                    "pixels": [{"at": [0.5, 0.5], "color": "#123456", "tolerance": 256}],
                }
            ],
        ),
        "signatures[0].pixels[0].tolerance：Input should be less",
    ),
    (
        "pixel outside the client area",
        put(
            "signatures",
            [
                {
                    "id": "s",
                    "screen": "menu",
                    "pixels": [{"at": [1.5, 0.5], "color": "#123456", "tolerance": 0}],
                }
            ],
        ),
        "signatures[0].pixels[0].at[0]：Input should be less",
    ),
    (
        "duplicate signature",
        put("signatures", [{"id": "s", "screen": "menu"}] * 2),
        "signatures：s 重复",
    ),
    (
        "duplicate note",
        put("notes", [{"id": "clues", "title": "线索"}] * 2),
        "notes：clues 重复",
    ),
    (
        "reread longer than read",
        put(
            "pacing",
            {
                "min_action_interval": 0.5,
                "read_base": 1,
                "read_per_char": 0.05,
                "reread_base": 2,
                "voice_unknown_wait": 6,
            },
        ),
        "pacing：reread_base 不能比 read_base 长",
    ),
    (
        "zero voice wait",
        put(
            "pacing",
            {
                "min_action_interval": 0.5,
                "read_base": 1,
                "read_per_char": 0.05,
                "voice_unknown_wait": 0,
            },
        ),
        "pacing.voice_unknown_wait：Input should be greater",
    ),
]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [pytest.param(mutate, expected, id=name) for name, mutate, expected in REJECTED],
)
def test_invalid_declaration_is_rejected(mutate: Mutation, expected: str) -> None:
    data = declaration()
    mutate(data)

    found = problems(data)

    assert any(expected in problem for problem in found), found


def test_every_cross_reference_problem_is_reported_at_once() -> None:
    data = declaration()
    data["screens"][0]["type"] = "弹窗"
    data["whitelist"]["click_regions"] = ["board"]
    data["notes"] = [{"id": "clues", "title": "线索"}] * 2

    found = problems(data)

    assert found == [
        "notes：clues 重复",
        "screens[0].type：弹窗 不是通用画面类型，也没有在 screen_types 中声明",
        "whitelist.click_regions[0]：没有声明 ROI board",
    ]


def write_package(root: Path, name: str, text: str) -> Path:
    directory = root / name
    directory.mkdir()
    (directory / ADAPTER_FILE).write_text(text, encoding="utf-8", newline="\n")
    return directory


def test_load_reads_the_declaration_of_a_directory(tmp_path: Path) -> None:
    directory = write_package(tmp_path, "sample", BASE)

    assert load_adapter(directory) == Adapter.model_validate(declaration())


def test_load_requires_the_id_to_match_the_directory(tmp_path: Path) -> None:
    directory = write_package(tmp_path, "other", BASE)

    with pytest.raises(AdapterError) as caught:
        load_adapter(directory)

    assert caught.value.problems == ("id：sample 与目录名 other 不同",)


def test_load_reports_a_missing_declaration(tmp_path: Path) -> None:
    with pytest.raises(AdapterError) as caught:
        load_adapter(tmp_path / "absent")

    assert caught.value.problems == (f"没有 {ADAPTER_FILE}",)


def test_load_reports_invalid_toml(tmp_path: Path) -> None:
    directory = write_package(tmp_path, "sample", BASE + "\n[window]\n")

    with pytest.raises(AdapterError) as caught:
        load_adapter(directory)

    assert caught.value.problems[0].startswith(f"{ADAPTER_FILE} 不是合法的 TOML")


def test_load_reports_a_file_that_is_not_utf8(tmp_path: Path) -> None:
    directory = tmp_path / "sample"
    directory.mkdir()
    (directory / ADAPTER_FILE).write_bytes(BASE.encode().replace("样例".encode(), b"\xff\xfe"))

    with pytest.raises(AdapterError) as caught:
        load_adapter(directory)

    assert caught.value.problems[0].startswith(f"{ADAPTER_FILE} 不是 UTF-8")


def test_load_rejects_toml_dates(tmp_path: Path) -> None:
    directory = write_package(
        tmp_path, "sample", BASE.replace('version = "1"', "version = 2026-11-01")
    )

    with pytest.raises(AdapterError) as caught:
        load_adapter(directory)

    assert caught.value.problems == ("version：Input should be a valid string",)
    assert "sample" in str(caught.value)
