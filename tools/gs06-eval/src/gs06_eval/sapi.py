"""用 Windows 自带的语音合成（SAPI 5 的 COM 接口 `SAPI.SpVoice`，经 PowerShell）把台词合成为 WAV。

语音取两处登记的：SAPI 的桌面语音与 OneCore 语音（Windows 设置里装的语音包，登记在
`HKLM\\SOFTWARE\\Microsoft\\Speech_OneCore\\Voices`）。只读这两处的登记，不改系统设置。

只用于评测与测试，生成的文件放临时目录或评测的输出目录，不入库。台词经 JSON 文件交给 PowerShell，
不拼进命令行；PowerShell 以 `-Command` 运行固定的脚本，不受执行策略影响；结果写回 UTF-8 的 JSON
文件，不经控制台的代码页。
"""

import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TIMEOUT = 60.0
"""一次合成的基本时限（秒），每句另加 `TIMEOUT_PER_ITEM`。"""
TIMEOUT_PER_ITEM = 15.0

FORMATS = {
    8_000: 6,
    11_025: 10,
    12_000: 14,
    16_000: 18,
    22_050: 22,
    24_000: 26,
    32_000: 30,
    44_100: 34,
    48_000: 38,
}
"""采样率到 SAPI 的 `SpeechAudioFormatType`（16 位单声道）。"""

RATES = range(-10, 11)
"""SAPI 的语速：−10 到 10，0 为正常。"""

_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$spec = Get-Content -LiteralPath $env:GS06_SAPI_SPEC -Raw -Encoding UTF8 | ConvertFrom-Json
$sp = New-Object -ComObject SAPI.SpVoice
$tokens = New-Object System.Collections.ArrayList
foreach ($t in $sp.GetVoices()) { [void]$tokens.Add($t) }
try {
  $onecore = New-Object -ComObject SAPI.SpObjectTokenCategory
  $onecore.SetId('HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech_OneCore\Voices', $false)
  foreach ($t in $onecore.EnumerateTokens()) { [void]$tokens.Add($t) }
} catch { }
function Attr($token, $name) {
  try { return [string]$token.GetAttribute($name) } catch { return '' }
}
$voices = New-Object System.Collections.ArrayList
foreach ($t in $tokens) {
  $culture = ''
  try {
    $lcid = [Convert]::ToInt32(((Attr $t 'Language') -split ';')[0], 16)
    $culture = [System.Globalization.CultureInfo]::GetCultureInfo($lcid).Name
  } catch { }
  [void]$voices.Add(@{
    id = [string]$t.Id; name = (Attr $t 'Name'); culture = $culture; gender = (Attr $t 'Gender')
  })
}
$format = New-Object -ComObject SAPI.SpAudioFormat
$format.Type = [int]$spec.format
foreach ($job in $spec.jobs) {
  $token = $null
  foreach ($t in $tokens) { if ([string]$t.Id -eq [string]$job.voice) { $token = $t; break } }
  if ($null -eq $token) { throw "no such voice: $($job.voice)" }
  $sp.Voice = $token
  $sp.Rate = [int]$job.rate
  foreach ($item in $job.items) {
    $stream = New-Object -ComObject SAPI.SpFileStream
    $stream.Format = $format
    $stream.Open([string]$item.path, 3, $false)
    try {
      $sp.AudioOutputStream = $stream
      [void]$sp.Speak([string]$item.text, 16)
    } finally {
      $stream.Close()
    }
  }
}
$json = @{ voices = @($voices) } | ConvertTo-Json -Depth 4 -Compress
$utf8 = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText([string]$spec.result, $json, $utf8)
"""


class SapiError(RuntimeError):
    """没有可用的 SAPI，或合成失败。"""


@dataclass(frozen=True)
class Voice:
    """一把已登记的语音。"""

    id: str
    """SAPI 的语音令牌（注册表路径），合成时以它选择语音。"""
    name: str
    culture: str
    """语言，如 `zh-CN`；登记里没有时为空。"""
    gender: str

    @property
    def language(self) -> str:
        """语言的主标签，如 `zh`。"""
        return self.culture.split("-")[0].lower()


@dataclass(frozen=True)
class Job:
    """用一把语音、一种语速依次合成若干句，每句一个 16 位单声道 WAV。"""

    voice: Voice
    rate: int
    items: tuple[tuple[str, Path], ...]
    """（台词、输出的 WAV）。"""

    def __post_init__(self) -> None:
        if self.rate not in RATES:
            raise ValueError(f"SAPI 的语速须在 −10 到 10 之间：{self.rate}")


def _powershell() -> str | None:
    if sys.platform != "win32":
        return None
    return shutil.which("powershell.exe")


def _run(jobs: Sequence[Job], sample_rate: int) -> list[Voice]:
    """运行脚本：合成 `jobs`，返回已登记的语音。"""
    powershell = _powershell()
    if powershell is None:
        raise SapiError("SAPI 只在 Windows 上可用")
    if sample_rate not in FORMATS:
        raise SapiError(f"SAPI 不支持 {sample_rate} Hz；可用：{', '.join(map(str, FORMATS))}")
    items = sum(len(job.items) for job in jobs)
    timeout = TIMEOUT + TIMEOUT_PER_ITEM * items
    with tempfile.TemporaryDirectory(prefix="gs06-sapi-") as directory:
        spec_path = Path(directory) / "spec.json"
        result_path = Path(directory) / "result.json"
        spec: Mapping[str, object] = {
            "format": FORMATS[sample_rate],
            "result": str(result_path),
            "jobs": [
                {
                    "voice": job.voice.id,
                    "rate": job.rate,
                    "items": [{"text": text, "path": str(path)} for text, path in job.items],
                }
                for job in jobs
            ],
        }
        spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        env = {**os.environ, "GS06_SAPI_SPEC": str(spec_path)}
        argv = [powershell, "-NoProfile", "-NonInteractive", "-Command", _SCRIPT]
        try:
            with subprocess.Popen(
                argv,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
            ) as process:
                try:
                    _, stderr = process.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    raise SapiError(f"SAPI 合成超过 {timeout:g} 秒") from None
        except OSError as error:
            raise SapiError(f"无法运行 PowerShell：{error}") from error
        if process.returncode != 0:
            raise SapiError(
                f"SAPI 合成失败（退出码 {process.returncode}）：{stderr.strip()[-500:]}"
            )
        try:
            result: Any = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SapiError(f"读不出 SAPI 的结果：{error}") from error
    return [
        Voice(str(v["id"]), str(v["name"]), str(v["culture"]), str(v["gender"]))
        for v in result["voices"]
    ]


@functools.cache
def voices() -> tuple[Voice, ...]:
    """已登记的语音：先 SAPI 的桌面语音，后 OneCore 语音；没有 SAPI 时为空。"""
    try:
        return tuple(_run([], 48_000))
    except SapiError:
        return ()


def available() -> bool:
    """本机能用 SAPI 合成语音。"""
    return bool(voices())


def synthesize_jobs(jobs: Sequence[Job], *, sample_rate: int = 48_000) -> None:
    """在一次 PowerShell 里依次合成全部 `jobs`。"""
    if jobs:
        _run(jobs, sample_rate)


def pick(culture: str = "zh-CN") -> Voice:
    """语言为 `culture` 的第一把语音，没有时取主标签相同的，再没有时取第一把。"""
    found = voices()
    if not found:
        raise SapiError("本机没有可用的 SAPI 语音（只在 Windows 上可用）")
    language = culture.split("-")[0].lower()
    exact = [v for v in found if v.culture.lower() == culture.lower()]
    same = [v for v in found if v.language == language]
    return (exact or same or list(found))[0]


def synthesize(
    items: Sequence[tuple[str, Path]],
    *,
    sample_rate: int = 48_000,
    culture: str = "zh-CN",
    rate: int = 0,
) -> str:
    """把每句台词合成为一个 16 位单声道 WAV，返回所用语音的名字（语音按 `pick(culture)`）。"""
    voice = pick(culture)
    synthesize_jobs([Job(voice, rate, tuple(items))], sample_rate=sample_rate)
    return voice.name
