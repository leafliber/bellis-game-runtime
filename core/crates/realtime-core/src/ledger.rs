//! 持有输入的账目与释放全部输入（`docs/modules/realtime-core.md` 的“持键账目与释放”）。
//!
//! 释放是唯一的安全路径：任何失效都调用 [`InputLedger::release_all`]，它只动本地状态与输入后端，
//! 不依赖核连接，也不等任何写入，调用方在释放完成之后才通知主进程。
//!
//! 这一步还没有输入后端（unsupported）：没有任何路径向账目加入输入，账目总是空的，释放只是清空。
//! 第 8 步在这里接假输入后端与动作块，G2 接 `SendInput` 与 `GetAsyncKeyState` 核验。

use core_protocol::messages::HeldInput;

/// 本实例按下且尚未抬起的输入。
#[derive(Debug, Default)]
pub(crate) struct InputLedger {
    held: Vec<HeldInput>,
}

impl InputLedger {
    /// 是否持有任何输入。
    pub(crate) fn is_empty(&self) -> bool {
        self.held.is_empty()
    }

    /// 释放全部输入：逐项抬起后清空账目。没有输入后端时只清空。
    pub(crate) fn release_all(&mut self) {
        self.held.clear();
    }
}
