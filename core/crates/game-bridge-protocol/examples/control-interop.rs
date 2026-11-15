//! 供 pytest 用冻结的 Bellis 产物核对控制连接的双向编解码。

use std::io;

use game_bridge_protocol::{BellisRequest, MonotonicMs, RuntimeNotification, RuntimeResponse};
use serde::{Deserialize, Serialize};

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum Sender {
    Bellis,
    Runtime,
}

#[derive(Deserialize)]
struct Case {
    sender: Sender,
    raw: String,
    request: Option<String>,
}

#[derive(Serialize)]
#[serde(untagged)]
enum Message {
    Request(BellisRequest),
    Notification(RuntimeNotification),
    Response(RuntimeResponse),
}

impl Case {
    fn decode(&self) -> Option<Message> {
        let raw = self.raw.as_bytes();
        match (&self.sender, &self.request) {
            (Sender::Bellis, None) => BellisRequest::decode(raw).ok().map(Message::Request),
            (Sender::Runtime, None) => RuntimeNotification::decode(raw)
                .ok()
                .map(Message::Notification),
            (Sender::Runtime, Some(request)) => {
                let request = BellisRequest::decode(request.as_bytes()).ok()?;
                RuntimeResponse::decode(raw, Some(&request))
                    .ok()
                    .map(Message::Response)
            }
            (Sender::Bellis, Some(_)) => None,
        }
    }
}

#[derive(Serialize)]
struct Report {
    messages: Vec<Option<String>>,
    now_ms: MonotonicMs,
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let cases: Vec<Case> = serde_json::from_reader(io::stdin().lock())?;
    let report = Report {
        messages: cases
            .iter()
            .map(|case| {
                case.decode()
                    .map(|message| serde_json::to_string(&message))
                    .transpose()
            })
            .collect::<Result<_, _>>()?,
        now_ms: MonotonicMs::from_ns(mono_clock::now_ns()),
    };
    serde_json::to_writer(io::stdout().lock(), &report)?;
    Ok(())
}
