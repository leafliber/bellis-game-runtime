//! 从 Windows 凭据管理器读取一条通用凭据（GS-03 的第 3 项）。
//!
//! 命名与编码同 Bellis 的 `SecretStore`（`@napi-rs/keyring` + `windows-native-keyring-store`）：
//! 服务 `bellis`、条目名 `<name>`，目标名为 `<name>.<service>`（分隔符 `.`，无前后缀），
//! 值以 UTF-16LE 存在 `CredentialBlob` 里。这里只读（`CredReadW`），写入由非提权一侧的 Python 做。
//! 只在 Windows 上实现。

/// 构造凭据目标名，同 keyring-rs 的默认 `<user>.<service>` 约定（这里 user 即条目名）。
#[must_use]
pub fn target_name(service: &str, name: &str) -> String {
    format!("{name}.{service}")
}

#[cfg(windows)]
pub use imp::read;

#[cfg(not(windows))]
pub fn read(_service: &str, _name: &str) -> std::io::Result<String> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "凭据管理器只在 Windows 上可用（unsupported）",
    ))
}

#[cfg(windows)]
mod imp {
    use std::io;

    use windows::Win32::Security::Credentials::{
        CRED_TYPE_GENERIC, CREDENTIALW, CredFree, CredReadW,
    };
    use windows::core::HSTRING;

    use super::target_name;

    /// 读取服务 `service`、条目名 `name` 的凭据值。
    ///
    /// # Errors
    ///
    /// 凭据不存在（`ERROR_NOT_FOUND`）或值不是合法的 UTF-16LE 时返回错误。
    pub fn read(service: &str, name: &str) -> io::Result<String> {
        let target = HSTRING::from(target_name(service, name));
        let mut credential: *mut CREDENTIALW = std::ptr::null_mut();
        // SAFETY: `target` 以 NUL 结尾；成功时把指针写入 `credential`，稍后用 `CredFree` 释放。
        unsafe { CredReadW(&target, CRED_TYPE_GENERIC, None, &raw mut credential) }
            .map_err(|error| win_error(&error))?;
        if credential.is_null() {
            return Err(io::Error::other("CredReadW 返回空凭据"));
        }
        // SAFETY: `credential` 由 `CredReadW` 成功写入，在 `CredFree` 之前有效。
        let value = unsafe { blob_to_string(&*credential) };
        // SAFETY: `credential` 来自 `CredReadW`，之后不再使用。
        unsafe { CredFree(credential.cast()) };
        value
    }

    /// `CredentialBlob` 是 UTF-16LE 字节（keyring-rs 的 `password encoded as UTF-16`），解码成字符串。
    ///
    /// # Safety
    ///
    /// `credential` 须是 `CredReadW` 成功返回、尚未释放的凭据。
    unsafe fn blob_to_string(credential: &CREDENTIALW) -> io::Result<String> {
        let len = credential.CredentialBlobSize as usize;
        if credential.CredentialBlob.is_null() || len == 0 {
            return Ok(String::new());
        }
        if !len.is_multiple_of(2) {
            return Err(io::Error::other("凭据值的字节数为奇数，不是 UTF-16"));
        }
        // SAFETY: 调用方保证 `CredentialBlob` 指向 `len` 字节，在释放前有效。
        let bytes = unsafe { std::slice::from_raw_parts(credential.CredentialBlob, len) };
        let (pairs, _rest) = bytes.as_chunks::<2>();
        let units: Vec<u16> = pairs
            .iter()
            .map(|&[lo, hi]| u16::from_le_bytes([lo, hi]))
            .collect();
        String::from_utf16(&units).map_err(io::Error::other)
    }

    fn win_error(error: &windows::core::Error) -> io::Error {
        io::Error::from_raw_os_error(error.code().0 & 0xFFFF)
    }
}
