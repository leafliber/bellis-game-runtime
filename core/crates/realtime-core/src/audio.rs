//! 音频环挂接、音频目标与音频线程（`docs/modules/realtime-core.md` 的“音频主循环”）。
//!
//! - `audio_ring.attach` 与 `audio.set_target` 的处理函数在 `RpcPeer` 的读线程上核对参数、打开共享内存，
//!   再把音频环的写入方或音频目标作为命令交给音频线程，随即回应。
//! - 音频线程独占写入方与音频源：音频环在实时核里只有这一个写方；WASAPI 音频源在音频线程上按音频环的
//!   格式创建并使用。
//! - 音频的采集状态与本实例最近写进音频环的一块放在 [`AudioStatus`] 里，它有自己的锁，音频线程只在更新的
//!   一瞬间持有。音频线程从不碰控制状态的锁与 `FrameStatus`，释放与权威检查也不读音频，所以音频线程卡住
//!   或写失败不影响释放与画面采集。

use std::fmt;
use std::io;
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::thread;
use std::time::{Duration, Instant};

use audio_capture::{AudioError, AudioFormat, AudioPacket, AudioSource};
use audio_ring::{AudioRingError, AudioRingWriter, BlockMeta, FakeAudioCapture, WriteError};
use capture::CaptureError;
use core_protocol::messages::{
    AudioTarget, AudioWindowTarget, CaptureState, FakeAudioTarget, LatestAudio,
};

use crate::frames::check_window_rule;

/// 没有音频源时重新识别的间隔。
const SEARCH_INTERVAL: Duration = Duration::from_millis(500);
/// 每次等包的时限：命令最多等这么久才被处理。
const PACKET_WAIT: Duration = Duration::from_millis(50);

// ---------------------------------------------------------------- 音频状态

/// 音频的采集状态与本实例最近写进音频环的一块，供 `core.health` 读取。
#[derive(Debug)]
pub(crate) struct AudioStatus {
    inner: Mutex<(CaptureState, Option<LatestAudio>)>,
}

impl Default for AudioStatus {
    fn default() -> Self {
        Self {
            inner: Mutex::new((CaptureState::Idle, None)),
        }
    }
}

impl AudioStatus {
    fn lock(&self) -> MutexGuard<'_, (CaptureState, Option<LatestAudio>)> {
        self.inner.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// 采集状态，以及本实例最近写进音频环的一块。
    pub(crate) fn snapshot(&self) -> (CaptureState, Option<LatestAudio>) {
        *self.lock()
    }

    /// 改变采集状态，返回原来的状态。
    fn set_state(&self, state: CaptureState) -> CaptureState {
        std::mem::replace(&mut self.lock().0, state)
    }

    /// 换了音频环：最近写入的一块属于原来的音频环，清空。
    fn forget(&self) {
        self.lock().1 = None;
    }

    fn record(&self, latest: LatestAudio) {
        self.lock().1 = Some(latest);
    }
}

// ---------------------------------------------------------------- 音频目标与声音的去处

/// 建立音频源失败的原因。
#[derive(Debug)]
pub(crate) enum OpenError {
    /// 按窗口识别规则找不到唯一的窗口。
    #[cfg_attr(
        not(windows),
        allow(dead_code, reason = "窗口目标只在 Windows 上建立音频源")
    )]
    Window(CaptureError),
    /// 音频源建立失败：进程已退出、系统不支持、系统调用失败、格式不符等。
    Audio(AudioError),
}

impl fmt::Display for OpenError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Window(error) => error.fmt(f),
            Self::Audio(error) => error.fmt(f),
        }
    }
}

/// 音频目标：在音频线程上按音频环的格式建立音频源，音频源失效后按同一目标重新建立（重新识别窗口）。
///
/// 处理函数把它交给音频线程，因为 WASAPI 音频源在哪个线程创建就在哪个线程使用。
pub(crate) trait AudioOpener: Send + fmt::Display {
    /// 按 `format` 建立音频源。找不到窗口等失败时返回错误，音频线程过一段时间再试。
    fn open(&mut self, format: AudioFormat) -> Result<Box<dyn AudioSource>, OpenError>;
}

/// 声音的去处，即音频环的写入方；单元测试以记录包的替身代替共享内存。
pub(crate) trait AudioSink: Send {
    /// 音频环的格式。
    fn format(&self) -> AudioFormat;
    /// 写入一个包，返回写出的最后一块。
    fn write(&mut self, packet: &AudioPacket) -> Result<BlockMeta, WriteError>;
}

impl AudioSink for AudioRingWriter {
    fn format(&self) -> AudioFormat {
        Self::format(self)
    }

    fn write(&mut self, packet: &AudioPacket) -> Result<BlockMeta, WriteError> {
        Self::write(self, packet)
    }
}

/// `audio.set_target` 不接受的目标。
#[derive(Debug)]
pub(crate) enum TargetError {
    /// 参数不合规则：进程名为空，或窗口类与标题都没给出。
    Invalid(&'static str),
    /// 这个平台不支持：非 Windows 上的窗口目标。
    #[cfg_attr(windows, allow(dead_code, reason = "只在非 Windows 上报"))]
    Unsupported(String),
    /// 打不开假音频输出，或它的头部与参数不符。
    Output(AudioRingError),
}

impl fmt::Display for TargetError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Invalid(reason) => f.write_str(reason),
            Self::Unsupported(reason) => write!(f, "unsupported: {reason}"),
            Self::Output(error) => write!(f, "fake audio output: {error}"),
        }
    }
}

/// 核对音频目标，返回音频线程用的 [`AudioOpener`]。假音频输出在这里先打开一次，打不开即报错。
pub(crate) fn audio_opener_for(target: AudioTarget) -> Result<Box<dyn AudioOpener>, TargetError> {
    match target {
        AudioTarget::Window(window) => {
            check_window_rule(
                &window.process_name,
                window.window_class.as_deref(),
                window.title.as_deref(),
            )
            .map_err(TargetError::Invalid)?;
            window_opener(window)
        }
        AudioTarget::Fake(fake) => {
            let opened = FakeAudioCapture::open(&fake).map_err(TargetError::Output)?;
            Ok(Box::new(FakeOpener {
                target: fake,
                opened: Some(opened),
            }))
        }
    }
}

#[cfg(windows)]
#[allow(
    clippy::unnecessary_wraps,
    reason = "与非 Windows 上的实现同一签名，那边返回不支持"
)]
fn window_opener(window: AudioWindowTarget) -> Result<Box<dyn AudioOpener>, TargetError> {
    Ok(Box::new(WindowOpener {
        selector: capture::WindowSelector {
            process_name: Some(window.process_name),
            class_name: window.window_class,
            title: window.title,
        },
    }))
}

#[cfg(not(windows))]
#[allow(
    clippy::needless_pass_by_value,
    reason = "与 Windows 上的实现同一签名，那边取走它的字段"
)]
fn window_opener(_: AudioWindowTarget) -> Result<Box<dyn AudioOpener>, TargetError> {
    Err(TargetError::Unsupported(
        "process loopback capture uses WASAPI, which is only available on Windows".into(),
    ))
}

/// 按窗口识别规则找到窗口，以 WASAPI 进程回环采它所属进程及其子进程的声音。
#[cfg(windows)]
struct WindowOpener {
    selector: capture::WindowSelector,
}

#[cfg(windows)]
impl AudioOpener for WindowOpener {
    fn open(&mut self, format: AudioFormat) -> Result<Box<dyn AudioSource>, OpenError> {
        let window = capture::win::find_window(&self.selector).map_err(OpenError::Window)?;
        let source = audio_capture::win::ProcessLoopback::new(window.process_id, format)
            .map_err(OpenError::Audio)?;
        Ok(Box::new(source))
    }
}

#[cfg(windows)]
impl fmt::Display for WindowOpener {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "the process of the window matching {}", self.selector)
    }
}

/// 假音频源：第一次用处理函数打开并核对过的那个，失效后按同一参数重新打开。只用于自动测试。
struct FakeOpener {
    target: FakeAudioTarget,
    opened: Option<FakeAudioCapture>,
}

impl AudioOpener for FakeOpener {
    fn open(&mut self, format: AudioFormat) -> Result<Box<dyn AudioSource>, OpenError> {
        let source = match self.opened.take() {
            Some(source) => source,
            None => FakeAudioCapture::open(&self.target).map_err(|error| {
                OpenError::Audio(AudioError::Os {
                    context: "FakeAudioCapture::open",
                    code: 0,
                    message: error.to_string(),
                })
            })?,
        };
        if source.format() != format {
            return Err(OpenError::Audio(AudioError::FormatMismatch(format!(
                "the fake output is {}, the audio ring is {format}",
                source.format()
            ))));
        }
        Ok(Box::new(source))
    }
}

impl fmt::Display for FakeOpener {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "the fake audio output {:?}", self.target.output.name)
    }
}

// ---------------------------------------------------------------- 音频线程

/// 交给音频线程的命令。
enum Command {
    /// `audio_ring.attach`：换成这个音频环。
    Attach(Box<dyn AudioSink>),
    /// `audio.set_target`：先停掉旧的音频源，再按新目标建立。
    Target(Box<dyn AudioOpener>),
    /// 会话结束。
    Stop,
}

/// 处理函数经它把命令交给音频线程。
pub(crate) struct Audio {
    commands: Sender<Command>,
}

/// 音频线程已经停止，命令没有送达。
#[derive(Debug)]
pub(crate) struct AudioStopped;

/// 音频线程本身，由会话持有，会话结束时停掉。
pub(crate) struct AudioThread {
    commands: Sender<Command>,
    exited: Receiver<()>,
}

impl Audio {
    /// 启动音频线程。线程一开始处于 `idle`，等音频环与音频目标都到了才建立音频源。
    pub(crate) fn spawn(status: Arc<AudioStatus>) -> io::Result<(Self, AudioThread)> {
        let (commands, receiver) = mpsc::channel();
        let (exited_sender, exited) = mpsc::channel::<()>();
        thread::Builder::new().name("audio".into()).spawn(move || {
            // 线程结束（音频源与写入方都已丢弃）时随之丢弃，等待方由此得知。
            let _exited = exited_sender;
            run(&receiver, status);
            // 先于 `_exited` 丢弃：等待方得知结束时，命令已一定送不到。
            drop(receiver);
        })?;
        Ok((
            Self {
                commands: commands.clone(),
            },
            AudioThread { commands, exited },
        ))
    }

    /// 换成 `writer` 指向的音频环。
    pub(crate) fn attach(&self, writer: AudioRingWriter) -> Result<(), AudioStopped> {
        self.send(Command::Attach(Box::new(writer)))
    }

    /// 设定音频目标；旧的音频源先停掉。
    pub(crate) fn set_target(&self, opener: Box<dyn AudioOpener>) -> Result<(), AudioStopped> {
        self.send(Command::Target(opener))
    }

    fn send(&self, command: Command) -> Result<(), AudioStopped> {
        self.commands.send(command).map_err(|_| AudioStopped)
    }
}

impl AudioThread {
    /// 让音频线程停掉音频源并结束，至多等 `timeout`；返回它是否已结束。等不到也不影响退出。
    pub(crate) fn stop(self, timeout: Duration) -> bool {
        let _ = self.commands.send(Command::Stop);
        matches!(
            self.exited.recv_timeout(timeout),
            Err(RecvTimeoutError::Disconnected)
        )
    }
}

/// 音频线程的循环：等命令，至多等 [`Pipeline::step`] 给出的时间，然后再做一轮。
fn run(commands: &Receiver<Command>, status: Arc<AudioStatus>) {
    let mut pipeline = Pipeline::new(status);
    let mut wait = None;
    loop {
        let first = match wait {
            None => commands.recv().map_err(|_| RecvTimeoutError::Disconnected),
            Some(timeout) => commands.recv_timeout(timeout),
        };
        let mut next = match first {
            Ok(command) => Some(command),
            Err(RecvTimeoutError::Timeout) => None,
            Err(RecvTimeoutError::Disconnected) => return,
        };
        while let Some(command) = next {
            if !pipeline.apply(command) {
                return;
            }
            next = commands.try_recv().ok();
        }
        wait = pipeline.step(Instant::now());
    }
}

/// 音频线程的状态机，不含线程与通道。转换见模块文档的“音频主循环”。
struct Pipeline {
    status: Arc<AudioStatus>,
    sink: Option<Box<dyn AudioSink>>,
    opener: Option<Box<dyn AudioOpener>>,
    source: Option<Box<dyn AudioSource>>,
    /// 音频源按这个格式建立；音频环换了格式时重建。
    source_format: Option<AudioFormat>,
    packet: AudioPacket,
    /// 下一个写入的块标不连续：新的音频源、新的音频环或丢了包之后。
    discontinuity: bool,
    /// 没有音频源时，到这个时刻再建立；`None` 表示立即建立。
    retry_at: Option<Instant>,
    /// 最近记录的问题；同样的问题只记一次。
    problem: Option<String>,
}

impl Pipeline {
    fn new(status: Arc<AudioStatus>) -> Self {
        Self {
            status,
            sink: None,
            opener: None,
            source: None,
            source_format: None,
            packet: AudioPacket::default(),
            discontinuity: true,
            retry_at: None,
            problem: None,
        }
    }

    /// 处理一条命令；返回 `false` 表示结束。
    fn apply(&mut self, command: Command) -> bool {
        match command {
            Command::Attach(sink) => {
                // 最近写入的一块属于原来的音频环。
                self.status.forget();
                if self.source_format != Some(sink.format()) {
                    // 音频源按旧音频环的格式建立：按新格式重建。
                    self.source = None;
                    self.retry_at = None;
                }
                self.sink = Some(sink);
                self.discontinuity = true;
            }
            Command::Target(opener) => {
                // 先停掉旧的音频源，新目标的音频源在此之后才建立。
                self.source = None;
                self.opener = Some(opener);
                self.retry_at = None;
                self.problem = None;
            }
            Command::Stop => {
                self.source = None;
                return false;
            }
        }
        true
    }

    /// 做一轮工作，返回下一轮之前最多等多久命令；`None` 表示一直等到下一条命令。
    fn step(&mut self, now: Instant) -> Option<Duration> {
        let Some(format) = self.sink.as_ref().map(|sink| sink.format()) else {
            self.enter(CaptureState::Idle, &"waiting for audio_ring.attach");
            return None;
        };
        let Some(opener) = self.opener.as_mut() else {
            self.enter(CaptureState::Idle, &"waiting for audio.set_target");
            return None;
        };
        if self.source.is_none() {
            if let Some(at) = self.retry_at
                && now < at
            {
                return Some(at - now);
            }
            match opener.open(format) {
                Ok(source) => {
                    let why = opener.to_string();
                    self.source = Some(source);
                    self.source_format = Some(format);
                    self.retry_at = None;
                    self.discontinuity = true;
                    self.enter(CaptureState::Capturing, &why);
                }
                Err(error) => {
                    self.search(now, &error);
                    return Some(SEARCH_INTERVAL);
                }
            }
        }
        let source = self.source.as_mut()?;
        match source.next_packet(PACKET_WAIT, &mut self.packet) {
            Ok(true) => {
                self.write();
                Some(Duration::ZERO)
            }
            Ok(false) => Some(Duration::ZERO),
            Err(error) => {
                // 音频源不再产出声音：丢掉它，过一段时间重新识别。
                self.source = None;
                self.search(now, &OpenError::Audio(error));
                Some(SEARCH_INTERVAL)
            }
        }
    }

    /// 没有音频源：进入 `searching`，到 [`SEARCH_INTERVAL`] 之后再建立。
    fn search(&mut self, now: Instant, error: &OpenError) {
        self.retry_at = Some(now + SEARCH_INTERVAL);
        self.enter(CaptureState::Searching, error);
        self.report(error.to_string());
    }

    fn write(&mut self) {
        let Some(sink) = self.sink.as_mut() else {
            return;
        };
        if self.discontinuity {
            self.packet.discontinuity = true;
        }
        match sink.write(&self.packet) {
            Ok(last) => {
                self.discontinuity = false;
                self.status.record(LatestAudio {
                    block_number: last.block_number,
                    capture_ns: last.capture_ns,
                });
                if let Some(problem) = self.problem.take() {
                    log!("audio: blocks reach the audio ring again (was: {problem})");
                }
            }
            // 音频环不受影响；丢了这一段声音，下一块标不连续。
            Err(error) => {
                self.discontinuity = true;
                self.report(format!("audio packet not written: {error}"));
            }
        }
    }

    fn enter(&self, state: CaptureState, why: &dyn fmt::Display) {
        let previous = self.status.set_state(state);
        if previous != state {
            log!("audio {previous:?} -> {state:?}: {why}");
        }
    }

    fn report(&mut self, problem: String) {
        if self.problem.as_ref() != Some(&problem) {
            log!("audio: {problem}");
            self.problem = Some(problem);
        }
    }
}

#[cfg(test)]
mod tests;
