"""GS-06 的合成样本：SAPI 的几把语音按几种语速读原创台词作纯配音；背景为音乐样（和弦与旋律）、
音效样（短促的敲击、铃声与风声样的噪声）、宽带噪声（粉红噪声），以及音乐样加另一把语音不停地读另一组
原创台词，作“带人声的背景音乐”的代用。不用任何游戏音频。组成见 `docs/modules/perception.md` 的
“GS-06 的评测”。
"""

import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from gs06_eval import sapi
from gs06_eval.mix import background_level_db, mix, relative_gain_db, rms_db, voice_level_db
from gs06_eval.suite import Suite, SuiteBackground, SuiteLine, write_suite
from gs06_eval.truth import voice_truth
from gs06_eval.wav import Audio, read_wav, write_wav

LINES: dict[str, tuple[str, ...]] = {
    "zh": (
        "今天的风有点大，我们早点回去吧。",
        "你看，钟楼上的灯又亮了。",
        "这件事我想了很久，还是决定告诉你。",
        "好的。",
        "如果明天下雨，我们就改在图书馆见面，记得带上那本蓝色的笔记本。",
        "嗯，我知道了，谢谢你。",
        "走廊尽头的那扇门，好像一直没有人打开过。",
        "别担心，一切都会好起来的。",
        "等一下！你刚才说的那个名字，我好像在哪里听过。",
        "火车晚点了二十分钟，站台上挤满了人。",
        "不行。",
        "把窗户关上吧，外面的雨越下越大了，桌上的信纸都被吹乱了。",
        "你确定要一个人去吗？",
        "我数到三，我们一起跳过去。一、二、三！",
        "原来如此，难怪他昨天一句话也没说。",
        "早上好，今天的早饭是热粥和煎蛋。",
    ),
    "en": (
        "The lighthouse keeper has not been seen since Tuesday.",
        "Wait, did you hear that?",
        "If we leave before sunrise, we can reach the river by noon.",
        "Fine.",
        "I kept the letter, but I never found the courage to open it.",
        "Hold the lantern a little higher, please.",
        "Nobody told me the bridge was closed.",
        "Thank you. I will remember this.",
    ),
    "ja": (
        "今日は風が強いから、早めに帰ろう。",
        "ちょっと待って、今の音は何？",
        "明日の朝、駅の前で会いましょう。",
        "はい。",
        "その手紙は、まだ誰にも見せていないんだ。",
        "窓を閉めてくれる？雨が入ってくるよ。",
        "大丈夫、きっとうまくいくから。",
        "階段の下に、古い鍵が落ちていた。",
    ),
}
"""为评测原创的台词，按语言的主标签。只用有台词的语言的语音。"""

CHATTER: dict[str, tuple[str, ...]] = {
    "zh": (
        "街角的面包店今天推出了新口味。",
        "听说山那边的湖已经结冰了。",
        "我们下周再约时间吧，到时候再说。",
        "这首歌我小时候常常听到。",
        "天气预报说周末会转晴。",
        "下一站下车，再走一小段就到了。",
    ),
    "en": (
        "The market opens at nine on weekends.",
        "They say the lake behind the hill has frozen over.",
        "Let us pick a time next week and talk again.",
        "I used to hear this song when I was little.",
        "The forecast says it will clear up by Sunday.",
    ),
    "ja": (
        "週末は晴れるらしいよ。",
        "次の駅で降りて、少し歩こう。",
        "この歌、子どものころによく聞いたな。",
        "来週また時間を決めて話そう。",
    ),
}
"""带人声的背景里另一把语音读的原创台词。"""

RATES = (-3, 0, 4)
"""SAPI 的语速（−10 到 10）。"""
LINES_PER_TAKE = 4
"""每把语音按每种语速读几句。"""
MAX_CHINESE = 4
MAX_OTHER = 2
"""至多用几把中文语音、几把其它语言的语音。"""
GAP = (0.8, 2.0)
"""句间静音的范围（秒）。"""
LEAD = 1.0
TAIL = 1.5
"""第一句之前、最后一句之后的静音（秒）。"""
CHATTER_GAP = (0.3, 0.8)
"""带人声的背景里句间静音的范围（秒）。"""
CHATTER_DB = -6.0
"""带人声的背景里人声相对音乐的电平（dB）。"""
MIX_DB = -12.0
"""正常混音里音乐样与音效样相对配音的电平（dB）。"""
SEED = 0

FILES = {
    "voice": "voice.wav",
    "mix": "mix.wav",
    "music": "music.wav",
    "effects": "effects.wav",
    "noise": "noise.wav",
    "vocal-music": "vocal-music.wav",
}


type Float64Array = NDArray[np.float64]


def choose_voices(voices: Sequence[sapi.Voice], limit: int | None = None) -> list[sapi.Voice]:
    """中文的在前，至多 `MAX_CHINESE` 把；其后是有台词的其它语言的，至多 `MAX_OTHER` 把；再截到
    `limit` 把。"""
    chinese = [v for v in voices if v.language == "zh"][:MAX_CHINESE]
    other = [v for v in voices if v.language != "zh" and v.language in LINES][:MAX_OTHER]
    chosen = chinese + other
    return chosen if limit is None else chosen[:limit]


def chatter_voice(chosen: Sequence[sapi.Voice]) -> sapi.Voice:
    """带人声的背景所用的另一把语音：选中的最后一把有背景台词的，只有一把时就用它。"""
    others = [v for v in chosen[1:] if v.language in CHATTER]
    return others[-1] if others else chosen[0]


@dataclass(frozen=True)
class Take:
    voice: sapi.Voice
    rate: int
    texts: tuple[str, ...]


def takes(chosen: Sequence[sapi.Voice], rates: Sequence[int]) -> list[Take]:
    """每把语音按每种语速读 `LINES_PER_TAKE` 句，句子在这种语言的台词里依次轮换。"""
    result: list[Take] = []
    for vi, voice in enumerate(chosen):
        pool = LINES[voice.language]
        for ri, rate in enumerate(rates):
            start = (vi * len(rates) + ri) * LINES_PER_TAKE
            texts = tuple(pool[(start + j) % len(pool)] for j in range(LINES_PER_TAKE))
            result.append(Take(voice, rate, texts))
    return result


@dataclass(frozen=True)
class Synthesized:
    voice: Audio
    """纯配音，单声道。"""
    lines: tuple[SuiteLine, ...]
    chatter: tuple[Float64Array, ...]
    """带人声的背景所用的各句，单声道。"""
    chatter_voice: sapi.Voice


def _read_line(path: Path, sample_rate: int) -> Float64Array:
    line = read_wav(path)
    if line.sample_rate != sample_rate:
        raise sapi.SapiError(f"SAPI 给出 {line.sample_rate} Hz，要的是 {sample_rate} Hz")
    return line.mono()


def synthesize(
    chosen: Sequence[sapi.Voice], rates: Sequence[int], sample_rate: int = 48_000
) -> Synthesized:
    """合成全部台词，按随机的句间静音接成纯配音。要 SAPI。"""
    plan = takes(chosen, rates)
    background_voice = chatter_voice(chosen)
    chatter_texts = CHATTER[background_voice.language]
    rng = np.random.default_rng(SEED)
    with tempfile.TemporaryDirectory(prefix="gs06-voice-") as directory:
        root = Path(directory)
        jobs = [
            sapi.Job(
                take.voice,
                take.rate,
                tuple((text, root / f"take-{ti}-{li}.wav") for li, text in enumerate(take.texts)),
            )
            for ti, take in enumerate(plan)
        ]
        chatter_job = sapi.Job(
            background_voice,
            0,
            tuple((text, root / f"chatter-{i}.wav") for i, text in enumerate(chatter_texts)),
        )
        sapi.synthesize_jobs([*jobs, chatter_job], sample_rate=sample_rate)
        pieces: list[Float64Array] = [np.zeros(round(LEAD * sample_rate))]
        position = len(pieces[0])
        lines: list[SuiteLine] = []
        for take, job in zip(plan, jobs, strict=True):
            for text, path in job.items:
                samples = _read_line(path, sample_rate)
                lines.append(
                    SuiteLine(
                        voice=take.voice.name,
                        culture=take.voice.culture,
                        rate=take.rate,
                        start=position / sample_rate,
                        end=(position + len(samples)) / sample_rate,
                        text=text,
                    )
                )
                gap = np.zeros(round(float(rng.uniform(*GAP)) * sample_rate))
                pieces += [samples, gap]
                position += len(samples) + len(gap)
        pieces[-1] = np.zeros(round(TAIL * sample_rate))
        chatter = tuple(_read_line(path, sample_rate) for _, path in chatter_job.items)
    samples = np.concatenate(pieces).astype(np.float32)[:, None]
    return Synthesized(Audio(samples, sample_rate), tuple(lines), chatter, background_voice)


def _envelope(length: int, sample_rate: int, attack: float, decay: float) -> Float64Array:
    t = np.arange(length) / sample_rate
    return np.minimum(1.0, t / attack) * np.exp(-t / decay)


def music(duration: float, sample_rate: int = 48_000, *, seed: int = 0) -> Audio:
    """音乐样的背景：每拍 0.5 秒，四拍一个和弦（带谐波的正弦），上面一条随机的旋律，立体声。"""
    rng = np.random.default_rng(seed)
    length = round(duration * sample_rate)
    out = np.zeros((length, 2))
    beat = round(0.5 * sample_rate)
    chords = ((220.0, 277.18, 329.63), (196.0, 246.94, 293.66), (174.61, 220.0, 261.63))
    scale = (440.0, 493.88, 554.37, 587.33, 659.25, 739.99)
    harmonics = (1.0, 0.5, 0.25, 0.12)
    for index, start in enumerate(range(0, length, beat)):
        span = min(beat * 2, length - start)
        t = np.arange(span) / sample_rate
        if index % 4 == 0:
            chord_span = min(beat * 4, length - start)
            tc = np.arange(chord_span) / sample_rate
            chord = sum(
                a * np.sin(2 * np.pi * f * k * tc)
                for f in chords[(index // 4) % len(chords)]
                for k, a in enumerate(harmonics, start=1)
            )
            out[start : start + chord_span] += (
                0.04 * chord * _envelope(chord_span, sample_rate, 0.05, 2.0)
            )[:, None] * [1.0, 0.8]
        note = scale[int(rng.integers(len(scale)))]
        tone = sum(a * np.sin(2 * np.pi * note * k * t) for k, a in enumerate(harmonics, start=1))
        out[start : start + span] += (0.06 * tone * _envelope(span, sample_rate, 0.01, 0.35))[
            :, None
        ] * [0.8, 1.0]
    return Audio(out.astype(np.float32), sample_rate)


def effects(duration: float, sample_rate: int = 48_000, *, seed: int = 1) -> Audio:
    """音效样的背景：随机时刻的敲击（衰减的噪声）、铃声（衰减的高频正弦）与风声（缓起缓落的噪声）。"""
    rng = np.random.default_rng(seed)
    length = round(duration * sample_rate)
    out = np.zeros(length)
    at = 0.3
    while at < duration:
        start = round(at * sample_rate)
        kind = int(rng.integers(3))
        if kind == 0:
            span = min(round(0.08 * sample_rate), length - start)
            burst = rng.standard_normal(span) * _envelope(span, sample_rate, 0.001, 0.015)
            out[start : start + span] += 0.3 * burst
        elif kind == 1:
            span = min(round(0.6 * sample_rate), length - start)
            t = np.arange(span) / sample_rate
            freq = float(rng.uniform(1500, 3000))
            ring = np.sin(2 * np.pi * freq * t) + 0.4 * np.sin(2 * np.pi * freq * 2.76 * t)
            out[start : start + span] += 0.12 * ring * _envelope(span, sample_rate, 0.002, 0.15)
        else:
            span = min(round(1.2 * sample_rate), length - start)
            noise = np.convolve(rng.standard_normal(span), np.ones(16) / 16, mode="same")
            swell = np.sin(np.pi * np.arange(span) / max(1, span)) ** 2
            out[start : start + span] += 0.15 * noise * swell
        at += float(rng.uniform(0.7, 2.5))
    return Audio(np.stack([out, out], axis=1).astype(np.float32), sample_rate)


def noise(duration: float, sample_rate: int = 48_000, *, seed: int = 2) -> Audio:
    """宽带噪声：粉红噪声（功率谱按 1/f，20 Hz 以下持平），两个声道互不相关，均方根 0.1。"""
    rng = np.random.default_rng(seed)
    length = round(duration * sample_rate)
    out = np.zeros((length, 2))
    if length == 0:
        return Audio(out.astype(np.float32), sample_rate)
    shape = 1.0 / np.sqrt(np.maximum(np.fft.rfftfreq(length, 1.0 / sample_rate), 20.0))
    for channel in range(2):
        spectrum = np.fft.rfft(rng.standard_normal(length)) * shape
        out[:, channel] = np.fft.irfft(spectrum, n=length)
    out *= 0.1 / max(1e-12, float(np.sqrt(np.mean(out**2))))
    return Audio(out.astype(np.float32), sample_rate)


def vocal_music(
    duration: float,
    chatter: Sequence[Float64Array],
    sample_rate: int = 48_000,
    *,
    seed: int = 3,
) -> Audio:
    """带人声的背景音乐的代用：另一段音乐样，加上 `chatter` 各句依次循环、句间短静音的人声，
    人声比音乐低 `CHATTER_DB`。"""
    if not chatter:
        raise ValueError("没有人声")
    rng = np.random.default_rng(seed)
    bed = music(duration, sample_rate, seed=seed + 1)
    length = bed.length
    voice = np.zeros(length)
    position = 0
    index = 0
    while position < length:
        line = chatter[index % len(chatter)][: length - position]
        voice[position : position + len(line)] = line
        position += len(line) + round(float(rng.uniform(*CHATTER_GAP)) * sample_rate)
        index += 1
    voice_rms = float(np.sqrt(np.mean(voice**2)))
    target = 10.0 ** ((rms_db(bed) + CHATTER_DB) / 20.0)
    voice *= target / max(voice_rms, 1e-12)
    samples = bed.samples.astype(np.float64) + voice[:, None]
    return Audio(samples.astype(np.float32), sample_rate)


@dataclass(frozen=True)
class Written:
    suite: Suite
    path: Path
    """`suite.toml` 的路径。"""
    voices: tuple[sapi.Voice, ...]
    chatter_voice: sapi.Voice
    duration: float


def write_samples(
    directory: Path,
    *,
    sample_rate: int = 48_000,
    max_voices: int | None = None,
    rates: Sequence[int] = RATES,
) -> Written:
    """合成整组样本写进 `directory`：各段 WAV 与 `suite.toml`。要 SAPI。"""
    chosen = choose_voices(sapi.voices(), max_voices)
    if not chosen:
        raise sapi.SapiError("本机没有有台词的语言的 SAPI 语音")
    synthesized = synthesize(chosen, rates, sample_rate)
    voice = Audio(synthesized.voice.samples.repeat(2, axis=1), sample_rate)
    duration = voice.duration
    backgrounds = {
        "music": music(duration, sample_rate),
        "effects": effects(duration, sample_rate),
        "noise": noise(duration, sample_rate),
        "vocal-music": vocal_music(duration, synthesized.chatter, sample_rate),
    }
    level = voice_level_db(voice, voice_truth(voice).segments)
    normal = voice
    for name in ("music", "effects"):
        background = backgrounds[name]
        gain = relative_gain_db(level, background_level_db(background, voice.length), MIX_DB)
        normal = mix(normal, background, gain)
    directory.mkdir(parents=True, exist_ok=True)
    for name, audio in (("voice", voice), ("mix", normal), *backgrounds.items()):
        write_wav(directory / FILES[name], audio)
    suite = Suite(
        voice=FILES["voice"],
        mix=FILES["mix"],
        backgrounds=tuple(
            SuiteBackground(name=name, file=FILES[name], vocal=name == "vocal-music")
            for name in backgrounds
        ),
        lines=synthesized.lines,
    )
    header = [
        "GS-06 的合成样本，由 uv run gs06 synth 写出。",
        "语音：" + "，".join(f"{v.name}（{v.culture}）" for v in chosen),
        f"带人声的背景：{synthesized.chatter_voice.name}；语速：{', '.join(map(str, rates))}",
    ]
    path = write_suite(directory, suite, header)
    return Written(suite, path, tuple(chosen), synthesized.chatter_voice, duration)
