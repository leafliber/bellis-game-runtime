//! 打开、写入与读取音频环的错误。

use std::fmt;
use std::io;

/// 打开音频环失败。核协议中 `audio_ring.attach` 的这些错误一律以 -32005 `audio_ring_mismatch` 回应。
#[derive(Debug)]
pub enum AudioRingError {
    /// 参数本身不合音频环的布局规则：布局版本不是本实现的、块槽数少于 2、格式不支持、块槽跨度没有对齐或
    /// 放不下一整块、`size_bytes` 放不下全部块槽，或名字不合法。
    InvalidParams(String),
    /// 打不开或映射不了共享内存，包括共享内存比 `size_bytes` 小。
    Open {
        /// 共享内存名。
        name: String,
        /// 系统给出的错误。
        source: io::Error,
    },
    /// 音频环头部的字段与参数不符。
    Mismatch {
        /// 字段名，与 `core_protocol::audio_ring::AudioRingHeader` 的字段同名。
        field: &'static str,
        /// 头部中的值。
        header: u64,
        /// 参数要求的值。
        expected: u64,
    },
}

impl fmt::Display for AudioRingError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidParams(reason) => write!(f, "invalid audio ring parameters: {reason}"),
            Self::Open { name, source } => {
                write!(f, "cannot open shared memory {name:?}: {source}")
            }
            Self::Mismatch {
                field,
                header,
                expected,
            } => write!(
                f,
                "audio ring header {field} is {header}, the parameters say {expected}"
            ),
        }
    }
}

impl std::error::Error for AudioRingError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Open { source, .. } => Some(source),
            _ => None,
        }
    }
}

/// 写包失败；音频环不受影响，可以继续写下一个包。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum WriteError {
    /// 包本身不合规则：没有采样点、字节数不是 `sample_count × channels × 4`，或块号超出 u64。
    InvalidPacket(String),
}

impl fmt::Display for WriteError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidPacket(reason) => write!(f, "invalid audio packet: {reason}"),
        }
    }
}

impl std::error::Error for WriteError {}

/// 读块失败：写方写出了不合布局的块头部。读到撕裂或已被覆盖的块不是错误，见 [`crate::BlockRead`]。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ReadError {
    /// 块头部在 seqlock 下稳定，内容却不合布局（采样点数为 0 或超过块容量、标志有未定义的位）。
    Corrupt(String),
}

impl fmt::Display for ReadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Corrupt(reason) => write!(f, "corrupt audio ring block: {reason}"),
        }
    }
}

impl std::error::Error for ReadError {}
