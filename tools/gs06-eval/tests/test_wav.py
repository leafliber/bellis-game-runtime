"""WAV 的读写：各种位深、`WAVE_FORMAT_EXTENSIBLE`、不支持的格式。"""

import struct
from pathlib import Path

import numpy as np
import pytest

from gs06_eval.wav import Audio, WavError, decode_wav, encode_wav, read_wav, write_wav


def riff(fmt: bytes, data: bytes, *, extra: bytes = b"") -> bytes:
    body = b"WAVE" + extra + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


def fmt(tag: int, channels: int, rate: int, bits: int) -> bytes:
    align = channels * bits // 8
    return struct.pack("<HHIIHH", tag, channels, rate, rate * align, align, bits)


def test_float32_round_trip(tmp_path: Path) -> None:
    samples = np.array([[0.0, -0.5], [0.25, 1.5], [-1.0, 0.125]], dtype=np.float32)
    path = tmp_path / "a.wav"
    write_wav(path, Audio(samples, 48_000))
    audio = read_wav(path)
    assert (audio.sample_rate, audio.channels, audio.length) == (48_000, 2, 3)
    assert np.array_equal(audio.samples, samples)
    assert audio.duration == pytest.approx(3 / 48_000)


def test_pcm16_round_trip_clips_to_full_scale() -> None:
    samples = np.array([[0.0], [0.5], [-2.0]], dtype=np.float32)
    audio = decode_wav(encode_wav(Audio(samples, 16_000), sample_format="pcm16"))
    assert audio.samples[:, 0].tolist() == pytest.approx([0.0, 0.5, -32767 / 32768], abs=1e-4)


def test_24_and_32_bit_pcm_and_64_bit_float() -> None:
    values = [0, 1 << 22, -(1 << 23)]
    data = b"".join(v.to_bytes(3, "little", signed=True) for v in values)
    audio = decode_wav(riff(fmt(1, 1, 8_000, 24), data))
    assert audio.samples[:, 0].tolist() == [0.0, 0.5, -1.0]
    data = struct.pack("<3i", 0, 1 << 30, -(1 << 31))
    assert decode_wav(riff(fmt(1, 1, 8_000, 32), data)).samples[:, 0].tolist() == [0.0, 0.5, -1.0]
    data = struct.pack("<2d", 0.25, -0.75)
    assert decode_wav(riff(fmt(3, 1, 8_000, 64), data)).samples[:, 0].tolist() == [0.25, -0.75]
    data = bytes([128, 192, 0])
    assert decode_wav(riff(fmt(1, 1, 8_000, 8), data)).samples[:, 0].tolist() == [0.0, 0.5, -1.0]


def test_extensible_format_and_other_chunks() -> None:
    base = fmt(0xFFFE, 2, 44_100, 16)
    # cbSize、有效位数、声道掩码与子格式（GUID 的前两个字节是格式码）。
    extension = struct.pack("<HHI", 22, 16, 3) + struct.pack("<H", 1) + bytes(14)
    data = struct.pack("<4h", 16384, -16384, 0, 32767)
    audio = decode_wav(
        riff(base + extension, data, extra=b"LIST" + struct.pack("<I", 3) + b"abc\0")
    )
    assert (audio.sample_rate, audio.channels, audio.length) == (44_100, 2, 2)
    assert audio.samples[0].tolist() == [0.5, -0.5]


def test_unsupported_and_broken_files_are_rejected() -> None:
    with pytest.raises(WavError, match="RIFF"):
        decode_wav(b"not a wav file")
    with pytest.raises(WavError, match="不支持"):
        decode_wav(riff(fmt(2, 1, 8_000, 16), b"\0\0"))
    with pytest.raises(WavError, match="data"):
        decode_wav(
            b"RIFF"
            + struct.pack("<I", 28)
            + b"WAVEfmt "
            + struct.pack("<I", 16)
            + fmt(1, 1, 8_000, 16)
        )
