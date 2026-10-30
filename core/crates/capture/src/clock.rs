//! 采集时刻的时钟换算（`docs/modules/realtime-core.md` 的“时钟”）。
//!
//! 采集时刻与主进程的 `time.perf_counter_ns()` 用同一个 QPC 时钟：Python 在 Windows 上把 QPC 刻度按
//! `ticks * 1e9 / frequency` 向下取整换算为纳秒；WGC 帧的 `SystemRelativeTime` 是同一个 QPC 换算成的
//! 100ns 单位。

/// 一个 100ns 单位的纳秒数。
const NS_PER_HUNDRED_NS: u64 = 100;
const NS_PER_SECOND: u128 = 1_000_000_000;

/// 把 WGC 帧的 `SystemRelativeTime`（100ns 单位）换算为纳秒；负值或溢出时为 `None`。
#[must_use]
pub fn hundred_ns_to_ns(hundred_ns: i64) -> Option<u64> {
    u64::try_from(hundred_ns)
        .ok()?
        .checked_mul(NS_PER_HUNDRED_NS)
}

/// 按 Python `time.perf_counter_ns()` 在 Windows 上的算法把 QPC 刻度换算为纳秒：
/// `ticks * 1e9 / frequency` 向下取整。刻度为负、频率不为正或结果溢出时为 `None`。
#[must_use]
pub fn qpc_ticks_to_ns(ticks: i64, frequency: i64) -> Option<u64> {
    let ticks = u128::try_from(ticks).ok()?;
    let frequency = u128::try_from(frequency).ok().filter(|f| *f > 0)?;
    u64::try_from(ticks * NS_PER_SECOND / frequency).ok()
}

#[cfg(test)]
mod tests {
    use super::{hundred_ns_to_ns, qpc_ticks_to_ns};

    #[test]
    fn hundred_ns_units_become_nanoseconds() {
        assert_eq!(hundred_ns_to_ns(0), Some(0));
        assert_eq!(hundred_ns_to_ns(12_345), Some(1_234_500));
        assert_eq!(hundred_ns_to_ns(-1), None);
        assert_eq!(hundred_ns_to_ns(i64::MAX), None);
    }

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
    fn both_conversions_agree_at_ten_megahertz() {
        // 频率为 10 MHz 时，SystemRelativeTime 与 QPC 刻度是同一个数，两种换算结果相同。
        for ticks in [0, 1, 99, 10_000_000, 1_234_567_890_123] {
            assert_eq!(hundred_ns_to_ns(ticks), qpc_ticks_to_ns(ticks, 10_000_000));
        }
    }
}
