//! 输入策略：动作白名单与静态禁区的执行（`docs/modules/realtime-core.md` 的“输入策略”）。
//!
//! 主进程把游戏适配包换算成 [`core_protocol::messages::InputPolicy`]，以 `input.configure` 每个实例交一次。
//! 这里核对它合不合规则（[`Policy::parse`]，不合回参数无效），并在许可与权威检查之外强制执行：
//!
//! - 提交时（`action.submit`）：持续状态的键与鼠标键须在白名单内，不允许轴；一次性动作里点击的键、短按的
//!   扫描码、滚动都须在白名单内，不允许相对位移。不符即整块拒绝（`not_allowed`）。
//! - 执行时：带坐标的动作落点须在某个 `click_regions` 内、不在任何 `forbidden_zones` 内；超过 `max_rate`
//!   的一次性动作丢弃；持续状态按住超过 `max_hold` 即释放全部输入。

use std::collections::HashSet;
use std::time::Duration;

use core_protocol::messages::{Action, HeldInput, InputPolicy, MouseButton, NormRect};

/// 一个归一化矩形，各分量已核对在 [0, 1] 且左上小于右下。
#[derive(Clone, Copy, Debug)]
pub(crate) struct Rect {
    left: f64,
    top: f64,
    right: f64,
    bottom: f64,
}

impl Rect {
    fn parse(index: usize, field: &str, rect: &NormRect) -> Result<Self, String> {
        let valid = [rect.left, rect.top, rect.right, rect.bottom]
            .iter()
            .all(|v| v.is_finite() && (0.0..=1.0).contains(v));
        if !(valid && rect.left < rect.right && rect.top < rect.bottom) {
            return Err(format!(
                "{field}[{index}] 不是 [0, 1] 内左上小于右下的归一化矩形"
            ));
        }
        Ok(Self {
            left: rect.left,
            top: rect.top,
            right: rect.right,
            bottom: rect.bottom,
        })
    }

    /// 归一化点 `(x, y)` 是否在矩形内（含左上、不含右下，与适配包的像素换算一致）。
    fn contains(&self, x: f64, y: f64) -> bool {
        self.left <= x && x < self.right && self.top <= y && y < self.bottom
    }
}

/// 核对过的输入策略。
#[derive(Debug)]
pub(crate) struct Policy {
    click_buttons: HashSet<MouseButton>,
    press_keys: HashSet<u16>,
    scroll: bool,
    click_regions: Vec<Rect>,
    forbidden_zones: Vec<Rect>,
    press_duration: Duration,
    /// 每秒至多几个一次性动作。
    max_rate: f64,
    hold_keys: HashSet<u16>,
    hold_buttons: HashSet<MouseButton>,
    max_hold: Option<Duration>,
}

/// 提交时持续状态或一次性动作不在白名单内。
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct NotAllowed(pub(crate) String);

impl Policy {
    /// 核对 [`InputPolicy`]；不合规则时返回说明（回参数无效 -32602）。
    pub(crate) fn parse(policy: &InputPolicy) -> Result<Self, String> {
        if !(1..=500).contains(&policy.press_duration_ms) {
            return Err(format!(
                "press_duration_ms 须在 1 到 500 之间，得到 {}",
                policy.press_duration_ms
            ));
        }
        if !(policy.max_rate.is_finite() && policy.max_rate > 0.0) {
            return Err(format!("max_rate 须大于 0，得到 {}", policy.max_rate));
        }
        let mut click_regions = Vec::with_capacity(policy.click_regions.len());
        for (index, rect) in policy.click_regions.iter().enumerate() {
            click_regions.push(Rect::parse(index, "click_regions", rect)?);
        }
        let mut forbidden_zones = Vec::with_capacity(policy.forbidden_zones.len());
        for (index, rect) in policy.forbidden_zones.iter().enumerate() {
            forbidden_zones.push(Rect::parse(index, "forbidden_zones", rect)?);
        }
        if (!policy.click_buttons.is_empty() || policy.scroll) && click_regions.is_empty() {
            return Err("允许点击或滚动时 click_regions 不能为空".into());
        }
        let allows_hold = !policy.hold_keys.is_empty() || !policy.hold_buttons.is_empty();
        if allows_hold != policy.max_hold_ms.is_some() {
            return Err("有可按住的项时必须给出 max_hold_ms，没有时须为 null".into());
        }
        if policy.max_hold_ms == Some(0) {
            return Err("max_hold_ms 须大于 0".into());
        }
        Ok(Self {
            click_buttons: policy.click_buttons.iter().copied().collect(),
            press_keys: policy.press_keys.iter().copied().collect(),
            scroll: policy.scroll,
            click_regions,
            forbidden_zones,
            press_duration: Duration::from_millis(u64::from(policy.press_duration_ms)),
            max_rate: policy.max_rate,
            hold_keys: policy.hold_keys.iter().copied().collect(),
            hold_buttons: policy.hold_buttons.iter().copied().collect(),
            max_hold: policy
                .max_hold_ms
                .map(|ms| Duration::from_millis(u64::from(ms))),
        })
    }

    /// 点击与短按的按住时长。
    pub(crate) fn press_duration(&self) -> Duration {
        self.press_duration
    }

    /// 最长按住时间；没有可按住的项时为 `None`。
    pub(crate) fn max_hold(&self) -> Option<Duration> {
        self.max_hold
    }

    /// 过去 1 秒已有 `recent` 个一次性动作时，是否还允许下一个。
    pub(crate) fn within_rate(&self, recent: usize) -> bool {
        // max_rate 个之内允许；用整数下界与它比较（max_rate 可以是小数）。
        f64::from(u32::try_from(recent).unwrap_or(u32::MAX)) < self.max_rate
    }

    /// 持续状态的一项是否在白名单内（键在 `hold_keys`、鼠标键在 `hold_buttons`，轴不允许）。
    pub(crate) fn allows_held(&self, input: &HeldInput) -> Result<(), NotAllowed> {
        match input {
            HeldInput::Key(key) if self.hold_keys.contains(&key.scan_code) => Ok(()),
            HeldInput::Key(key) => Err(NotAllowed(format!(
                "扫描码 {:#x} 不在 hold_keys 内",
                key.scan_code
            ))),
            HeldInput::MouseButton(button) if self.hold_buttons.contains(&button.button) => Ok(()),
            HeldInput::MouseButton(button) => Err(NotAllowed(format!(
                "鼠标键 {:?} 不在 hold_buttons 内",
                button.button
            ))),
            HeldInput::Axis(axis) => Err(NotAllowed(format!("不允许轴 {}", axis.axis))),
        }
    }

    /// 一次性动作的种类是否在白名单内（提交时核对；不含落点与速率，那些在执行时核对）。
    pub(crate) fn allows_action(&self, action: &Action) -> Result<(), NotAllowed> {
        match action {
            Action::Click(click) if self.click_buttons.contains(&click.button) => Ok(()),
            Action::Click(click) => Err(NotAllowed(format!(
                "鼠标键 {:?} 不在 click_buttons 内",
                click.button
            ))),
            Action::KeyPress(press) if self.press_keys.contains(&press.scan_code) => Ok(()),
            Action::KeyPress(press) => Err(NotAllowed(format!(
                "扫描码 {:#x} 不在 press_keys 内",
                press.scan_code
            ))),
            Action::Scroll(_) if self.scroll => Ok(()),
            Action::Scroll(_) => Err(NotAllowed("不允许滚动".into())),
            Action::MouseMove(_) => Err(NotAllowed("不允许相对位移".into())),
        }
    }

    /// 执行时核对带坐标的动作的落点：客户区像素 `(x, y)` 按 `width`×`height` 归一化后，须在某个
    /// `click_regions` 内、不在任何 `forbidden_zones` 内。`width`、`height` 为 0 时不允许。
    pub(crate) fn point_allowed(&self, x: u32, y: u32, width: u32, height: u32) -> bool {
        if width == 0 || height == 0 {
            return false;
        }
        let nx = f64::from(x) / f64::from(width);
        let ny = f64::from(y) / f64::from(height);
        let in_region = self.click_regions.iter().any(|r| r.contains(nx, ny));
        let forbidden = self.forbidden_zones.iter().any(|r| r.contains(nx, ny));
        in_region && !forbidden
    }
}

#[cfg(test)]
mod tests {
    use super::Policy;
    use core_protocol::messages::{
        Action, Click, HeldAxis, HeldInput, HeldKey, HeldMouseButton, InputPolicy, KeyPress,
        MouseButton, MouseMove, NormRect, Scroll,
    };

    fn rect(left: f64, top: f64, right: f64, bottom: f64) -> NormRect {
        NormRect {
            left,
            top,
            right,
            bottom,
        }
    }

    fn base() -> InputPolicy {
        InputPolicy {
            click_buttons: vec![MouseButton::Left],
            press_keys: vec![0x1C],
            scroll: true,
            click_regions: vec![rect(0.0, 0.0, 1.0, 1.0)],
            forbidden_zones: vec![],
            press_duration_ms: 50,
            max_rate: 2.0,
            hold_keys: vec![0x11],
            hold_buttons: vec![MouseButton::Right],
            max_hold_ms: Some(2000),
        }
    }

    #[test]
    fn parse_rejects_bad_policies() {
        let mut p = base();
        p.press_duration_ms = 0;
        assert!(Policy::parse(&p).is_err());
        let mut p = base();
        p.press_duration_ms = 501;
        assert!(Policy::parse(&p).is_err());
        let mut p = base();
        p.max_rate = 0.0;
        assert!(Policy::parse(&p).is_err());
        let mut p = base();
        p.click_regions = vec![rect(0.5, 0.0, 0.5, 1.0)];
        assert!(Policy::parse(&p).is_err(), "左不小于右");
        let mut p = base();
        p.click_regions = vec![];
        assert!(Policy::parse(&p).is_err(), "允许点击时区域不能空");
        let mut p = base();
        p.hold_keys = vec![];
        p.hold_buttons = vec![];
        assert!(
            Policy::parse(&p).is_err(),
            "没有可按住的项却给了 max_hold_ms"
        );
        let mut p = base();
        p.max_hold_ms = None;
        assert!(Policy::parse(&p).is_err(), "有可按住的项却没给 max_hold_ms");
        assert!(Policy::parse(&base()).is_ok());
    }

    #[test]
    fn submit_time_whitelist() {
        let policy = Policy::parse(&base()).unwrap();
        assert!(
            policy
                .allows_held(&HeldInput::Key(HeldKey { scan_code: 0x11 }))
                .is_ok()
        );
        assert!(
            policy
                .allows_held(&HeldInput::Key(HeldKey { scan_code: 0x1C }))
                .is_err()
        );
        assert!(
            policy
                .allows_held(&HeldInput::MouseButton(HeldMouseButton {
                    button: MouseButton::Right
                }))
                .is_ok()
        );
        assert!(
            policy
                .allows_held(&HeldInput::Axis(HeldAxis {
                    axis: "yaw".into(),
                    value: 0.0
                }))
                .is_err(),
            "不允许轴"
        );
        assert!(
            policy
                .allows_action(&Action::Click(Click {
                    id: 1,
                    x: 0,
                    y: 0,
                    button: MouseButton::Left
                }))
                .is_ok()
        );
        assert!(
            policy
                .allows_action(&Action::Click(Click {
                    id: 1,
                    x: 0,
                    y: 0,
                    button: MouseButton::Right
                }))
                .is_err()
        );
        assert!(
            policy
                .allows_action(&Action::KeyPress(KeyPress {
                    id: 1,
                    scan_code: 0x1C
                }))
                .is_ok()
        );
        assert!(
            policy
                .allows_action(&Action::MouseMove(MouseMove {
                    id: 1,
                    dx: 1,
                    dy: 1
                }))
                .is_err(),
            "不允许相对位移"
        );
        let mut no_scroll = base();
        no_scroll.scroll = false;
        let policy = Policy::parse(&no_scroll).unwrap();
        assert!(
            policy
                .allows_action(&Action::Scroll(Scroll {
                    id: 1,
                    x: 0,
                    y: 0,
                    notches: 1
                }))
                .is_err()
        );
    }

    #[test]
    fn region_and_forbidden_zone() {
        let mut p = base();
        p.click_regions = vec![rect(0.25, 0.25, 0.75, 0.75)];
        p.forbidden_zones = vec![rect(0.4, 0.4, 0.6, 0.6)];
        let policy = Policy::parse(&p).unwrap();
        // 100×100 的客户区：(50, 20) 在区域外；(30, 30) 在区域内不在禁区；(50, 50) 在禁区内。
        assert!(!policy.point_allowed(50, 20, 100, 100));
        assert!(policy.point_allowed(30, 30, 100, 100));
        assert!(!policy.point_allowed(50, 50, 100, 100));
        assert!(!policy.point_allowed(30, 30, 0, 100), "尺寸为 0 不允许");
    }

    #[test]
    fn rate_counts_against_max_rate() {
        let policy = Policy::parse(&base()).unwrap(); // max_rate = 2
        assert!(policy.within_rate(0));
        assert!(policy.within_rate(1));
        assert!(!policy.within_rate(2));
    }
}
