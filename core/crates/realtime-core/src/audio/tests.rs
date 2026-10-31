//! 音频线程的状态机：以按脚本交包或报错的音频源与记录包的替身核对每条转换。

use std::collections::VecDeque;
use std::fmt;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use audio_capture::{AudioError, AudioFormat, AudioPacket, AudioSource};
use audio_ring::{BlockMeta, WriteError};
use capture::CaptureError;
use core_protocol::messages::{AudioTarget, AudioWindowTarget, CaptureState, LatestAudio};

use super::{
    Audio, AudioOpener, AudioSink, AudioStatus, Command, OpenError, PACKET_WAIT, Pipeline,
    SEARCH_INTERVAL, TargetError, audio_opener_for,
};

type Log = Arc<Mutex<Vec<String>>>;
type Step = Result<Option<AudioPacket>, AudioError>;

const STEREO: AudioFormat = AudioFormat {
    sample_rate: 48_000,
    channels: 2,
};
const MONO: AudioFormat = AudioFormat {
    sample_rate: 16_000,
    channels: 1,
};

fn entries(log: &Log) -> Vec<String> {
    log.lock().unwrap().clone()
}

/// 一个包：采集时刻为 `1000 × number`，`samples` 个采样点。
fn packet(number: u64, samples: u32) -> AudioPacket {
    AudioPacket {
        capture_ns: 1_000 * number,
        sample_count: samples,
        data: vec![0; samples as usize * 8],
        discontinuity: false,
    }
}

/// 按脚本交包或报错的音频源，脚本用完后一直没有新包；被丢弃时记一笔。
struct ScriptedSource {
    name: &'static str,
    steps: VecDeque<Step>,
    log: Log,
}

impl AudioSource for ScriptedSource {
    fn next_packet(
        &mut self,
        timeout: Duration,
        out: &mut AudioPacket,
    ) -> Result<bool, AudioError> {
        assert_eq!(timeout, PACKET_WAIT);
        match self.steps.pop_front() {
            None | Some(Ok(None)) => Ok(false),
            Some(Ok(Some(packet))) => {
                *out = packet;
                Ok(true)
            }
            Some(Err(error)) => Err(error),
        }
    }
}

impl Drop for ScriptedSource {
    fn drop(&mut self) {
        self.log.lock().unwrap().push(format!("drop {}", self.name));
    }
}

/// 按脚本建立音频源的目标：每次建立取下一份脚本并记下格式，脚本用完后报找不到窗口。
struct ScriptedOpener {
    name: &'static str,
    opens: VecDeque<Result<Vec<Step>, OpenError>>,
    log: Log,
}

impl ScriptedOpener {
    fn boxed(
        name: &'static str,
        opens: Vec<Result<Vec<Step>, OpenError>>,
        log: &Log,
    ) -> Box<dyn AudioOpener> {
        Box::new(Self {
            name,
            opens: opens.into(),
            log: Arc::clone(log),
        })
    }
}

impl AudioOpener for ScriptedOpener {
    fn open(&mut self, format: AudioFormat) -> Result<Box<dyn AudioSource>, OpenError> {
        self.log
            .lock()
            .unwrap()
            .push(format!("open {} at {} Hz", self.name, format.sample_rate));
        let steps = self.opens.pop_front().unwrap_or_else(|| Err(not_found()))?;
        Ok(Box::new(ScriptedSource {
            name: self.name,
            steps: steps.into(),
            log: Arc::clone(&self.log),
        }))
    }
}

impl fmt::Display for ScriptedOpener {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.name)
    }
}

/// 记下写入的包（采集时刻与不连续标志），块号从 100 起每个包加一；采样点数为 0 的包写不进去。
struct RecordingSink {
    format: AudioFormat,
    next: u64,
    written: Arc<Mutex<Vec<(u64, bool)>>>,
}

impl RecordingSink {
    fn boxed(format: AudioFormat, written: &Arc<Mutex<Vec<(u64, bool)>>>) -> Box<dyn AudioSink> {
        Box::new(Self {
            format,
            next: 100,
            written: Arc::clone(written),
        })
    }
}

impl AudioSink for RecordingSink {
    fn format(&self) -> AudioFormat {
        self.format
    }

    fn write(&mut self, packet: &AudioPacket) -> Result<BlockMeta, WriteError> {
        if packet.sample_count == 0 {
            return Err(WriteError::InvalidPacket("no samples".into()));
        }
        self.written
            .lock()
            .unwrap()
            .push((packet.capture_ns, packet.discontinuity));
        self.next += 1;
        Ok(BlockMeta {
            block_number: self.next,
            capture_ns: packet.capture_ns,
            sample_count: packet.sample_count,
            flags: 0,
        })
    }
}

fn not_found() -> OpenError {
    OpenError::Window(CaptureError::WindowNotFound {
        selector: "title \"Game\"".into(),
    })
}

/// 一条音频管线与它的替身。
struct Harness {
    pipeline: Pipeline,
    status: Arc<AudioStatus>,
    log: Log,
    written: Arc<Mutex<Vec<(u64, bool)>>>,
}

impl Harness {
    fn new() -> Self {
        let status = Arc::new(AudioStatus::default());
        Self {
            pipeline: Pipeline::new(Arc::clone(&status)),
            status,
            log: Log::default(),
            written: Arc::default(),
        }
    }

    fn attach(&mut self, format: AudioFormat) {
        assert!(
            self.pipeline
                .apply(Command::Attach(RecordingSink::boxed(format, &self.written)))
        );
    }

    fn target(&mut self, name: &'static str, opens: Vec<Result<Vec<Step>, OpenError>>) {
        let opener = ScriptedOpener::boxed(name, opens, &self.log);
        assert!(self.pipeline.apply(Command::Target(opener)));
    }

    fn state(&self) -> CaptureState {
        self.status.snapshot().0
    }

    fn latest(&self) -> Option<LatestAudio> {
        self.status.snapshot().1
    }

    fn written(&self) -> Vec<(u64, bool)> {
        self.written.lock().unwrap().clone()
    }
}

#[test]
fn idle_until_both_a_ring_and_a_target_are_given() {
    let mut h = Harness::new();
    let now = Instant::now();
    assert_eq!(h.pipeline.step(now), None);
    assert_eq!(h.state(), CaptureState::Idle);
    h.target("game", vec![Ok(vec![])]);
    assert_eq!(h.pipeline.step(now), None);
    assert_eq!(h.state(), CaptureState::Idle);
    assert_eq!(entries(&h.log), Vec::<String>::new());
    h.attach(STEREO);
    assert_eq!(h.pipeline.step(now), Some(Duration::ZERO));
    assert_eq!(h.state(), CaptureState::Capturing);
    assert_eq!(entries(&h.log), ["open game at 48000 Hz"]);
}

#[test]
fn packets_are_written_and_the_first_after_a_new_source_is_discontinuous() {
    let mut h = Harness::new();
    h.attach(STEREO);
    h.target(
        "game",
        vec![Ok(vec![
            Ok(Some(packet(1, 480))),
            Ok(None),
            Ok(Some(packet(2, 480))),
        ])],
    );
    let now = Instant::now();
    for _ in 0..3 {
        assert_eq!(h.pipeline.step(now), Some(Duration::ZERO));
    }
    assert_eq!(h.written(), [(1_000, true), (2_000, false)]);
    assert_eq!(
        h.latest(),
        Some(LatestAudio {
            block_number: 102,
            capture_ns: 2_000,
        })
    );
}

#[test]
fn a_missing_window_is_searched_for_again_after_the_interval() {
    let mut h = Harness::new();
    h.attach(STEREO);
    h.target("game", vec![Err(not_found()), Ok(vec![])]);
    let now = Instant::now();
    assert_eq!(h.pipeline.step(now), Some(SEARCH_INTERVAL));
    assert_eq!(h.state(), CaptureState::Searching);
    // 间隔未到：不重新识别。
    let soon = now + SEARCH_INTERVAL / 2;
    assert_eq!(h.pipeline.step(soon), Some(SEARCH_INTERVAL / 2));
    assert_eq!(entries(&h.log).len(), 1);
    assert_eq!(h.pipeline.step(now + SEARCH_INTERVAL), Some(Duration::ZERO));
    assert_eq!(h.state(), CaptureState::Capturing);
    assert_eq!(entries(&h.log).len(), 2);
}

#[test]
fn a_failing_source_is_dropped_and_its_replacement_starts_discontinuous() {
    let mut h = Harness::new();
    h.attach(STEREO);
    h.target(
        "game",
        vec![
            Ok(vec![
                Ok(Some(packet(1, 480))),
                Ok(Some(packet(2, 480))),
                Err(AudioError::ProcessExited),
            ]),
            Ok(vec![Ok(Some(packet(9, 480)))]),
        ],
    );
    let now = Instant::now();
    h.pipeline.step(now);
    h.pipeline.step(now);
    assert_eq!(h.pipeline.step(now), Some(SEARCH_INTERVAL));
    assert_eq!(h.state(), CaptureState::Searching);
    assert_eq!(entries(&h.log), ["open game at 48000 Hz", "drop game"]);
    // 最近写入的一块不变。
    assert_eq!(h.latest().map(|latest| latest.capture_ns), Some(2_000));
    let later = now + SEARCH_INTERVAL;
    assert_eq!(h.pipeline.step(later), Some(Duration::ZERO));
    assert_eq!(h.state(), CaptureState::Capturing);
    assert_eq!(h.written(), [(1_000, true), (2_000, false), (9_000, true)]);
}

#[test]
fn a_new_target_stops_the_old_source_first() {
    let mut h = Harness::new();
    h.attach(STEREO);
    h.target("old", vec![Ok(vec![])]);
    h.pipeline.step(Instant::now());
    h.target("new", vec![Ok(vec![Ok(Some(packet(5, 10)))])]);
    assert_eq!(entries(&h.log), ["open old at 48000 Hz", "drop old"]);
    h.pipeline.step(Instant::now());
    assert_eq!(
        entries(&h.log),
        ["open old at 48000 Hz", "drop old", "open new at 48000 Hz"]
    );
    assert_eq!(h.written(), [(5_000, true)]);
}

#[test]
fn a_packet_that_cannot_be_written_leaves_the_latest_and_marks_the_gap() {
    let mut h = Harness::new();
    h.attach(STEREO);
    h.target(
        "game",
        vec![Ok(vec![
            Ok(Some(packet(1, 480))),
            Ok(Some(packet(2, 0))),
            Ok(Some(packet(3, 480))),
        ])],
    );
    let now = Instant::now();
    for _ in 0..3 {
        h.pipeline.step(now);
    }
    assert_eq!(h.written(), [(1_000, true), (3_000, true)]);
    assert_eq!(h.state(), CaptureState::Capturing);
}

#[test]
fn attaching_another_ring_forgets_the_latest_and_rebuilds_on_a_new_format() {
    let mut h = Harness::new();
    h.attach(STEREO);
    h.target(
        "game",
        vec![
            Ok(vec![Ok(Some(packet(1, 480))), Ok(Some(packet(2, 480)))]),
            Ok(vec![Ok(Some(packet(3, 160)))]),
        ],
    );
    let now = Instant::now();
    h.pipeline.step(now);
    assert!(h.latest().is_some());
    // 同一格式：音频源保留，最近一块清空，下一块标不连续。
    h.attach(STEREO);
    assert_eq!(h.latest(), None);
    h.pipeline.step(now);
    assert_eq!(h.written(), [(1_000, true), (2_000, true)]);
    // 新格式：音频源按新格式重建。
    h.attach(MONO);
    h.pipeline.step(now);
    assert_eq!(
        entries(&h.log),
        [
            "open game at 48000 Hz",
            "drop game",
            "open game at 16000 Hz"
        ]
    );
    assert_eq!(h.written().last(), Some(&(3_000, true)));
}

#[test]
fn stop_drops_the_source() {
    let mut h = Harness::new();
    h.attach(STEREO);
    h.target("game", vec![Ok(vec![])]);
    h.pipeline.step(Instant::now());
    assert!(!h.pipeline.apply(Command::Stop));
    assert_eq!(entries(&h.log), ["open game at 48000 Hz", "drop game"]);
}

#[test]
fn the_audio_thread_writes_packets_and_stops_promptly() {
    let status = Arc::new(AudioStatus::default());
    let (audio, thread) = Audio::spawn(Arc::clone(&status)).unwrap();
    let written = Arc::default();
    let log = Log::default();
    audio
        .send(Command::Attach(RecordingSink::boxed(STEREO, &written)))
        .unwrap();
    let steps = (1..=3).map(|n| Ok(Some(packet(n, 480)))).collect();
    audio
        .set_target(ScriptedOpener::boxed("game", vec![Ok(steps)], &log))
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    while written.lock().unwrap().len() < 3 {
        assert!(Instant::now() < deadline, "packets were not written");
        thread::sleep(Duration::from_millis(1));
    }
    assert_eq!(status.snapshot().0, CaptureState::Capturing);
    let started = Instant::now();
    assert!(thread.stop(Duration::from_secs(2)));
    assert!(started.elapsed() < Duration::from_secs(1));
    assert_eq!(entries(&log), ["open game at 48000 Hz", "drop game"]);
    assert!(
        audio
            .set_target(ScriptedOpener::boxed("late", vec![], &log))
            .is_err()
    );
}

fn window(process: &str, class: Option<&str>, title: Option<&str>) -> AudioTarget {
    AudioTarget::Window(AudioWindowTarget {
        process_name: process.into(),
        window_class: class.map(Into::into),
        title: title.map(Into::into),
    })
}

#[test]
fn window_targets_need_a_process_and_a_class_or_title() {
    assert!(matches!(
        audio_opener_for(window("", None, Some("Game"))),
        Err(TargetError::Invalid(_))
    ));
    assert!(matches!(
        audio_opener_for(window("game.exe", None, None)),
        Err(TargetError::Invalid(_))
    ));
}

#[cfg(windows)]
#[test]
fn window_targets_are_accepted_on_windows() {
    assert!(audio_opener_for(window("game.exe", None, Some("Game"))).is_ok());
}

#[cfg(not(windows))]
#[test]
fn window_targets_are_unsupported_off_windows() {
    assert!(matches!(
        audio_opener_for(window("game.exe", None, Some("Game"))),
        Err(TargetError::Unsupported(_))
    ));
}
