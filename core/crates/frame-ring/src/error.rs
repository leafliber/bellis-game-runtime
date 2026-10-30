//! 打开、写入与读取帧环的错误。

use std::fmt;
use std::io;

/// 打开帧环失败。核协议中 `frame_ring.attach` 的这些错误一律以 -32002 `frame_ring_mismatch` 回应。
#[derive(Debug)]
pub enum FrameRingError {
    /// 参数本身不合帧环的布局规则：布局版本不是本实现的、槽数少于 2、槽跨度没有对齐或放不下最大的帧、
    /// `size_bytes` 放不下全部槽，或名字不合法。
    InvalidParams(String),
    /// 打不开或映射不了共享内存，包括共享内存比 `size_bytes` 小。
    Open {
        /// 共享内存名。
        name: String,
        /// 系统给出的错误。
        source: io::Error,
    },
    /// 帧环头部的字段与参数不符。
    Mismatch {
        /// 字段名，与 `core_protocol::frame_ring::FrameRingHeader` 的字段同名。
        field: &'static str,
        /// 头部中的值。
        header: u64,
        /// 参数要求的值。
        expected: u64,
    },
}

impl fmt::Display for FrameRingError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidParams(reason) => write!(f, "invalid frame ring parameters: {reason}"),
            Self::Open { name, source } => {
                write!(f, "cannot open shared memory {name:?}: {source}")
            }
            Self::Mismatch {
                field,
                header,
                expected,
            } => write!(
                f,
                "frame ring header {field} is {header}, the parameters say {expected}"
            ),
        }
    }
}

impl std::error::Error for FrameRingError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Open { source, .. } => Some(source),
            _ => None,
        }
    }
}

/// 写帧失败；帧环不受影响，可以继续写下一帧。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum WriteError {
    /// 帧比帧环的最大尺寸大。
    TooLarge {
        /// 帧宽。
        width: u32,
        /// 帧高。
        height: u32,
        /// 帧环的最大宽度。
        max_width: u32,
        /// 帧环的最大高度。
        max_height: u32,
    },
    /// 帧本身不合规则：帧号为 0、宽或高为 0、行跨度小于 `width * 4`、像素少于 `stride * height`，
    /// 或帧号超出 u64。
    InvalidFrame(&'static str),
}

impl fmt::Display for WriteError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TooLarge {
                width,
                height,
                max_width,
                max_height,
            } => write!(
                f,
                "a {width}x{height} frame does not fit a {max_width}x{max_height} frame ring"
            ),
            Self::InvalidFrame(reason) => write!(f, "invalid frame: {reason}"),
        }
    }
}

impl std::error::Error for WriteError {}

/// 读帧失败：写方写出了不合布局的槽头部。读到撕裂的帧不是错误，见 [`crate::ReadOutcome::Torn`]。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ReadError {
    /// 槽头部在 seqlock 下稳定，内容却不合布局（尺寸超出、行跨度不对或像素格式未知）。
    Corrupt(String),
}

impl fmt::Display for ReadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Corrupt(reason) => write!(f, "corrupt frame ring slot: {reason}"),
        }
    }
}

impl std::error::Error for ReadError {}
