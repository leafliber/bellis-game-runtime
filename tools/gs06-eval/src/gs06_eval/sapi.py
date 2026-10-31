"""用 Windows 自带的语音合成（SAPI，经 PowerShell 的 `System.Speech`）把台词合成为 WAV。

只用于自测与测试，生成的文件放临时目录或评测的输出目录，不入库。台词经 JSON 文件交给 PowerShell，
不拼进命令行；PowerShell 以 `-Command` 运行固定的脚本，不受执行策略影响。
"""

import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

TIMEOUT = 120.0
"""一次合成的时限（秒）。"""

_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$spec = Get-Content -LiteralPath $env:GS06_SAPI_SPEC -Raw -Encoding UTF8 | ConvertFrom-Json
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  $voices = @($synth.GetInstalledVoices() | Where-Object { $_.Enabled })
  if ($voices.Count -eq 0) { throw 'no installed voice' }
  if ($spec.list) {
    foreach ($v in $voices) { [Console]::Out.WriteLine($v.VoiceInfo.Culture.Name) }
    return
  }
  $voice = $voices | Where-Object { $_.VoiceInfo.Culture.Name -eq $spec.culture } |
    Select-Object -First 1
  if ($voice) { $synth.SelectVoice($voice.VoiceInfo.Name) }
  $bits = [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen
  $mono = [System.Speech.AudioFormat.AudioChannel]::Mono
  $format = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new([int]$spec.rate, $bits, $mono)
  foreach ($item in $spec.items) {
    $synth.SetOutputToWaveFile([string]$item.path, $format)
    $synth.Speak([string]$item.text)
  }
  $synth.SetOutputToNull()
  [Console]::Out.Write($synth.Voice.Name)
} finally {
  $synth.Dispose()
}
"""


class SapiError(RuntimeError):
    """没有可用的 SAPI，或合成失败。"""


def _powershell() -> str | None:
    if sys.platform != "win32":
        return None
    return shutil.which("powershell.exe")


def _run(spec: Mapping[str, object]) -> str:
    powershell = _powershell()
    if powershell is None:
        raise SapiError("SAPI 只在 Windows 上可用")
    with tempfile.TemporaryDirectory(prefix="gs06-sapi-") as directory:
        path = Path(directory) / "spec.json"
        path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        env = {**os.environ, "GS06_SAPI_SPEC": str(path)}
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
                    stdout, stderr = process.communicate(timeout=TIMEOUT)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    raise SapiError(f"SAPI 合成超过 {TIMEOUT:g} 秒") from None
        except OSError as error:
            raise SapiError(f"无法运行 PowerShell：{error}") from error
    if process.returncode != 0:
        raise SapiError(f"SAPI 合成失败（退出码 {process.returncode}）：{stderr.strip()[-500:]}")
    return stdout


@functools.cache
def cultures() -> tuple[str, ...]:
    """已安装且启用的语音的语言（如 `zh-CN`）；没有 SAPI 或没有语音时为空。"""
    try:
        return tuple(line.strip() for line in _run({"list": True}).splitlines() if line.strip())
    except SapiError:
        return ()


def available() -> bool:
    """本机能用 SAPI 合成语音。"""
    return bool(cultures())


def synthesize(
    items: Sequence[tuple[str, Path]], *, sample_rate: int = 48_000, culture: str = "zh-CN"
) -> str:
    """把每句台词合成为一个 16 位单声道 WAV，返回所用语音的名字。

    有 `culture` 的语音就用它，否则用系统缺省的语音。
    """
    spec = {
        "list": False,
        "rate": sample_rate,
        "culture": culture,
        "items": [{"text": text, "path": str(path)} for text, path in items],
    }
    return _run(spec).strip()
