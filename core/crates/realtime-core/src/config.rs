//! 启动参数与凭据（`docs/modules/realtime-core.md` 的“启动与生命线”与“输入代理”）。
//!
//! 凭据按 Bellis 的子进程约定（`ChildEnv`）从环境变量取得：通道地址、base64url 编码的会话密钥与连接时使用
//! 的角色；操作员许可连接与只用于测试的测试设备连接各另用一组环境变量。时限与输入后端从命令行参数取得，
//! 缺省值的依据见模块文档。输入后端与许可源的门禁由 [`InputSetup::select`] 强制：许可源只有一个，测试许可源
//! 只能与假输入后端一起、且只在没有操作员许可连接时启用。

use std::time::Duration;

use bellis_channel::ChannelCredentials;

use crate::sendinput::Combo;

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
/// 操作员许可连接的地址：独立运行中操作员控制台的通道地址（GR-36）。
pub const ENV_OPERATOR_PATH: &str = "GAME_RUNTIME_OPERATOR_PATH";
/// 操作员许可连接的会话密钥（base64url 无填充）。
pub const ENV_OPERATOR_KEY: &str = "GAME_RUNTIME_OPERATOR_KEY";

/// 心跳超时的缺省值：主进程以 100ms 为周期发心跳，连续丢失约 5 次才算超时。
pub const DEFAULT_HEARTBEAT_TIMEOUT: Duration = Duration::from_millis(500);
/// `core.health` 的缺省周期。
pub const DEFAULT_HEALTH_INTERVAL: Duration = Duration::from_millis(1000);
/// 动作允许的帧龄的缺省值：30 Hz 时约 15 帧没有更新，与心跳超时同一量级。
pub const DEFAULT_MAX_FRAME_AGE: Duration = Duration::from_millis(500);
/// 从发出 `core.hello` 到收到回应的时限，与通道握手的缺省时限相同。
pub const HELLO_TIMEOUT: Duration = Duration::from_millis(2000);

/// 输入后端。假后端只用于测试；真实后端（`SendInput`）只在 Windows 上，只与生产许可源一起选出。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum InputBackendKind {
    /// 假输入后端：输入事件经测试设备连接交给测试一方。只用于测试。
    Fake,
    /// 真实后端：用 `SendInput` 注入。只在 Windows 上、只与生产许可源一起。
    SendInput,
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
    /// 急停组合键；只在给出 `--emergency-hotkey` 时为 `Some`，只与真实后端一起给出。
    pub emergency_hotkey: Option<Combo>,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            heartbeat_timeout: DEFAULT_HEARTBEAT_TIMEOUT,
            health_interval: DEFAULT_HEALTH_INTERVAL,
            max_frame_age: DEFAULT_MAX_FRAME_AGE,
            input_backend: None,
            emergency_hotkey: None,
        }
    }
}

/// 用法说明，参数不合法时打印。
pub const USAGE: &str = "usage: realtime-core [--heartbeat-timeout-ms <ms>] \
                         [--health-interval-ms <ms>] [--max-frame-age-ms <ms>] \
                         [--input-backend fake|sendinput] [--emergency-hotkey <combo>]";

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
                    "sendinput" => InputBackendKind::SendInput,
                    _ => {
                        return Err(format!(
                            "input backend {name:?} is unsupported: use fake (tests only) or \
                             sendinput (real input, Windows only)"
                        ));
                    }
                });
                continue;
            }
            if arg == "--emergency-hotkey" {
                let combo = args.next().ok_or_else(value)?;
                options.emergency_hotkey = Some(Combo::parse(&combo)?);
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

/// 主进程交来的连接信息。`Debug` 不含密钥（[`ChannelCredentials`] 的 `Debug` 已隐去）。
#[derive(Debug)]
pub struct Launch {
    /// 通道地址与会话密钥。
    pub credentials: ChannelCredentials,
    /// 连接时使用的角色。
    pub role: String,
    /// 测试设备连接的地址与密钥，只用于测试；没有给出时为 `None`。
    pub test_device: Option<ChannelCredentials>,
    /// 操作员许可连接的地址与密钥；没有给出时为 `None`。
    pub operator: Option<ChannelCredentials>,
}

impl Launch {
    /// 从环境变量读取连接信息；`get` 读取一个变量，测试中可替换。
    ///
    /// # Errors
    ///
    /// 缺少变量、测试设备或操作员许可连接的地址与密钥只给出一个，或密钥格式不正确时返回说明，说明中
    /// 不含密钥。
    pub fn from_env(get: impl Fn(&str) -> Option<String>) -> Result<Self, String> {
        let optional = |name: &str| get(name).filter(|v| !v.is_empty());
        let required = |name: &str| optional(name).ok_or_else(|| format!("{name} is not set"));
        let path = required(ENV_CHANNEL_PATH)?;
        let key = required(ENV_CHANNEL_KEY)?;
        let role = required(ENV_ROLE)?;
        let credentials = ChannelCredentials::decode(path, &key)
            .map_err(|error| format!("{ENV_CHANNEL_KEY} is invalid: {}", error.code().as_str()))?;
        let pair = |path_name: &str, key_name: &str| match (optional(path_name), optional(key_name))
        {
            (None, None) => Ok(None),
            (Some(path), Some(key)) => ChannelCredentials::decode(path, &key)
                .map(Some)
                .map_err(|error| format!("{key_name} is invalid: {}", error.code().as_str())),
            _ => Err(format!("give both {path_name} and {key_name}, or neither")),
        };
        let test_device = pair(ENV_TEST_DEVICE_PATH, ENV_TEST_DEVICE_KEY)?;
        let operator = pair(ENV_OPERATOR_PATH, ENV_OPERATOR_KEY)?;
        Ok(Self {
            credentials,
            role,
            test_device,
            operator,
        })
    }
}

/// 生产许可源：真实后端只能与它一起选出（G2 第 2 步），没有自己给自己许可的路径。G3 的 Bellis 控制连接
/// 也加在这里。
#[derive(Debug)]
pub enum PermitSource {
    /// 操作员许可连接：独立运行中操作员控制台的测试会话授予许可（GR-36）。
    Operator(ChannelCredentials),
}

/// 假输入后端的许可源。
#[derive(Debug)]
pub enum FakePermit {
    /// 测试许可源，经测试设备连接。只用于测试，只在没有操作员许可连接时构造。
    TestLease,
    /// 生产许可源；测试设备连接只承载假输入事件。
    Source(PermitSource),
}

/// 输入后端与许可源的组合，只由 [`InputSetup::select`] 构造。许可源只有一个：测试许可源只在
/// [`InputSetup::Fake`] 里、且只在没有生产许可源时出现；真实后端（[`InputSetup::Real`]）只带
/// [`PermitSource`]，不能与测试许可源一起出现，也不能同时给测试设备的凭据。
#[derive(Debug)]
pub enum InputSetup {
    /// 没有输入后端，也没有许可源：动作块照常校验与调度，但权威检查因没有许可从不通过，不发任何输入。
    None,
    /// 假输入后端，事件经测试设备连接交给测试一方。只用于测试。
    Fake {
        /// 测试设备连接的地址与密钥。
        test_device: ChannelCredentials,
        /// 许可从哪里来。
        permit: FakePermit,
    },
    /// 真实后端（SendInput），只在 Windows 上，只与生产许可源一起。
    Real {
        /// 生产许可源。
        source: PermitSource,
        /// 急停组合键。
        emergency: Combo,
    },
}

impl InputSetup {
    /// 按 `--input-backend`、测试设备的凭据、操作员许可连接的凭据与急停组合键选出组合。
    ///
    /// # Errors
    ///
    /// 以下情形返回说明：假后端没有测试设备的凭据；给了测试设备的凭据却不是假后端；给了操作员许可连接的
    /// 凭据却没有输入后端；真实后端没有生产许可源、或同时给了测试设备的凭据、或不在 Windows 上；
    /// 给了 `--emergency-hotkey` 却不是真实后端。
    pub fn select(
        backend: Option<InputBackendKind>,
        test_device: Option<ChannelCredentials>,
        operator: Option<ChannelCredentials>,
        emergency: Option<Combo>,
    ) -> Result<Self, String> {
        if emergency.is_some() && backend != Some(InputBackendKind::SendInput) {
            return Err(
                "--emergency-hotkey is only for the real backend (--input-backend sendinput)"
                    .into(),
            );
        }
        match (backend, test_device, operator) {
            (None, None, None) => Ok(Self::None),
            (None, _, Some(_)) => Err(format!(
                "the operator lease connection ({ENV_OPERATOR_PATH}) needs an input backend"
            )),
            (None, Some(_), None) => Err(format!(
                "the test lease source ({ENV_TEST_DEVICE_PATH}) is only available with \
                 --input-backend fake"
            )),
            (Some(InputBackendKind::Fake), None, _) => Err(format!(
                "--input-backend fake needs the test device: set {ENV_TEST_DEVICE_PATH} and \
                 {ENV_TEST_DEVICE_KEY}"
            )),
            (Some(InputBackendKind::Fake), Some(test_device), operator) => Ok(Self::Fake {
                test_device,
                permit: match operator {
                    Some(operator) => FakePermit::Source(PermitSource::Operator(operator)),
                    None => FakePermit::TestLease,
                },
            }),
            (Some(InputBackendKind::SendInput), _, _) if !cfg!(windows) => Err(
                "the real input backend (--input-backend sendinput) is only available on Windows"
                    .into(),
            ),
            (Some(InputBackendKind::SendInput), Some(_), _) => Err(format!(
                "the real input backend cannot take the test device credentials \
                 ({ENV_TEST_DEVICE_PATH})"
            )),
            (Some(InputBackendKind::SendInput), None, None) => Err(format!(
                "the real input backend needs a production lease source (the operator lease \
                 connection {ENV_OPERATOR_PATH})"
            )),
            (Some(InputBackendKind::SendInput), None, Some(operator)) => Ok(Self::Real {
                source: PermitSource::Operator(operator),
                emergency: emergency.unwrap_or_else(Combo::default_hotkey),
            }),
        }
    }

    /// 操作员许可连接的地址与密钥；没有时为 `None`。
    #[must_use]
    pub fn operator(&self) -> Option<&ChannelCredentials> {
        match self {
            Self::Fake {
                permit: FakePermit::Source(PermitSource::Operator(operator)),
                ..
            }
            | Self::Real {
                source: PermitSource::Operator(operator),
                ..
            } => Some(operator),
            Self::None
            | Self::Fake {
                permit: FakePermit::TestLease,
                ..
            } => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{
        Combo, DEFAULT_MAX_FRAME_AGE, ENV_CHANNEL_KEY, ENV_CHANNEL_PATH, ENV_OPERATOR_KEY,
        ENV_OPERATOR_PATH, ENV_ROLE, ENV_TEST_DEVICE_KEY, ENV_TEST_DEVICE_PATH, FakePermit,
        InputBackendKind, InputSetup, Launch, Options, PermitSource,
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
    fn input_backends_are_fake_and_sendinput() {
        assert_eq!(
            Options::parse(args(&["--input-backend", "sendinput"]))
                .unwrap()
                .input_backend,
            Some(InputBackendKind::SendInput)
        );
        for name in ["real", "FAKE", ""] {
            let error = Options::parse(args(&["--input-backend", name])).unwrap_err();
            assert!(error.contains("unsupported"), "{error}");
        }
    }

    #[test]
    fn emergency_hotkey_parses_and_is_only_for_the_real_backend() {
        let parsed = Options::parse(args(&[
            "--input-backend",
            "sendinput",
            "--emergency-hotkey",
            "ctrl+alt+end",
        ]))
        .unwrap();
        assert_eq!(parsed.input_backend, Some(InputBackendKind::SendInput));
        assert!(parsed.emergency_hotkey.is_some());
        // 坏组合键在解析时就报错。
        assert!(Options::parse(args(&["--emergency-hotkey", "ctrl+shift"])).is_err());
        // 给了急停组合键却不是真实后端：门禁拒绝。
        let error = InputSetup::select(
            Some(InputBackendKind::Fake),
            Some(device()),
            None,
            Some(Combo::default_hotkey()),
        )
        .unwrap_err();
        assert!(error.contains("emergency"), "{error}");
    }

    #[test]
    fn the_real_backend_needs_a_production_source_and_no_test_device() {
        // 没有生产许可源：拒绝。
        let error =
            InputSetup::select(Some(InputBackendKind::SendInput), None, None, None).unwrap_err();
        if cfg!(windows) {
            assert!(error.contains("production lease source"), "{error}");
        } else {
            assert!(error.contains("only available on Windows"), "{error}");
        }
        if cfg!(windows) {
            // 真实后端与操作员许可连接一起选出。
            let setup = InputSetup::select(
                Some(InputBackendKind::SendInput),
                None,
                Some(operator()),
                None,
            )
            .unwrap();
            assert_eq!(setup.operator().unwrap().path, "operator-path");
            assert!(matches!(setup, InputSetup::Real { .. }));
            // 真实后端不能同时给测试设备的凭据。
            let error = InputSetup::select(
                Some(InputBackendKind::SendInput),
                Some(device()),
                Some(operator()),
                None,
            )
            .unwrap_err();
            assert!(error.contains(ENV_TEST_DEVICE_PATH), "{error}");
        }
    }

    fn credentials(path: &str) -> ChannelCredentials {
        ChannelCredentials::decode(path, &encode_session_key(&create_session_key())).unwrap()
    }

    fn device() -> ChannelCredentials {
        credentials("device-path")
    }

    fn operator() -> ChannelCredentials {
        credentials("operator-path")
    }

    /// 门禁：测试许可源只能与假后端一起启用，假后端也离不开测试设备。
    #[test]
    fn the_test_lease_source_only_comes_with_the_fake_backend() {
        let setup = InputSetup::select(None, None, None, None).unwrap();
        assert!(matches!(setup, InputSetup::None));
        assert!(setup.operator().is_none());
        match InputSetup::select(Some(InputBackendKind::Fake), Some(device()), None, None) {
            Ok(InputSetup::Fake {
                test_device,
                permit: FakePermit::TestLease,
            }) => assert_eq!(test_device.path, "device-path"),
            other => panic!("{other:?}"),
        }
        let error = InputSetup::select(None, Some(device()), None, None).unwrap_err();
        assert!(
            error.contains("only available with --input-backend fake"),
            "{error}"
        );
        let error = InputSetup::select(Some(InputBackendKind::Fake), None, None, None).unwrap_err();
        assert!(error.contains(ENV_TEST_DEVICE_PATH), "{error}");
    }

    /// 门禁：给了操作员许可连接时许可只来自它，测试许可源不构造；它离不开输入后端。
    #[test]
    fn the_operator_is_the_only_permit_source_when_given() {
        let setup = InputSetup::select(
            Some(InputBackendKind::Fake),
            Some(device()),
            Some(operator()),
            None,
        )
        .unwrap();
        assert_eq!(setup.operator().unwrap().path, "operator-path");
        match setup {
            InputSetup::Fake {
                test_device,
                permit: FakePermit::Source(PermitSource::Operator(operator)),
            } => {
                assert_eq!(test_device.path, "device-path");
                assert_eq!(operator.path, "operator-path");
            }
            other => panic!("{other:?}"),
        }
        // 没有输入后端：许可无处可用。
        for device in [None, Some(device())] {
            let error = InputSetup::select(None, device, Some(operator()), None).unwrap_err();
            assert!(error.contains("needs an input backend"), "{error}");
            assert!(error.contains(ENV_OPERATOR_PATH), "{error}");
        }
        // 假后端仍离不开测试设备：假输入事件要交给它。
        let error = InputSetup::select(Some(InputBackendKind::Fake), None, Some(operator()), None)
            .unwrap_err();
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
    fn launch_reads_the_operator_only_as_a_pair_and_never_prints_its_key() {
        let key = encode_session_key(&create_session_key());
        let operator_key = encode_session_key(&create_session_key());
        let mut env = HashMap::from([
            (ENV_CHANNEL_PATH, "pipe-path".to_owned()),
            (ENV_CHANNEL_KEY, key.clone()),
            (ENV_ROLE, "game-runtime.core".to_owned()),
        ]);
        assert!(
            Launch::from_env(|name| env.get(name).cloned())
                .unwrap()
                .operator
                .is_none()
        );
        env.insert(ENV_OPERATOR_KEY, operator_key.clone());
        let error = Launch::from_env(|name| env.get(name).cloned()).unwrap_err();
        assert!(error.contains("or neither"), "{error}");
        assert!(!error.contains(&operator_key), "{error}");
        env.insert(ENV_OPERATOR_PATH, "operator-path".to_owned());
        let launch = Launch::from_env(|name| env.get(name).cloned()).unwrap();
        assert_eq!(launch.operator.as_ref().unwrap().path, "operator-path");
        assert!(launch.test_device.is_none());
        let shown = format!("{launch:?}");
        assert!(shown.contains("operator-path"), "{shown}");
        assert!(!shown.contains(&operator_key), "{shown}");
        assert!(!shown.contains(&key), "{shown}");
        // 不规范的密钥：说明里只有变量名与错误码，没有密钥本身。
        let bad = format!("{}B", &operator_key[..42]);
        env.insert(ENV_OPERATOR_KEY, bad.clone());
        let error = Launch::from_env(|name| env.get(name).cloned()).unwrap_err();
        assert!(error.contains(ENV_OPERATOR_KEY), "{error}");
        assert!(!error.contains(&bad), "{error}");
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
