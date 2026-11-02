//! 实时核的进程入口。职责与边界见 `docs/modules/realtime-core.md`。
//!
//! 由主进程以参数列表拉起（`runtime/src/game_runtime/core_link/`）：凭据在环境变量里，时限与输入后端在
//! 命令行参数里，stdin 是生命线。退出码：0 表示释放全部输入后正常结束（`core.stop`、核连接断开或 stdin
//! EOF），1 表示没能建立会话（连不上主进程、操作员控制台或测试设备），2 表示参数不合法（含输入后端与许可源
//! 的门禁），101 表示 panic。

use std::fmt;
use std::io::{self, Write as _};
use std::process::ExitCode;

use realtime_core::config::{InputSetup, Launch, Options, USAGE};
use realtime_core::{install_panic_exit, session};

/// 写一行到 stderr；写失败时丢弃，不 panic。
fn say(message: fmt::Arguments<'_>) {
    let _ = writeln!(io::stderr(), "realtime-core: {message}");
}

fn main() -> ExitCode {
    install_panic_exit();
    // 坐标一律用物理像素，须在创建任何窗口或读取任何坐标之前声明（画面采集）。采集线程继承进程的声明。
    #[cfg(windows)]
    if let Err(error) = capture::win::dpi::declare_per_monitor_aware_v2() {
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
    let mut launch = match Launch::from_env(|name| std::env::var(name).ok()) {
        Ok(launch) => launch,
        Err(error) => {
            say(format_args!("{error}"));
            return ExitCode::FAILURE;
        }
    };
    // 门禁：许可源只有一个；测试许可源只能与假输入后端一起、且只在没有操作员许可连接时启用；操作员许可
    // 连接离不开输入后端。
    let input = match InputSetup::select(
        options.input_backend,
        launch.test_device.take(),
        launch.operator.take(),
        options.emergency_hotkey.clone(),
    ) {
        Ok(input) => input,
        Err(error) => {
            say(format_args!("{error}\n{USAGE}"));
            return ExitCode::from(2);
        }
    };
    match session::run(&options, &launch, input, io::stdin()) {
        Ok(_) => ExitCode::SUCCESS,
        Err(error) => {
            say(format_args!("{error}"));
            ExitCode::FAILURE
        }
    }
}
