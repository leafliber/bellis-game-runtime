//! 跨语言测试用的 Rust 一方，由 `runtime/tests/audio_ring/test_interop.py` 编译并拉起。只用于测试。
//!
//! 参数是核协议消息的 JSON：`<ring>` 为 `audio_ring.attach` 的 params，`<target>` 为
//! `audio.set_target` 的 params（须是 `fake` 分支）。结果逐行写到标准输出，失败时以 1 退出。
//!
//! - `attach <ring>`：挂接并核对，输出 `ok <write_seq>`。
//! - `write <ring> <count>`：按 [`pattern`] 尽快写 `count` 块，输出 `ok <最后的块号>`。
//! - `read <target> <last>`：先输出 `ready`，再经假音频源从（空的）假音频输出逐块读，直到读到块号为 `last` 的
//!   一块；每块按 [`pattern`] 核对，块号跳过时须标不连续、连续时不标；输出 `ok <读到的块数> <跳过的块数>`。
//!   10 秒内没有新块即失败。
//!
//! [`pattern`] 要求块容量至少为 7。

use std::io::Write;
use std::process::ExitCode;
use std::time::{Duration, Instant};

use audio_capture::{AudioPacket, AudioSource};
use audio_ring::{AudioRingWriter, FakeAudioCapture};
use core_protocol::messages::{
    AudioRingAttachParams, AudioSetTargetParams, AudioTarget, FakeAudioTarget,
};

/// 块号为 `number` 的块的图案：采样点数与采集时刻由块号算出，每个样本值都是同一个由块号算出的 4 字节。
/// 读方只凭元数据就能核对整块，撕裂的块因此一定能被发现。Python 一方有同样的定义。
fn pattern(number: u64, capacity: u32, channels: u32) -> AudioPacket {
    let sample_count = capacity - u32::try_from(number % 7).expect("small");
    #[allow(clippy::cast_possible_truncation, reason = "只取低 32 位作图案")]
    let word = (number as u32).wrapping_mul(0x9E37_79B1).to_le_bytes();
    AudioPacket {
        capture_ns: number * 1000 + 7,
        sample_count,
        data: word.repeat(sample_count as usize * channels as usize),
        discontinuity: false,
    }
}

fn parse<T: serde::de::DeserializeOwned>(what: &str, text: Option<&String>) -> Result<T, String> {
    let text = text.ok_or(format!("missing {what}"))?;
    serde_json::from_str(text).map_err(|error| format!("bad {what}: {error}"))
}

fn fake_target(text: Option<&String>) -> Result<FakeAudioTarget, String> {
    let params: AudioSetTargetParams = parse("target", text)?;
    match params.target {
        AudioTarget::Fake(target) => Ok(target),
        AudioTarget::Window(_) => Err("the target is not fake".into()),
    }
}

fn number(text: Option<&String>) -> Result<u64, String> {
    text.ok_or("missing number")?
        .parse()
        .map_err(|_| "the number must be a whole number".to_owned())
}

fn attach(args: &[String]) -> Result<(), String> {
    let params: AudioRingAttachParams = parse("ring", args.first())?;
    let writer = AudioRingWriter::attach(&params).map_err(|error| error.to_string())?;
    println!("ok {}", writer.last_block_number());
    Ok(())
}

fn write(args: &[String]) -> Result<(), String> {
    let params: AudioRingAttachParams = parse("ring", args.first())?;
    let count = number(args.get(1))?;
    let mut writer = AudioRingWriter::attach(&params).map_err(|error| error.to_string())?;
    for _ in 0..count {
        let next = writer.last_block_number() + 1;
        let block = pattern(next, params.block_capacity, params.channels);
        writer.write(&block).map_err(|error| error.to_string())?;
    }
    println!("ok {}", writer.last_block_number());
    Ok(())
}

fn read(args: &[String]) -> Result<(), String> {
    let target = fake_target(args.first())?;
    let last = number(args.get(1))?;
    let (capacity, channels) = (target.output.block_capacity, target.output.channels);
    let mut source = FakeAudioCapture::open(&target).map_err(|error| error.to_string())?;
    let mut packet = AudioPacket::default();
    // 假音频输出是空的：第一块应是块 1。
    let (mut got, mut skipped, mut previous) = (0u64, 0u64, 0u64);
    let mut last_new = Instant::now();
    // 准备好了再让 Python 开始写，免得在打开之前就写完了。
    println!("ready");
    std::io::stdout().flush().map_err(|e| e.to_string())?;
    while previous != last {
        if !source
            .next_packet(Duration::from_millis(100), &mut packet)
            .map_err(|error| error.to_string())?
        {
            if last_new.elapsed() > Duration::from_secs(10) {
                return Err(format!("no new block for 10 s after block {previous}"));
            }
            continue;
        }
        last_new = Instant::now();
        let number = (packet.capture_ns - 7) / 1000;
        let mut expected = pattern(number, capacity, channels);
        if number <= previous {
            return Err(format!("block {number} came after block {previous}"));
        }
        expected.discontinuity = number != previous + 1;
        if packet != expected {
            return Err(format!(
                "block {number}: got {} samples, discontinuity {}; expected {} samples, discontinuity {}, or the values differ",
                packet.sample_count,
                packet.discontinuity,
                expected.sample_count,
                expected.discontinuity
            ));
        }
        skipped += number - previous - 1;
        got += 1;
        previous = number;
    }
    println!("ok {got} {skipped}");
    Ok(())
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let rest = args.get(1..).unwrap_or_default();
    let result = match args.first().map(String::as_str) {
        Some("attach") => attach(rest),
        Some("write") => write(rest),
        Some("read") => read(rest),
        _ => Err("usage: audio_ring_interop attach|write|read ...".into()),
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => {
            println!("error {message}");
            ExitCode::FAILURE
        }
    }
}
