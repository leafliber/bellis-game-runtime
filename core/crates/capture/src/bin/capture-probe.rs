//! GS-01 的真机探测程序：按识别条件采一个窗口若干秒，报告帧率、帧龄、丢帧、变换版本的变化与黄色采集边框
//! 的处理结果。用法见 AGENTS.md 的常用命令，设计见 `docs/modules/realtime-core.md` 的“画面采集”。

use std::process::ExitCode;

#[cfg(windows)]
fn main() -> ExitCode {
    probe::main()
}

#[cfg(not(windows))]
fn main() -> ExitCode {
    eprintln!("capture-probe: Windows Graphics Capture is only available on Windows");
    ExitCode::FAILURE
}

#[cfg(windows)]
mod probe {
    use capture::win::{
        BorderRequest, PatternStyle, PatternWindow, WgcCapture, WgcOptions, dpi, list_windows,
        qpc_now_ns,
    };
    use capture::{CaptureError, CaptureSource, Frame, FrameMeta, Point, Size, WindowSelector};
    use std::process::ExitCode;
    use std::time::{Duration, Instant};

    const USAGE: &str = "\
usage:
  capture-probe --list
  capture-probe [--process NAME] [--class NAME] [--title TEXT] [OPTIONS]
  capture-probe --pattern-window [--animate] [--borderless] [--dpi-unaware] [OPTIONS]

--list                    list visible top-level windows with their process, class and title
--process NAME            process image file name, e.g. notepad.exe (case-insensitive)
--class NAME              exact window class name
--title TEXT              exact window title
--pattern-window          open a known-pattern test window and capture it
--animate                 redraw the pattern window on every DWM composition and measure paint latency
                          (not measured with --dpi-unaware)
--borderless              make the pattern window borderless
--dpi-unaware             make the pattern window DPI-unaware (stretched by the system)

OPTIONS:
--seconds N               how long to capture (default 10)
--keep-border             do not request removal of the yellow capture border (for comparison)
--min-update-interval-ms N  set the session's minimum interval between frames";

    struct Args {
        list: bool,
        selector: WindowSelector,
        seconds: u64,
        border: BorderRequest,
        min_update_interval: Option<Duration>,
        pattern: Option<PatternArgs>,
    }

    struct PatternArgs {
        animate: bool,
        style: PatternStyle,
    }

    fn parse(mut args: impl Iterator<Item = String>) -> Result<Args, String> {
        let mut parsed = Args {
            list: false,
            selector: WindowSelector::default(),
            seconds: 10,
            border: BorderRequest::Remove,
            min_update_interval: None,
            pattern: None,
        };
        let mut animate = false;
        let mut style = PatternStyle::default();
        while let Some(arg) = args.next() {
            let mut value = || args.next().ok_or(format!("{arg} needs a value"));
            match arg.as_str() {
                "--list" => parsed.list = true,
                "--process" => parsed.selector.process_name = Some(value()?),
                "--class" => parsed.selector.class_name = Some(value()?),
                "--title" => parsed.selector.title = Some(value()?),
                "--seconds" => {
                    parsed.seconds = value()?
                        .parse()
                        .map_err(|_| "--seconds needs a whole number".to_owned())?;
                }
                "--keep-border" => parsed.border = BorderRequest::Keep,
                "--min-update-interval-ms" => {
                    let millis: u64 = value()?
                        .parse()
                        .map_err(|_| "--min-update-interval-ms needs a whole number".to_owned())?;
                    parsed.min_update_interval = Some(Duration::from_millis(millis));
                }
                "--pattern-window" => {
                    parsed.pattern = Some(PatternArgs {
                        animate: false,
                        style: PatternStyle::default(),
                    });
                }
                "--animate" => animate = true,
                "--borderless" => style.borderless = true,
                "--dpi-unaware" => style.dpi_unaware = true,
                "--help" | "-h" => return Err(String::new()),
                other => return Err(format!("unknown argument {other:?}")),
            }
        }
        if let Some(pattern) = &mut parsed.pattern {
            pattern.animate = animate;
            pattern.style = style;
            if !parsed.selector.is_empty() {
                return Err("--pattern-window chooses its own window".into());
            }
        } else if animate || style != PatternStyle::default() {
            return Err("--animate, --borderless and --dpi-unaware need --pattern-window".into());
        } else if !parsed.list && parsed.selector.is_empty() {
            return Err("give --process, --class or --title (or --pattern-window)".into());
        }
        Ok(parsed)
    }

    pub(super) fn main() -> ExitCode {
        let args = match parse(std::env::args().skip(1)) {
            Ok(args) => args,
            Err(message) => {
                if !message.is_empty() {
                    eprintln!("capture-probe: {message}");
                }
                eprintln!("{USAGE}");
                return ExitCode::from(2);
            }
        };
        if let Err(error) = dpi::declare_per_monitor_aware_v2() {
            eprintln!("capture-probe: failed to declare per-monitor DPI awareness v2: {error}");
            return ExitCode::FAILURE;
        }
        match run(&args) {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => {
                eprintln!("capture-probe: {error}");
                ExitCode::FAILURE
            }
        }
    }

    fn run(args: &Args) -> Result<(), CaptureError> {
        if args.list {
            for window in list_windows()? {
                let process = window.info.process_image.as_deref().unwrap_or("?");
                println!(
                    "hwnd={:#x} pid={} process={process:?} class={:?} title={:?}",
                    window.hwnd.0 as usize,
                    window.process_id,
                    window.info.class_name,
                    window.info.title
                );
            }
            return Ok(());
        }

        let mut selector = args.selector.clone();
        let mut _pattern_window = None;
        if let Some(pattern) = &args.pattern {
            let title = format!("capture-probe pattern {}", std::process::id());
            let window = PatternWindow::open(
                &title,
                pattern.style,
                Point { x: 120, y: 120 },
                Size {
                    width: 800,
                    height: 600,
                },
            )?;
            window.set_animate(pattern.animate);
            selector = WindowSelector {
                process_name: std::env::current_exe().ok().and_then(|path| {
                    path.file_name()
                        .map(|name| name.to_string_lossy().into_owned())
                }),
                class_name: None,
                title: Some(title),
            };
            _pattern_window = Some(window);
            // 给窗口一点时间完成第一次绘制。
            std::thread::sleep(Duration::from_millis(200));
        }

        let mut capture = WgcCapture::new(&WgcOptions {
            selector,
            border: args.border,
            min_update_interval: args.min_update_interval,
        })?;
        let window = capture.window().clone();
        println!(
            "window: hwnd={:#x} pid={} process={:?} class={:?} title={:?}",
            window.hwnd.0 as usize,
            window.process_id,
            window.info.process_image.as_deref().unwrap_or("?"),
            window.info.class_name,
            window.info.title
        );
        let border = capture.border().clone();
        println!(
            "border: request={:?} access={} set_error={} border_required={}",
            border.request,
            border.access.as_deref().unwrap_or("-"),
            border.set_error.as_deref().unwrap_or("-"),
            border
                .border_required
                .map_or_else(|| "?".to_owned(), |required| required.to_string())
        );
        println!(
            "min update interval: {}",
            capture
                .min_update_interval()
                .map_or_else(|| "?".to_owned(), |interval| format!("{interval:?}"))
        );

        // 不感知 DPI 的窗口被系统拉伸，时间戳方块不在物理像素的原位，读不出来。
        let measure_paint = args
            .pattern
            .as_ref()
            .is_some_and(|pattern| pattern.animate && !pattern.style.dpi_unaware);
        let report = capture_for(
            &mut capture,
            Duration::from_secs(args.seconds),
            measure_paint,
        );
        report.print(&capture);
        Ok(())
    }

    #[derive(Default)]
    struct Report {
        elapsed: Duration,
        /// 交出时刻减采集时刻，可能为负：采集时刻是 DWM 合成的目标时刻，可能晚于帧交出的时刻。
        ages_ns: Vec<i64>,
        intervals_ns: Vec<i64>,
        paint_to_capture_ns: Vec<i64>,
        paint_to_delivery_ns: Vec<i64>,
        skipped_numbers: u64,
        versions: Vec<(Duration, FrameMeta)>,
        timeouts: u64,
        errors: Vec<(Duration, String)>,
    }

    fn signed_diff(later: u64, earlier: u64) -> i64 {
        i64::try_from(i128::from(later) - i128::from(earlier)).unwrap_or(i64::MAX)
    }

    fn capture_for(capture: &mut WgcCapture, duration: Duration, measure_paint: bool) -> Report {
        let mut report = Report::default();
        let mut frame = Frame::default();
        let mut last: Option<FrameMeta> = None;
        let mut second_ages: Vec<i64> = Vec::new();
        let mut next_tick = Duration::from_secs(1);
        let mut last_error: Option<String> = None;
        let start = Instant::now();
        while start.elapsed() < duration {
            match capture.next_frame(Duration::from_secs(1), &mut frame) {
                Ok(true) => {
                    let now = qpc_now_ns();
                    let elapsed = start.elapsed();
                    let meta = frame.meta;
                    let age = signed_diff(now, meta.captured_at_ns);
                    report.ages_ns.push(age);
                    second_ages.push(age);
                    if let Some(previous) = last {
                        report
                            .intervals_ns
                            .push(signed_diff(meta.captured_at_ns, previous.captured_at_ns));
                        report.skipped_numbers += meta.number.saturating_sub(previous.number + 1);
                    }
                    if last
                        .is_none_or(|previous| previous.transform_version != meta.transform_version)
                    {
                        report.versions.push((elapsed, meta));
                    }
                    if measure_paint && let Some(painted) = PatternWindow::paint_time_ns(&frame) {
                        report
                            .paint_to_capture_ns
                            .push(signed_diff(meta.captured_at_ns, painted));
                        report.paint_to_delivery_ns.push(signed_diff(now, painted));
                    }
                    last = Some(meta);
                    last_error = None;
                }
                Ok(false) => report.timeouts += 1,
                Err(error) => {
                    let message = error.to_string();
                    if last_error.as_deref() != Some(&message) {
                        println!("{:>6.2}s error: {message}", start.elapsed().as_secs_f64());
                        report.errors.push((start.elapsed(), message.clone()));
                        last_error = Some(message);
                    }
                    if !error.is_recoverable() {
                        break;
                    }
                    std::thread::sleep(Duration::from_millis(100));
                }
            }
            if start.elapsed() >= next_tick {
                println!(
                    "{:>6.2}s frames={:<4} age_ms min={} p50={} max={} version={} size={} origin=({}, {}) dpi={}",
                    next_tick.as_secs_f64(),
                    second_ages.len(),
                    ms(second_ages.iter().copied().min()),
                    ms(percentile(&second_ages, 0.5)),
                    ms(second_ages.iter().copied().max()),
                    last.map_or(0, |meta| meta.transform_version),
                    last.map_or_else(String::new, |meta| format!(
                        "{}x{}",
                        meta.width, meta.height
                    )),
                    last.map_or(0, |meta| meta.client_origin.x),
                    last.map_or(0, |meta| meta.client_origin.y),
                    last.map_or(0, |meta| meta.dpi),
                );
                second_ages.clear();
                next_tick += Duration::from_secs(1);
            }
        }
        report.elapsed = start.elapsed();
        report
    }

    impl Report {
        fn print(&self, capture: &WgcCapture) {
            let seconds = self.elapsed.as_secs_f64();
            let frames = self.ages_ns.len();
            println!("--- summary ---");
            #[allow(clippy::cast_precision_loss)]
            let fps = frames as f64 / seconds;
            println!("duration {seconds:.2} s, frames delivered {frames} ({fps:.1} fps)");
            print_distribution(
                "frame age at delivery = delivered - captured_at (ms)",
                &self.ages_ns,
            );
            let negative = self.ages_ns.iter().filter(|age| **age < 0).count();
            println!("  frames delivered before their captured_at: {negative}");
            print_distribution("capture interval (ms)", &self.intervals_ns);
            let median = percentile(&self.intervals_ns, 0.5);
            let long_gaps = median.map_or(0, |median| {
                self.intervals_ns
                    .iter()
                    .filter(|interval| **interval * 2 > median * 3)
                    .count()
            });
            println!("  intervals over 1.5x median: {long_gaps}");
            if !self.paint_to_capture_ns.is_empty() {
                print_distribution("paint to captured_at (ms)", &self.paint_to_capture_ns);
                print_distribution("paint to delivery (ms)", &self.paint_to_delivery_ns);
            }
            let stats = capture.stats();
            println!(
                "frame numbers skipped between delivered frames {}; source: taken {} delivered {} superseded {} stale {} uncroppable {} pool recreations {}",
                self.skipped_numbers,
                stats.taken,
                stats.delivered,
                stats.superseded,
                stats.stale,
                stats.uncroppable,
                stats.pool_recreations
            );
            println!("timeouts (no frame within 1 s) {}", self.timeouts);
            for (at, meta) in &self.versions {
                println!(
                    "transform version {} from {:.2} s: client origin ({}, {}) size {}x{} dpi {}",
                    meta.transform_version,
                    at.as_secs_f64(),
                    meta.client_origin.x,
                    meta.client_origin.y,
                    meta.width,
                    meta.height,
                    meta.dpi
                );
            }
            for (at, error) in &self.errors {
                println!("error at {:.2} s: {error}", at.as_secs_f64());
            }
            println!(
                "note: whether the yellow border is gone must be checked on screen; the API values above are not proof"
            );
        }
    }

    fn print_distribution(name: &str, values: &[i64]) {
        println!(
            "{name}: min {} p50 {} p95 {} p99 {} max {} over {}",
            ms(values.iter().copied().min()),
            ms(percentile(values, 0.5)),
            ms(percentile(values, 0.95)),
            ms(percentile(values, 0.99)),
            ms(values.iter().copied().max()),
            values.len()
        );
    }

    /// 最近秩百分位数。
    fn percentile(values: &[i64], quantile: f64) -> Option<i64> {
        if values.is_empty() {
            return None;
        }
        let mut sorted = values.to_vec();
        sorted.sort_unstable();
        #[allow(
            clippy::cast_possible_truncation,
            clippy::cast_sign_loss,
            clippy::cast_precision_loss
        )]
        let rank = (quantile * sorted.len() as f64).ceil() as usize;
        Some(sorted[rank.clamp(1, sorted.len()) - 1])
    }

    fn ms(ns: Option<i64>) -> String {
        #[allow(clippy::cast_precision_loss)]
        ns.map_or_else(|| "-".to_owned(), |ns| format!("{:.2}", ns as f64 / 1e6))
    }
}
