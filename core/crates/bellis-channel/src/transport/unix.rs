//! POSIX UDS 的客户端字节流。

use std::io::{self, Read, Write};
use std::net::Shutdown;
use std::os::unix::net::UnixStream;
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use super::{Transport, remaining, shut_down, timed_out};

pub struct UnixTransport {
    stream: UnixStream,
    // 读超时是 socket 选项，只允许一个读者，以免两个读者互相改写。
    read_lock: Mutex<()>,
    write_lock: Mutex<()>,
    closed: AtomicBool,
}

impl UnixTransport {
    /// # Errors
    ///
    /// 连不上时返回 IO 错误。
    pub fn connect(path: &str) -> io::Result<Self> {
        UnixStream::connect(path).map(Self::new)
    }

    #[must_use]
    pub fn new(stream: UnixStream) -> Self {
        Self {
            stream,
            read_lock: Mutex::new(()),
            write_lock: Mutex::new(()),
            closed: AtomicBool::new(false),
        }
    }
}

impl Transport for UnixTransport {
    fn read(&self, buf: &mut [u8], deadline: Option<Instant>) -> io::Result<usize> {
        let _guard = self
            .read_lock
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        loop {
            if self.closed.load(Ordering::Acquire) {
                return Err(shut_down());
            }
            let timeout = match remaining(deadline) {
                Some(left) if left.is_zero() => return Err(timed_out()),
                // set_read_timeout 不接受 0。
                Some(left) => Some(left.max(Duration::from_millis(1))),
                None => None,
            };
            self.stream.set_read_timeout(timeout)?;
            match (&self.stream).read(buf) {
                // 套接字的读超时可能比截止时间早一点返回；没到截止时间就接着等。
                Err(error)
                    if matches!(
                        error.kind(),
                        io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
                    ) => {}
                Ok(0) if self.closed.load(Ordering::Acquire) => return Err(shut_down()),
                result => return result,
            }
        }
    }

    fn write_all(&self, buf: &[u8]) -> io::Result<()> {
        let _guard = self
            .write_lock
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if self.closed.load(Ordering::Acquire) {
            return Err(shut_down());
        }
        (&self.stream).write_all(buf)
    }

    fn shutdown(&self) {
        self.closed.store(true, Ordering::Release);
        // 唤醒阻塞中的 read（返回 0）与 write（返回 EPIPE）。
        let _ = self.stream.shutdown(Shutdown::Both);
    }
}
