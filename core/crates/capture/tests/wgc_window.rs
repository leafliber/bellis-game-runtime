//! WGC 真机测试：自建已知图案窗口，采集并核对像素、几何与变换版本。
//!
//! 需要 Windows 的交互桌面，全部标 `#[ignore]`，不进 `uv run check`；在 `core/` 下用
//! `cargo test -p capture -- --ignored --nocapture` 运行。

#![cfg(windows)]

mod common;

use capture::win::{PatternStyle, PatternWindow, WgcCapture, WgcOptions, query_geometry};
use capture::{CaptureError, CaptureSource, Frame, Point, Size};
use common::{BORDERLESS, FRAMED, SIZE, Target, WAIT};
use std::time::{Duration, Instant};

/// 等到 `next_frame` 返回错误；期间交出的帧不计。
fn wait_for_error(capture: &mut WgcCapture) -> CaptureError {
    let mut frame = Frame::default();
    let deadline = Instant::now() + WAIT;
    loop {
        match capture.next_frame(Duration::from_millis(200), &mut frame) {
            Err(error) => return error,
            Ok(_) => assert!(Instant::now() < deadline, "no error within {WAIT:?}"),
        }
    }
}

fn check_style(name: &str, style: PatternStyle, origin: Point) {
    let target = Target::open(name, style, origin, SIZE);
    let mut capture = target.capture();
    let (first, _) = target.wait_for_pattern(&mut capture, SIZE, 0);
    target.assert_meta_matches(&first);
    assert_eq!(first.meta.client_origin, origin);

    target.window.set_variant(1);
    let (second, check) = target.wait_for_pattern(&mut capture, SIZE, 1);
    assert!(second.meta.number > first.meta.number);
    assert!(second.meta.captured_at_ns > first.meta.captured_at_ns);
    assert_eq!(second.meta.transform_version, first.meta.transform_version);
    println!(
        "{name}: frame {:?}; check {check:?}; border {:?}; stats {:?}",
        second.meta,
        capture.border(),
        capture.stats()
    );
}

#[test]
#[ignore = "needs an interactive Windows desktop"]
fn captures_framed_client_area_exactly() {
    check_style("framed", FRAMED, Point { x: 200, y: 200 });
}

#[test]
#[ignore = "needs an interactive Windows desktop"]
fn captures_borderless_window_exactly() {
    check_style("borderless", BORDERLESS, Point { x: 1000, y: 200 });
}

#[test]
#[ignore = "needs an interactive Windows desktop"]
fn transform_version_increments_on_move_and_resize() {
    let origin = Point { x: 200, y: 900 };
    let target = Target::open("move-resize", FRAMED, origin, SIZE);
    let mut capture = target.capture();
    let (first, _) = target.wait_for_pattern(&mut capture, SIZE, 0);
    let base = first.meta.transform_version;

    // 只移动、不改内容：记录 WGC 是否因此出新帧。
    let moved = Point {
        x: origin.x + 37,
        y: origin.y + 23,
    };
    target.window.set_client_rect(moved, SIZE).expect("move");
    let mut probe = Frame::default();
    let frame_after_move = capture
        .next_frame(Duration::from_secs(1), &mut probe)
        .expect("capture after move");
    println!(
        "move without repaint produced a frame within 1 s: {frame_after_move}; meta {:?}",
        probe.meta
    );
    if frame_after_move {
        assert_eq!(probe.meta.client_origin, moved);
        assert_eq!(probe.meta.transform_version, base + 1);
    }

    target.window.set_variant(1);
    let (after_move, _) = target.wait_for_pattern(&mut capture, SIZE, 1);
    target.assert_meta_matches(&after_move);
    assert_eq!(after_move.meta.client_origin, moved);
    assert_eq!(after_move.meta.transform_version, base + 1, "move");

    let resized = Size {
        width: 720,
        height: 400,
    };
    target
        .window
        .set_client_rect(moved, resized)
        .expect("resize");
    target.window.set_variant(2);
    let (after_resize, _) = target.wait_for_pattern(&mut capture, resized, 2);
    target.assert_meta_matches(&after_resize);
    assert_eq!(after_resize.meta.client_origin, moved);
    assert!(
        after_resize.meta.transform_version > after_move.meta.transform_version,
        "resize"
    );
    let stats = capture.stats();
    assert!(stats.pool_recreations >= 1, "pool recreated: {stats:?}");
    println!(
        "move-resize: versions {base} -> {} -> {}; stats {stats:?}",
        after_move.meta.transform_version, after_resize.meta.transform_version
    );
}

#[test]
#[ignore = "needs an interactive Windows desktop"]
fn occluded_window_is_captured_whole() {
    let origin = Point { x: 1000, y: 900 };
    let target = Target::open("occluded", FRAMED, origin, SIZE);
    // 置顶的无边框窗口盖住目标客户区的右下部分。
    let cover = Target::open(
        "cover",
        BORDERLESS,
        Point {
            x: origin.x + 200,
            y: origin.y + 150,
        },
        Size {
            width: 400,
            height: 300,
        },
    );
    cover.window.set_variant(2);
    cover.window.set_topmost().expect("cover on top");

    let mut capture = target.capture();
    target.window.set_variant(1);
    // 被盖住的像素也要与目标窗口的图案一致：wait_for_pattern 核对全部像素。
    let (frame, check) = target.wait_for_pattern(&mut capture, SIZE, 1);
    target.assert_meta_matches(&frame);
    let below = query_geometry(target.window.hwnd()).unwrap().client;
    let above = query_geometry(cover.window.hwnd()).unwrap().client;
    assert!(
        above.left < below.right && above.top < below.bottom,
        "cover {above:?} overlaps target {below:?}"
    );
    println!(
        "occluded: target {below:?} cover {above:?}; frame {:?}; check {check:?}",
        frame.meta
    );
}

#[test]
#[ignore = "needs an interactive Windows desktop"]
fn minimized_and_closed_windows_are_reported() {
    let target = Target::open("minimize", FRAMED, Point { x: 1800, y: 200 }, SIZE);
    let mut capture = target.capture();
    target.wait_for_pattern(&mut capture, SIZE, 0);

    target.window.minimize();
    let error = wait_for_error(&mut capture);
    assert_eq!(error, CaptureError::WindowMinimized);
    assert!(error.is_recoverable());
    let restart = WgcCapture::new(&WgcOptions {
        selector: target.selector(),
        ..WgcOptions::default()
    });
    assert_eq!(
        restart.err(),
        Some(CaptureError::WindowMinimized),
        "starting on a minimized window"
    );

    target.window.restore();
    target.window.set_variant(3);
    target.wait_for_pattern(&mut capture, SIZE, 3);

    drop(target);
    let error = wait_for_error(&mut capture);
    assert_eq!(error, CaptureError::WindowClosed);
    assert!(!error.is_recoverable());
}

#[test]
#[ignore = "needs an interactive Windows desktop"]
fn ambiguous_windows_are_refused() {
    let first = Target::open("twin", FRAMED, Point { x: 1800, y: 900 }, SIZE);
    let _second =
        PatternWindow::open(&first.title, FRAMED, Point { x: 1850, y: 950 }, SIZE).unwrap();
    let result = WgcCapture::new(&WgcOptions {
        selector: first.selector(),
        ..WgcOptions::default()
    });
    assert!(
        matches!(
            result,
            Err(CaptureError::AmbiguousWindow { matches: 2, .. })
        ),
        "{:?}",
        result.err()
    );
}

/// 不感知 DPI 的窗口由系统拉伸显示：采到的帧是物理像素，尺寸与位置与物理坐标下的客户区一致。
/// 拉伸会模糊边缘，所以只核对四块中心的颜色与四边以品红为主。
#[test]
#[ignore = "needs an interactive Windows desktop"]
fn dpi_unaware_window_is_captured_in_physical_pixels() {
    let style = PatternStyle {
        dpi_unaware: true,
        ..FRAMED
    };
    let origin = Point { x: 2600, y: 200 };
    let target = Target::open("dpi-unaware", style, origin, SIZE);
    let geometry = query_geometry(target.window.hwnd()).expect("geometry");
    let size = geometry.client.size();
    let mut capture = target.capture();
    let mut frame = Frame::default();
    let deadline = Instant::now() + WAIT;
    while !(capture
        .next_frame(Duration::from_millis(250), &mut frame)
        .expect("capture")
        && frame.meta.width == size.width
        && frame.meta.height == size.height)
    {
        target.window.repaint();
        assert!(
            Instant::now() < deadline,
            "no {size} frame; last meta {:?}, stats {:?}",
            frame.meta,
            capture.stats()
        );
    }
    target.assert_meta_matches(&frame);
    println!(
        "dpi-unaware: requested client {SIZE} at {origin:?}, physical client {:?} dpi {}; frame {:?}",
        geometry.client, geometry.dpi, frame.meta
    );

    let rgb = |x, y| {
        let [blue, green, red, _] = frame.pixel(x, y).unwrap();
        [red, green, blue]
    };
    let (width, height) = (size.width, size.height);
    for (x, y) in [
        (width / 4, height / 4),
        (3 * width / 4, height / 4),
        (width / 4, 3 * height / 4),
        (3 * width / 4, 3 * height / 4),
    ] {
        assert_eq!(
            rgb(x, y),
            PatternWindow::expected_rgb(size, 0, x, y),
            "quadrant centre ({x}, {y})"
        );
    }
    let magenta_ish = |x, y| {
        let [red, green, blue] = rgb(x, y);
        red >= 128 && blue >= 128 && green <= 128
    };
    for (x, y) in [
        (0, height / 2),
        (width - 1, height / 2),
        (width / 3, 0),
        (width / 3, height - 1),
    ] {
        assert!(
            magenta_ish(x, y),
            "edge ({x}, {y}) is {:?}",
            frame.pixel(x, y)
        );
    }
}
