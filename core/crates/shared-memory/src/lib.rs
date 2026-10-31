//! 按名字打开别的进程创建的共享内存，映射为一段可读写的内存。
//!
//! Windows 上是命名文件映射（`OpenFileMappingW` 与 `MapViewOfFile`），其它平台是 POSIX 共享内存
//! （`shm_open` 与 `mmap`）。对映射的访问一律经原子操作：[`SharedMemory::u64_at`]、
//! [`SharedMemory::u32_at`]、[`SharedMemory::store`] 与 [`SharedMemory::load`]。
//!
//! 帧环（`frame-ring`）与音频环（`audio-ring`）都经它访问主进程创建的共享内存。本 crate 只打开，不创建；
//! 只用于测试的 [`Created`] 在 `testing` 特性下才有。

#[cfg(unix)]
mod unix;
#[cfg(windows)]
mod windows;

#[cfg(unix)]
use unix as sys;
#[cfg(windows)]
use windows as sys;

#[cfg(feature = "testing")]
pub use sys::Created;

use std::io;
use std::ptr::NonNull;
use std::sync::atomic::{AtomicU8, AtomicU32, AtomicU64, Ordering};

/// 一段映射到本进程的共享内存；drop 时解除映射，不删除共享内存本身。
#[derive(Debug)]
pub struct SharedMemory {
    ptr: NonNull<u8>,
    len: usize,
}

// SAFETY: 映射在整个生命期内有效，与线程无关；经 `&self` 的访问都是原子操作。
unsafe impl Send for SharedMemory {}
// SAFETY: 同上；多个线程经 `&self` 并发访问时都是原子操作，没有数据竞争。
unsafe impl Sync for SharedMemory {}

impl SharedMemory {
    /// 打开名为 `name` 的共享内存，映射它开头的 `len` 字节。
    ///
    /// # Errors
    ///
    /// `len` 为 0、名字不合法、共享内存不存在、比 `len` 小或映射失败时返回系统给出的错误。
    ///
    /// # Panics
    ///
    /// 映射的起点没有按 8 字节对齐时 panic（映射总是按页对齐的）。
    pub fn open(name: &str, len: usize) -> io::Result<Self> {
        if len == 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "cannot map zero bytes",
            ));
        }
        let ptr = sys::open(name, len)?;
        // 映射按页对齐；8 字节对齐是下面原子访问的前提。
        assert!(
            ptr.as_ptr().addr().is_multiple_of(8),
            "mapping is misaligned"
        );
        Ok(Self { ptr, len })
    }

    /// `offset` 起的 `len` 字节在映射内的起始地址；越界时 panic。
    fn range(&self, offset: usize, len: usize) -> *mut u8 {
        assert!(
            offset.checked_add(len).is_some_and(|end| end <= self.len),
            "{offset}+{len} is outside the {}-byte mapping",
            self.len
        );
        // SAFETY: 上面已核对 offset 不超过映射的长度。
        unsafe { self.ptr.as_ptr().add(offset) }
    }

    /// `offset` 处的 u64。
    ///
    /// # Panics
    ///
    /// `offset` 不是 8 的倍数或不在映射内时 panic。
    #[must_use]
    pub fn u64_at(&self, offset: usize) -> &AtomicU64 {
        assert!(offset.is_multiple_of(8), "u64 at {offset} is misaligned");
        let ptr = self.range(offset, 8);
        // SAFETY: 在映射内且 8 字节对齐（映射起点按页对齐）；映射活得与 `self` 一样久；
        // 本进程对映射的访问都是原子操作。
        unsafe { AtomicU64::from_ptr(ptr.cast()) }
    }

    /// `offset` 处的 u32。
    ///
    /// # Panics
    ///
    /// `offset` 不是 4 的倍数或不在映射内时 panic。
    #[must_use]
    pub fn u32_at(&self, offset: usize) -> &AtomicU32 {
        assert!(offset.is_multiple_of(4), "u32 at {offset} is misaligned");
        let ptr = self.range(offset, 4);
        // SAFETY: 同 `u64_at`，对齐为 4 字节。
        unsafe { AtomicU32::from_ptr(ptr.cast()) }
    }

    /// 以 Relaxed 的原子写把 `src` 复制到 `offset` 起；对齐的部分每次写 8 字节。
    ///
    /// # Panics
    ///
    /// 越出映射时 panic。
    pub fn store(&self, offset: usize, src: &[u8]) {
        let base = self.range(offset, src.len());
        let head = base.align_offset(8).min(src.len());
        let (head_bytes, rest) = src.split_at(head);
        let (words, tail_bytes) = rest.as_chunks::<8>();
        for (i, byte) in head_bytes.iter().enumerate() {
            // SAFETY: `base + i` 在上面核对过的范围内；单字节总是对齐的。
            unsafe { AtomicU8::from_ptr(base.add(i)) }.store(*byte, Ordering::Relaxed);
        }
        let mut at = head;
        for word in words {
            let word = u64::from_ne_bytes(*word);
            // SAFETY: `base + at` 在范围内，且 `base + head` 已对齐到 8 字节，`at - head` 是 8 的倍数。
            unsafe { AtomicU64::from_ptr(base.add(at).cast()) }.store(word, Ordering::Relaxed);
            at += 8;
        }
        for byte in tail_bytes {
            // SAFETY: `base + at` 在范围内。
            unsafe { AtomicU8::from_ptr(base.add(at)) }.store(*byte, Ordering::Relaxed);
            at += 1;
        }
    }

    /// 以 Relaxed 的原子读把 `offset` 起的 `dst.len()` 字节复制到 `dst`。
    ///
    /// # Panics
    ///
    /// 越出映射时 panic。
    pub fn load(&self, offset: usize, dst: &mut [u8]) {
        let base = self.range(offset, dst.len());
        let head = base.align_offset(8).min(dst.len());
        let (head_bytes, rest) = dst.split_at_mut(head);
        let (words, tail_bytes) = rest.as_chunks_mut::<8>();
        for (i, byte) in head_bytes.iter_mut().enumerate() {
            // SAFETY: 同 `store`。
            *byte = unsafe { AtomicU8::from_ptr(base.add(i)) }.load(Ordering::Relaxed);
        }
        let mut at = head;
        for word in words {
            // SAFETY: 同 `store`。
            *word = unsafe { AtomicU64::from_ptr(base.add(at).cast()) }
                .load(Ordering::Relaxed)
                .to_ne_bytes();
            at += 8;
        }
        for byte in tail_bytes {
            // SAFETY: 同 `store`。
            *byte = unsafe { AtomicU8::from_ptr(base.add(at)) }.load(Ordering::Relaxed);
            at += 1;
        }
    }
}

impl Drop for SharedMemory {
    fn drop(&mut self) {
        // SAFETY: `ptr` 与 `len` 来自 `sys::open` 的同一次映射，之后不再访问。
        unsafe { sys::close(self.ptr, self.len) };
    }
}
