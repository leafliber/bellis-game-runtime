//! 实时核的进程入口。职责与边界见 `docs/modules/realtime-core.md`。

#[cfg(windows)]
mod dpi;

use std::process::ExitCode;

fn main() -> ExitCode {
    // 坐标一律用物理像素，须在创建任何窗口或读取任何坐标之前声明（画面采集）。
    #[cfg(windows)]
    if let Err(error) = dpi::declare_per_monitor_aware_v2() {
        eprintln!("realtime-core: failed to declare per-monitor DPI awareness v2: {error}");
        return ExitCode::FAILURE;
    }
    ExitCode::SUCCESS
}
