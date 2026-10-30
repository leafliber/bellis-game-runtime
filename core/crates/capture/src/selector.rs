//! 窗口识别条件（`docs/modules/realtime-core.md` 的“窗口识别”）。

use std::fmt;

/// 窗口识别条件：按进程映像文件名、窗口类名与标题匹配可见的顶层窗口，给出的条件须全部满足。
///
/// 游戏的识别条件来自配置（G1 起来自游戏适配包），应给出进程名加窗口类或标题。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct WindowSelector {
    /// 进程映像的文件名，如 `game.exe`；不含目录，不区分大小写。
    pub process_name: Option<String>,
    /// 窗口类名，精确匹配。
    pub class_name: Option<String>,
    /// 窗口标题，精确匹配。
    pub title: Option<String>,
}

/// 一个顶层窗口的识别信息。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct WindowInfo {
    /// 进程映像的完整路径；无权查询时为 `None`。
    pub process_image: Option<String>,
    /// 窗口类名。
    pub class_name: String,
    /// 窗口标题。
    pub title: String,
}

impl WindowSelector {
    /// 没有给出任何条件。空条件不匹配任何窗口，以免误采整屏或任意窗口。
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.process_name.is_none() && self.class_name.is_none() && self.title.is_none()
    }

    /// `window` 是否满足全部给出的条件。
    #[must_use]
    pub fn matches(&self, window: &WindowInfo) -> bool {
        if self.is_empty() {
            return false;
        }
        let process_ok = self.process_name.as_deref().is_none_or(|wanted| {
            window
                .process_image
                .as_deref()
                .is_some_and(|image| same_file_name(file_name(image), wanted))
        });
        let class_ok = self
            .class_name
            .as_deref()
            .is_none_or(|wanted| window.class_name == wanted);
        let title_ok = self
            .title
            .as_deref()
            .is_none_or(|wanted| window.title == wanted);
        process_ok && class_ok && title_ok
    }
}

impl fmt::Display for WindowSelector {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let parts = [
            ("process", &self.process_name),
            ("class", &self.class_name),
            ("title", &self.title),
        ];
        let mut first = true;
        for (name, value) in parts {
            if let Some(value) = value {
                if !first {
                    f.write_str(" and ")?;
                }
                write!(f, "{name} {value:?}")?;
                first = false;
            }
        }
        if first {
            f.write_str("an empty selector")?;
        }
        Ok(())
    }
}

/// 路径的最后一段；Windows 路径以 `\` 或 `/` 分隔。
fn file_name(path: &str) -> &str {
    path.rsplit(['\\', '/']).next().unwrap_or(path)
}

/// 规范化后比较文件名：Windows 的文件名不区分大小写。
fn same_file_name(a: &str, b: &str) -> bool {
    a.to_lowercase() == b.to_lowercase()
}

#[cfg(test)]
mod tests {
    use super::{WindowInfo, WindowSelector};

    fn notepad() -> WindowInfo {
        WindowInfo {
            process_image: Some(r"C:\Windows\System32\Notepad.EXE".into()),
            class_name: "Notepad".into(),
            title: "无标题 - 记事本".into(),
        }
    }

    fn selector(process: Option<&str>, class: Option<&str>, title: Option<&str>) -> WindowSelector {
        WindowSelector {
            process_name: process.map(Into::into),
            class_name: class.map(Into::into),
            title: title.map(Into::into),
        }
    }

    #[test]
    fn process_name_matches_file_name_case_insensitively() {
        let window = notepad();
        assert!(selector(Some("notepad.exe"), None, None).matches(&window));
        assert!(selector(Some("NOTEPAD.exe"), Some("Notepad"), None).matches(&window));
        assert!(!selector(Some("notepad"), None, None).matches(&window));
        assert!(!selector(Some(r"System32\notepad.exe"), None, None).matches(&window));
    }

    #[test]
    fn forward_slashes_also_separate_directories() {
        let window = WindowInfo {
            process_image: Some("C:/Games/Game.exe".into()),
            ..WindowInfo::default()
        };
        assert!(selector(Some("game.exe"), None, None).matches(&window));
    }

    #[test]
    fn class_and_title_match_exactly_and_all_conditions_apply() {
        let window = notepad();
        assert!(selector(Some("notepad.exe"), None, Some("无标题 - 记事本")).matches(&window));
        assert!(!selector(Some("notepad.exe"), None, Some("无标题")).matches(&window));
        assert!(!selector(Some("notepad.exe"), Some("notepad"), None).matches(&window));
        assert!(!selector(Some("other.exe"), Some("Notepad"), None).matches(&window));
    }

    #[test]
    fn unknown_process_image_never_matches_a_process_name() {
        let window = WindowInfo {
            process_image: None,
            ..notepad()
        };
        assert!(!selector(Some("notepad.exe"), None, None).matches(&window));
        assert!(selector(None, Some("Notepad"), None).matches(&window));
    }

    #[test]
    fn empty_selector_matches_nothing() {
        let empty = WindowSelector::default();
        assert!(empty.is_empty());
        assert!(!empty.matches(&notepad()));
        assert_eq!(empty.to_string(), "an empty selector");
    }

    #[test]
    fn display_lists_given_conditions() {
        let selector = selector(Some("game.exe"), None, Some("Game"));
        assert_eq!(
            selector.to_string(),
            r#"process "game.exe" and title "Game""#
        );
    }
}
