//! 跨语言测试用的 Rust 一方，由 `runtime/tests/frame_ring/test_interop.py` 编译并拉起。只用于测试。
//!
//! 参数是核协议消息的 JSON：`<ring>` 为 `frame_ring.attach` 的 params，`<target>` 为
//! `capture.set_target` 的 params（须是 `fake` 分支）。结果逐行写到标准输出，失败时以 1 退出。
//!
//! - `attach <ring>`：挂接并核对，输出 `ok <write_seq>`。
//! - `write <ring> <count>`：按 [`pattern`] 尽快写 `count` 帧（采集源帧号 1..=count），输出 `ok <最后的帧号>`。
//! - `pipe <ring> <target>`：假采集源取帧、写进帧环，每帧输出 `frame <帧环帧号> <假显示面帧号>`；
//!   标准输入关闭后输出 `ok` 并退出。
//! - `read <target> <count>`：先输出 `ready`，再经假采集源从假显示面读 `count` 帧，每帧按 [`pattern`]
//!   核对，输出 `ok <最后的帧号>`；10 秒内没有新帧即失败。
//!
//! [`pattern`] 要求帧环的最大尺寸至少为 7×5。

use std::io::{Read, Write};
use std::process::ExitCode;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant};

use capture::{CaptureSource, Frame, FrameMeta, Point};
use core_protocol::messages::{
    CaptureSetTargetParams, CaptureTarget, FakeTarget, FrameRingAttachParams,
};
use frame_ring::{FakeCapture, FrameRingWriter};

/// 第 `number` 帧的图案：尺寸、采集时刻与变换版本都由帧号算出，每个像素都是同一个由帧号算出的值。
/// 读方只凭元数据就能核对整帧，撕裂的帧因此一定能被发现。Python 一方有同样的定义。
fn pattern(number: u64, max_width: u32, max_height: u32) -> (FrameMeta, [u8; 4]) {
    let width = max_width - u32::try_from(number % 7).expect("small");
    let height = max_height - u32::try_from(number % 5).expect("small");
    #[allow(clippy::cast_possible_truncation, reason = "只取低 32 位作图案")]
    let word = (number as u32).wrapping_mul(0x9E37_79B1).to_le_bytes();
    let meta = FrameMeta {
        number,
        captured_at_ns: number * 1000 + 7,
        transform_version: number / 5 + 1,
        width,
        height,
        stride: width * 4,
        client_origin: Point { x: 0, y: 0 },
        dpi: 96,
    };
    (meta, word)
}

/// 按图案生成一帧，每行末尾带 4 字节填充，写入方须去掉它。
fn pattern_frame(number: u64, max_width: u32, max_height: u32) -> Frame {
    let (mut meta, word) = pattern(number, max_width, max_height);
    meta.stride += 4;
    let mut row = word.repeat(meta.width as usize);
    row.extend_from_slice(&[0xEE; 4]);
    Frame {
        meta,
        pixels: row.repeat(meta.height as usize),
    }
}

fn check_pattern(frame: &Frame, max_width: u32, max_height: u32) -> Result<(), String> {
    let (expected, word) = pattern(frame.meta.number, max_width, max_height);
    let meta = &frame.meta;
    let got = (
        meta.width,
        meta.height,
        meta.stride,
        meta.captured_at_ns,
        meta.transform_version,
    );
    let want = (
        expected.width,
        expected.height,
        expected.stride,
        expected.captured_at_ns,
        expected.transform_version,
    );
    if got != want {
        return Err(format!(
            "frame {}: metadata {got:?}, expected {want:?}",
            meta.number
        ));
    }
    if frame.pixels.len() != meta.stride as usize * meta.height as usize
        || !frame
            .pixels
            .as_chunks::<4>()
            .0
            .iter()
            .all(|pixel| *pixel == word)
    {
        return Err(format!(
            "frame {}: pixels do not match the pattern",
            meta.number
        ));
    }
    Ok(())
}

fn parse<T: serde::de::DeserializeOwned>(what: &str, text: Option<&String>) -> Result<T, String> {
    let text = text.ok_or(format!("missing {what}"))?;
    serde_json::from_str(text).map_err(|error| format!("bad {what}: {error}"))
}

fn fake_target(text: Option<&String>) -> Result<FakeTarget, String> {
    let params: CaptureSetTargetParams = parse("target", text)?;
    match params.target {
        CaptureTarget::Fake(target) => Ok(target),
        CaptureTarget::Window(_) => Err("the target is not fake".into()),
    }
}

fn count(text: Option<&String>) -> Result<u64, String> {
    text.ok_or("missing count")?
        .parse()
        .map_err(|_| "count must be a whole number".to_owned())
}

fn attach(args: &[String]) -> Result<(), String> {
    let params: FrameRingAttachParams = parse("ring", args.first())?;
    let writer = FrameRingWriter::attach(&params).map_err(|error| error.to_string())?;
    println!("ok {}", writer.last_frame_number());
    Ok(())
}

fn write(args: &[String]) -> Result<(), String> {
    let params: FrameRingAttachParams = parse("ring", args.first())?;
    let count = count(args.get(1))?;
    let mut writer = FrameRingWriter::attach(&params).map_err(|error| error.to_string())?;
    let mut last = writer.last_frame_number();
    for number in 1..=count {
        let frame = pattern_frame(number, params.max_width, params.max_height);
        last = writer.write(&frame).map_err(|error| error.to_string())?;
    }
    println!("ok {last}");
    Ok(())
}

fn pipe(args: &[String]) -> Result<(), String> {
    let params: FrameRingAttachParams = parse("ring", args.first())?;
    let target = fake_target(args.get(1))?;
    let mut writer = FrameRingWriter::attach(&params).map_err(|error| error.to_string())?;
    let mut capture = FakeCapture::open(&target).map_err(|error| error.to_string())?;
    let closed = Arc::new(AtomicBool::new(false));
    {
        let closed = closed.clone();
        thread::spawn(move || {
            let _ = std::io::stdin().read_to_end(&mut Vec::new());
            closed.store(true, Ordering::Release);
        });
    }
    let mut frame = Frame::default();
    let mut stdout = std::io::stdout().lock();
    while !closed.load(Ordering::Acquire) {
        if capture
            .next_frame(Duration::from_millis(20), &mut frame)
            .map_err(|error| error.to_string())?
        {
            let number = writer.write(&frame).map_err(|error| error.to_string())?;
            writeln!(stdout, "frame {number} {}", frame.meta.number).map_err(|e| e.to_string())?;
            stdout.flush().map_err(|e| e.to_string())?;
        }
    }
    writeln!(stdout, "ok").map_err(|e| e.to_string())?;
    Ok(())
}

fn read(args: &[String]) -> Result<(), String> {
    let target = fake_target(args.first())?;
    let count = count(args.get(1))?;
    let (max_width, max_height) = (target.surface.max_width, target.surface.max_height);
    let mut capture = FakeCapture::open(&target).map_err(|error| error.to_string())?;
    let mut frame = Frame::default();
    let mut frames = 0u64;
    let mut last_new = Instant::now();
    // 准备好了再让 Python 开始写，免得在打开之前就写完了。
    println!("ready");
    std::io::stdout().flush().map_err(|e| e.to_string())?;
    while frames < count {
        if capture
            .next_frame(Duration::from_millis(100), &mut frame)
            .map_err(|error| error.to_string())?
        {
            check_pattern(&frame, max_width, max_height)?;
            frames += 1;
            last_new = Instant::now();
        } else if last_new.elapsed() > Duration::from_secs(10) {
            return Err(format!(
                "no new frame for 10 s after frame {}",
                frame.meta.number
            ));
        }
    }
    println!("ok {}", frame.meta.number);
    Ok(())
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let rest = args.get(1..).unwrap_or_default();
    let result = match args.first().map(String::as_str) {
        Some("attach") => attach(rest),
        Some("write") => write(rest),
        Some("pipe") => pipe(rest),
        Some("read") => read(rest),
        _ => Err("usage: ring_interop attach|write|pipe|read ...".into()),
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => {
            println!("error {message}");
            ExitCode::FAILURE
        }
    }
}
