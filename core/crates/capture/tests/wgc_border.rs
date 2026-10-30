//! 黄色采集边框的真机测试：读屏幕像素，所以单独成一个测试程序，免得与其它测试的窗口同时出现在屏幕上。
//!
//! 需要 Windows 的交互桌面，标 `#[ignore]`，不进 `uv run check`；在 `core/` 下用
//! `cargo test -p capture -- --ignored --nocapture` 运行。

#![cfg(windows)]

mod common;

use capture::Rect;
use capture::win::{BorderRequest, WgcCapture, WgcOptions, query_geometry};
use common::{FRAMED, SIZE, Target};
use std::time::Duration;
use windows::Win32::Graphics::Gdi::{
    BI_RGB, BITMAPINFO, BITMAPINFOHEADER, BitBlt, CAPTUREBLT, CreateCompatibleBitmap,
    CreateCompatibleDC, DIB_RGB_COLORS, DeleteDC, DeleteObject, GetDC, GetDIBits, ReleaseDC,
    SRCCOPY, SelectObject,
};

/// 用 GDI 从屏幕读回 `area` 的像素（含分层窗口），按 B、G、R、A 排列。
fn grab_screen(area: Rect) -> Vec<[u8; 4]> {
    let (width, height) = (area.right - area.left, area.bottom - area.top);
    let mut pixels = vec![[0u8; 4]; (width * height).try_into().unwrap()];
    let mut info = BITMAPINFO::default();
    info.bmiHeader.biSize = u32::try_from(size_of::<BITMAPINFOHEADER>()).unwrap();
    info.bmiHeader.biWidth = width;
    info.bmiHeader.biHeight = -height;
    info.bmiHeader.biPlanes = 1;
    info.bmiHeader.biBitCount = 32;
    info.bmiHeader.biCompression = BI_RGB.0;
    // SAFETY: 设备上下文与位图在这里创建并释放；GetDIBits 写入的缓冲足够容纳 width × height 个 32 位像素。
    unsafe {
        let screen = GetDC(None);
        let memory = CreateCompatibleDC(Some(screen));
        let bitmap = CreateCompatibleBitmap(screen, width, height);
        let previous = SelectObject(memory, bitmap.into());
        BitBlt(
            memory,
            0,
            0,
            width,
            height,
            Some(screen),
            area.left,
            area.top,
            SRCCOPY | CAPTUREBLT,
        )
        .expect("BitBlt from the screen");
        SelectObject(memory, previous);
        GetDIBits(
            memory,
            bitmap,
            0,
            height.try_into().unwrap(),
            Some(pixels.as_mut_ptr().cast()),
            &raw mut info,
            DIB_RGB_COLORS,
        );
        let _ = DeleteObject(bitmap.into());
        let _ = DeleteDC(memory);
        ReleaseDC(None, screen);
    }
    pixels
}

/// 黄色采集边框画在窗口边界上。对照：保留边框时，屏幕上窗口边界附近应出现黄色像素；请求去掉后不应出现。
#[test]
#[ignore = "needs an interactive Windows desktop"]
fn yellow_border_is_removed_on_screen() {
    let target = Target::open("border", FRAMED, capture::Point { x: 1500, y: 500 }, SIZE);
    std::thread::sleep(Duration::from_millis(500));
    let bounds = query_geometry(target.window.hwnd()).unwrap().frame_bounds;
    let reach = 6;
    let area = Rect {
        left: bounds.left - reach,
        top: bounds.top - reach,
        right: bounds.right + reach,
        bottom: bounds.bottom + reach,
    };
    let width = usize::try_from(area.right - area.left).unwrap();
    // 窗口边界内外各 `reach` 像素宽的一圈。
    let in_ring = |index: usize| {
        let x = area.left + i32::try_from(index % width).unwrap();
        let y = area.top + i32::try_from(index / width).unwrap();
        let near = |value: i32, edge: i32| (value - edge).abs() <= reach;
        near(x, bounds.left)
            || near(x, bounds.right - 1)
            || near(y, bounds.top)
            || near(y, bounds.bottom - 1)
    };
    let yellowish = |[b, g, r, _]: [u8; 4]| r >= 150 && g >= 100 && b <= 100 && r - b >= 100;
    let baseline = grab_screen(area);

    let mut counts = Vec::new();
    for request in [BorderRequest::Keep, BorderRequest::Remove] {
        let mut capture = WgcCapture::new(&WgcOptions {
            selector: target.selector(),
            border: request,
            min_update_interval: None,
        })
        .expect("start capture");
        target.wait_for_pattern(&mut capture, SIZE, 0);
        std::thread::sleep(Duration::from_secs(1));
        let shot = grab_screen(area);
        let appeared = baseline
            .iter()
            .zip(&shot)
            .enumerate()
            .filter(|(index, (before, after))| {
                in_ring(*index) && before[..3] != after[..3] && yellowish(**after)
            })
            .count();
        println!(
            "border {request:?}: {:?}; yellow pixels appeared around the window: {appeared}",
            capture.border()
        );
        counts.push(appeared);
        drop(capture);
        std::thread::sleep(Duration::from_millis(500));
    }
    assert!(
        counts[0] > 100,
        "control failed: the kept border was not seen on screen"
    );
    assert_eq!(counts[1], 0, "yellow border still visible after removal");
}
