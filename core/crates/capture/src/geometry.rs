//! 客户区裁剪与变换版本（`docs/modules/realtime-core.md` 的“客户区与变换版本”）。坐标一律是屏幕物理像素。

use std::fmt;

/// 一个点。
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub struct Point {
    /// 横坐标。
    pub x: i32,
    /// 纵坐标。
    pub y: i32,
}

/// 宽与高。
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub struct Size {
    /// 宽。
    pub width: u32,
    /// 高。
    pub height: u32,
}

impl fmt::Display for Size {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}x{}", self.width, self.height)
    }
}

/// 矩形，含左上、不含右下。
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub struct Rect {
    /// 左边界（含）。
    pub left: i32,
    /// 上边界（含）。
    pub top: i32,
    /// 右边界（不含）。
    pub right: i32,
    /// 下边界（不含）。
    pub bottom: i32,
}

impl Rect {
    /// 左上角。
    #[must_use]
    pub fn origin(&self) -> Point {
        Point {
            x: self.left,
            y: self.top,
        }
    }

    /// 宽高；右下不在左上之后时为 0。
    #[must_use]
    pub fn size(&self) -> Size {
        Size {
            width: span(self.left, self.right),
            height: span(self.top, self.bottom),
        }
    }

    /// `inner` 是否完全落在本矩形之内。
    #[must_use]
    pub fn contains(&self, inner: &Rect) -> bool {
        inner.left >= self.left
            && inner.top >= self.top
            && inner.right <= self.right
            && inner.bottom <= self.bottom
    }
}

fn span(start: i32, end: i32) -> u32 {
    u32::try_from(i64::from(end) - i64::from(start)).unwrap_or(0)
}

/// 某一时刻窗口在屏幕上的几何。
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct WindowGeometry {
    /// DWM 窗口边界（`DWMWA_EXTENDED_FRAME_BOUNDS`），即 WGC 采到的窗口画面在屏幕上的范围。
    pub frame_bounds: Rect,
    /// 客户区在屏幕上的范围。
    pub client: Rect,
    /// 客户区所在显示器的有效 DPI。
    pub dpi: u32,
}

impl WindowGeometry {
    /// 这一几何决定的客户区到屏幕的变换。
    #[must_use]
    pub fn transform(&self) -> Transform {
        Transform {
            client_origin: self.client.origin(),
            client_size: self.client.size(),
            dpi: self.dpi,
        }
    }
}

/// 客户区在 WGC 画面中的位置。
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct CropBox {
    /// 左边距。
    pub x: u32,
    /// 上边距。
    pub y: u32,
    /// 宽。
    pub width: u32,
    /// 高。
    pub height: u32,
}

/// 无法从一帧中裁出客户区的原因。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CropError {
    /// 画面尺寸与当前窗口边界不一致：这帧是在窗口改变尺寸之前（或之后）采的，与当前几何对不上。
    StaleContent {
        /// 画面尺寸。
        content: Size,
        /// 当前窗口边界的尺寸。
        bounds: Size,
    },
    /// 客户区不完全在窗口边界之内。
    ClientOutsideBounds,
    /// 客户区为空。
    EmptyClient,
}

/// 从尺寸为 `content` 的 WGC 窗口画面中裁出客户区。
///
/// 画面覆盖 `geometry.frame_bounds`，所以客户区在画面中的偏移是客户区原点减去窗口边界原点。画面尺寸须
/// 与窗口边界一致，否则这帧与 `geometry` 不是同一时刻的，裁剪结果不可信。
///
/// # Errors
///
/// 见 [`CropError`]。
pub fn client_crop(geometry: &WindowGeometry, content: Size) -> Result<CropBox, CropError> {
    let bounds = geometry.frame_bounds.size();
    if content != bounds {
        return Err(CropError::StaleContent { content, bounds });
    }
    if !geometry.frame_bounds.contains(&geometry.client) {
        return Err(CropError::ClientOutsideBounds);
    }
    let size = geometry.client.size();
    if size.width == 0 || size.height == 0 {
        return Err(CropError::EmptyClient);
    }
    Ok(CropBox {
        x: span(geometry.frame_bounds.left, geometry.client.left),
        y: span(geometry.frame_bounds.top, geometry.client.top),
        width: size.width,
        height: size.height,
    })
}

/// 客户区到屏幕的变换：客户区原点、尺寸与所在显示器的 DPI。
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Transform {
    /// 客户区左上角在屏幕上的位置。
    pub client_origin: Point,
    /// 客户区尺寸。
    pub client_size: Size,
    /// 客户区所在显示器的有效 DPI。
    pub dpi: u32,
}

/// 计算窗口与 DPI 变换版本。
///
/// 版本从 1 开始（0 表示还没有观测）；之后每次观测到的变换与上一次不同（窗口移动、改变尺寸或 DPI
/// 变化）时加一。同一变换上的帧可以共用坐标换算，版本不同的帧不能。
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct TransformTracker {
    last: Option<Transform>,
    version: u64,
}

impl TransformTracker {
    /// 记录一次观测到的变换，返回它的版本。
    pub fn observe(&mut self, transform: Transform) -> u64 {
        if self.last != Some(transform) {
            self.last = Some(transform);
            self.version += 1;
        }
        self.version
    }

    /// 当前版本；还没有观测时为 0。
    #[must_use]
    pub fn version(&self) -> u64 {
        self.version
    }
}

#[cfg(test)]
mod tests {
    use super::{
        CropBox, CropError, Point, Rect, Size, Transform, TransformTracker, WindowGeometry,
        client_crop,
    };

    fn rect(left: i32, top: i32, width: i32, height: i32) -> Rect {
        Rect {
            left,
            top,
            right: left + width,
            bottom: top + height,
        }
    }

    /// 带标题栏的普通窗口：窗口边界 816×639，客户区在其中偏 (1, 31)，大小 814×607。
    fn framed_window() -> WindowGeometry {
        WindowGeometry {
            frame_bounds: rect(100, 200, 816, 639),
            client: rect(101, 231, 814, 607),
            dpi: 96,
        }
    }

    #[test]
    fn crops_client_area_out_of_window_content() {
        let geometry = framed_window();
        let crop = client_crop(&geometry, geometry.frame_bounds.size());
        assert_eq!(
            crop,
            Ok(CropBox {
                x: 1,
                y: 31,
                width: 814,
                height: 607
            })
        );
    }

    #[test]
    fn borderless_window_crops_whole_content() {
        let geometry = WindowGeometry {
            frame_bounds: rect(-1920, 0, 1920, 1080),
            client: rect(-1920, 0, 1920, 1080),
            dpi: 144,
        };
        assert_eq!(
            client_crop(
                &geometry,
                Size {
                    width: 1920,
                    height: 1080
                }
            ),
            Ok(CropBox {
                x: 0,
                y: 0,
                width: 1920,
                height: 1080
            })
        );
    }

    #[test]
    fn content_from_another_size_is_stale() {
        let geometry = framed_window();
        let content = Size {
            width: 800,
            height: 639,
        };
        assert_eq!(
            client_crop(&geometry, content),
            Err(CropError::StaleContent {
                content,
                bounds: Size {
                    width: 816,
                    height: 639
                }
            })
        );
    }

    #[test]
    fn client_outside_bounds_or_empty_is_rejected() {
        let mut geometry = framed_window();
        geometry.client.right = geometry.frame_bounds.right + 1;
        let content = geometry.frame_bounds.size();
        assert_eq!(
            client_crop(&geometry, content),
            Err(CropError::ClientOutsideBounds)
        );

        let mut geometry = framed_window();
        geometry.client.bottom = geometry.client.top;
        assert_eq!(client_crop(&geometry, content), Err(CropError::EmptyClient));
    }

    #[test]
    fn inverted_rect_has_zero_size() {
        let inverted = Rect {
            left: 10,
            top: 10,
            right: 5,
            bottom: 0,
        };
        assert_eq!(inverted.size(), Size::default());
        let extreme = Rect {
            left: i32::MIN,
            top: 0,
            right: i32::MAX,
            bottom: 1,
        };
        assert_eq!(extreme.size().width, u32::MAX);
    }

    #[test]
    fn transform_version_counts_changes_only() {
        let mut tracker = TransformTracker::default();
        assert_eq!(tracker.version(), 0);
        let geometry = framed_window();
        let base = geometry.transform();
        assert_eq!(
            base,
            Transform {
                client_origin: Point { x: 101, y: 231 },
                client_size: Size {
                    width: 814,
                    height: 607
                },
                dpi: 96
            }
        );
        assert_eq!(tracker.observe(base), 1);
        assert_eq!(tracker.observe(base), 1, "same transform keeps the version");

        let moved = Transform {
            client_origin: Point { x: 140, y: 231 },
            ..base
        };
        assert_eq!(tracker.observe(moved), 2, "move");
        let resized = Transform {
            client_size: Size {
                width: 1024,
                height: 607,
            },
            ..moved
        };
        assert_eq!(tracker.observe(resized), 3, "resize");
        let rescaled = Transform {
            dpi: 144,
            ..resized
        };
        assert_eq!(tracker.observe(rescaled), 4, "DPI change");
        assert_eq!(tracker.observe(base), 5, "moving back is still a change");
        assert_eq!(tracker.version(), 5);
    }

    #[test]
    fn window_frame_change_without_client_change_keeps_version() {
        let mut tracker = TransformTracker::default();
        let geometry = framed_window();
        tracker.observe(geometry.transform());
        let mut taller_title = geometry;
        taller_title.frame_bounds.top -= 10;
        assert_eq!(tracker.observe(taller_title.transform()), 1);
    }
}
