//! 导出生成物：JSON Schema、帧环与音频环的偏移表与示例消息，由 `core-protocol-export` 写到仓库的
//! `protocol/`。
//!
//! 输出是确定的：不含时间戳，键按声明顺序，换行为 LF，每个文件以换行结尾。

use schemars::Schema;
use schemars::generate::SchemaSettings;
use schemars::transform::RecursiveTransform;
use serde::Serialize;
use serde::de::DeserializeOwned;
use serde_json::{Map, Value, json};

use crate::PROTOCOL_VERSION;
use crate::messages::{
    Accepted, Action, ActionBlock, ActionOutcome, ActionReceiptParams, ActionSubmitResult,
    AudioRingAttachParams, AudioSetTargetParams, AudioTarget, AudioWindowTarget,
    CaptureSetTargetParams, CaptureState, CaptureTarget, Click, ControlStep, CoreHealthParams,
    CoreHelloParams, CoreHelloResult, DropReason, Dropped, Empty, Executed, FakeAudioTarget,
    FakeAxisRelease, FakeAxisSet, FakeClick, FakeInputEvent, FakeInputEventParams, FakeKeyDown,
    FakeKeyPress, FakeKeyUp, FakeMouseDown, FakeMouseMove, FakeMouseUp, FakeReleaseAll, FakeScroll,
    FakeTarget, FrameRingAttachParams, HeldAxis, HeldInput, HeldKey, HeldMouseButton, InputPolicy,
    InputReleasedParams, InputResidualParams, InputVerificationParams, KeyPress, LatestAudio,
    LatestFrame, LeaseGrantParams, LeaseRevokeParams, LeaseStatus, MouseButton, MouseMove,
    NormRect, Producer, ProducerHeartbeatParams, ProducerRevokeParams, ProducerRevokeResult,
    RejectReason, Rejected, ReleaseReason, ReleaseVerification, ResidualCause, Scroll,
    VerificationResult, WindowTarget,
};
use crate::methods::{
    ActionReceipt, ActionSubmit, AudioRingAttach, AudioSetTarget, CaptureSetTarget, CoreHealth,
    CoreHello, CoreStop, FakeInput, FrameRingAttach, InputConfigure, InputReleased, InputResidual,
    InputVerification, Method, MethodInfo, OperatorInputResidual, OperatorLeaseGrant,
    OperatorLeaseRevoke, ProducerHeartbeat, ProducerRevoke, Request, TestLeaseGrant,
    TestLeaseRevoke, catalog, error_code, operator_catalog, test_device_catalog,
};
use crate::{audio_ring, frame_ring};

/// Schema 相对 `protocol/` 的路径。
pub const SCHEMA_PATH: &str = "schema/core-protocol.schema.json";
/// 帧环偏移表相对 `protocol/` 的路径。
pub const FRAME_RING_LAYOUT_PATH: &str = "frame-ring-layout.json";
/// 音频环偏移表相对 `protocol/` 的路径。
pub const AUDIO_RING_LAYOUT_PATH: &str = "audio-ring-layout.json";
/// 示例消息目录相对 `protocol/` 的路径。
pub const FIXTURES_DIR: &str = "fixtures";

/// 一个生成的文件。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct File {
    /// 相对 `protocol/` 的路径，以 `/` 分隔。
    pub path: String,
    /// 文件内容。
    pub contents: String,
}

/// 全部生成物。
#[must_use]
pub fn files() -> Vec<File> {
    let mut files = vec![
        File {
            path: SCHEMA_PATH.to_owned(),
            contents: to_json(&schema()),
        },
        File {
            path: FRAME_RING_LAYOUT_PATH.to_owned(),
            contents: to_json(&frame_ring::layout()),
        },
        File {
            path: AUDIO_RING_LAYOUT_PATH.to_owned(),
            contents: to_json(&audio_ring::layout()),
        },
    ];
    files.extend(fixtures().into_iter().map(|fixture| File {
        path: format!("{FIXTURES_DIR}/{}", fixture.path),
        contents: fixture.json,
    }));
    files
}

/// 以两空格缩进输出 JSON，末尾加换行。
fn to_json<T: Serialize + ?Sized>(value: &T) -> String {
    let mut text = serde_json::to_string_pretty(value).expect("generated values serialize to JSON");
    text.push('\n');
    text
}

/// 核协议的 JSON Schema（2020-12）。
///
/// `$defs` 是全部消息类型；`x-methods` 按方法名列出核连接上每个方法的方向与 params、result 的引用，
/// 通知的 `result` 为 `null`；`x-operator-methods` 与 `x-test-device-methods` 同样列出操作员许可连接与
/// 只用于测试的测试设备连接上的方法；`x-protocol-version` 与 `x-error-codes` 是协议版本与自定义错误码。
#[must_use]
pub fn schema() -> Value {
    let mut generator = SchemaSettings::draft2020_12()
        .with_transform(RecursiveTransform(add_integer_bounds))
        .with_transform(RecursiveTransform(mark_type_discriminator))
        .into_generator();
    let methods = method_refs(&catalog(), &mut generator);
    let operator_methods = method_refs(&operator_catalog(), &mut generator);
    let test_device_methods = method_refs(&test_device_catalog(), &mut generator);
    let error_codes: Map<String, Value> = error_code::ALL
        .iter()
        .map(|&(name, code)| (name.to_owned(), code.into()))
        .collect();
    json!({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "CoreProtocol",
        "description": "主进程与实时核之间核连接上的消息，由 core/crates/core-protocol 生成。",
        "x-protocol-version": PROTOCOL_VERSION,
        "x-methods": methods,
        "x-operator-methods": operator_methods,
        "x-test-device-methods": test_device_methods,
        "x-error-codes": error_codes,
        "$defs": generator.take_definitions(true),
    })
}

/// 一组方法的方向与 params、result 的引用。
fn method_refs(catalog: &[MethodInfo], generator: &mut schemars::SchemaGenerator) -> Value {
    let mut methods = Map::new();
    for method in catalog {
        let params = (method.params)(generator);
        let result = method.result.map(|result| result(generator));
        methods.insert(
            method.name.to_owned(),
            json!({ "direction": method.direction, "params": params, "result": result }),
        );
    }
    Value::Object(methods)
}

/// 给整数补上 Rust 类型的上下界。schemars 只给部分整数类型写出上界；补齐后 Pydantic 与 serde
/// 拒绝同样的越界值。
fn add_integer_bounds(schema: &mut Schema) {
    let bounds: (Value, Value) = match schema.get("format").and_then(Value::as_str) {
        Some("uint8") => (0.into(), u8::MAX.into()),
        Some("uint16") => (0.into(), u16::MAX.into()),
        Some("uint32") => (0.into(), u32::MAX.into()),
        Some("uint64") => (0.into(), u64::MAX.into()),
        Some("int8") => (i8::MIN.into(), i8::MAX.into()),
        Some("int16") => (i16::MIN.into(), i16::MAX.into()),
        Some("int32") => (i32::MIN.into(), i32::MAX.into()),
        Some("int64") => (i64::MIN.into(), i64::MAX.into()),
        _ => return,
    };
    if let Some(object) = schema.as_object_mut() {
        object.entry("minimum").or_insert(bounds.0);
        object.entry("maximum").or_insert(bounds.1);
    }
}

/// 给以 `type` 为内部标签的联合类型加上 `discriminator`，让 datamodel-codegen 生成按标签选分支的
/// Pydantic 联合类型，而不是按字段逐个尝试。`discriminator` 来自 OpenAPI，对 JSON Schema
/// 只是注解，不改变校验结果。
fn mark_type_discriminator(schema: &mut Schema) {
    let Some(branches) = schema.get("oneOf").and_then(Value::as_array) else {
        return;
    };
    let tagged = |branch: &Value| {
        branch
            .pointer("/properties/type/const")
            .is_some_and(Value::is_string)
            && branch["required"]
                .as_array()
                .is_some_and(|required| required.contains(&"type".into()))
    };
    if branches.iter().all(tagged) {
        schema.insert("discriminator".into(), json!({ "propertyName": "type" }));
    }
}

/// 一条示例消息：某个方法的 params 或 result。
#[derive(Debug, Clone)]
pub struct Fixture {
    /// 相对 `fixtures/` 的路径：`<方法名>/params.json` 或 `<方法名>/result.json`，
    /// 同一方法有多条时在 `.json` 前加 `.<用例名>`。
    pub path: String,
    /// 消息内容。
    pub json: String,
    /// 把消息解析为它的 Rust 类型再序列化回 JSON。
    pub reparse: fn(&str) -> serde_json::Result<String>,
}

impl Fixture {
    fn params<M: Method>(case: Option<&str>, value: &M::Params) -> Self {
        Self::new::<M::Params>(M::NAME, "params", case, value)
    }

    fn result<M: Request>(case: Option<&str>, value: &M::Result) -> Self {
        Self::new::<M::Result>(M::NAME, "result", case, value)
    }

    fn new<T: Serialize + DeserializeOwned>(
        method: &str,
        part: &str,
        case: Option<&str>,
        value: &T,
    ) -> Self {
        let path = match case {
            Some(case) => format!("{method}/{part}.{case}.json"),
            None => format!("{method}/{part}.json"),
        };
        Self {
            path,
            json: to_json(value),
            reparse: reparse::<T>,
        }
    }
}

fn reparse<T: Serialize + DeserializeOwned>(text: &str) -> serde_json::Result<String> {
    let value: T = serde_json::from_str(text)?;
    Ok(to_json(&value))
}

/// 示例消息：每个方法的 params 与 result 至少一条，每个联合类型的每个分支至少出现一次。
/// 数值都是合成的占位值。
#[must_use]
#[allow(clippy::too_many_lines)] // 逐条列出示例比拆开更好核对
pub fn fixtures() -> Vec<Fixture> {
    let step_control = || Producer {
        id: "step-control".to_owned(),
        generation: 3,
    };
    let continuous = || Producer {
        id: "continuous-policy".to_owned(),
        generation: 4,
    };
    let ring = |name: &str, width: u32, height: u32, slots: u32| {
        let stride = (frame_ring::SLOT_PIXELS_OFFSET + u64::from(width) * u64::from(height) * 4)
            .next_multiple_of(frame_ring::SLOT_ALIGN);
        FrameRingAttachParams {
            name: name.into(),
            size_bytes: frame_ring::SLOTS_OFFSET + u64::from(slots) * stride,
            layout_version: frame_ring::LAYOUT_VERSION,
            slot_count: slots,
            slot_stride: stride,
            max_width: width,
            max_height: height,
        }
    };
    let audio = |name: &str, sample_rate: u32, channels: u32, capacity: u32, blocks: u32| {
        let stride = (audio_ring::BLOCK_SAMPLES_OFFSET
            + u64::from(capacity) * u64::from(channels) * u64::from(audio_ring::BYTES_PER_VALUE))
        .next_multiple_of(audio_ring::BLOCK_ALIGN);
        AudioRingAttachParams {
            name: name.into(),
            size_bytes: audio_ring::BLOCKS_OFFSET + u64::from(blocks) * stride,
            layout_version: audio_ring::LAYOUT_VERSION,
            block_count: blocks,
            block_stride: stride,
            block_capacity: capacity,
            sample_rate,
            channels,
            sample_format: audio_ring::SAMPLE_FORMAT_F32,
        }
    };
    let mut fixtures = vec![
        Fixture::params::<CoreHello>(
            None,
            &CoreHelloParams {
                protocol_version: PROTOCOL_VERSION,
                core_version: "0.0.0".into(),
            },
        ),
        Fixture::result::<CoreHello>(
            None,
            &CoreHelloResult {
                protocol_version: PROTOCOL_VERSION,
            },
        ),
        Fixture::params::<FrameRingAttach>(
            None,
            &ring("bellis-game-runtime-frames-example", 1920, 1080, 3),
        ),
        Fixture::result::<FrameRingAttach>(None, &Empty {}),
        Fixture::params::<CaptureSetTarget>(
            Some("window"),
            &CaptureSetTargetParams {
                target: CaptureTarget::Window(WindowTarget {
                    process_name: "synthetic-game.exe".into(),
                    window_class: None,
                    title: Some("Synthetic Game".into()),
                }),
            },
        ),
        Fixture::params::<CaptureSetTarget>(
            Some("fake"),
            &CaptureSetTargetParams {
                target: CaptureTarget::Fake(FakeTarget {
                    surface: ring("bellis-game-runtime-fake-display-example", 320, 180, 2),
                }),
            },
        ),
        Fixture::result::<CaptureSetTarget>(None, &Empty {}),
        Fixture::params::<AudioRingAttach>(
            None,
            &audio("bellis-game-runtime-audio-example", 48_000, 2, 960, 400),
        ),
        Fixture::result::<AudioRingAttach>(None, &Empty {}),
        Fixture::params::<AudioSetTarget>(
            Some("window"),
            &AudioSetTargetParams {
                target: AudioTarget::Window(AudioWindowTarget {
                    process_name: "synthetic-game.exe".into(),
                    window_class: Some("SyntheticGameWindow".into()),
                    title: None,
                }),
            },
        ),
        Fixture::params::<AudioSetTarget>(
            Some("fake"),
            &AudioSetTargetParams {
                target: AudioTarget::Fake(FakeAudioTarget {
                    output: audio("bellis-game-runtime-fake-audio-example", 16_000, 1, 160, 8),
                }),
            },
        ),
        Fixture::result::<AudioSetTarget>(None, &Empty {}),
        Fixture::params::<InputConfigure>(
            None,
            &InputPolicy {
                click_buttons: vec![MouseButton::Left],
                press_keys: vec![0x1C, 0x39],
                scroll: true,
                click_regions: vec![NormRect {
                    left: 0.0,
                    top: 0.0,
                    right: 1.0,
                    bottom: 1.0,
                }],
                forbidden_zones: vec![NormRect {
                    left: 0.0,
                    top: 0.0,
                    right: 0.2,
                    bottom: 0.1,
                }],
                press_duration_ms: 50,
                max_rate: 10.0,
                hold_keys: vec![0x11],
                hold_buttons: vec![MouseButton::Right],
                max_hold_ms: Some(2000),
            },
        ),
        Fixture::result::<InputConfigure>(None, &Empty {}),
        Fixture::params::<ProducerHeartbeat>(
            None,
            &ProducerHeartbeatParams {
                producer: step_control(),
                seq: 42,
            },
        ),
        Fixture::params::<ActionSubmit>(
            Some("step"),
            &ActionBlock {
                producer: step_control(),
                seq: 7,
                base_frame: 1200,
                start_ns: 5_000_000_000,
                deadline_ns: 5_500_000_000,
                steps: vec![ControlStep {
                    offset_ns: 0,
                    held: vec![],
                    actions: vec![Action::Click(Click {
                        id: 1,
                        x: 640,
                        y: 360,
                        button: MouseButton::Left,
                    })],
                }],
            },
        ),
        Fixture::params::<ActionSubmit>(
            Some("continuous"),
            &ActionBlock {
                producer: continuous(),
                seq: 1,
                base_frame: 1500,
                start_ns: 6_000_000_000,
                deadline_ns: 6_100_000_000,
                steps: vec![
                    ControlStep {
                        offset_ns: 0,
                        held: vec![
                            HeldInput::Key(HeldKey { scan_code: 0x11 }),
                            HeldInput::MouseButton(HeldMouseButton {
                                button: MouseButton::Right,
                            }),
                            HeldInput::Axis(HeldAxis {
                                axis: "camera_yaw".into(),
                                value: 0.25,
                            }),
                        ],
                        actions: vec![Action::MouseMove(MouseMove {
                            id: 10,
                            dx: 12,
                            dy: -3,
                        })],
                    },
                    ControlStep {
                        offset_ns: 33_333_333,
                        held: vec![HeldInput::Key(HeldKey { scan_code: 0xE04B })],
                        actions: vec![
                            Action::KeyPress(KeyPress {
                                id: 11,
                                scan_code: 0x39,
                            }),
                            Action::Scroll(Scroll {
                                id: 12,
                                x: 100,
                                y: 200,
                                notches: -1,
                            }),
                        ],
                    },
                    ControlStep {
                        offset_ns: 66_666_666,
                        held: vec![],
                        actions: vec![],
                    },
                ],
            },
        ),
        Fixture::result::<ActionSubmit>(
            Some("accepted"),
            &ActionSubmitResult::Accepted(Accepted {}),
        ),
        Fixture::result::<ActionSubmit>(
            Some("rejected"),
            &ActionSubmitResult::Rejected(Rejected {
                reason: RejectReason::StaleGeneration,
                message: "generation 2 is older than the current generation 3".into(),
            }),
        ),
        Fixture::params::<ProducerRevoke>(None, &ProducerRevokeParams { generation: 3 }),
        Fixture::result::<ProducerRevoke>(
            None,
            &ProducerRevokeResult {
                release_id: Some(3),
            },
        ),
        Fixture::params::<CoreStop>(None, &Empty {}),
        Fixture::result::<CoreStop>(None, &Empty {}),
        Fixture::params::<ActionReceipt>(
            None,
            &ActionReceiptParams {
                producer: step_control(),
                block_seq: 7,
                outcomes: vec![
                    ActionOutcome::Executed(Executed {
                        id: 1,
                        at_ns: 5_000_120_000,
                    }),
                    ActionOutcome::Dropped(Dropped {
                        id: 2,
                        reason: DropReason::Superseded,
                    }),
                ],
            },
        ),
        Fixture::params::<ActionReceipt>(
            Some("real-backend"),
            &ActionReceiptParams {
                producer: step_control(),
                block_seq: 8,
                outcomes: vec![
                    // `Uncertain` 与 `Executed` 字段相同、只有标签不同，放进示例会让“换标签即被拒”的
                    // 核对失配；它的往返由 `realtime-core` 的单元测试覆盖。
                    ActionOutcome::Dropped(Dropped {
                        id: 4,
                        reason: DropReason::NotAllowed,
                    }),
                    ActionOutcome::Dropped(Dropped {
                        id: 5,
                        reason: DropReason::RateLimited,
                    }),
                ],
            },
        ),
        Fixture::params::<InputReleased>(
            Some("revoked"),
            &InputReleasedParams {
                release_id: 3,
                reason: ReleaseReason::GenerationRevoked,
                producer: Some(step_control()),
                released_at_ns: 5_600_000_000,
                verification: ReleaseVerification::Unverified,
            },
        ),
        Fixture::params::<InputReleased>(
            Some("takeover"),
            &InputReleasedParams {
                release_id: 4,
                reason: ReleaseReason::Takeover,
                producer: Some(step_control()),
                released_at_ns: 5_650_000_000,
                verification: ReleaseVerification::UpFailed,
            },
        ),
        Fixture::params::<InputResidual>(
            Some("release"),
            &InputResidualParams {
                release_id: 5,
                cause: ResidualCause::Release,
                reason: Some(ReleaseReason::EmergencyStop),
                items: vec![
                    HeldInput::Key(HeldKey { scan_code: 0x11 }),
                    HeldInput::MouseButton(HeldMouseButton {
                        button: MouseButton::Left,
                    }),
                ],
                checked_at_ns: 5_700_050_000,
                repair_accepted: false,
            },
        ),
        Fixture::params::<InputResidual>(
            Some("core-start"),
            &InputResidualParams {
                release_id: 1,
                cause: ResidualCause::CoreStart,
                reason: None,
                items: vec![HeldInput::Key(HeldKey { scan_code: 0x1E })],
                checked_at_ns: 1_050_000,
                repair_accepted: true,
            },
        ),
        Fixture::params::<OperatorInputResidual>(
            None,
            &InputResidualParams {
                release_id: 4,
                cause: ResidualCause::Release,
                reason: Some(ReleaseReason::Takeover),
                items: vec![HeldInput::MouseButton(HeldMouseButton {
                    button: MouseButton::Left,
                })],
                checked_at_ns: 5_700_050_000,
                repair_accepted: true,
            },
        ),
        Fixture::params::<InputReleased>(
            Some("core-start"),
            &InputReleasedParams {
                release_id: 1,
                reason: ReleaseReason::CoreStart,
                producer: None,
                released_at_ns: 1_000_000,
                verification: ReleaseVerification::Unverified,
            },
        ),
        Fixture::params::<InputVerification>(
            Some("verified"),
            &InputVerificationParams {
                release_id: 3,
                result: VerificationResult::Verified,
                checked_at_ns: 5_650_000_000,
            },
        ),
        Fixture::params::<InputVerification>(
            Some("residual"),
            &InputVerificationParams {
                release_id: 5,
                result: VerificationResult::Residual,
                checked_at_ns: 5_700_050_000,
            },
        ),
        Fixture::params::<InputVerification>(
            Some("unavailable"),
            &InputVerificationParams {
                release_id: 4,
                result: VerificationResult::Unavailable,
                checked_at_ns: 5_700_050_000,
            },
        ),
        Fixture::params::<InputVerification>(
            Some("superseded"),
            &InputVerificationParams {
                release_id: 2,
                result: VerificationResult::Superseded,
                checked_at_ns: 5_650_000_000,
            },
        ),
        Fixture::params::<CoreHealth>(
            None,
            &CoreHealthParams {
                now_ns: 5_700_000_000,
                capture: CaptureState::Capturing,
                inputs_held: false,
                producer: Some(step_control()),
                latest_frame: Some(LatestFrame {
                    frame_number: 1200,
                    capture_ns: 5_683_000_000,
                }),
                lease: LeaseStatus::Valid,
                audio: CaptureState::Capturing,
                latest_audio: Some(LatestAudio {
                    block_number: 570,
                    capture_ns: 5_688_000_000,
                }),
            },
        ),
        Fixture::params::<OperatorLeaseGrant>(
            None,
            &LeaseGrantParams {
                token: 5,
                expires_ns: 5_500_000_000,
            },
        ),
        Fixture::result::<OperatorLeaseGrant>(None, &Empty {}),
        Fixture::params::<OperatorLeaseRevoke>(None, &LeaseRevokeParams { token: 5 }),
        Fixture::result::<OperatorLeaseRevoke>(None, &Empty {}),
        Fixture::params::<TestLeaseGrant>(
            None,
            &LeaseGrantParams {
                token: 2,
                expires_ns: 5_500_000_000,
            },
        ),
        Fixture::result::<TestLeaseGrant>(None, &Empty {}),
        Fixture::params::<TestLeaseRevoke>(None, &LeaseRevokeParams { token: 2 }),
        Fixture::result::<TestLeaseRevoke>(None, &Empty {}),
    ];
    let events = [
        (
            "key-down",
            FakeInputEvent::KeyDown(FakeKeyDown { scan_code: 0x11 }),
        ),
        (
            "key-up",
            FakeInputEvent::KeyUp(FakeKeyUp { scan_code: 0x11 }),
        ),
        (
            "mouse-down",
            FakeInputEvent::MouseDown(FakeMouseDown {
                button: MouseButton::Right,
            }),
        ),
        (
            "mouse-up",
            FakeInputEvent::MouseUp(FakeMouseUp {
                button: MouseButton::Right,
            }),
        ),
        (
            "axis-set",
            FakeInputEvent::AxisSet(FakeAxisSet {
                axis: "camera_yaw".into(),
                value: -0.5,
            }),
        ),
        (
            "axis-release",
            FakeInputEvent::AxisRelease(FakeAxisRelease {
                axis: "camera_yaw".into(),
            }),
        ),
        (
            "click",
            FakeInputEvent::Click(FakeClick {
                id: 1,
                x: 640,
                y: 360,
                button: MouseButton::Left,
            }),
        ),
        (
            "key-press",
            FakeInputEvent::KeyPress(FakeKeyPress {
                id: 11,
                scan_code: 0x1C,
            }),
        ),
        (
            "scroll",
            FakeInputEvent::Scroll(FakeScroll {
                id: 12,
                x: 100,
                y: 200,
                notches: -1,
            }),
        ),
        (
            "mouse-move",
            FakeInputEvent::MouseMove(FakeMouseMove {
                id: 10,
                dx: 12,
                dy: -3,
            }),
        ),
        (
            "release-all",
            FakeInputEvent::ReleaseAll(FakeReleaseAll {
                reason: ReleaseReason::LeaseExpired,
            }),
        ),
    ];
    fixtures.extend(events.into_iter().zip(1..).map(|((case, event), seq)| {
        Fixture::params::<FakeInput>(
            Some(case),
            &FakeInputEventParams {
                seq,
                at_ns: 5_000_000_000 + seq * 1_000_000,
                event,
            },
        )
    }));
    fixtures
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;

    use serde_json::Value;

    use super::{fixtures, schema};
    use crate::methods::{
        InputReleased, InputResidual, InputVerification, Method, MethodInfo, OperatorInputReleased,
        OperatorInputResidual, OperatorInputVerification, catalog, operator_catalog,
        test_device_catalog,
    };

    /// 三组方法：核连接上的、操作员许可连接上的与测试设备连接上的。
    fn all_methods() -> Vec<MethodInfo> {
        let mut methods = catalog();
        methods.extend(operator_catalog());
        methods.extend(test_device_catalog());
        methods
    }

    /// 在 `value` 中每个对象节点的路径（JSON Pointer）。
    fn object_pointers(value: &Value, pointer: &str, out: &mut Vec<String>) {
        match value {
            Value::Object(map) => {
                out.push(pointer.to_owned());
                for (key, child) in map {
                    object_pointers(child, &format!("{pointer}/{key}"), out);
                }
            }
            Value::Array(items) => {
                for (index, child) in items.iter().enumerate() {
                    object_pointers(child, &format!("{pointer}/{index}"), out);
                }
            }
            _ => {}
        }
    }

    /// 每个 `type` 标签所在对象的路径。
    fn tagged_pointers(value: &Value) -> Vec<String> {
        let mut pointers = Vec::new();
        object_pointers(value, "", &mut pointers);
        pointers.retain(|pointer| {
            value
                .pointer(pointer)
                .and_then(|node| node.get("type"))
                .is_some()
        });
        pointers
    }

    #[test]
    fn every_method_has_params_and_result_fixtures() {
        let paths: BTreeSet<String> = fixtures().into_iter().map(|fixture| fixture.path).collect();
        for method in all_methods() {
            let has = |part: &str| {
                paths.iter().any(|path| {
                    path.strip_prefix(method.name)
                        .and_then(|rest| rest.strip_prefix('/'))
                        .is_some_and(|file| file.starts_with(&format!("{part}.")))
                })
            };
            assert!(has("params"), "{} has no params fixture", method.name);
            assert_eq!(
                has("result"),
                method.result.is_some(),
                "{} result fixture",
                method.name
            );
        }
    }

    #[test]
    fn fixtures_round_trip_through_their_types() {
        for fixture in fixtures() {
            let reparsed = (fixture.reparse)(&fixture.json).expect(&fixture.path);
            assert_eq!(reparsed, fixture.json, "{}", fixture.path);
        }
    }

    /// 在每个对象节点（含联合类型的分支）加一个未知字段，都必须被拒绝。
    #[test]
    fn unknown_fields_are_rejected_at_every_level() {
        for fixture in fixtures() {
            let value: Value = serde_json::from_str(&fixture.json).unwrap();
            let mut pointers = Vec::new();
            object_pointers(&value, "", &mut pointers);
            for pointer in pointers {
                let mut changed = value.clone();
                changed
                    .pointer_mut(&pointer)
                    .and_then(Value::as_object_mut)
                    .unwrap()
                    .insert("unexpected".into(), Value::Bool(true));
                let result = (fixture.reparse)(&changed.to_string());
                assert!(
                    result.is_err(),
                    "{} accepted an unknown field at {pointer:?}",
                    fixture.path
                );
            }
        }
    }

    /// `type` 标签选出分支：未知标签被拒绝；把标签换成另一个分支的名字也被拒绝，
    /// 说明解析按标签选分支，而不是按字段去猜。
    #[test]
    fn type_tag_selects_the_union_branch() {
        let mut checked = 0;
        for fixture in fixtures() {
            let value: Value = serde_json::from_str(&fixture.json).unwrap();
            for pointer in tagged_pointers(&value) {
                for tag in [
                    "no_such_type",
                    "click",
                    "window",
                    "executed",
                    "accepted",
                    "key",
                ] {
                    let original = value.pointer(&pointer).unwrap()["type"].clone();
                    if original == tag {
                        continue;
                    }
                    let mut changed = value.clone();
                    changed.pointer_mut(&pointer).unwrap()["type"] = tag.into();
                    let result = (fixture.reparse)(&changed.to_string());
                    assert!(
                        result.is_err(),
                        "{} accepted type {tag:?} at {pointer:?}",
                        fixture.path
                    );
                }
                checked += 1;
            }
        }
        assert!(checked > 0);
    }

    #[test]
    fn schema_lists_every_method_with_typed_params_and_result() {
        let schema = schema();
        let defs = schema["$defs"].as_object().unwrap();
        for (key, catalog) in [
            ("x-methods", catalog()),
            ("x-operator-methods", operator_catalog()),
            ("x-test-device-methods", test_device_catalog()),
        ] {
            let methods = schema[key].as_object().unwrap();
            let names: Vec<&str> = methods.keys().map(String::as_str).collect();
            let expected: Vec<&str> = catalog.iter().map(|method| method.name).collect();
            assert_eq!(names, expected, "{key}");
            for (name, method) in methods {
                for part in ["params", "result"] {
                    let Some(reference) = method[part]["$ref"].as_str() else {
                        assert!(part == "result" && method[part].is_null(), "{name} {part}");
                        continue;
                    };
                    let def = reference.strip_prefix("#/$defs/").unwrap();
                    assert!(
                        defs.contains_key(def),
                        "{name} {part} refers to missing {def}"
                    );
                }
            }
        }
    }

    /// 测试设备连接与核连接、操作员许可连接没有同名的方法；操作员许可连接与核连接只共有
    /// 输入释放与核验的通知，两处的 params 相同。
    #[test]
    fn the_groups_share_only_the_core_to_operator_notifications() {
        let core: BTreeSet<&str> = catalog().iter().map(|method| method.name).collect();
        let operator: BTreeSet<&str> = operator_catalog()
            .iter()
            .map(|method| method.name)
            .collect();
        for method in test_device_catalog() {
            assert!(!core.contains(method.name), "{}", method.name);
            assert!(!operator.contains(method.name), "{}", method.name);
        }
        let mut shared: Vec<&str> = core.intersection(&operator).copied().collect();
        shared.sort_unstable();
        assert_eq!(
            shared,
            [
                InputReleased::NAME,
                InputResidual::NAME,
                InputVerification::NAME
            ]
        );
        assert_eq!(OperatorInputReleased::NAME, InputReleased::NAME);
        assert_eq!(OperatorInputResidual::NAME, InputResidual::NAME);
        assert_eq!(OperatorInputVerification::NAME, InputVerification::NAME);
        let schema = schema();
        for name in [
            InputReleased::NAME,
            InputResidual::NAME,
            InputVerification::NAME,
        ] {
            assert_eq!(
                schema["x-operator-methods"][name]["params"],
                schema["x-methods"][name]["params"]
            );
        }
    }
}
