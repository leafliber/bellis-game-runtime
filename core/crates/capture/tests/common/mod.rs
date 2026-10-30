//! 真机测试共用的已知图案窗口与采集辅助。

#![allow(dead_code, reason = "每个测试文件只用到其中一部分")]

use capture::win::{
    PatternCheck, PatternStyle, PatternWindow, WgcCapture, WgcOptions, dpi, qpc_now_ns,
    query_geometry,
};
use capture::{CaptureSource, Frame, Point, Size, WindowSelector};
use std::time::{Duration, Instant};

pub const SIZE: Size = Size {
    width: 640,
    height: 480,
};
pub const WAIT: Duration = Duration::from_secs(5);
pub const FRAMED: PatternStyle = PatternStyle {
    borderless: false,
    dpi_unaware: false,
};
pub const BORDERLESS: PatternStyle = PatternStyle {
    borderless: true,
    dpi_unaware: false,
};

/// 一个已知图案窗口与采集它所需的识别条件。
pub struct Target {
    pub window: PatternWindow,
    pub title: String,
    pub style: PatternStyle,
}

impl Target {
    pub fn open(name: &str, style: PatternStyle, origin: Point, size: Size) -> Self {
        dpi::declare_per_monitor_aware_v2().expect("declare per-monitor DPI awareness v2");
        let title = format!("capture-test {name} {}", std::process::id());
        let window = PatternWindow::open(&title, style, origin, size).expect("open pattern window");
        Self {
            window,
            title,
            style,
        }
    }

    pub fn selector(&self) -> WindowSelector {
        selector(&self.title)
    }

    pub fn capture(&self) -> WgcCapture {
        WgcCapture::new(&WgcOptions {
            selector: self.selector(),
            ..WgcOptions::default()
        })
        .expect("start capture")
    }

    /// Windows 11 有边框窗口的圆角半径是 8 个逻辑像素；按 DPI 换算后再留 2 个像素的余量。
    fn corner(&self, dpi: u32) -> u32 {
        if self.style.borderless {
            0
        } else {
            8 * dpi / 96 + 2
        }
    }

    /// 等到一帧尺寸为 `size`、内容是颜色轮换为 `variant` 的图案（有边框窗口的底部两角除外）。窗口静止时
    /// WGC 不出新帧，所以等不到时请窗口重画一次。
    pub fn wait_for_pattern(
        &self,
        capture: &mut WgcCapture,
        size: Size,
        variant: u32,
    ) -> (Frame, PatternCheck) {
        let deadline = Instant::now() + WAIT;
        let mut frame = Frame::default();
        let mut last_check: Option<PatternCheck> = None;
        while Instant::now() < deadline {
            match capture.next_frame(Duration::from_millis(250), &mut frame) {
                Ok(true) => {
                    if frame.meta.width == size.width && frame.meta.height == size.height {
                        let check =
                            PatternWindow::verify(&frame, variant, self.corner(frame.meta.dpi));
                        if check.is_exact() {
                            return (frame, check);
                        }
                        last_check = Some(check);
                    }
                }
                Ok(false) => self.window.repaint(),
                Err(error) => panic!("capture failed: {error}"),
            }
        }
        panic!(
            "no exact {size} frame of variant {variant} within {WAIT:?}; last meta {:?}, last check {last_check:?}, stats {:?}",
            frame.meta,
            capture.stats()
        );
    }

    /// 帧的元数据与窗口现在的几何一致，且采集时刻在现在前后一秒之内。
    pub fn assert_meta_matches(&self, frame: &Frame) {
        let geometry = query_geometry(self.window.hwnd()).expect("query geometry");
        let meta = frame.meta;
        assert_eq!(
            meta.client_origin,
            geometry.client.origin(),
            "client origin"
        );
        assert_eq!(
            Size {
                width: meta.width,
                height: meta.height
            },
            geometry.client.size(),
            "client size"
        );
        assert_eq!(meta.dpi, geometry.dpi, "dpi");
        assert_eq!(meta.stride, meta.width * 4, "stride");
        assert_eq!(
            frame.pixels.len(),
            (meta.stride * meta.height) as usize,
            "pixel buffer"
        );
        let now = qpc_now_ns();
        assert!(
            meta.captured_at_ns.abs_diff(now) < 1_000_000_000,
            "captured_at {} is not within 1 s of now {now}",
            meta.captured_at_ns
        );
    }
}

/// 本测试进程中标题为 `title` 的窗口。
pub fn selector(title: &str) -> WindowSelector {
    let exe = std::env::current_exe().expect("current exe");
    WindowSelector {
        process_name: Some(exe.file_name().unwrap().to_string_lossy().into_owned()),
        class_name: None,
        title: Some(title.to_owned()),
    }
}
