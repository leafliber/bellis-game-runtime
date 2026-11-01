"""合成样本：选语音与排台词（假的语音列表）、合成的背景；在 Windows 上有 SAPI 时列出语音并合成。"""

from pathlib import Path

import numpy as np
import pytest

from gs06_eval import sapi
from gs06_eval.mix import rms_db
from gs06_eval.synth import (
    CHATTER_DB,
    LINES,
    LINES_PER_TAKE,
    chatter_voice,
    choose_voices,
    effects,
    music,
    noise,
    takes,
    vocal_music,
)
from gs06_eval.wav import read_wav

RATE = 16_000


def voice(name: str, culture: str) -> sapi.Voice:
    return sapi.Voice(f"token-{name}", name, culture, "Female")


VOICES = [
    voice("en1", "en-US"),
    voice("zh1", "zh-CN"),
    voice("fr1", "fr-FR"),
    voice("zh2", "zh-TW"),
    voice("ja1", "ja-JP"),
    voice("zh3", "zh-CN"),
    voice("en2", "en-GB"),
    voice("zh4", "zh-HK"),
    voice("zh5", "zh-CN"),
    voice("none", ""),
]


def test_voices_are_chosen_chinese_first() -> None:
    names = [v.name for v in choose_voices(VOICES)]
    # 中文至多 4 把；没有台词的语言（法语、语言不明的）不选；其它语言至多 2 把。
    assert names == ["zh1", "zh2", "zh3", "zh4", "en1", "ja1"]
    assert [v.name for v in choose_voices(VOICES, 2)] == ["zh1", "zh2"]
    assert choose_voices([voice("fr1", "fr-FR")]) == []
    # 带人声的背景用另一把：选中的最后一把。
    assert chatter_voice(choose_voices(VOICES)).name == "ja1"
    assert chatter_voice([voice("zh1", "zh-CN")]).name == "zh1"


def test_takes_rotate_through_the_lines() -> None:
    chosen = [voice("zh1", "zh-CN"), voice("en1", "en-US")]
    plan = takes(chosen, (-3, 0, 4))
    assert [(t.voice.name, t.rate) for t in plan] == [
        ("zh1", -3),
        ("zh1", 0),
        ("zh1", 4),
        ("en1", -3),
        ("en1", 0),
        ("en1", 4),
    ]
    assert all(len(t.texts) == LINES_PER_TAKE for t in plan)
    zh = [text for t in plan[:3] for text in t.texts]
    assert zh == [LINES["zh"][i % len(LINES["zh"])] for i in range(3 * LINES_PER_TAKE)]
    assert all(text in LINES["en"] for t in plan[3:] for text in t.texts)


def test_backgrounds_are_deterministic_stereo_and_as_long_as_asked() -> None:
    for make in (music, effects, noise):
        a, b = make(3.0, RATE), make(3.0, RATE)
        assert (a.length, a.channels, a.sample_rate) == (3 * RATE, 2, RATE)
        assert np.array_equal(a.samples, b.samples)
        assert rms_db(a) > -60
    assert noise(0.0, RATE).length == 0


def test_noise_is_pink_and_broadband() -> None:
    audio = noise(4.0, RATE)
    assert float(np.sqrt(np.mean(audio.samples.astype(np.float64) ** 2))) == pytest.approx(0.1)
    # 两个声道互不相关。
    left, right = audio.samples[:, 0], audio.samples[:, 1]
    assert abs(float(np.corrcoef(left, right)[0, 1])) < 0.05
    # 粉红噪声：每个倍频程的能量大致相同。
    spectrum = np.abs(np.fft.rfft(left.astype(np.float64))) ** 2
    freqs = np.fft.rfftfreq(len(left), 1 / RATE)
    bands = [float(spectrum[(freqs >= f) & (freqs < 2 * f)].sum()) for f in (250, 500, 1000, 2000)]
    assert max(bands) / min(bands) < 1.5


def test_vocal_music_puts_a_voice_below_the_music() -> None:
    t = np.arange(RATE) / RATE
    line = 0.5 * np.sin(2 * np.pi * 300 * t)
    audio = vocal_music(5.0, [line], RATE)
    assert (audio.length, audio.channels) == (5 * RATE, 2)
    bed = music(5.0, RATE, seed=4)
    voice_part = audio.samples.astype(np.float64) - bed.samples.astype(np.float64)
    voice_db = 10 * np.log10(np.mean(voice_part[:, 0] ** 2))
    assert voice_db == pytest.approx(rms_db(bed) + CHATTER_DB, abs=0.5)
    with pytest.raises(ValueError, match="没有人声"):
        vocal_music(1.0, [], RATE)


def test_sapi_lists_voices_and_synthesizes_at_two_rates(tmp_path: Path) -> None:
    if not sapi.available():
        pytest.skip("没有可用的 SAPI 语音（只在 Windows 上）")
    found = sapi.voices()
    assert all(v.id and v.name for v in found)
    chosen = sapi.pick("zh-CN")
    text = LINES.get(chosen.language, LINES["en"])[0]
    slow, fast = tmp_path / "slow.wav", tmp_path / "fast.wav"
    sapi.synthesize_jobs(
        [sapi.Job(chosen, -5, ((text, slow),)), sapi.Job(chosen, 5, ((text, fast),))],
        sample_rate=RATE,
    )
    a, b = read_wav(slow), read_wav(fast)
    assert (a.sample_rate, a.channels) == (RATE, 1)
    assert a.duration > b.duration > 0.2
    with pytest.raises(ValueError, match="语速"):
        sapi.Job(chosen, 11, ())
    with pytest.raises(sapi.SapiError, match="不支持"):
        sapi.synthesize_jobs([sapi.Job(chosen, 0, ((text, slow),))], sample_rate=12_345)
