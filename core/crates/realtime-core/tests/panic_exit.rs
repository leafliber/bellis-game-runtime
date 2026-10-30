//! panic 即退出：工作线程的 panic 也让整个进程以 101 退出，不被吞掉。
//!
//! 测试以环境变量为开关重新运行自己的测试程序：子进程装上钩子后让一个工作线程 panic，
//! 父进程核对子进程的退出码与 stderr。

use std::env;
use std::process::Command;
use std::thread;
use std::time::Duration;

use realtime_core::{PANIC_EXIT_CODE, install_panic_exit};

const CHILD: &str = "REALTIME_CORE_PANIC_EXIT_CHILD";

#[test]
fn worker_thread_panic_exits_the_process() {
    if env::var_os(CHILD).is_some() {
        install_panic_exit();
        let worker = thread::spawn(|| panic!("worker panicked on purpose"));
        let _ = worker.join();
        // 走到这里说明 panic 被吞掉了：子进程以 0 退出，父进程的断言失败。
        thread::sleep(Duration::from_millis(100));
        return;
    }
    let output = Command::new(env::current_exe().expect("测试程序的路径"))
        .args([
            "--exact",
            "worker_thread_panic_exits_the_process",
            "--nocapture",
            "--test-threads=1",
        ])
        .env(CHILD, "1")
        .output()
        .expect("运行子进程");
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert_eq!(output.status.code(), Some(PANIC_EXIT_CODE), "{stderr}");
    assert!(stderr.contains("worker panicked on purpose"), "{stderr}");
    assert!(stderr.contains("panic; exiting with code 101"), "{stderr}");
}
