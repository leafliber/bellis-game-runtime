//! 帧环的实时核一侧：按 `frame_ring.attach` 的参数打开主进程创建的共享内存并写帧
//! （[`FrameRingWriter`]），以及只用于自动测试的假采集源（[`FakeCapture`]）与它用的读取方
//! （[`FrameRingReader`]）。布局与读写规则见 `docs/modules/core-protocol.md` 的“帧环与音频环”，
//! 假采集源见 `docs/modules/realtime-core.md` 的“假采集源”。
//!
//! 共享内存由主进程（或测试一侧）创建，本 crate 只打开、核对与读写，不创建也不删除。
//!
//! **内存序。** 本进程对共享内存的每一次访问都是原子操作：槽的 `seq` 与 `write_seq` 用
//! Release/Acquire，槽头部其余字段与像素用 Relaxed，按字节对齐情况拆成 8 字节与单字节的原子访问。
//! 所以读写双方都在 Rust 里时（测试中的同进程并发）也没有数据竞争；与 Python 一方的配合依赖
//! x86-64 的内存模型，见文档。

mod error;
mod fake;
mod reader;
mod ring;
mod writer;

#[cfg(test)]
mod tests;

pub use error::{FrameRingError, ReadError, WriteError};
pub use fake::{FAKE_DPI, FakeCapture};
pub use reader::{FrameRingReader, ReadOutcome};
pub use ring::SlotMeta;
pub use writer::FrameRingWriter;
