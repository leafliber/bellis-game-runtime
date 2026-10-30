//! 实时核的进程入口。职责与边界见 `docs/modules/realtime-core.md`。
//!
//! 由主进程以参数列表拉起（`runtime/src/game_runtime/core_link/`）：凭据在环境变量里，时限在命令行参数里，
//! stdin 是生命线。退出码：0 表示释放全部输入后正常结束（`core.stop`、核连接断开或 stdin EOF），
//! 1 表示没能建立会话，2 表示参数不合法，101 表示 panic。

#[cfg(windows)]
mod dpi;

use std::fmt;
use std::io::{self, Write as _};
use std::process::ExitCode;

use realtime_core::config::{Launch, Options, USAGE};
use realtime_core::{install_panic_exit, session};

/// 写一行到 stderr；写失败时丢弃，不 panic。
fn say(message: fmt::Arguments<'_>) {
    let _ = writeln!(io::stderr(), "realtime-core: {message}");
}

fn main() -> ExitCode {
    install_panic_exit();
    // 坐标一律用物理像素，须在创建任何窗口或读取任何坐标之前声明（画面采集）。
    #[cfg(windows)]
    if let Err(error) = dpi::declare_per_monitor_aware_v2() {
        say(format_args!(
            "failed to declare per-monitor DPI awareness v2: {error}"
        ));
        return ExitCode::FAILURE;
    }
    let options = match Options::parse(std::env::args().skip(1)) {
        Ok(options) => options,
        Err(error) => {
            say(format_args!("{error}\n{USAGE}"));
            return ExitCode::from(2);
        }
    };
    let launch = match Launch::from_env(|name| std::env::var(name).ok()) {
        Ok(launch) => launch,
        Err(error) => {
            say(format_args!("{error}"));
            return ExitCode::FAILURE;
        }
    };
    match session::run(&options, &launch, io::stdin()) {
        Ok(_) => ExitCode::SUCCESS,
        Err(error) => {
            say(format_args!("{error}"));
            ExitCode::FAILURE
        }
    }
}
