//! 实时核：游戏侧的安全核。职责与边界见 `docs/modules/realtime-core.md`。
//!
//! 这里是启动与生命线（同一文档的“启动与生命线”）：按主进程交来的凭据连上核连接、握手，维护控制产出者的
//! 身份、代次与心跳，任何失效都先释放全部输入；核连接断开、stdin 读到 EOF 或收到 `core.stop` 时释放后退出。
//! 以及采集主循环（同一文档的“采集主循环”）：挂接帧环、设定采集目标，在采集线程上取帧写进帧环。
//! 以及动作块的执行（同一文档的“输入代理”与“动作块的执行”）：校验与调度动作块，每次把输入交给输入后端
//! 之前核对权威规则的三个条件；输入后端只有只用于测试的假后端。
//! 标准线程加阻塞 IO，不用异步运行时。

/// 写一行日志到 stderr。写失败（例如读端已关闭）时丢弃，不 panic：日志不能打断释放与退出。
macro_rules! log {
    ($($arg:tt)*) => {{
        use std::io::Write as _;
        let _ = writeln!(std::io::stderr(), "realtime-core: {}", format_args!($($arg)*));
    }};
}

pub mod config;
mod control;
mod fake;
mod frames;
mod lease;
mod ledger;
mod panic_exit;
mod queue;
pub mod session;

pub use panic_exit::{PANIC_EXIT_CODE, install_panic_exit};
