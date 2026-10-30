//! 启动参数与凭据（`docs/modules/realtime-core.md` 的“启动与生命线”与“输入代理”）。
//!
//! 凭据按 Bellis 的子进程约定（`ChildEnv`）从环境变量取得：通道地址、base64url 编码的会话密钥与连接时使用
//! 的角色；只用于测试的测试设备连接另用一组环境变量。时限与输入后端从命令行参数取得，缺省值的依据见
//! 模块文档。测试许可源只能与假输入后端一起启用，由 [`InputSetup::select`] 强制。

use std::fmt;
use std::time::Duration;

use bellis_channel::ChannelCredentials;

/// 通道地址，Windows 上是命名管道，其它平台是 UDS 路径。
pub const ENV_CHANNEL_PATH: &str = "BELLIS_CHANNEL_PATH";
/// 会话密钥的 base64url 无填充编码。
pub const ENV_CHANNEL_KEY: &str = "BELLIS_CHANNEL_KEY";
/// 连接主进程时使用的角色。
pub const ENV_ROLE: &str = "BELLIS_ROLE";
/// 测试设备连接的地址，只用于测试：只在 `--input-backend fake` 时给出。
pub const ENV_TEST_DEVICE_PATH: &str = "GAME_RUNTIME_TEST_DEVICE_PATH";
/// 测试设备连接的会话密钥（base64url 无填充），只用于测试。
pub const ENV_TEST_DEVICE_KEY: &str = "GAME_RUNTIME_TEST_DEVICE_KEY";

/// 心跳超时的缺省值：主进程以 100ms 为周期发心跳，连续丢失约 5 次才算超时。
pub const DEFAULT_HEARTBEAT_TIMEOUT: Duration = Duration::from_millis(500);
/// `core.health` 的缺省周期。
pub const DEFAULT_HEALTH_INTERVAL: Duration = Duration::from_millis(1000);
/// 动作允许的帧龄的缺省值：30 Hz 时约 15 帧没有更新，与心跳超时同一量级。
pub const DEFAULT_MAX_FRAME_AGE: Duration = Duration::from_millis(500);
/// 从发出 `core.hello` 到收到回应的时限，与通道握手的缺省时限相同。
pub const HELLO_TIMEOUT: Duration = Duration::from_millis(2000);

/// 输入后端。G0 只有只用于测试的假后端；真实后端（`SendInput`）在 G2 实现，现在 unsupported。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum InputBackendKind {
    /// 假输入后端：输入事件经测试设备连接交给测试一方。只用于测试。
    Fake,
}

/// 命令行参数给出的时限与输入后端。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Options {
    /// 当前控制产出者的心跳超过这么久没有到达，即释放全部输入并待命。
    pub heartbeat_timeout: Duration,
    /// 发 `core.health` 的周期。
    pub health_interval: Duration,
    /// 动作允许的帧龄：最新写入帧的帧龄超过它即不发输入并释放。
    pub max_frame_age: Duration,
    /// 输入后端；`None` 表示没有，动作块照常校验，但没有许可，从不发出输入。
    pub input_backend: Option<InputBackendKind>,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            heartbeat_timeout: DEFAULT_HEARTBEAT_TIMEOUT,
            health_interval: DEFAULT_HEALTH_INTERVAL,
            max_frame_age: DEFAULT_MAX_FRAME_AGE,
            input_backend: None,
        }
    }
}

/// 用法说明，参数不合法时打印。
pub const USAGE: &str = "usage: realtime-core [--heartbeat-timeout-ms <ms>] \
                         [--health-interval-ms <ms>] [--max-frame-age-ms <ms>] \
                         [--input-backend fake]";

impl Options {
    /// 解析命令行参数（不含程序名）。
    ///
    /// # Errors
    ///
    /// 未知参数、缺少取值、时限不是正整数或输入后端不受支持时返回说明。
    pub fn parse(args: impl IntoIterator<Item = String>) -> Result<Self, String> {
        let mut options = Self::default();
        let mut args = args.into_iter();
        while let Some(arg) = args.next() {
            let value = || format!("{arg} needs a value");
            if arg == "--input-backend" {
                let name = args.next().ok_or_else(value)?;
                options.input_backend = Some(match name.as_str() {
                    "fake" => InputBackendKind::Fake,
                    _ => {
                        return Err(format!(
                            "input backend {name:?} is unsupported: G0 only has the fake backend \
                             for tests; real input (SendInput) comes in G2"
                        ));
                    }
                });
                continue;
            }
            let slot = match arg.as_str() {
                "--heartbeat-timeout-ms" => &mut options.heartbeat_timeout,
                "--health-interval-ms" => &mut options.health_interval,
                "--max-frame-age-ms" => &mut options.max_frame_age,
                _ => return Err(format!("unknown argument {arg:?}")),
            };
            let value = args.next().ok_or_else(value)?;
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
    /// 测试设备连接的地址与密钥，只用于测试；没有给出时为 `None`。
    pub test_device: Option<ChannelCredentials>,
}

impl Launch {
    /// 从环境变量读取连接信息；`get` 读取一个变量，测试中可替换。
    ///
    /// # Errors
    ///
    /// 缺少变量、测试设备的地址与密钥只给出一个，或密钥格式不正确时返回说明，说明中不含密钥。
    pub fn from_env(get: impl Fn(&str) -> Option<String>) -> Result<Self, String> {
        let optional = |name: &str| get(name).filter(|v| !v.is_empty());
        let required = |name: &str| optional(name).ok_or_else(|| format!("{name} is not set"));
        let path = required(ENV_CHANNEL_PATH)?;
        let key = required(ENV_CHANNEL_KEY)?;
        let role = required(ENV_ROLE)?;
        let credentials = ChannelCredentials::decode(path, &key)
            .map_err(|error| format!("{ENV_CHANNEL_KEY} is invalid: {}", error.code().as_str()))?;
        let test_device = match (
            optional(ENV_TEST_DEVICE_PATH),
            optional(ENV_TEST_DEVICE_KEY),
        ) {
            (None, None) => None,
            (Some(path), Some(key)) => {
                Some(ChannelCredentials::decode(path, &key).map_err(|error| {
                    format!(
                        "{ENV_TEST_DEVICE_KEY} is invalid: {}",
                        error.code().as_str()
                    )
                })?)
            }
            _ => {
                return Err(format!(
                    "give both {ENV_TEST_DEVICE_PATH} and {ENV_TEST_DEVICE_KEY}, or neither"
                ));
            }
        };
        Ok(Self {
            credentials,
            role,
            test_device,
        })
    }
}

impl fmt::Debug for Launch {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Launch")
            .field("path", &self.credentials.path)
            .field("role", &self.role)
            .field(
                "test_device",
                &self.test_device.as_ref().map(|device| &device.path),
            )
            .finish_non_exhaustive()
    }
}

/// 输入后端与许可源的组合。测试许可源只能与假后端一起出现：它只在 [`InputSetup::Fake`] 里，
/// 由 [`InputSetup::select`] 构造。
pub enum InputSetup {
    /// 没有输入后端，也没有许可源：动作块照常校验与调度，但权威检查因没有许可从不通过，不发任何输入。
    None,
    /// 假输入后端与测试许可源，都经测试设备连接。只用于测试。
    Fake {
        /// 测试设备连接的地址与密钥。
        test_device: ChannelCredentials,
    },
}

impl InputSetup {
    /// 按 `--input-backend` 与测试设备的凭据选出组合。
    ///
    /// # Errors
    ///
    /// 假后端没有测试设备的凭据，或给了测试设备的凭据却不是假后端（测试许可源不能与其它后端或
    /// 没有后端同时启用）时返回说明。
    pub fn select(
        backend: Option<InputBackendKind>,
        test_device: Option<ChannelCredentials>,
    ) -> Result<Self, String> {
        match (backend, test_device) {
            (None, None) => Ok(Self::None),
            (Some(InputBackendKind::Fake), Some(test_device)) => Ok(Self::Fake { test_device }),
            (Some(InputBackendKind::Fake), None) => Err(format!(
                "--input-backend fake needs the test device: set {ENV_TEST_DEVICE_PATH} and \
                 {ENV_TEST_DEVICE_KEY}"
            )),
            (None, Some(_)) => Err(format!(
                "the test lease source ({ENV_TEST_DEVICE_PATH}) is only available with \
                 --input-backend fake"
            )),
        }
    }
}

impl fmt::Debug for InputSetup {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::None => f.write_str("InputSetup::None"),
            Self::Fake { test_device } => f
                .debug_struct("InputSetup::Fake")
                .field("test_device", &test_device.path)
                .finish_non_exhaustive(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{
        DEFAULT_MAX_FRAME_AGE, ENV_CHANNEL_KEY, ENV_CHANNEL_PATH, ENV_ROLE, ENV_TEST_DEVICE_KEY,
        ENV_TEST_DEVICE_PATH, InputBackendKind, InputSetup, Launch, Options,
    };
    use bellis_channel::{ChannelCredentials, create_session_key, encode_session_key};
    use std::collections::HashMap;
    use std::time::Duration;

    fn args(list: &[&str]) -> Vec<String> {
        list.iter().map(|&s| s.to_owned()).collect()
    }

    #[test]
    fn options_default_and_override() {
        let defaults = Options::parse(args(&[])).unwrap();
        assert_eq!(defaults, Options::default());
        assert_eq!(defaults.max_frame_age, DEFAULT_MAX_FRAME_AGE);
        assert_eq!(defaults.input_backend, None);
        let parsed = Options::parse(args(&[
            "--health-interval-ms",
            "50",
            "--heartbeat-timeout-ms",
            "300",
            "--max-frame-age-ms",
            "120",
            "--input-backend",
            "fake",
        ]))
        .unwrap();
        assert_eq!(parsed.heartbeat_timeout, Duration::from_millis(300));
        assert_eq!(parsed.health_interval, Duration::from_millis(50));
        assert_eq!(parsed.max_frame_age, Duration::from_millis(120));
        assert_eq!(parsed.input_backend, Some(InputBackendKind::Fake));
    }

    #[test]
    fn options_reject_bad_arguments() {
        for bad in [
            &["--verbose"][..],
            &["--heartbeat-timeout-ms"],
            &["--heartbeat-timeout-ms", "0"],
            &["--health-interval-ms", "-1"],
            &["--health-interval-ms", "1.5"],
            &["--max-frame-age-ms", "0"],
            &["--input-backend"],
        ] {
            assert!(Options::parse(args(bad)).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn only_the_fake_input_backend_exists() {
        for name in ["sendinput", "real", "FAKE", ""] {
            let error = Options::parse(args(&["--input-backend", name])).unwrap_err();
            assert!(error.contains("unsupported"), "{error}");
        }
    }

    fn device() -> ChannelCredentials {
        ChannelCredentials::decode("device-path", &encode_session_key(&create_session_key()))
            .unwrap()
    }

    /// 门禁：测试许可源只能与假后端一起启用，假后端也离不开测试设备。
    #[test]
    fn the_test_lease_source_only_comes_with_the_fake_backend() {
        assert!(matches!(
            InputSetup::select(None, None),
            Ok(InputSetup::None)
        ));
        match InputSetup::select(Some(InputBackendKind::Fake), Some(device())) {
            Ok(InputSetup::Fake { test_device }) => assert_eq!(test_device.path, "device-path"),
            other => panic!("{other:?}"),
        }
        let error = InputSetup::select(None, Some(device())).unwrap_err();
        assert!(
            error.contains("only available with --input-backend fake"),
            "{error}"
        );
        let error = InputSetup::select(Some(InputBackendKind::Fake), None).unwrap_err();
        assert!(error.contains(ENV_TEST_DEVICE_PATH), "{error}");
    }

    #[test]
    fn launch_reads_the_test_device_only_as_a_pair() {
        let key = encode_session_key(&create_session_key());
        let mut env = HashMap::from([
            (ENV_CHANNEL_PATH, "pipe-path".to_owned()),
            (ENV_CHANNEL_KEY, key.clone()),
            (ENV_ROLE, "game-runtime.core".to_owned()),
        ]);
        assert!(
            Launch::from_env(|name| env.get(name).cloned())
                .unwrap()
                .test_device
                .is_none()
        );
        env.insert(ENV_TEST_DEVICE_PATH, "device-path".to_owned());
        let error = Launch::from_env(|name| env.get(name).cloned()).unwrap_err();
        assert!(error.contains("or neither"), "{error}");
        env.insert(ENV_TEST_DEVICE_KEY, key.clone());
        let launch = Launch::from_env(|name| env.get(name).cloned()).unwrap();
        assert_eq!(launch.test_device.unwrap().path, "device-path");
        env.insert(ENV_TEST_DEVICE_KEY, "bad".to_owned());
        let error = Launch::from_env(|name| env.get(name).cloned()).unwrap_err();
        assert!(error.contains(ENV_TEST_DEVICE_KEY), "{error}");
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
