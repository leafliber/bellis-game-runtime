//! 启动参数与凭据（`docs/modules/realtime-core.md` 的“启动与生命线”）。
//!
//! 凭据按 Bellis 的子进程约定（`ChildEnv`）从环境变量取得：通道地址、base64url 编码的会话密钥与连接时使用
//! 的角色。时限从命令行参数取得，缺省值的依据见模块文档。

use std::fmt;
use std::time::Duration;

use bellis_channel::ChannelCredentials;

/// 通道地址，Windows 上是命名管道，其它平台是 UDS 路径。
pub const ENV_CHANNEL_PATH: &str = "BELLIS_CHANNEL_PATH";
/// 会话密钥的 base64url 无填充编码。
pub const ENV_CHANNEL_KEY: &str = "BELLIS_CHANNEL_KEY";
/// 连接主进程时使用的角色。
pub const ENV_ROLE: &str = "BELLIS_ROLE";

/// 心跳超时的缺省值：主进程以 100ms 为周期发心跳，连续丢失约 5 次才算超时。
pub const DEFAULT_HEARTBEAT_TIMEOUT: Duration = Duration::from_millis(500);
/// `core.health` 的缺省周期。
pub const DEFAULT_HEALTH_INTERVAL: Duration = Duration::from_millis(1000);
/// 从发出 `core.hello` 到收到回应的时限，与通道握手的缺省时限相同。
pub const HELLO_TIMEOUT: Duration = Duration::from_millis(2000);

/// 命令行参数给出的时限。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Options {
    /// 当前控制产出者的心跳超过这么久没有到达，即释放全部输入并待命。
    pub heartbeat_timeout: Duration,
    /// 发 `core.health` 的周期。
    pub health_interval: Duration,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            heartbeat_timeout: DEFAULT_HEARTBEAT_TIMEOUT,
            health_interval: DEFAULT_HEALTH_INTERVAL,
        }
    }
}

/// 用法说明，参数不合法时打印。
pub const USAGE: &str =
    "usage: realtime-core [--heartbeat-timeout-ms <ms>] [--health-interval-ms <ms>]";

impl Options {
    /// 解析命令行参数（不含程序名）。
    ///
    /// # Errors
    ///
    /// 未知参数、缺少取值或取值不是正整数时返回说明。
    pub fn parse(args: impl IntoIterator<Item = String>) -> Result<Self, String> {
        let mut options = Self::default();
        let mut args = args.into_iter();
        while let Some(arg) = args.next() {
            let slot = match arg.as_str() {
                "--heartbeat-timeout-ms" => &mut options.heartbeat_timeout,
                "--health-interval-ms" => &mut options.health_interval,
                _ => return Err(format!("unknown argument {arg:?}")),
            };
            let value = args.next().ok_or_else(|| format!("{arg} needs a value"))?;
            *slot = match value.parse::<u64>() {
                Ok(millis) if millis > 0 => Duration::from_millis(millis),
                _ => return Err(format!("{arg} must be a positive integer, got {value:?}")),
            };
        }
        Ok(options)
    }
}

/// 主进程交来的连接信息。
pub struct Launch {
    /// 通道地址与会话密钥。
    pub credentials: ChannelCredentials,
    /// 连接时使用的角色。
    pub role: String,
}

impl Launch {
    /// 从环境变量读取连接信息；`get` 读取一个变量，测试中可替换。
    ///
    /// # Errors
    ///
    /// 缺少变量或密钥格式不正确时返回说明，说明中不含密钥。
    pub fn from_env(get: impl Fn(&str) -> Option<String>) -> Result<Self, String> {
        let required = |name: &str| {
            get(name)
                .filter(|v| !v.is_empty())
                .ok_or_else(|| format!("{name} is not set"))
        };
        let path = required(ENV_CHANNEL_PATH)?;
        let key = required(ENV_CHANNEL_KEY)?;
        let role = required(ENV_ROLE)?;
        let credentials = ChannelCredentials::decode(path, &key)
            .map_err(|error| format!("{ENV_CHANNEL_KEY} is invalid: {}", error.code().as_str()))?;
        Ok(Self { credentials, role })
    }
}

impl fmt::Debug for Launch {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Launch")
            .field("path", &self.credentials.path)
            .field("role", &self.role)
            .finish_non_exhaustive()
    }
}

#[cfg(test)]
mod tests {
    use super::{ENV_CHANNEL_KEY, ENV_CHANNEL_PATH, ENV_ROLE, Launch, Options};
    use bellis_channel::{create_session_key, encode_session_key};
    use std::collections::HashMap;
    use std::time::Duration;

    fn args(list: &[&str]) -> Vec<String> {
        list.iter().map(|&s| s.to_owned()).collect()
    }

    #[test]
    fn options_default_and_override() {
        assert_eq!(Options::parse(args(&[])), Ok(Options::default()));
        let parsed = Options::parse(args(&[
            "--health-interval-ms",
            "50",
            "--heartbeat-timeout-ms",
            "300",
        ]))
        .unwrap();
        assert_eq!(parsed.heartbeat_timeout, Duration::from_millis(300));
        assert_eq!(parsed.health_interval, Duration::from_millis(50));
    }

    #[test]
    fn options_reject_bad_arguments() {
        for bad in [
            &["--verbose"][..],
            &["--heartbeat-timeout-ms"],
            &["--heartbeat-timeout-ms", "0"],
            &["--health-interval-ms", "-1"],
            &["--health-interval-ms", "1.5"],
        ] {
            assert!(Options::parse(args(bad)).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn launch_reads_bellis_child_environment() {
        let key = create_session_key();
        let mut env = HashMap::from([
            (ENV_CHANNEL_PATH, "pipe-path".to_owned()),
            (ENV_CHANNEL_KEY, encode_session_key(&key)),
            (ENV_ROLE, "game-runtime.core".to_owned()),
        ]);
        let launch = Launch::from_env(|name| env.get(name).cloned()).unwrap();
        assert_eq!(launch.credentials.path, "pipe-path");
        assert_eq!(launch.credentials.key, key);
        assert_eq!(launch.role, "game-runtime.core");
        assert!(!format!("{launch:?}").contains(&encode_session_key(&key)));

        env.insert(ENV_CHANNEL_KEY, "not-a-key".to_owned());
        let error = Launch::from_env(|name| env.get(name).cloned()).unwrap_err();
        assert!(error.contains("address-invalid"), "{error}");
        env.remove(ENV_ROLE);
        assert!(Launch::from_env(|name| env.get(name).cloned()).is_err());
    }
}
