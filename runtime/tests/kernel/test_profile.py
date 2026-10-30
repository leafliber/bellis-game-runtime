"""profile 与 patch：格式同 Bellis，按包的顺序展开，再依次应用 patch。"""

import json
from pathlib import Path

import pytest
from game_runtime.kernel import Profile, ProfilePatch, read_patch, read_profile, resolve_profile
from pydantic import ValidationError

PROFILE = Profile.model_validate(
    {
        "name": "sim",
        "packs": [
            {
                "name": "core",
                "entries": [
                    {"id": "adapter", "plugin": "adapter-sim"},
                    {"id": "ocr", "plugin": "ocr-sim", "config": {"lang": "zh"}},
                ],
            },
            {
                "name": "control",
                "entries": [
                    {
                        "id": "step",
                        "plugin": "step-controller",
                        "disabled": False,
                        "grant": ["control.submit", "frames.read"],
                    }
                ],
            },
        ],
    }
)


def test_expands_packs_in_order_with_defaults() -> None:
    entries = resolve_profile(PROFILE)
    assert [(e.id, e.plugin, e.disabled) for e in entries] == [
        ("adapter", "adapter-sim", False),
        ("ocr", "ocr-sim", False),
        ("step", "step-controller", False),
    ]
    assert entries[0].config == {}
    assert entries[0].grant == frozenset()
    assert entries[1].config == {"lang": "zh"}
    assert entries[2].grant == frozenset({"control.submit", "frames.read"})


def test_patches_apply_in_order() -> None:
    local = ProfilePatch.model_validate(
        {
            "name": "local",
            "ops": [
                {"op": "replace", "entry": {"id": "ocr", "plugin": "ocr-other"}},
                {
                    "op": "insert",
                    "entry": {"id": "scene", "plugin": "scene-sim"},
                    "after": "adapter",
                },
                {"op": "insert", "entry": {"id": "telemetry", "plugin": "telemetry"}},
                {"op": "disable", "id": "step"},
                {"op": "disable", "id": "adapter"},
            ],
        }
    )
    later = ProfilePatch.model_validate({"name": "later", "ops": [{"op": "enable", "id": "step"}]})
    entries = resolve_profile(PROFILE, [local, later])
    assert [(e.id, e.plugin, e.disabled) for e in entries] == [
        ("adapter", "adapter-sim", True),
        ("scene", "scene-sim", False),
        ("ocr", "ocr-other", False),
        ("step", "step-controller", False),
        ("telemetry", "telemetry", False),
    ]
    # replace 换掉整个条目，不与旧条目合并：旧的配置不保留。
    assert entries[2].config == {}
    # disable 与 enable 只改开关，配置与授予保留。
    assert entries[3].grant == frozenset({"control.submit", "frames.read"})


def test_replace_drops_old_grant() -> None:
    patch = ProfilePatch.model_validate(
        {"name": "p", "ops": [{"op": "replace", "entry": {"id": "step", "plugin": "step-other"}}]}
    )
    step = resolve_profile(PROFILE, [patch])[2]
    assert (step.plugin, step.grant) == ("step-other", frozenset())


@pytest.mark.parametrize(
    ("op", "message"),
    [
        ({"op": "disable", "id": "nope"}, "没有条目 nope"),
        ({"op": "enable", "id": "nope"}, "没有条目 nope"),
        ({"op": "replace", "entry": {"id": "nope", "plugin": "x"}}, "没有可替换的条目 nope"),
        ({"op": "insert", "entry": {"id": "ocr", "plugin": "x"}}, "条目 ocr 已存在"),
        ({"op": "insert", "entry": {"id": "new", "plugin": "x"}, "after": "nope"}, "没有条目 nope"),
    ],
)
def test_patch_must_match_entries(op: dict[str, object], message: str) -> None:
    patch = ProfilePatch.model_validate({"name": "bad", "ops": [op]})
    with pytest.raises(ValueError, match=f"patch bad：{message}"):
        resolve_profile(PROFILE, [patch])


def test_duplicate_entry_ids_across_packs_are_rejected() -> None:
    profile = Profile.model_validate(
        {
            "name": "dup",
            "packs": [
                {"name": "a", "entries": [{"id": "x", "plugin": "x"}]},
                {"name": "b", "entries": [{"id": "x", "plugin": "y"}]},
            ],
        }
    )
    with pytest.raises(ValueError, match="条目 x 重复"):
        resolve_profile(profile)


NO_ITEMS: list[object] = []


def _with_entry(**fields: object) -> dict[str, object]:
    entry: dict[str, object] = {"id": "x", "plugin": "x", **fields}
    return {"name": "sim", "packs": [{"name": "p", "entries": [entry]}]}


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"name": "Bad Name", "packs": NO_ITEMS}, id="profile 名不是合法标识"),
        pytest.param({"name": "sim", "packs": NO_ITEMS, "extra": 1}, id="未知字段"),
        pytest.param({"name": "sim", "packs": [{"name": "", "entries": NO_ITEMS}]}, id="包名为空"),
        pytest.param(_with_entry(plugin="X"), id="插件标识含大写"),
        pytest.param(_with_entry(disabled="no"), id="disabled 不是布尔"),
        pytest.param(_with_entry(grant=["a b"]), id="权限格式不对"),
        pytest.param(_with_entry(config=[1]), id="配置不是对象"),
        pytest.param(_with_entry(enabled=True), id="条目有未知字段"),
    ],
)
def test_malformed_profile_is_rejected(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Profile.model_validate(data)


def test_unknown_patch_op_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ProfilePatch.model_validate({"name": "p", "ops": [{"op": "remove", "id": "x"}]})


def test_reads_bellis_format_json(tmp_path: Path) -> None:
    profile_path = tmp_path / "live.json"
    profile_path.write_text(
        json.dumps(
            {
                "name": "live",
                "packs": [
                    {
                        "name": "perception",
                        "entries": [
                            {
                                "id": "vlm",
                                "plugin": "vlm-openai",
                                "disabled": True,
                                "grant": ["net.egress:*.example.com", "secret:vlm"],
                                "config": {"endpoint": "https://api.example.com/v1", "retries": 2},
                            },
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    patch_path = tmp_path / "models.json"
    patch_path.write_text(
        json.dumps({"name": "models", "ops": [{"op": "enable", "id": "vlm"}]}), encoding="utf-8"
    )

    [vlm] = resolve_profile(read_profile(profile_path), [read_patch(patch_path)])
    assert vlm.disabled is False
    assert vlm.grant == frozenset({"net.egress:*.example.com", "secret:vlm"})
    assert vlm.config == {"endpoint": "https://api.example.com/v1", "retries": 2}
