//! 采集时刻的时钟换算（`docs/modules/realtime-core.md` 的“时钟”）。
//!
//! 采集时刻与主进程的 `time.perf_counter_ns()` 用同一个 QPC 时钟：WGC 帧的 `SystemRelativeTime` 是 QPC
//! 换算成的 100ns 单位；现在的时刻由 [`mono_clock::now_ns`] 读取。

/// 一个 100ns 单位的纳秒数。
const NS_PER_HUNDRED_NS: u64 = 100;

/// 把 WGC 帧的 `SystemRelativeTime`（100ns 单位）换算为纳秒；负值或溢出时为 `None`。
#[must_use]
pub fn hundred_ns_to_ns(hundred_ns: i64) -> Option<u64> {
    u64::try_from(hundred_ns)
        .ok()?
        .checked_mul(NS_PER_HUNDRED_NS)
}

#[cfg(test)]
mod tests {
    use super::hundred_ns_to_ns;
    use mono_clock::qpc_ticks_to_ns;

    #[test]
    fn hundred_ns_units_become_nanoseconds() {
        assert_eq!(hundred_ns_to_ns(0), Some(0));
        assert_eq!(hundred_ns_to_ns(12_345), Some(1_234_500));
        assert_eq!(hundred_ns_to_ns(-1), None);
        assert_eq!(hundred_ns_to_ns(i64::MAX), None);
    }

    #[test]
    fn both_conversions_agree_at_ten_megahertz() {
        // 频率为 10 MHz 时，SystemRelativeTime 与 QPC 刻度是同一个数，两种换算结果相同。
        for ticks in [0, 1, 99, 10_000_000, 1_234_567_890_123] {
            assert_eq!(hundred_ns_to_ns(ticks), qpc_ticks_to_ns(ticks, 10_000_000));
        }
    }
}
