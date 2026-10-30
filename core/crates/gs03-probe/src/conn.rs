//! 非提权一侧交来的连接信息：通道地址与密钥、连接角色、帧环参数、要写的帧数。
//!
//! JSON 的形状与 Python 一侧 `gs03_probe` 写进凭据管理器（或经命令行传入）的完全一致。

use core_protocol::messages::FrameRingAttachParams;
use serde::Deserialize;

/// 连接信息的版本，Python 与 Rust 两侧同步改动。
pub const CONN_INFO_VERSION: u32 = 1;

/// 非提权一侧交来的全部连接信息。
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ConnInfo {
    /// 版本号，必须等于 [`CONN_INFO_VERSION`]。
    pub v: u32,
    /// 通道地址：Windows 上是命名管道，其它平台是 UDS 路径。
    pub channel_path: String,
    /// base64url 无填充编码的会话密钥。
    pub channel_key: String,
    /// 连接时使用的角色。
    pub role: String,
    /// 帧环挂接参数，与 `frame_ring.attach` 相同。
    pub frame_ring: FrameRingAttachParams,
    /// 要写入帧环的帧数。
    pub frames: u64,
}

impl ConnInfo {
    /// 解析 JSON，并核对版本。
    ///
    /// # Errors
    ///
    /// JSON 不合法或版本不符时返回说明。
    pub fn parse(text: &str) -> Result<Self, String> {
        let info: Self = serde_json::from_str(text)
            .map_err(|error| format!("连接信息不是合法 JSON：{error}"))?;
        if info.v != CONN_INFO_VERSION {
            return Err(format!(
                "连接信息版本为 {}，本探针支持 {CONN_INFO_VERSION}",
                info.v
            ));
        }
        Ok(info)
    }
}

#[cfg(test)]
mod tests {
    use super::{CONN_INFO_VERSION, ConnInfo};

    fn sample() -> String {
        format!(
            r#"{{"v":{CONN_INFO_VERSION},"channel_path":"\\\\.\\pipe\\bellis-x","channel_key":"{key}",
            "role":"game-runtime.core","frames":3,"frame_ring":{{"name":"Local\\bellis-frames-x",
            "size_bytes":4096,"layout_version":1,"slot_count":3,"slot_stride":1024,
            "max_width":16,"max_height":12}}}}"#,
            key = "A".repeat(43),
        )
    }

    #[test]
    fn parses_a_well_formed_blob() {
        let info = ConnInfo::parse(&sample()).expect("should parse");
        assert_eq!(info.role, "game-runtime.core");
        assert_eq!(info.frames, 3);
        assert_eq!(info.frame_ring.max_width, 16);
    }

    #[test]
    fn rejects_a_wrong_version() {
        let bad = sample().replace(
            &format!("\"v\":{CONN_INFO_VERSION}"),
            &format!("\"v\":{}", CONN_INFO_VERSION + 1),
        );
        assert!(ConnInfo::parse(&bad).is_err());
    }

    #[test]
    fn rejects_unknown_fields() {
        let bad = sample().replace("\"frames\":3", "\"frames\":3,\"extra\":true");
        assert!(ConnInfo::parse(&bad).is_err());
    }
}
