//! 游戏声音的采集：音频源接口与 WASAPI 进程回环实现。设计见 `docs/modules/realtime-core.md` 的
//! “音频采集”。
//!
//! 接口与格式在所有平台编译与测试；WASAPI 实现只在 Windows 上编译（[`win`]）。
//!
//! 用词：一个**采样点**是同一时刻各声道的样本值，F32 交错排列时占 `声道数 × 4` 字节。

#[cfg(windows)]
pub mod win;

use std::fmt;
use std::time::Duration;

/// 一个样本值（F32）的字节数。
pub const BYTES_PER_VALUE: u32 = 4;

/// 音频源交出的采样格式：32 位浮点（本机字节序）、各声道交错排列。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct AudioFormat {
    /// 采样率（Hz）。
    pub sample_rate: u32,
    /// 声道数。
    pub channels: u32,
}

impl AudioFormat {
    /// 一个采样点的字节数：`channels * 4`。
    #[must_use]
    pub fn bytes_per_sample(&self) -> usize {
        self.channels as usize * BYTES_PER_VALUE as usize
    }

    /// `samples` 个采样点的时长（纳秒），向下取整。采样率为 0 时为 0。
    #[must_use]
    pub fn duration_ns(&self, samples: u64) -> u64 {
        if self.sample_rate == 0 {
            return 0;
        }
        let ns = u128::from(samples) * 1_000_000_000 / u128::from(self.sample_rate);
        u64::try_from(ns).unwrap_or(u64::MAX)
    }
}

impl fmt::Display for AudioFormat {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{} Hz, {} channel(s), f32",
            self.sample_rate, self.channels
        )
    }
}

/// 一个音频包：一段连续的采样点。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct AudioPacket {
    /// 第一个采样点的采集时刻：QPC 时钟的纳秒数，与主进程 `time.perf_counter_ns()` 同一时钟。
    pub capture_ns: u64,
    /// 采样点数。
    pub sample_count: u32,
    /// 样本值，F32 本机字节序、各声道交错；长度为 `sample_count * 声道数 * 4`。
    pub data: Vec<u8>,
    /// 这个包与音频源交出的上一个包之间不连续（中间丢了声音，或时间轴断开）。
    pub discontinuity: bool,
}

/// 音频源：每次调用交出下一个音频包。
///
/// 与画面不同，音频源按顺序交出每一个包，不只交出最新的；音频源自己丢了声音时以
/// [`AudioPacket::discontinuity`] 标明。
pub trait AudioSource {
    /// 等待下一个包并写入 `packet`，复用 `packet` 的缓冲。
    ///
    /// 返回 `Ok(true)` 表示 `packet` 已换成新的包；`timeout` 内没有新包时返回 `Ok(false)`，
    /// `packet` 不变。
    ///
    /// # Errors
    ///
    /// 见 [`AudioError`]。返回错误之后这个音频源不再产出声音，须新建；返回错误时 `packet` 的内容
    /// 不确定。
    fn next_packet(
        &mut self,
        timeout: Duration,
        packet: &mut AudioPacket,
    ) -> Result<bool, AudioError>;
}

/// 采集声音失败的原因。音频源报错之后都不再产出声音，须重新识别目标并新建。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AudioError {
    /// 目标进程已退出。
    ProcessExited,
    /// 系统不支持所需的能力，例如进程回环（Windows 10 build 20348 起才有）。
    Unsupported(String),
    /// 音频源的格式与音频环不符。只有假音频源会报：假音频输出与音频环的采样率或声道数不同。
    FormatMismatch(String),
    /// 音频源取到的块不合格式。只有假音频源会报：假音频输出里的块头部不合音频环布局。
    InvalidBlock(String),
    /// 系统调用失败。
    Os {
        /// 失败的调用。
        context: &'static str,
        /// HRESULT 或 Win32 错误码。
        code: i32,
        /// 系统给出的说明。
        message: String,
    },
}

impl fmt::Display for AudioError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::ProcessExited => f.write_str("the target process has exited"),
            Self::Unsupported(what) => write!(f, "unsupported: {what}"),
            Self::FormatMismatch(reason) => write!(f, "audio format mismatch: {reason}"),
            Self::InvalidBlock(reason) => {
                write!(f, "the audio source got an invalid block: {reason}")
            }
            Self::Os {
                context,
                code,
                message,
            } => write!(f, "{context} failed (0x{code:08X}): {message}"),
        }
    }
}

impl std::error::Error for AudioError {}

#[cfg(test)]
mod tests {
    use super::AudioFormat;

    #[test]
    fn a_sample_holds_one_f32_per_channel() {
        let stereo = AudioFormat {
            sample_rate: 48_000,
            channels: 2,
        };
        assert_eq!(stereo.bytes_per_sample(), 8);
        let mono = AudioFormat {
            sample_rate: 16_000,
            channels: 1,
        };
        assert_eq!(mono.bytes_per_sample(), 4);
    }

    #[test]
    fn durations_round_down_to_whole_nanoseconds() {
        let format = AudioFormat {
            sample_rate: 48_000,
            channels: 2,
        };
        assert_eq!(format.duration_ns(0), 0);
        assert_eq!(format.duration_ns(480), 10_000_000);
        assert_eq!(format.duration_ns(1), 20_833);
        assert_eq!(format.duration_ns(u64::MAX), u64::MAX);
        let none = AudioFormat {
            sample_rate: 0,
            channels: 2,
        };
        assert_eq!(none.duration_ns(480), 0);
    }
}
