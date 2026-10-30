//! 主进程与实时核共用的单调时钟（`docs/modules/realtime-core.md` 的“时钟”）。
//!
//! [`now_ns`] 的读数与主进程的 `time.perf_counter_ns()` 同一刻度，核协议中以 `_ns` 结尾的时刻与帧环的
//! 采集时刻都用它：
//!
//! - Windows：QPC 刻度按 Python 的算法 `ticks * 1e9 / frequency` 向下取整（[`qpc_ticks_to_ns`]）；
//! - Linux：`CLOCK_MONOTONIC`，Python 3.12 的 `perf_counter_ns` 在 Linux 上读的就是它。
//!
//! 其它平台的 Python 用别的时钟（macOS 上是 `mach_absolute_time`），这里不支持，编译即报错。
//! 两边读同一时钟由 pytest 实测核对（`runtime/tests/core_link/test_clock.py`）。

#[cfg(not(any(windows, target_os = "linux")))]
compile_error!("mono-clock 只支持 Windows 与 Linux：其它平台上 Python perf_counter_ns 的时钟不同");

const NS_PER_SECOND: u128 = 1_000_000_000;

/// 按 Python `time.perf_counter_ns()` 在 Windows 上的算法把 QPC 刻度换算为纳秒：
/// `ticks * 1e9 / frequency` 向下取整。刻度为负、频率不为正或结果溢出时为 `None`。
#[must_use]
pub fn qpc_ticks_to_ns(ticks: i64, frequency: i64) -> Option<u64> {
    let ticks = u128::try_from(ticks).ok()?;
    let frequency = u128::try_from(frequency).ok().filter(|f| *f > 0)?;
    u64::try_from(ticks * NS_PER_SECOND / frequency).ok()
}

/// 现在的时刻（纳秒），与主进程 `time.perf_counter_ns()` 同一时钟。
#[cfg(windows)]
#[must_use]
pub fn now_ns() -> u64 {
    use std::sync::OnceLock;
    use windows::Win32::System::Performance::{QueryPerformanceCounter, QueryPerformanceFrequency};

    static FREQUENCY: OnceLock<i64> = OnceLock::new();
    let frequency = *FREQUENCY.get_or_init(|| {
        let mut frequency = 0;
        // SAFETY: 只写入局部变量。Windows XP 起这个调用不会失败；万一失败，频率为 0，换算结果为 0。
        let _ = unsafe { QueryPerformanceFrequency(&raw mut frequency) };
        frequency
    });
    let mut ticks = 0;
    // SAFETY: 只写入局部变量。Windows XP 起这个调用不会失败；万一失败，刻度为 0，换算结果为 0。
    let _ = unsafe { QueryPerformanceCounter(&raw mut ticks) };
    qpc_ticks_to_ns(ticks, frequency).unwrap_or(0)
}

/// 现在的时刻（纳秒），与主进程 `time.perf_counter_ns()` 同一时钟。
#[cfg(target_os = "linux")]
#[must_use]
pub fn now_ns() -> u64 {
    let mut now = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    // SAFETY: 只写入局部变量；CLOCK_MONOTONIC 在 Linux 上总是可用，失败时读数保持 0。
    let _ = unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &raw mut now) };
    let seconds = u64::try_from(now.tv_sec).unwrap_or(0);
    let nanos = u64::try_from(now.tv_nsec).unwrap_or(0);
    seconds.saturating_mul(1_000_000_000).saturating_add(nanos)
}

#[cfg(test)]
mod tests {
    use super::{now_ns, qpc_ticks_to_ns};
    use std::thread;
    use std::time::{Duration, Instant};

    #[test]
    fn qpc_ticks_floor_like_python() {
        // 10 MHz 是 Windows 10 起常见的 QPC 频率：刻度恰为 100ns，换算没有误差。
        assert_eq!(qpc_ticks_to_ns(12_345, 10_000_000), Some(1_234_500));
        // 其它频率向下取整：3 个刻度 × 1e9 / 3_579_545 = 838.1…ns。
        assert_eq!(qpc_ticks_to_ns(3, 3_579_545), Some(838));
        // 很大的刻度不溢出中间结果。
        assert_eq!(
            qpc_ticks_to_ns(i64::MAX / 2, 1_000_000_000),
            Some(4_611_686_018_427_387_903)
        );
        assert_eq!(qpc_ticks_to_ns(-1, 10_000_000), None);
        assert_eq!(qpc_ticks_to_ns(1, 0), None);
        assert_eq!(qpc_ticks_to_ns(i64::MAX, 1), None);
    }

    #[test]
    fn now_is_monotonic_and_runs_at_wall_rate() {
        let mut last = now_ns();
        assert!(last > 0, "a monotonic clock that reads 0 is broken");
        for _ in 0..10_000 {
            let now = now_ns();
            assert!(now >= last, "{now} < {last}");
            last = now;
        }
        // 与 std 的单调时钟同速：睡眠前后两边量出的间隔相差不到 5ms（只核对刻度，不核对零点）。
        let (start, start_ns) = (Instant::now(), now_ns());
        thread::sleep(Duration::from_millis(50));
        let (elapsed, elapsed_ns) = (start.elapsed(), now_ns() - start_ns);
        let elapsed = u64::try_from(elapsed.as_nanos()).unwrap();
        assert!(
            elapsed.abs_diff(elapsed_ns) < 5_000_000,
            "std measured {elapsed}ns, mono-clock {elapsed_ns}ns"
        );
        assert!(elapsed_ns >= 50_000_000);
    }

    /// Windows 上读数恰是 QPC 刻度按 Python 的算法换算的结果：落在前后两次直接换算之间。
    #[cfg(windows)]
    #[test]
    fn now_is_qpc_converted_like_python() {
        use windows::Win32::System::Performance::{
            QueryPerformanceCounter, QueryPerformanceFrequency,
        };
        let read = || {
            let mut ticks = 0;
            // SAFETY: 只写入局部变量。
            unsafe { QueryPerformanceCounter(&raw mut ticks) }.expect("QueryPerformanceCounter");
            ticks
        };
        let mut frequency = 0;
        // SAFETY: 只写入局部变量。
        unsafe { QueryPerformanceFrequency(&raw mut frequency) }
            .expect("QueryPerformanceFrequency");
        let before = qpc_ticks_to_ns(read(), frequency).unwrap();
        let now = now_ns();
        let after = qpc_ticks_to_ns(read(), frequency).unwrap();
        assert!(
            before <= now && now <= after,
            "{before} <= {now} <= {after}"
        );
    }
}
