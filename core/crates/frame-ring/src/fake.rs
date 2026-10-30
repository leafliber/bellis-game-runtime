//! 假采集源，只用于自动测试：从测试一侧创建并写入的假显示面取帧，供实时核在没有窗口与 WGC 的环境里
//! 跑除采集之外的全链路。设计见 `docs/modules/realtime-core.md` 的“假采集源”。
//!
//! 假显示面与帧环同一布局：测试一侧（Python）创建它，把合成游戏的帧逐帧写进去，写法与实时核写帧环
//! 相同；假采集源用 [`FrameRingReader`] 读最新的一帧。

use std::thread;
use std::time::{Duration, Instant};

use capture::{CaptureError, CaptureSource, Frame, FrameMeta, Point};
use core_protocol::messages::FakeTarget;

use crate::error::{FrameRingError, ReadError};
use crate::reader::{FrameRingReader, ReadOutcome};

/// 假采集源交出的帧的 DPI：没有显示器，按 100% 缩放。
pub const FAKE_DPI: u32 = 96;

/// 等新帧时两次查看 `write_seq` 的间隔。
const POLL_INTERVAL: Duration = Duration::from_millis(1);

/// 每次读最新一帧最多尝试的次数；都撕裂时等下一轮再读。
const READ_ATTEMPTS: u32 = 4;

/// 假采集源，只用于自动测试。
#[derive(Debug)]
pub struct FakeCapture {
    surface: FrameRingReader,
    last_number: u64,
    scratch: Vec<u8>,
}

impl FakeCapture {
    /// 打开 `capture.set_target` 的 `fake` 分支指明的假显示面，核对规则与 `frame_ring.attach` 相同。
    ///
    /// # Errors
    ///
    /// 见 [`FrameRingError`]。
    pub fn open(target: &FakeTarget) -> Result<Self, FrameRingError> {
        Ok(Self {
            surface: FrameRingReader::open(&target.surface)?,
            last_number: 0,
            scratch: Vec::new(),
        })
    }
}

impl CaptureSource for FakeCapture {
    /// 等假显示面上出现比上次交出的更新的帧，交出最新的一帧。
    ///
    /// 帧号是假显示面上的帧号，其间没交出的帧算作丢弃；采集时刻与变换版本取自假显示面；
    /// 客户区原点为 (0, 0)，DPI 为 [`FAKE_DPI`]。超时内没有新帧时返回 `Ok(false)`，`frame` 不变。
    fn next_frame(&mut self, timeout: Duration, frame: &mut Frame) -> Result<bool, CaptureError> {
        let deadline = Instant::now() + timeout;
        loop {
            if self.surface.latest_frame_number() > self.last_number {
                match self.surface.read_latest(&mut self.scratch, READ_ATTEMPTS) {
                    Ok(ReadOutcome::Frame(meta)) if meta.frame_number > self.last_number => {
                        frame.meta = FrameMeta {
                            number: meta.frame_number,
                            captured_at_ns: meta.capture_ns,
                            transform_version: meta.transform_version,
                            width: meta.width,
                            height: meta.height,
                            stride: meta.row_stride,
                            client_origin: Point { x: 0, y: 0 },
                            dpi: FAKE_DPI,
                        };
                        frame.pixels.clear();
                        frame.pixels.extend_from_slice(&self.scratch);
                        self.last_number = meta.frame_number;
                        return Ok(true);
                    }
                    Ok(_) => {}
                    Err(ReadError::Corrupt(reason)) => {
                        return Err(CaptureError::InvalidFrame(reason));
                    }
                }
            }
            let now = Instant::now();
            if now >= deadline {
                return Ok(false);
            }
            thread::sleep(POLL_INTERVAL.min(deadline - now));
        }
    }
}
