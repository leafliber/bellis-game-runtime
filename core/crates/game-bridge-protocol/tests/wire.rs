use game_bridge_protocol::{
    BellisRequest, ControlCall, ControlEvent, ControlResult, DecodeError, GAME_BRIDGE_MAJOR,
    MAX_MONOTONIC_MS, MonotonicMs, RuntimeNotification, RuntimeResponse,
};

const DESCRIBE: &[u8] =
    br#"{"jsonrpc":"2.0","id":1,"method":"game.describe","params":{"major":1}}"#;
const RENEW: &[u8] =
    br#"{"jsonrpc":"2.0","id":"renew","method":"lease.renew","params":{"token":7,"expiresAtMs":1}}"#;

#[test]
fn exact_integer_spelling_in_control_fields() {
    let health = r#"{"jsonrpc":"2.0","method":"health.reported","params":{"atMs":1,"leaseState":"valid","frameAgeMs":1,"queueDepth":1}}"#;
    let release = r#"{"jsonrpc":"2.0","method":"input.released","params":{"token":INTEGER,"atMs":1,"reason":"core_start","verification":"unverified"}}"#;
    let stop = r#"{"jsonrpc":"2.0","id":INTEGER,"method":"input.stop","params":{"throughToken":1,"reason":"stop"}}"#;
    let lease = r#"{"jsonrpc":"2.0","id":1,"method":"lease.renew","params":{"token":1,"expiresAtMs":INTEGER}}"#;
    for spelling in ["1.0", "1e0", "1E+0", "-0", "false", r#""1""#] {
        for name in ["atMs", "frameAgeMs", "queueDepth"] {
            let raw = health.replace(
                &format!(r#""{name}":1"#),
                &format!(r#""{name}":{spelling}"#),
            );
            assert!(
                RuntimeNotification::decode(raw.as_bytes()).is_err(),
                "{spelling} in {name}"
            );
        }
        assert!(
            RuntimeNotification::decode(release.replace("INTEGER", spelling).as_bytes()).is_err()
        );
        assert!(BellisRequest::decode(lease.replace("INTEGER", spelling).as_bytes()).is_err());
        // 字符串是合法编号。其余写法在编号位置同样无效。
        if spelling != r#""1""# {
            assert!(BellisRequest::decode(stop.replace("INTEGER", spelling).as_bytes()).is_err());
        }
    }
    for id in ["9007199254740992", "-9007199254740992", "null", r#""""#] {
        assert!(BellisRequest::decode(stop.replace("INTEGER", id).as_bytes()).is_err());
    }
    for id in ["9007199254740991", "-9007199254740991", "0", r#""1""#] {
        assert!(BellisRequest::decode(stop.replace("INTEGER", id).as_bytes()).is_ok());
    }
}

#[test]
fn control_frames_require_named_objects_and_closed_envelopes() {
    for invalid in [
        br#"["2.0",1,"game.describe",{"major":1}]"#.as_slice(),
        br#"{"jsonrpc":"2.0","id":1,"method":"game.describe","params":[1]}"#,
        br#"{"jsonrpc":"2.0","id":1,"method":"game.describe","params":null}"#,
        br#"{"jsonrpc":"2.0","id":1,"method":"game.describe"}"#,
        br#"{"jsonrpc":"1.0","id":1,"method":"game.describe","params":{"major":1}}"#,
        br#"{"jsonrpc":"2.0","id":1,"method":"game.describe","params":{"major":1},"result":null}"#,
        br#"{"jsonrpc":"2.0","id":1,"method":"health.reported","params":{"atMs":0,"leaseState":"revoked","frameAgeMs":null,"queueDepth":0}}"#,
    ] {
        assert!(BellisRequest::decode(invalid).is_err(), "{invalid:?}");
    }
    let valid_report = br#"{"jsonrpc":"2.0","method":"health.reported","params":{"atMs":0,"leaseState":"revoked","frameAgeMs":null,"queueDepth":0}}"#;
    assert!(BellisRequest::decode(valid_report).is_err());
    assert!(matches!(
        RuntimeNotification::decode(valid_report).unwrap().event,
        ControlEvent::HealthReported(report) if report.frame_age_ms.is_none()
    ));
    assert!(RuntimeNotification::decode(RENEW).is_err());
    assert!(RuntimeNotification::decode(br#"{"jsonrpc":"2.0","id":null,"method":"health.reported","params":{"atMs":0,"leaseState":"revoked","frameAgeMs":null,"queueDepth":0}}"#).is_err());
}

#[test]
fn last_duplicate_key_keeps_its_own_lexeme() {
    let template = r#"{"jsonrpc":"2.0","id":1,"method":"lease.renew","params":{"token":FIRST,"toke\u006e":LAST,"expiresAtMs":1}}"#;
    let accepted = template.replace("FIRST", "1.0").replace("LAST", "7");
    let rejected = template.replace("FIRST", "7").replace("LAST", "1.0");
    assert!(matches!(
        BellisRequest::decode(accepted.as_bytes()).unwrap().call,
        ControlCall::LeaseRenew(lease) if lease.token.get() == 7
    ));
    assert!(BellisRequest::decode(rejected.as_bytes()).is_err());
}

#[test]
fn responses_use_both_pending_id_and_result_shape() {
    let describe = BellisRequest::decode(DESCRIBE).unwrap();
    let renew = BellisRequest::decode(RENEW).unwrap();
    let ack = br#"{"jsonrpc":"2.0","id":"renew","result":{}}"#;
    let description =
        br#"{"jsonrpc":"2.0","id":1,"result":{"major":1,"runtimeId":"core-1","nowMs":0}}"#;
    assert!(matches!(
        RuntimeResponse::decode(ack, Some(&renew)).unwrap(),
        RuntimeResponse::Success(response) if matches!(response.result, ControlResult::Ack(_))
    ));
    assert!(matches!(
        RuntimeResponse::decode(ack, None),
        Err(DecodeError::UnmatchedResponse)
    ));
    assert!(matches!(
        RuntimeResponse::decode(
            br#"{"jsonrpc":"2.0","id":"1","result":{"major":1,"runtimeId":"core-1","nowMs":0}}"#,
            Some(&describe)
        ),
        Err(DecodeError::UnmatchedResponse)
    ));
    assert!(matches!(
        RuntimeResponse::decode(description, Some(&describe)).unwrap(),
        RuntimeResponse::Success(response) if matches!(response.result, ControlResult::Description(_))
    ));
    for invalid in [
        br#"{"jsonrpc":"2.0","id":"renew","result":[]}"#.as_slice(),
        br#"{"jsonrpc":"2.0","id":"renew","result":null}"#,
        br#"{"jsonrpc":"2.0","id":"renew","result":{"major":1,"runtimeId":"core-1","nowMs":0}}"#,
        br#"{"jsonrpc":"2.0","id":"renew","result":{},"error":null}"#,
        br#"{"jsonrpc":"2.0","id":"renew"}"#,
    ] {
        assert!(RuntimeResponse::decode(invalid, Some(&renew)).is_err());
    }
    assert!(
        RuntimeResponse::decode(br#"{"jsonrpc":"2.0","id":1,"result":{}}"#, Some(&describe))
            .is_err()
    );
}

#[test]
fn public_major_is_read_only_after_response_correlation() {
    let describe = BellisRequest::decode(DESCRIBE).unwrap();
    let renew = BellisRequest::decode(RENEW).unwrap();
    let template = r#"{"jsonrpc":"2.0","id":1,"result":{"major":MAJOR,"nowMs":0.5,"new":{"token":-0,"id":9007199254740993}}}"#;
    let future = template.replace("MAJOR", "65535");
    assert!(matches!(
        RuntimeResponse::decode(future.as_bytes(), Some(&describe)),
        Err(DecodeError::IncompatibleMajor(65535))
    ));
    assert!(matches!(
        RuntimeResponse::decode(future.as_bytes(), Some(&renew)),
        Err(DecodeError::UnmatchedResponse)
    ));
    let wrong_method = future.replace(r#""id":1"#, r#""id":"renew""#);
    assert!(matches!(
        RuntimeResponse::decode(wrong_method.as_bytes(), Some(&renew)),
        Err(DecodeError::ExtraField)
    ));
    for invalid in [
        "1", "-0", "0", "65536", "2.0", "2e0", r#""2""#, "true", "null",
    ] {
        assert!(
            !matches!(
                RuntimeResponse::decode(
                    template.replace("MAJOR", invalid).as_bytes(),
                    Some(&describe)
                ),
                Ok(_) | Err(DecodeError::IncompatibleMajor(_))
            ),
            "{invalid}"
        );
    }
    let future_request = str::from_utf8(DESCRIBE).unwrap().replace("1}", "2}");
    assert!(matches!(
        BellisRequest::decode(future_request.as_bytes()).unwrap().call,
        ControlCall::Describe(params) if params.major > GAME_BRIDGE_MAJOR
    ));
}

#[test]
fn rpc_failure_keeps_arbitrary_error_data() {
    let renew = BellisRequest::decode(RENEW).unwrap();
    let error_data = r#"{"token":1.5,"id":9007199254740993,"code":-0,"items":[true,1e400,null]}"#;
    let template = r#"{"jsonrpc":"2.0","id":"renew","error":{"code":CODE,"message":"lease refused","data":DATA}}"#;
    for data in [error_data, "null", "true", "1e400", "[1e0]", r#""text""#] {
        let raw = template.replace("CODE", "-32004").replace("DATA", data);
        let response = RuntimeResponse::decode(raw.as_bytes(), Some(&renew)).unwrap();
        assert_eq!(serde_json::to_string(&response).unwrap(), raw);
        let RuntimeResponse::Failure(response) = response else {
            panic!("expected failure");
        };
        assert_eq!(response.error.code(), -32_004);
        assert_eq!(response.error.data.unwrap().get(), data);
    }
    for invalid in ["-32004.0", "-32004e0", "-0", "false", "9007199254740992"] {
        let raw = template.replace("CODE", invalid).replace("DATA", "null");
        assert!(RuntimeResponse::decode(raw.as_bytes(), Some(&renew)).is_err());
    }

    let unassociated = r#"{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":""}}"#;
    let response = RuntimeResponse::decode(unassociated.as_bytes(), None).unwrap();
    assert_eq!(serde_json::to_string(&response).unwrap(), unassociated);
    assert!(matches!(
        RuntimeResponse::decode(unassociated.as_bytes(), Some(&renew)),
        Err(DecodeError::UnmatchedResponse)
    ));
    for raw in [
        br#"{"jsonrpc":"2.0","error":{"code":-32700,"message":""}}"#.as_slice(),
        br#"{"jsonrpc":"2.0","id":null,"error":null}"#,
        br#"{"jsonrpc":"2.0","id":null,"error":{"code":0,"message":"","other":null}}"#,
        br#"{"jsonrpc":"2.0","id":null,"error":{"code":0,"message":"","data":NaN}}"#,
    ] {
        assert!(RuntimeResponse::decode(raw, None).is_err());
    }
}

#[test]
fn time_conversion_floors_and_preserves_absolute_deadline() {
    assert_eq!(MonotonicMs::from_ns(999_999).to_ns(), 0);
    assert_eq!(MonotonicMs::from_ns(2_999_999).to_ns(), 2_000_000);
    let maximum = MonotonicMs::new(MAX_MONOTONIC_MS).unwrap();
    assert_eq!(maximum, MonotonicMs::from_ns(u64::MAX));
    assert_eq!(maximum.to_ns(), 18_446_744_073_709_000_000);
    assert!(MonotonicMs::new(MAX_MONOTONIC_MS + 1).is_none());

    let renew = r#"{"jsonrpc":"2.0","id":1,"method":"lease.renew","params":{"token":1,"expiresAtMs":144500}}"#;
    let ControlCall::LeaseRenew(lease) = BellisRequest::decode(renew.as_bytes()).unwrap().call
    else {
        panic!("expected lease renewal");
    };
    let received_ns = 144_350_000_000;
    assert_eq!(lease.expires_at_ms.to_ns() - received_ns, 150_000_000);
}
