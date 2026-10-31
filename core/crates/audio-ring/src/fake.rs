//! 假音频源，只用于自动测试：从测试一侧创建并写入的假音频输出取声音，供实时核在没有 WASAPI 的环境里
//! 跑音频环的全链路。设计见 `docs/modules/realtime-core.md` 的“假音频源”。
//!
//! 假音频输出与音频环同一布局：测试一侧（Python）创建它，逐块写入声音，写法与实时核写音频环相同；
//! 假音频源用 [`AudioRingReader`] 按块号从小到大逐块读。

use std::thread;
use std::time::{Duration, Instant};

use audio_capture::{AudioError, AudioFormat, AudioPacket, AudioSource};
use core_protocol::audio_ring::FLAG_DISCONTINUITY;
use core_protocol::messages::FakeAudioTarget;

use crate::error::{AudioRingError, ReadError};
use crate::reader::{AudioRingReader, BlockRead};

/// 等新块时两次查看 `write_seq` 的间隔。
const POLL_INTERVAL: Duration = Duration::from_millis(1);

/// 假音频源，只用于自动测试。
#[derive(Debug)]
pub struct FakeAudioCapture {
    output: AudioRingReader,
    /// 下一个要交出的块号。
    next: u64,
    /// 跳过了被覆盖的块，下一个交出的包标不连续。
    skipped: bool,
    /// 读块用的缓冲；读到完整的一块才与包的缓冲交换。
    scratch: Vec<u8>,
}

impl FakeAudioCapture {
    /// 打开 `audio.set_target` 的 `fake` 分支指明的假音频输出，核对规则与 `audio_ring.attach` 相同。
    /// 与真实的音频源一样只交出打开之后写入的声音。
    ///
    /// # Errors
    ///
    /// 见 [`AudioRingError`]。
    pub fn open(target: &FakeAudioTarget) -> Result<Self, AudioRingError> {
        let output = AudioRingReader::open(&target.output)?;
        let next = output.latest_block_number() + 1;
        Ok(Self {
            output,
            next,
            skipped: false,
            scratch: Vec::new(),
        })
    }

    /// 假音频输出的格式。
    #[must_use]
    pub fn format(&self) -> AudioFormat {
        self.output.format()
    }

    /// 按块号交出下一块；还没有时返回 `Ok(false)`。块撕裂时也返回 `Ok(false)`，下一轮再读。
    fn take(&mut self, packet: &mut AudioPacket) -> Result<bool, AudioError> {
        loop {
            let latest = self.output.latest_block_number();
            if latest < self.next {
                return Ok(false);
            }
            let oldest = latest
                .saturating_sub(u64::from(self.output.block_count()) - 1)
                .max(1);
            if self.next < oldest {
                self.next = oldest;
                self.skipped = true;
            }
            match self.output.read_block(self.next, &mut self.scratch) {
                Ok(BlockRead::Block(meta)) => {
                    std::mem::swap(&mut packet.data, &mut self.scratch);
                    packet.capture_ns = meta.capture_ns;
                    packet.sample_count = meta.sample_count;
                    packet.discontinuity = meta.flags & FLAG_DISCONTINUITY != 0 || self.skipped;
                    self.skipped = false;
                    self.next += 1;
                    return Ok(true);
                }
                // 读的期间被覆盖了：跳过它，重新看最新的块号。
                Ok(BlockRead::Missing) => {
                    self.next += 1;
                    self.skipped = true;
                }
                Ok(BlockRead::Torn) => return Ok(false),
                Err(ReadError::Corrupt(reason)) => return Err(AudioError::InvalidBlock(reason)),
            }
        }
    }
}

impl AudioSource for FakeAudioCapture {
    /// 等假音频输出上出现下一块，把它作为一个包交出：采集时刻、采样点数与样本值取自那一块，块的不连续
    /// 标志照样带上；跳过了被覆盖的块时也标不连续。超时内没有新块时返回 `Ok(false)`，`packet` 不变。
    fn next_packet(
        &mut self,
        timeout: Duration,
        packet: &mut AudioPacket,
    ) -> Result<bool, AudioError> {
        let deadline = Instant::now() + timeout;
        loop {
            if self.take(packet)? {
                return Ok(true);
            }
            let now = Instant::now();
            if now >= deadline {
                return Ok(false);
            }
            thread::sleep(POLL_INTERVAL.min(deadline - now));
        }
    }
}
