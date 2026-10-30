//! 发送队列：在锁内或在不能阻塞的线程上产生的通知先放进队列，由专门的发送线程写出。
//!
//! 释放路径与 `RpcPeer` 读线程上的处理函数因此从不等网络写入：对端卡住时只有发送线程阻塞。
//! 用于核连接上调度线程与测试设备连接发出的通知，以及假输入后端交给测试一方的事件。

use std::collections::VecDeque;
use std::sync::{Condvar, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant};

use serde_json::{Map, Value};

/// 队列中的一条通知：方法名与 params。
type Item = (&'static str, Map<String, Value>);

#[derive(Default)]
struct Inner {
    items: VecDeque<Item>,
    /// 发送线程正在写一条。
    sending: bool,
    closed: bool,
}

/// 多生产者、单个发送线程的通知队列，不设上限。
#[derive(Default)]
pub(crate) struct SendQueue {
    inner: Mutex<Inner>,
    changed: Condvar,
}

impl SendQueue {
    fn lock(&self) -> MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// 放进一条通知，立即返回。关闭后放进的丢弃。
    pub(crate) fn push(&self, method: &'static str, params: Map<String, Value>) {
        let mut inner = self.lock();
        if inner.closed {
            return;
        }
        inner.items.push_back((method, params));
        drop(inner);
        self.changed.notify_all();
    }

    /// 发送线程的循环：逐条交给 `send`，直到关闭。写失败（连接已断开）由 `send` 自己记录，
    /// 之后的仍逐条交给它，由它快速失败。
    pub(crate) fn run(&self, mut send: impl FnMut(&'static str, &Map<String, Value>)) {
        let mut inner = self.lock();
        loop {
            if let Some((method, params)) = inner.items.pop_front() {
                inner.sending = true;
                drop(inner);
                send(method, &params);
                inner = self.lock();
                inner.sending = false;
                self.changed.notify_all();
                continue;
            }
            if inner.closed {
                return;
            }
            inner = self
                .changed
                .wait(inner)
                .unwrap_or_else(PoisonError::into_inner);
        }
    }

    /// 等到此前放进的全部写出（或交给 `send` 失败），至多 `timeout`；返回是否已全部写出。
    pub(crate) fn flush(&self, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        let mut inner = self.lock();
        while !inner.items.is_empty() || inner.sending {
            let now = Instant::now();
            if now >= deadline {
                return false;
            }
            inner = self
                .changed
                .wait_timeout(inner, deadline - now)
                .unwrap_or_else(PoisonError::into_inner)
                .0;
        }
        true
    }

    /// 关闭并丢弃队列里剩下的通知，发送线程写完正在写的一条就结束；此后放进的也丢弃。要写完的先
    /// [`Self::flush`]。
    pub(crate) fn abandon(&self) {
        let mut inner = self.lock();
        inner.closed = true;
        inner.items.clear();
        drop(inner);
        self.changed.notify_all();
    }
}

#[cfg(test)]
mod tests {
    use super::SendQueue;
    use serde_json::Map;
    use std::sync::mpsc;
    use std::sync::{Arc, Barrier};
    use std::thread;
    use std::time::Duration;

    #[test]
    fn items_are_sent_in_order_and_flush_waits_for_them() {
        let queue = Arc::new(SendQueue::default());
        let (sent, received) = mpsc::channel();
        let gate = Arc::new(Barrier::new(2));
        let sender = thread::spawn({
            let (queue, gate) = (Arc::clone(&queue), Arc::clone(&gate));
            move || {
                gate.wait();
                queue.run(|method, _| sent.send(method).unwrap());
            }
        });
        queue.push("a", Map::new());
        queue.push("b", Map::new());
        // 发送线程还没开始：等不到全部写出。
        assert!(!queue.flush(Duration::from_millis(20)));
        gate.wait();
        assert!(queue.flush(Duration::from_secs(5)));
        assert_eq!(received.try_iter().collect::<Vec<_>>(), ["a", "b"]);
        queue.abandon();
        queue.push("after close", Map::new());
        sender.join().unwrap();
        assert!(received.try_recv().is_err());
    }

    #[test]
    fn abandoning_drops_what_is_left() {
        let queue = SendQueue::default();
        queue.push("a", Map::new());
        queue.abandon();
        let mut sent = Vec::new();
        queue.run(|method, _| sent.push(method));
        assert_eq!(sent, Vec::<&str>::new());
        assert!(queue.flush(Duration::ZERO));
    }
}
