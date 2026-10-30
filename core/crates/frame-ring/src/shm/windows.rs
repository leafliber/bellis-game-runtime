//! Windows 的命名文件映射：`OpenFileMappingW` 与 `MapViewOfFile`。
//!
//! 文件映射由主进程以只允许当前用户的 DACL 创建；这里只按名字打开。视图让映射对象保持存在，
//! 所以映射之后立即关闭句柄。

use std::io;
use std::ptr::NonNull;

use windows::Win32::Foundation::CloseHandle;
use windows::Win32::System::Memory::{
    FILE_MAP_READ, FILE_MAP_WRITE, MEMORY_MAPPED_VIEW_ADDRESS, MapViewOfFile, OpenFileMappingW,
    UnmapViewOfFile,
};
use windows::core::HSTRING;

fn os_error(error: &windows::core::Error) -> io::Error {
    // HRESULT_FROM_WIN32 把 Win32 错误码放在低 16 位。
    io::Error::from_raw_os_error(error.code().0 & 0xFFFF)
}

/// 打开名为 `name` 的文件映射并映射开头的 `len` 字节。
pub(super) fn open(name: &str, len: usize) -> io::Result<NonNull<u8>> {
    if name.contains('\0') {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "shared memory name contains NUL",
        ));
    }
    let access = FILE_MAP_READ | FILE_MAP_WRITE;
    // SAFETY: 名字是 HSTRING，由 windows crate 以 NUL 结尾传入；不继承句柄。
    let handle = unsafe { OpenFileMappingW(access.0, false, &HSTRING::from(name)) }
        .map_err(|error| os_error(&error))?;
    // SAFETY: `handle` 是上面打开的文件映射；`len` 不为 0。映射比 `len` 小时调用失败。
    let view = unsafe { MapViewOfFile(handle, access, 0, 0, len) };
    let mapped = NonNull::new(view.Value.cast::<u8>()).ok_or_else(io::Error::last_os_error);
    // SAFETY: `handle` 有效且之后不再使用；视图不依赖句柄保持打开。关闭失败时没有可做的补救。
    let _ = unsafe { CloseHandle(handle) };
    mapped
}

/// 解除映射。
///
/// # Safety
///
/// `ptr` 须来自 [`open`]，之后不再访问这段内存。
pub(super) unsafe fn close(ptr: NonNull<u8>, _len: usize) {
    let view = MEMORY_MAPPED_VIEW_ADDRESS {
        Value: ptr.as_ptr().cast(),
    };
    // SAFETY: 由调用方保证。解除映射失败时没有可做的补救，忽略。
    let _ = unsafe { UnmapViewOfFile(view) };
}

/// 测试用：以缺省的安全描述符创建一个文件映射，drop 时关闭句柄（最后一个句柄与视图关闭后映射消失）。
/// 生产中只有主进程创建文件映射，DACL 只允许当前用户。
#[cfg(test)]
#[derive(Debug)]
pub(crate) struct Created {
    handle: windows::Win32::Foundation::HANDLE,
}

#[cfg(test)]
impl Created {
    pub(crate) fn new(name: &str, len: usize) -> io::Result<Self> {
        use windows::Win32::Foundation::{
            ERROR_ALREADY_EXISTS, GetLastError, INVALID_HANDLE_VALUE,
        };
        use windows::Win32::System::Memory::{CreateFileMappingW, PAGE_READWRITE};

        let len = u64::try_from(len).map_err(io::Error::other)?;
        #[allow(clippy::cast_possible_truncation, reason = "拆成高低两个 32 位")]
        let (high, low) = ((len >> 32) as u32, len as u32);
        // SAFETY: 以页面文件为后备的命名映射；名字由 HSTRING 以 NUL 结尾传入。
        let handle = unsafe {
            CreateFileMappingW(
                INVALID_HANDLE_VALUE,
                None,
                PAGE_READWRITE,
                high,
                low,
                &HSTRING::from(name),
            )
        }
        .map_err(|error| os_error(&error))?;
        let created = Self { handle };
        // SAFETY: 只读取本线程的最后错误码。
        if unsafe { GetLastError() } == ERROR_ALREADY_EXISTS {
            return Err(io::Error::from(io::ErrorKind::AlreadyExists));
        }
        Ok(created)
    }
}

#[cfg(test)]
impl Drop for Created {
    fn drop(&mut self) {
        // SAFETY: `handle` 来自 `CreateFileMappingW`，之后不再使用。
        let _ = unsafe { CloseHandle(self.handle) };
    }
}
