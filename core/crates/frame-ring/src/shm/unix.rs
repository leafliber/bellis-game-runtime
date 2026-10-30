//! POSIX 共享内存：`shm_open` 与 `mmap`。
//!
//! 只按名字打开，不靠文件权限位做安全判断；共享内存由主进程以 `O_CREAT | O_EXCL` 创建，权限位只是
//! 缺省的 0600。

use std::ffi::CString;
use std::io;
use std::ptr::NonNull;

fn c_name(name: &str) -> io::Result<CString> {
    CString::new(name).map_err(|_| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "shared memory name contains NUL",
        )
    })
}

/// 打开名为 `name` 的共享内存并映射开头的 `len` 字节。
pub(super) fn open(name: &str, len: usize) -> io::Result<NonNull<u8>> {
    let name = c_name(name)?;
    // SAFETY: `name` 是以 NUL 结尾的字符串；只打开已有的共享内存。
    let fd = unsafe { libc::shm_open(name.as_ptr(), libc::O_RDWR, 0) };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let mapped = map(fd, len);
    // SAFETY: `fd` 是上面打开的描述符；映射不依赖描述符保持打开。
    unsafe { libc::close(fd) };
    mapped
}

fn map(fd: libc::c_int, len: usize) -> io::Result<NonNull<u8>> {
    // SAFETY: `stat` 是纯输出参数，全零是合法的初值。
    let mut stat: libc::stat = unsafe { std::mem::zeroed() };
    // SAFETY: `fd` 有效，`stat` 可写。
    if unsafe { libc::fstat(fd, &raw mut stat) } != 0 {
        return Err(io::Error::last_os_error());
    }
    let size = u64::try_from(stat.st_size).unwrap_or(0);
    if size < len as u64 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("shared memory holds {size} bytes, fewer than {len}"),
        ));
    }
    // SAFETY: 由系统选地址；`len` 不为 0 且不超过共享内存的大小。
    let ptr = unsafe {
        libc::mmap(
            std::ptr::null_mut(),
            len,
            libc::PROT_READ | libc::PROT_WRITE,
            libc::MAP_SHARED,
            fd,
            0,
        )
    };
    if ptr == libc::MAP_FAILED {
        return Err(io::Error::last_os_error());
    }
    NonNull::new(ptr.cast()).ok_or_else(|| io::Error::other("mmap returned null"))
}

/// 解除映射。
///
/// # Safety
///
/// `ptr` 与 `len` 须来自同一次 [`open`]，之后不再访问这段内存。
pub(super) unsafe fn close(ptr: NonNull<u8>, len: usize) {
    // SAFETY: 由调用方保证。解除映射失败时没有可做的补救，忽略。
    unsafe { libc::munmap(ptr.as_ptr().cast(), len) };
}

/// 测试用：创建一块共享内存，drop 时删除它的名字。生产中只有主进程创建共享内存。
#[cfg(test)]
#[derive(Debug)]
pub(crate) struct Created {
    name: CString,
}

#[cfg(test)]
impl Created {
    pub(crate) fn new(name: &str, len: usize) -> io::Result<Self> {
        let name = c_name(name)?;
        // SAFETY: `name` 是以 NUL 结尾的字符串。
        let fd = unsafe {
            libc::shm_open(
                name.as_ptr(),
                libc::O_CREAT | libc::O_EXCL | libc::O_RDWR,
                0o600,
            )
        };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        let created = Self { name };
        let resized = match libc::off_t::try_from(len) {
            Ok(size) => {
                // SAFETY: `fd` 是上面创建的描述符。
                if unsafe { libc::ftruncate(fd, size) } == 0 {
                    Ok(())
                } else {
                    Err(io::Error::last_os_error())
                }
            }
            Err(error) => Err(io::Error::other(error)),
        };
        // SAFETY: `fd` 是上面创建的描述符，之后不再使用。
        unsafe { libc::close(fd) };
        resized.map(|()| created)
    }
}

#[cfg(test)]
impl Drop for Created {
    fn drop(&mut self) {
        // SAFETY: `name` 是以 NUL 结尾的字符串。
        unsafe { libc::shm_unlink(self.name.as_ptr()) };
    }
}
