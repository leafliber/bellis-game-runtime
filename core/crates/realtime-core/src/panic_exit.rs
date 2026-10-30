//! panic 即退出（`docs/modules/realtime-core.md` 的“启动与生命线”）。
//!
//! 标准库在非主线程 panic 时只结束那个线程，读线程或看门狗线程的 panic 因而会被吞掉，实时核带着残缺的
//! 状态继续运行。这里装一个 panic 钩子：先照常打印 panic 信息，再以 [`PANIC_EXIT_CODE`] 退出整个进程。
//! 不在钩子里释放输入：panic 时的状态不可信，重启后先释放全部输入（`core_start`）。

use std::panic;
use std::process;

/// panic 退出时的退出码，与 Rust 主线程 panic 的缺省退出码相同。
pub const PANIC_EXIT_CODE: i32 = 101;

/// 装上 panic 钩子：任何线程 panic 都在打印信息后退出整个进程。进程启动时调用一次。
pub fn install_panic_exit() {
    let default = panic::take_hook();
    panic::set_hook(Box::new(move |info| {
        default(info);
        log!("panic; exiting with code {PANIC_EXIT_CODE}");
        process::exit(PANIC_EXIT_CODE);
    }));
}
