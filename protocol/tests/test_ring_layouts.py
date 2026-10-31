"""手写的 Python 帧环与音频环布局与 Rust 导出的偏移表逐项一致：常量、结构大小与对齐、字段顺序、
偏移、大小与类型。偏移表由 `uv run generate` 从 Rust 的 `#[repr(C)]` 结构导出。"""

import ctypes
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from core_protocol import audio_ring, frame_ring

PROTOCOL_DIR = Path(__file__).resolve().parents[1]
RINGS: dict[str, tuple[ModuleType, Path]] = {
    "frame_ring": (frame_ring, PROTOCOL_DIR / "frame-ring-layout.json"),
    "audio_ring": (audio_ring, PROTOCOL_DIR / "audio-ring-layout.json"),
}

# 偏移表中的字段类型对应的 ctypes 类型；原子类型在 Python 侧按同宽的整数读写。
FIELD_TYPES: dict[str, type[ctypes.c_uint32 | ctypes.c_uint64]] = {
    "u32": ctypes.c_uint32,
    "u64": ctypes.c_uint64,
    "atomic_u64": ctypes.c_uint64,
}


def _layout(ring: str) -> dict[str, Any]:
    return json.loads(RINGS[ring][1].read_text(encoding="utf-8"))


def _cases(part: str) -> list[Any]:
    return [
        pytest.param(ring, item, id=f"{ring}.{item['name']}")
        for ring in RINGS
        for item in _layout(ring)[part]
    ]


def _structs(ring: str) -> dict[str, type[ctypes.Structure]]:
    module = RINGS[ring][0]
    return {struct.__name__: struct for struct in module.STRUCTS}


@pytest.mark.parametrize(("ring", "constant"), _cases("constants"))
def test_constant_matches(ring: str, constant: dict[str, Any]) -> None:
    assert getattr(RINGS[ring][0], constant["name"]) == constant["value"]


@pytest.mark.parametrize("ring", RINGS)
def test_the_same_structs_are_defined(ring: str) -> None:
    assert sorted(_structs(ring)) == sorted(struct["name"] for struct in _layout(ring)["structs"])


@pytest.mark.parametrize(("ring", "layout"), _cases("structs"))
def test_struct_layout_matches(ring: str, layout: dict[str, Any]) -> None:
    struct = _structs(ring)[layout["name"]]
    assert ctypes.sizeof(struct) == layout["size"]
    assert ctypes.alignment(struct) == layout["align"]
    declared = [(field[0], field[1]) for field in struct._fields_]
    expected = [(field["name"], FIELD_TYPES[field["type"]]) for field in layout["fields"]]
    assert declared == expected
    for field in layout["fields"]:
        descriptor = getattr(struct, field["name"])
        assert (descriptor.offset, descriptor.size) == (field["offset"], field["size"]), field
