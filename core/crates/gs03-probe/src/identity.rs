//! 报告当前进程的用户 SID 与完整性级别（GS-03 的第 2、3 项）。
//!
//! 完整性级别取令牌的 `TokenIntegrityLevel`（`TOKEN_MANDATORY_LABEL`），把其 SID
//! （形如 `S-1-16-<RID>`）的最后一段 RID 映射成名字；用户 SID 取 `TokenUser`。只在 Windows 上实现，
//! 其它平台报告 `unsupported`，这样连接与写帧的逻辑仍能在 Linux 容器的检查里编译。

use serde::Serialize;

/// 进程身份：报给非提权一侧核对。
#[derive(Debug, Clone, Serialize)]
pub struct Identity {
    /// 用户 SID，形如 `S-1-5-21-…`；取不到时为 `unsupported` 或错误说明。
    pub user_sid: String,
    /// 完整性级别的名字，例如 `high`、`medium`、`system`。
    pub integrity_level: String,
    /// 完整性级别的 RID（`S-1-16-<RID>` 的最后一段）；非 Windows 上为 `None`。
    pub integrity_rid: Option<u32>,
}

/// 把 `S-1-16-<RID>` 的 RID 映射成名字。未知值给出十六进制，便于记录原始数据。
///
/// 只在 Windows 上由 [`current`] 使用；非 Windows 的构建里用不到，但保留以便跨平台测试。
#[must_use]
#[cfg_attr(not(windows), allow(dead_code))]
pub fn integrity_name(rid: u32) -> String {
    match rid {
        0x0000 => "untrusted".to_owned(),
        0x1000 => "low".to_owned(),
        0x2000 => "medium".to_owned(),
        0x2100 => "medium-plus".to_owned(),
        0x3000 => "high".to_owned(),
        0x4000 => "system".to_owned(),
        0x5000 => "protected-process".to_owned(),
        other => format!("unknown(0x{other:04x})"),
    }
}

/// `S-1-16-<RID>` 字符串里的 RID；不是完整性 SID 时为 `None`。
#[must_use]
#[cfg_attr(not(windows), allow(dead_code))]
pub fn integrity_rid_from_sid(sid: &str) -> Option<u32> {
    let rest = sid.strip_prefix("S-1-16-")?;
    rest.parse().ok()
}

#[cfg(windows)]
pub use imp::current;

/// 非 Windows 上没有令牌完整性级别的概念；探针报告 `unsupported`，不假装有结果。
#[cfg(not(windows))]
#[must_use]
pub fn current() -> Identity {
    Identity {
        user_sid: "unsupported".to_owned(),
        integrity_level: "unsupported".to_owned(),
        integrity_rid: None,
    }
}

#[cfg(windows)]
mod imp {
    use std::io;

    use windows::Win32::Foundation::{CloseHandle, HANDLE, HLOCAL, LocalFree};
    use windows::Win32::Security::Authorization::ConvertSidToStringSidW;
    use windows::Win32::Security::{
        GetTokenInformation, PSID, TOKEN_MANDATORY_LABEL, TOKEN_QUERY, TOKEN_USER,
        TokenIntegrityLevel, TokenUser,
    };
    use windows::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};
    use windows::core::PWSTR;

    use super::{Identity, integrity_name, integrity_rid_from_sid};

    /// 当前进程的身份；取令牌信息失败时把错误写进字段，不中断探针。
    #[must_use]
    pub fn current() -> Identity {
        match query() {
            Ok(identity) => identity,
            Err(error) => Identity {
                user_sid: format!("error: {error}"),
                integrity_level: format!("error: {error}"),
                integrity_rid: None,
            },
        }
    }

    fn query() -> io::Result<Identity> {
        let mut token = OpenToken::for_current_process()?;
        let user = sid_to_string(token.user_sid()?)?;
        let integrity = sid_to_string(token.integrity_sid()?)?;
        let integrity_rid = integrity_rid_from_sid(&integrity);
        let integrity_level = match integrity_rid {
            Some(rid) => integrity_name(rid),
            None => format!("unparsed({integrity})"),
        };
        Ok(Identity {
            user_sid: user,
            integrity_level,
            integrity_rid,
        })
    }

    /// 当前进程令牌的句柄，`Drop` 时关闭。
    struct OpenToken {
        handle: HANDLE,
        user: Vec<u64>,
        label: Vec<u64>,
    }

    impl OpenToken {
        fn for_current_process() -> io::Result<Self> {
            let mut handle = HANDLE::default();
            // SAFETY: `GetCurrentProcess` 返回伪句柄，始终有效；写入我们自己的 `handle`。
            unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &raw mut handle) }
                .map_err(|error| win_error(&error))?;
            Ok(Self {
                handle,
                user: Vec::new(),
                label: Vec::new(),
            })
        }

        fn user_sid(&mut self) -> io::Result<PSID> {
            self.user = token_information(self.handle, TokenUser)?;
            // SAFETY: 缓冲区由 `GetTokenInformation(TokenUser)` 填入，按 u64 对齐，开头是 `TOKEN_USER`。
            let info = unsafe { &*self.user.as_ptr().cast::<TOKEN_USER>() };
            Ok(info.User.Sid)
        }

        fn integrity_sid(&mut self) -> io::Result<PSID> {
            self.label = token_information(self.handle, TokenIntegrityLevel)?;
            // SAFETY: 缓冲区由 `GetTokenInformation(TokenIntegrityLevel)` 填入，开头是 `TOKEN_MANDATORY_LABEL`。
            let info = unsafe { &*self.label.as_ptr().cast::<TOKEN_MANDATORY_LABEL>() };
            Ok(info.Label.Sid)
        }
    }

    impl Drop for OpenToken {
        fn drop(&mut self) {
            // SAFETY: `handle` 来自 `OpenProcessToken`，之后不再使用。
            let _ = unsafe { CloseHandle(self.handle) };
        }
    }

    /// 取令牌信息到一个按 u64 对齐的缓冲区（含指针的结构体要求 8 字节对齐）。
    fn token_information(
        token: HANDLE,
        class: windows::Win32::Security::TOKEN_INFORMATION_CLASS,
    ) -> io::Result<Vec<u64>> {
        let mut needed = 0u32;
        // 第一次只为取长度：缓冲区为空，必然返回“缓冲区不足”，忽略它的错误。
        // SAFETY: 传 `None` 作缓冲、长度 0；只写 `needed`。
        let _ = unsafe { GetTokenInformation(token, class, None, 0, &raw mut needed) };
        if needed == 0 {
            return Err(io::Error::other("GetTokenInformation 未给出所需长度"));
        }
        let words = (needed as usize).div_ceil(size_of::<u64>()).max(1);
        let mut buffer = vec![0u64; words];
        // SAFETY: `buffer` 至少 `needed` 字节且按 u64 对齐；按 `needed` 作长度写入。
        unsafe {
            GetTokenInformation(
                token,
                class,
                Some(buffer.as_mut_ptr().cast()),
                needed,
                &raw mut needed,
            )
        }
        .map_err(|error| win_error(&error))?;
        Ok(buffer)
    }

    /// 把 SID 转成字符串，释放系统分配的缓冲。
    fn sid_to_string(sid: PSID) -> io::Result<String> {
        let mut text = PWSTR::null();
        // SAFETY: `sid` 来自上面取到的令牌信息，在 `OpenToken` 存活期间有效；输出写入我们的 `text`。
        unsafe { ConvertSidToStringSidW(sid, &raw mut text) }.map_err(|error| win_error(&error))?;
        if text.is_null() {
            return Err(io::Error::other("ConvertSidToStringSidW 返回空指针"));
        }
        // SAFETY: `text` 是 `ConvertSidToStringSidW` 分配的以 NUL 结尾的宽字符串。
        let value = unsafe { text.to_string() }.map_err(io::Error::other);
        // SAFETY: `text` 由系统以 `LocalAlloc` 分配，用 `LocalFree` 释放；之后不再使用。
        let _ = unsafe { LocalFree(Some(HLOCAL(text.as_ptr().cast()))) };
        value
    }

    fn win_error(error: &windows::core::Error) -> io::Error {
        // 把 HRESULT 还原成 Win32 错误码（低 16 位）。
        io::Error::from_raw_os_error(error.code().0 & 0xFFFF)
    }
}

#[cfg(test)]
mod tests {
    use super::{integrity_name, integrity_rid_from_sid};

    #[test]
    fn maps_known_integrity_rids() {
        assert_eq!(integrity_name(0x2000), "medium");
        assert_eq!(integrity_name(0x3000), "high");
        assert_eq!(integrity_name(0x4000), "system");
        assert!(integrity_name(0x1234).starts_with("unknown"));
    }

    #[test]
    fn parses_integrity_rid_from_sid() {
        assert_eq!(integrity_rid_from_sid("S-1-16-12288"), Some(12288));
        assert_eq!(integrity_rid_from_sid("S-1-5-21-1-2-3"), None);
        assert_eq!(integrity_rid_from_sid("nonsense"), None);
    }
}
