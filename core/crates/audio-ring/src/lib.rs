//! 音频环的实时核一侧：按 `audio_ring.attach` 的参数打开主进程创建的共享内存并写块
//! （[`AudioRingWriter`]），以及只用于自动测试的假音频源（[`FakeAudioCapture`]）与它用的读取方
//! （[`AudioRingReader`]）。布局与读写规则见 `docs/modules/core-protocol.md` 的“音频环”，
//! 假音频源见 `docs/modules/realtime-core.md` 的“假音频源”。
//!
//! 共享内存由主进程（或测试一侧）创建，本 crate 只打开、核对与读写，不创建也不删除。
//!
//! **内存序。** 与帧环相同：本进程对共享内存的每一次访问都是原子操作，块的 `seq` 与 `write_seq` 用
//! Release/Acquire，块头部其余字段与样本值用 Relaxed；与 Python 一方的配合依赖 x86-64 的内存模型。

mod error;
mod fake;
mod reader;
mod ring;
mod writer;

#[cfg(test)]
mod tests;

pub use error::{AudioRingError, ReadError, WriteError};
pub use fake::FakeAudioCapture;
pub use reader::{AudioRingReader, BlockRead};
pub use ring::BlockMeta;
pub use writer::AudioRingWriter;
