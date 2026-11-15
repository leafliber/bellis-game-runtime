"""Chat Completions 的 SSE 正文与用量，忽略思考文本。"""

import asyncio
import json
import math
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import cast

from game_runtime.models.errors import ModelCallError, ModelFailure

_NEWLINE = re.compile(rb"\r\n?|\n")


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int | None
    completion_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int | None


@dataclass(slots=True)
class StreamStats:
    started: float
    first_output_s: float | None = None
    usage: Usage | None = None


async def read_completion(pieces: AsyncIterator[bytes], stats: StreamStats) -> str:
    parts: list[str] = []
    stopped = False
    done = False
    async for event in _events(pieces):
        if done:
            raise ModelCallError(ModelFailure.PROTOCOL)
        if event == b"[DONE]":
            if not stopped or not parts:
                raise ModelCallError(ModelFailure.INVALID_OUTPUT)
            done = True
            continue

        block = json_object(event, ModelFailure.PROTOCOL)
        if block.get("error") is not None:
            raise ModelCallError(ModelFailure.PROVIDER)
        usage = _as_object(block.get("usage"))
        if usage is not None:
            stats.usage = _usage(usage)
        raw_choices = block.get("choices")
        if not isinstance(raw_choices, list):
            raise ModelCallError(ModelFailure.PROTOCOL)
        choices = cast(list[object], raw_choices)
        if not choices:
            continue
        if stopped or len(choices) != 1:
            raise ModelCallError(ModelFailure.PROTOCOL)
        choice = _as_object(choices[0])
        if choice is None or type(choice.get("index")) is not int or choice["index"] != 0:
            raise ModelCallError(ModelFailure.PROTOCOL)
        delta = _as_object(choice.get("delta"))
        finish = choice.get("finish_reason")
        if delta is None:
            raise ModelCallError(ModelFailure.PROTOCOL)
        if any(
            delta.get(field) for field in ("refusal", "tool_calls", "function_call")
        ) or finish not in (None, "stop"):
            raise ModelCallError(ModelFailure.INVALID_OUTPUT)
        content: object = delta.get("content")
        if content is not None and not isinstance(content, str):
            raise ModelCallError(ModelFailure.PROTOCOL)
        if content:
            if stats.first_output_s is None:
                stats.first_output_s = asyncio.get_running_loop().time() - stats.started
            parts.append(content)
        stopped = finish == "stop"
    if not done:
        raise ModelCallError(ModelFailure.PROTOCOL)
    return "".join(parts)


async def _events(pieces: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    line = bytearray()
    data: list[bytes] = []
    first_line = True
    preceded_by_cr = False
    async for piece in pieces:
        if preceded_by_cr:
            piece = piece.removeprefix(b"\n")
        preceded_by_cr = piece.endswith(b"\r")
        start = 0
        for match in _NEWLINE.finditer(piece):
            line.extend(piece[start : match.start()])
            start = match.end()
            if first_line:
                line = line.removeprefix(b"\xef\xbb\xbf")
                first_line = False
            if not line and data:
                yield b"\n".join(data)
                data.clear()
            elif line.startswith(b"data:"):
                data.append(bytes(line[5:]).removeprefix(b" "))
            line.clear()
        line.extend(piece[start:])
    # 兼容在完整结束标记后直接关闭流的端点。
    terminal_line = not data and bytes(line) in (b"data:[DONE]", b"data: [DONE]")
    if terminal_line or (not line and data == [b"[DONE]"]):
        yield b"[DONE]"
    elif data or line.startswith(b"data:"):
        raise ModelCallError(ModelFailure.PROTOCOL)


def _usage(value: dict[str, object]) -> Usage:
    details = _as_object(value.get("completion_tokens_details"))
    reasoning = details.get("reasoning_tokens") if details is not None else None
    return Usage(
        _token_count(value.get("prompt_tokens")),
        _token_count(value.get("completion_tokens")),
        _token_count(reasoning),
        _token_count(value.get("total_tokens")),
    )


def _token_count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _as_object(value: object) -> dict[str, object] | None:
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def json_object(raw: bytes | str, failure: ModelFailure) -> dict[str, object]:
    """只接受 UTF-8 JSON 对象，拒绝重复的键和非有限数。"""
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        value: object = json.loads(
            text,
            parse_constant=_bad_constant,
            parse_float=_finite_float,
            object_pairs_hook=_unique_keys,
        )
    except (ValueError, RecursionError):
        raise ModelCallError(failure) from None
    fields = _as_object(value)
    if fields is None:
        raise ModelCallError(failure)
    return fields


def _bad_constant(_: str) -> None:
    raise ValueError("JSON 不允许非有限数")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        _bad_constant(value)
    return number


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("JSON 键重复")
    return value
