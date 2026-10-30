"""手写的 Python 帧环布局与 Rust 导出的偏移表逐项一致：常量、结构大小与对齐、字段顺序、偏移、
大小与类型。偏移表由 `uv run generate` 从 Rust 的 `#[repr(C)]` 结构导出。"""

import ctypes
import json
from pathlib import Path
from typing import Any

import pytest
from core_protocol import frame_ring

LAYOUT_PATH = Path(__file__).resolve().parents[1] / "frame-ring-layout.json"
LAYOUT: dict[str, Any] = json.loads(LAYOUT_PATH.read_text(encoding="utf-8"))
STRUCTS = {struct.__name__: struct for struct in frame_ring.STRUCTS}

# 偏移表中的字段类型对应的 ctypes 类型；原子类型在 Python 侧按同宽的整数读写。
FIELD_TYPES: dict[str, type[ctypes.c_uint32 | ctypes.c_uint64]] = {
    "u32": ctypes.c_uint32,
    "u64": ctypes.c_uint64,
    "atomic_u64": ctypes.c_uint64,
}


@pytest.mark.parametrize("constant", LAYOUT["constants"], ids=lambda constant: constant["name"])
def test_constant_matches(constant: dict[str, Any]) -> None:
    assert getattr(frame_ring, constant["name"]) == constant["value"]


def test_the_same_structs_are_defined() -> None:
    assert sorted(STRUCTS) == sorted(struct["name"] for struct in LAYOUT["structs"])


@pytest.mark.parametrize("layout", LAYOUT["structs"], ids=lambda layout: layout["name"])
def test_struct_layout_matches(layout: dict[str, Any]) -> None:
    struct = STRUCTS[layout["name"]]
    assert ctypes.sizeof(struct) == layout["size"]
    assert ctypes.alignment(struct) == layout["align"]
    declared = [(field[0], field[1]) for field in struct._fields_]
    expected = [(field["name"], FIELD_TYPES[field["type"]]) for field in layout["fields"]]
    assert declared == expected
    for field in layout["fields"]:
        descriptor = getattr(struct, field["name"])
        assert (descriptor.offset, descriptor.size) == (field["offset"], field["size"]), field
