//! 帧环挂接、采集目标与采集线程（`docs/modules/realtime-core.md` 的“采集主循环”）。
//!
//! - `frame_ring.attach` 与 `capture.set_target` 的处理函数在 `RpcPeer` 的读线程上核对参数、打开共享内存，
//!   再把帧环的写入方或采集目标作为命令交给采集线程，随即回应。
//! - 采集线程独占写入方与采集源：帧环在实时核里只有这一个写方；WGC 采集源不是 `Send`，在采集线程上
//!   创建并使用。
//! - 采集状态、本实例最近写进帧环的一帧与客户区几何的变化放在 [`FrameStatus`] 里，它有自己的锁；最近
//!   一帧的采集时刻另存一份原子量。取帧、写帧都不持锁。换目标或丢弃采集源时，由控制侧一次作废帧与产出者代次。
//!   释放全部输入只用控制状态的锁，帧龄检查只读原子量，所以释放不等采集线程，也不依赖写帧成功。

use std::collections::VecDeque;
use std::fmt;
use std::io;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::thread;
use std::time::{Duration, Instant};

use capture::{CaptureError, CaptureSource, Frame, WindowIdentity};
use core_protocol::messages::{CaptureState, CaptureTarget, FakeTarget, LatestFrame, WindowTarget};
use frame_ring::{FakeCapture, FrameRingError, FrameRingWriter, WriteError};

/// 没有采集源时重新识别的间隔。
const SEARCH_INTERVAL: Duration = Duration::from_millis(500);
/// 每次等帧的时限：命令最多等这么久才被处理。
const FRAME_WAIT: Duration = Duration::from_millis(50);
/// 可恢复的错误（最小化、独占全屏）之后再取帧的间隔。
const RECOVER_INTERVAL: Duration = Duration::from_millis(100);

// ---------------------------------------------------------------- 采集状态

/// 记几次客户区几何的变化：基准帧比最早一次记下的变化还早时，几何已查不到。
const GEOMETRY_HISTORY: usize = 16;

/// 帧的客户区几何：尺寸与 DPI。只移动窗口不改变它，所以客户区坐标照常可用。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct Geometry {
    pub(crate) width: u32,
    pub(crate) height: u32,
    pub(crate) dpi: u32,
}

/// 采集状态、本实例最近写进帧环的一帧与客户区几何的变化，供 `core.health` 与控制状态读取。
///
/// 最近一帧的采集时刻另存一份原子量：权威检查与定时检查读帧龄时不拿这把锁。
#[derive(Debug)]
pub(crate) struct FrameStatus {
    inner: Mutex<StatusInner>,
    has_latest: AtomicBool,
    latest_capture_ns: AtomicU64,
}

#[derive(Debug)]
struct StatusInner {
    state: CaptureState,
    latest: Option<LatestFrame>,
    first_frame: Option<u64>,
    window: Option<WindowIdentity>,
    /// 客户区几何的变化点：（从这一帧起，几何），按帧号递增，至多 [`GEOMETRY_HISTORY`] 个。
    geometry: VecDeque<(u64, Geometry)>,
}

impl Default for FrameStatus {
    fn default() -> Self {
        Self {
            inner: Mutex::new(StatusInner {
                state: CaptureState::Idle,
                latest: None,
                first_frame: None,
                window: None,
                geometry: VecDeque::new(),
            }),
            has_latest: AtomicBool::new(false),
            latest_capture_ns: AtomicU64::new(0),
        }
    }
}

impl FrameStatus {
    fn lock(&self) -> MutexGuard<'_, StatusInner> {
        self.inner.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// 采集状态，以及本实例最近写进帧环的一帧（帧号与采集时刻），是判断帧龄的依据。
    pub(crate) fn snapshot(&self) -> (CaptureState, Option<LatestFrame>) {
        let inner = self.lock();
        (inner.state, inner.latest)
    }

    /// 本实例最近写进帧环的一帧的采集时刻；还没写过帧时为 `None`。不拿锁。
    pub(crate) fn latest_capture_ns(&self) -> Option<u64> {
        self.has_latest
            .load(Ordering::Acquire)
            .then(|| self.latest_capture_ns.load(Ordering::Acquire))
    }

    /// 本实例最近写入帧的客户区几何（尺寸与 DPI）；还没写过帧时为 `None`。供输入策略的落点归一化。
    pub(crate) fn latest_geometry(&self) -> Option<Geometry> {
        self.lock().geometry.back().map(|&(_, geometry)| geometry)
    }

    /// 基准帧号是否落在本次采集写出的范围内。
    pub(crate) fn contains_frame(&self, frame_number: u64) -> bool {
        let inner = self.lock();
        inner.first_frame.is_some_and(|first| frame_number >= first)
            && inner
                .latest
                .is_some_and(|latest| frame_number <= latest.frame_number)
    }

    /// 最近写入帧所对应的原生窗口及其几何。
    #[cfg(any(windows, test))]
    pub(crate) fn captured_window(&self) -> Option<(WindowIdentity, Geometry)> {
        let inner = self.lock();
        Some((inner.window?, inner.geometry.back()?.1))
    }

    /// 基准帧之后客户区的尺寸或 DPI 是否变了。基准帧的几何查不到时也算变了：比本实例在当前帧环
    /// 写出的第一帧早（旧实例或挂接之前的帧）、比最近写入的一帧晚，或早于记下的最早一次变化。
    pub(crate) fn geometry_changed_since(&self, base_frame: u64) -> bool {
        let inner = self.lock();
        let Some(latest) = inner.latest else {
            return true;
        };
        let Some(&(first, _)) = inner.geometry.front() else {
            return true;
        };
        if base_frame > latest.frame_number || base_frame < first {
            return true;
        }
        let containing = inner
            .geometry
            .iter()
            .rposition(|&(from, _)| from <= base_frame);
        containing != Some(inner.geometry.len() - 1)
    }

    /// 改变采集状态，返回原来的状态。
    fn set_state(&self, state: CaptureState) -> CaptureState {
        std::mem::replace(&mut self.lock().state, state)
    }

    /// 换了帧环或采集源：清空旧帧、窗口身份与几何。
    pub(crate) fn forget(&self) {
        let mut inner = self.lock();
        self.has_latest.store(false, Ordering::Release);
        inner.latest = None;
        inner.first_frame = None;
        inner.window = None;
        inner.geometry.clear();
    }

    /// 记下刚写进帧环的一帧与它的几何。只由采集线程调用（单元测试也用它造出最近一帧）。
    pub(crate) fn record(
        &self,
        latest: LatestFrame,
        geometry: Geometry,
        window: Option<WindowIdentity>,
    ) {
        let mut inner = self.lock();
        inner.first_frame.get_or_insert(latest.frame_number);
        inner.window = window;
        if inner
            .geometry
            .back()
            .is_none_or(|&(_, last)| last != geometry)
        {
            if inner.geometry.len() == GEOMETRY_HISTORY {
                inner.geometry.pop_front();
            }
            inner.geometry.push_back((latest.frame_number, geometry));
        }
        inner.latest = Some(latest);
        self.latest_capture_ns
            .store(latest.capture_ns, Ordering::Release);
        self.has_latest.store(true, Ordering::Release);
    }

    /// 持有锁，模拟采集一侧占着它；只用于测试释放不依赖采集一侧。
    #[cfg(test)]
    pub(crate) fn hold(&self) -> MutexGuard<'_, impl Sized> {
        self.lock()
    }
}

// ---------------------------------------------------------------- 采集目标与帧的去处

/// 采集目标：在采集线程上建立采集源，采集源失效后按同一目标重新建立（重新识别窗口）。
///
/// 处理函数把它交给采集线程，因为 WGC 采集源不是 `Send`，只能在采集线程上创建。
pub(crate) trait SourceOpener: Send + fmt::Display {
    /// 建立采集源。找不到窗口等失败时返回错误，采集线程过一段时间再试。
    fn open(&mut self) -> Result<Box<dyn CaptureSource>, CaptureError>;
}

/// 帧的去处，即帧环的写入方；单元测试以记录帧的替身代替共享内存。
pub(crate) trait FrameSink: Send {
    /// 第一次写入前续用该实时核的帧号。
    fn resume_after(&mut self, previous_frame: u64);
    /// 写入一帧，返回它在帧环中的帧号。
    fn write(&mut self, frame: &Frame) -> Result<u64, WriteError>;
}

impl FrameSink for FrameRingWriter {
    fn resume_after(&mut self, previous_frame: u64) {
        Self::resume_after(self, previous_frame);
    }

    fn write(&mut self, frame: &Frame) -> Result<u64, WriteError> {
        Self::write(self, frame)
    }
}

/// `capture.set_target` 不接受的目标。
#[derive(Debug)]
pub(crate) enum TargetError {
    /// 参数不合规则：进程名为空，或窗口类与标题都没给出。
    Invalid(&'static str),
    /// 这个平台或系统不支持。
    Unsupported(String),
    /// 打不开假显示面，或它的头部与参数不符。
    Surface(FrameRingError),
}

impl fmt::Display for TargetError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Invalid(reason) => f.write_str(reason),
            Self::Unsupported(reason) => write!(f, "unsupported: {reason}"),
            Self::Surface(error) => write!(f, "fake display: {error}"),
        }
    }
}

/// 核对采集目标，返回采集线程用的 [`SourceOpener`]。假显示面在这里先打开一次，打不开即报错。
pub(crate) fn opener_for(target: CaptureTarget) -> Result<Box<dyn SourceOpener>, TargetError> {
    match target {
        CaptureTarget::Window(window) => {
            check_window(&window)?;
            window_opener(window)
        }
        CaptureTarget::Fake(fake) => {
            let opened = FakeCapture::open(&fake).map_err(TargetError::Surface)?;
            Ok(Box::new(FakeOpener {
                target: fake,
                opened: Some(opened),
            }))
        }
    }
}

/// 窗口识别规则须有进程名，并给出窗口类与标题至少一个。音频目标的窗口规则同样（`audio` 模块）。
pub(crate) fn check_window_rule(
    process_name: &str,
    window_class: Option<&str>,
    title: Option<&str>,
) -> Result<(), &'static str> {
    if process_name.is_empty() {
        return Err("process_name is empty");
    }
    if window_class.is_none() && title.is_none() {
        return Err("give window_class or title along with process_name");
    }
    Ok(())
}

fn check_window(window: &WindowTarget) -> Result<(), TargetError> {
    check_window_rule(
        &window.process_name,
        window.window_class.as_deref(),
        window.title.as_deref(),
    )
    .map_err(TargetError::Invalid)
}

#[cfg(windows)]
fn window_opener(window: WindowTarget) -> Result<Box<dyn SourceOpener>, TargetError> {
    use capture::WindowSelector;
    use capture::win::{WgcOptions, is_supported};

    match is_supported() {
        Ok(true) => {}
        Ok(false) => {
            return Err(TargetError::Unsupported(
                "this system does not support Windows Graphics Capture".into(),
            ));
        }
        Err(error) => return Err(TargetError::Unsupported(error.to_string())),
    }
    let selector = WindowSelector {
        process_name: Some(window.process_name),
        class_name: window.window_class,
        title: window.title,
    };
    Ok(Box::new(WindowOpener {
        options: WgcOptions {
            selector,
            ..WgcOptions::default()
        },
    }))
}

#[cfg(not(windows))]
#[allow(
    clippy::needless_pass_by_value,
    reason = "与 Windows 上的实现同一签名，那边取走它的字段"
)]
fn window_opener(_: WindowTarget) -> Result<Box<dyn SourceOpener>, TargetError> {
    Err(TargetError::Unsupported(
        "window capture uses Windows Graphics Capture, which is only available on Windows".into(),
    ))
}

/// 按窗口识别规则建立 WGC 采集源。
#[cfg(windows)]
struct WindowOpener {
    options: capture::win::WgcOptions,
}

#[cfg(windows)]
impl SourceOpener for WindowOpener {
    fn open(&mut self) -> Result<Box<dyn CaptureSource>, CaptureError> {
        Ok(Box::new(capture::win::WgcCapture::new(&self.options)?))
    }
}

#[cfg(windows)]
impl fmt::Display for WindowOpener {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "the window matching {}", self.options.selector)
    }
}

/// 假采集源：第一次用处理函数打开并核对过的那个，失效后按同一参数重新打开。只用于自动测试。
struct FakeOpener {
    target: FakeTarget,
    opened: Option<FakeCapture>,
}

impl SourceOpener for FakeOpener {
    fn open(&mut self) -> Result<Box<dyn CaptureSource>, CaptureError> {
        let capture = match self.opened.take() {
            Some(capture) => capture,
            None => FakeCapture::open(&self.target).map_err(|error| CaptureError::Os {
                context: "FakeCapture::open",
                code: 0,
                message: error.to_string(),
            })?,
        };
        Ok(Box::new(capture))
    }
}

impl fmt::Display for FakeOpener {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "the fake display {:?}", self.target.surface.name)
    }
}

// ---------------------------------------------------------------- 采集线程

/// 交给采集线程的命令。
enum Command {
    /// `frame_ring.attach`：换成这个帧环。
    Attach(Box<dyn FrameSink>),
    /// `capture.set_target`：先停掉旧的采集源，再按新目标建立。
    Target(Box<dyn SourceOpener>),
    /// 会话结束。
    Stop,
}

/// 处理函数经它把命令交给采集线程。
pub(crate) struct Capture {
    commands: Sender<Command>,
}

/// 采集线程已经停止，命令没有送达。
#[derive(Debug)]
pub(crate) struct CaptureStopped;

/// 采集线程本身，由会话持有，会话结束时停掉。
pub(crate) struct CaptureThread {
    commands: Sender<Command>,
    exited: Receiver<()>,
}

impl Capture {
    /// 启动采集线程。线程一开始处于 `idle`，等帧环与采集目标都到了才建立采集源。
    pub(crate) fn spawn(
        status: Arc<FrameStatus>,
        invalidate: impl Fn() + Send + 'static,
    ) -> io::Result<(Self, CaptureThread)> {
        let (commands, receiver) = mpsc::channel();
        let (exited_sender, exited) = mpsc::channel::<()>();
        thread::Builder::new()
            .name("capture".into())
            .spawn(move || {
                // 线程结束（采集源与写入方都已丢弃）时随之丢弃，等待方由此得知。
                let _exited = exited_sender;
                run(&receiver, Pipeline::new(status, invalidate));
                // 先于 `_exited` 丢弃：等待方得知结束时，命令已一定送不到。
                drop(receiver);
            })?;
        Ok((
            Self {
                commands: commands.clone(),
            },
            CaptureThread { commands, exited },
        ))
    }

    /// 换成 `writer` 指向的帧环。
    pub(crate) fn attach(&self, writer: FrameRingWriter) -> Result<(), CaptureStopped> {
        self.send(Command::Attach(Box::new(writer)))
    }

    /// 设定采集目标；旧的采集源先停掉。
    pub(crate) fn set_target(&self, opener: Box<dyn SourceOpener>) -> Result<(), CaptureStopped> {
        self.send(Command::Target(opener))
    }

    fn send(&self, command: Command) -> Result<(), CaptureStopped> {
        self.commands.send(command).map_err(|_| CaptureStopped)
    }
}

impl CaptureThread {
    /// 让采集线程停掉采集源并结束，至多等 `timeout`；返回它是否已结束。等不到也不影响退出。
    pub(crate) fn stop(self, timeout: Duration) -> bool {
        let _ = self.commands.send(Command::Stop);
        matches!(
            self.exited.recv_timeout(timeout),
            Err(RecvTimeoutError::Disconnected)
        )
    }
}

/// 采集线程的循环：等命令，至多等 [`Pipeline::step`] 给出的时间，然后再做一轮。
fn run(commands: &Receiver<Command>, mut pipeline: Pipeline) {
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

/// 采集线程的状态机，不含线程与通道。转换见模块文档的“采集主循环”。
struct Pipeline {
    status: Arc<FrameStatus>,
    invalidate: Box<dyn Fn() + Send>,
    sink: Option<Box<dyn FrameSink>>,
    opener: Option<Box<dyn SourceOpener>>,
    source: Option<Box<dyn CaptureSource>>,
    frame: Frame,
    last_frame_number: u64,
    /// 没有采集源时，到这个时刻再建立；`None` 表示立即建立。
    retry_at: Option<Instant>,
    /// 最近记录的问题；同样的问题只记一次。
    problem: Option<String>,
}

impl Pipeline {
    fn new(status: Arc<FrameStatus>, invalidate: impl Fn() + Send + 'static) -> Self {
        Self {
            status,
            invalidate: Box::new(invalidate),
            sink: None,
            opener: None,
            source: None,
            frame: Frame::default(),
            last_frame_number: 0,
            retry_at: None,
            problem: None,
        }
    }

    /// 处理一条命令；返回 `false` 表示结束。
    fn apply(&mut self, command: Command) -> bool {
        match command {
            Command::Attach(mut sink) => {
                if self.sink.is_some() {
                    (self.invalidate)();
                    sink.resume_after(self.last_frame_number);
                }
                self.sink = Some(sink);
            }
            Command::Target(opener) => {
                if self.opener.is_some() {
                    (self.invalidate)();
                }
                // 先停掉旧的采集源，新目标的采集源在此之后才建立。
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
        if self.sink.is_none() {
            self.enter(CaptureState::Idle, &"waiting for frame_ring.attach");
            return None;
        }
        let Some(opener) = self.opener.as_mut() else {
            self.enter(CaptureState::Idle, &"waiting for capture.set_target");
            return None;
        };
        if self.source.is_none() {
            if let Some(at) = self.retry_at
                && now < at
            {
                return Some(at - now);
            }
            match opener.open() {
                Ok(source) => {
                    let why = opener.to_string();
                    self.source = Some(source);
                    self.retry_at = None;
                    self.enter(CaptureState::Capturing, &why);
                }
                Err(error) => {
                    self.search(now, &error);
                    return Some(SEARCH_INTERVAL);
                }
            }
        }
        let source = self.source.as_mut()?;
        match source.next_frame(FRAME_WAIT, &mut self.frame) {
            Ok(true) => {
                let window = source.window_identity();
                self.write(window);
                Some(Duration::ZERO)
            }
            Ok(false) => Some(Duration::ZERO),
            Err(error) if error.is_recoverable() => {
                // 窗口还在，采集源保留，等它恢复；这期间没有新帧，帧龄随之增长。
                self.report(error.to_string());
                Some(RECOVER_INTERVAL)
            }
            Err(error) => {
                // 采集源不再产出帧：丢掉它，过一段时间重新识别窗口。
                (self.invalidate)();
                self.source = None;
                self.search(now, &error);
                Some(SEARCH_INTERVAL)
            }
        }
    }

    /// 没有采集源：进入 `searching`，到 [`SEARCH_INTERVAL`] 之后再建立。
    fn search(&mut self, now: Instant, error: &CaptureError) {
        self.retry_at = Some(now + SEARCH_INTERVAL);
        self.enter(CaptureState::Searching, error);
        self.report(error.to_string());
    }

    fn write(&mut self, window: Option<WindowIdentity>) {
        let Some(sink) = self.sink.as_mut() else {
            return;
        };
        match sink.write(&self.frame) {
            Ok(frame_number) => {
                self.last_frame_number = frame_number;
                let meta = &self.frame.meta;
                self.status.record(
                    LatestFrame {
                        frame_number,
                        capture_ns: meta.captured_at_ns,
                    },
                    Geometry {
                        width: meta.width,
                        height: meta.height,
                        dpi: meta.dpi,
                    },
                    window,
                );
                if let Some(problem) = self.problem.take() {
                    log!("capture: frames reach the frame ring again (was: {problem})");
                }
            }
            // 帧环不受影响；这帧不算写入，最近写入的一帧不变，帧龄照样增长。
            Err(error) => self.report(format!("frame not written: {error}")),
        }
    }

    fn enter(&self, state: CaptureState, why: &dyn fmt::Display) {
        let previous = self.status.set_state(state);
        if previous != state {
            log!("capture {previous:?} -> {state:?}: {why}");
        }
    }

    fn report(&mut self, problem: String) {
        if self.problem.as_ref() != Some(&problem) {
            log!("capture: {problem}");
            self.problem = Some(problem);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{
        Capture, Command, FRAME_WAIT, FrameSink, FrameStatus, Geometry, Pipeline, RECOVER_INTERVAL,
        SEARCH_INTERVAL, SourceOpener, TargetError, check_window, opener_for,
    };
    use capture::{CaptureError, CaptureSource, Frame, FrameMeta, WindowIdentity};
    use core_protocol::messages::{CaptureState, CaptureTarget, LatestFrame, WindowTarget};
    use frame_ring::WriteError;
    use std::collections::VecDeque;
    use std::fmt;
    use std::sync::{Arc, Mutex};
    use std::thread;
    use std::time::{Duration, Instant};

    type Log = Arc<Mutex<Vec<String>>>;
    type Step = Result<Option<Frame>, CaptureError>;

    fn entries(log: &Log) -> Vec<String> {
        log.lock().unwrap().clone()
    }

    /// 1×1 的帧；宽度超过 [`RecordingSink::MAX_WIDTH`] 时替身写不进去。
    fn frame(number: u64, width: u32) -> Frame {
        Frame {
            meta: FrameMeta {
                number,
                captured_at_ns: 1_000 * number,
                transform_version: 1,
                width,
                height: 1,
                stride: width * 4,
                ..FrameMeta::default()
            },
            pixels: vec![0; width as usize * 4],
        }
    }

    /// 按脚本交帧或报错的采集源，脚本用完后一直没有新帧；被丢弃时记一笔。
    struct ScriptedSource {
        name: &'static str,
        steps: VecDeque<Step>,
        log: Log,
    }

    impl CaptureSource for ScriptedSource {
        fn window_identity(&self) -> Option<WindowIdentity> {
            Some(WindowIdentity {
                handle: isize::from(self.name.as_bytes()[0]),
                process_id: 123,
            })
        }

        fn next_frame(&mut self, timeout: Duration, out: &mut Frame) -> Result<bool, CaptureError> {
            assert_eq!(timeout, FRAME_WAIT);
            match self.steps.pop_front() {
                None | Some(Ok(None)) => Ok(false),
                Some(Ok(Some(frame))) => {
                    *out = frame;
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

    /// 按脚本建立采集源的目标：每次建立取下一份脚本，脚本用完后报找不到窗口。
    struct ScriptedOpener {
        name: &'static str,
        opens: VecDeque<Result<Vec<Step>, CaptureError>>,
        log: Log,
    }

    impl ScriptedOpener {
        fn boxed(
            name: &'static str,
            opens: Vec<Result<Vec<Step>, CaptureError>>,
            log: &Log,
        ) -> Box<dyn SourceOpener> {
            Box::new(Self {
                name,
                opens: opens.into(),
                log: Arc::clone(log),
            })
        }
    }

    impl SourceOpener for ScriptedOpener {
        fn open(&mut self) -> Result<Box<dyn CaptureSource>, CaptureError> {
            self.log.lock().unwrap().push(format!("open {}", self.name));
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

    /// 记下写入的采集源帧号，按 101、102…… 编帧环帧号；宽于 [`Self::MAX_WIDTH`] 的帧写不进去。
    struct RecordingSink {
        next: u64,
        written: Arc<Mutex<Vec<u64>>>,
    }

    impl RecordingSink {
        const MAX_WIDTH: u32 = 4;

        fn boxed(written: &Arc<Mutex<Vec<u64>>>) -> Box<dyn FrameSink> {
            Box::new(Self {
                next: 100,
                written: Arc::clone(written),
            })
        }
    }

    impl FrameSink for RecordingSink {
        fn resume_after(&mut self, previous_frame: u64) {
            self.next = self.next.max(previous_frame);
        }

        fn write(&mut self, frame: &Frame) -> Result<u64, WriteError> {
            if frame.meta.width > Self::MAX_WIDTH {
                return Err(WriteError::TooLarge {
                    width: frame.meta.width,
                    height: frame.meta.height,
                    max_width: Self::MAX_WIDTH,
                    max_height: 1,
                });
            }
            self.written.lock().unwrap().push(frame.meta.number);
            self.next += 1;
            Ok(self.next)
        }
    }

    fn not_found() -> CaptureError {
        CaptureError::WindowNotFound {
            selector: "title \"Game\"".into(),
        }
    }

    /// 帧环帧号为 `frame_number`、采集源帧号为 `source_number` 的帧作为最近写入的一帧。
    #[allow(
        clippy::unnecessary_wraps,
        reason = "与 FrameStatus::snapshot 的 Option 直接比较"
    )]
    fn latest(frame_number: u64, source_number: u64) -> Option<LatestFrame> {
        Some(LatestFrame {
            frame_number,
            capture_ns: 1_000 * source_number,
        })
    }

    struct Fixture {
        status: Arc<FrameStatus>,
        pipeline: Pipeline,
        log: Log,
        written: Arc<Mutex<Vec<u64>>>,
    }

    impl Fixture {
        fn new() -> Self {
            let status = Arc::new(FrameStatus::default());
            let invalidate = Arc::clone(&status);
            Self {
                pipeline: Pipeline::new(Arc::clone(&status), move || invalidate.forget()),
                status,
                log: Log::default(),
                written: Arc::default(),
            }
        }

        fn attach(&mut self) {
            assert!(
                self.pipeline
                    .apply(Command::Attach(RecordingSink::boxed(&self.written)))
            );
        }

        fn target(&mut self, name: &'static str, opens: Vec<Result<Vec<Step>, CaptureError>>) {
            let opener = ScriptedOpener::boxed(name, opens, &self.log);
            assert!(self.pipeline.apply(Command::Target(opener)));
        }

        fn state(&self) -> CaptureState {
            self.status.snapshot().0
        }

        fn latest(&self) -> Option<LatestFrame> {
            self.status.snapshot().1
        }
    }

    #[test]
    fn idle_until_both_a_ring_and_a_target_are_given() {
        let mut fx = Fixture::new();
        let now = Instant::now();
        assert_eq!(fx.pipeline.step(now), None);
        assert_eq!(fx.state(), CaptureState::Idle);
        fx.target("a", vec![Ok(vec![])]);
        assert_eq!(fx.pipeline.step(now), None);
        assert_eq!(fx.state(), CaptureState::Idle);
        assert!(entries(&fx.log).is_empty(), "no source before the ring");
        fx.attach();
        assert_eq!(fx.pipeline.step(now), Some(Duration::ZERO));
        assert_eq!(fx.state(), CaptureState::Capturing);
        assert_eq!(entries(&fx.log), ["open a"]);
    }

    #[test]
    fn frames_are_written_and_the_latest_is_recorded() {
        let mut fx = Fixture::new();
        fx.attach();
        fx.target(
            "a",
            vec![Ok(vec![
                Ok(Some(frame(1, 2))),
                Ok(None),
                Ok(Some(frame(3, 2))),
            ])],
        );
        let now = Instant::now();
        assert_eq!(fx.latest(), None);
        fx.pipeline.step(now);
        assert_eq!(fx.latest(), latest(101, 1));
        // 没有新帧：最近写入的一帧不变。
        fx.pipeline.step(now);
        assert_eq!(fx.latest(), latest(101, 1));
        fx.pipeline.step(now);
        assert_eq!(fx.latest(), latest(102, 3));
        assert_eq!(*fx.written.lock().unwrap(), [1, 3]);
        assert_eq!(fx.state(), CaptureState::Capturing);
    }

    #[test]
    fn a_missing_window_is_searched_for_again_after_the_interval() {
        let mut fx = Fixture::new();
        fx.attach();
        fx.target("a", vec![Err(not_found()), Ok(vec![Ok(Some(frame(1, 2)))])]);
        let t0 = Instant::now();
        assert_eq!(fx.pipeline.step(t0), Some(SEARCH_INTERVAL));
        assert_eq!(fx.state(), CaptureState::Searching);
        // 间隔之内不重新识别，只等剩下的时间。
        let early = t0 + SEARCH_INTERVAL / 2;
        assert_eq!(fx.pipeline.step(early), Some(SEARCH_INTERVAL / 2));
        assert_eq!(entries(&fx.log), ["open a"]);
        fx.pipeline.step(t0 + SEARCH_INTERVAL);
        assert_eq!(fx.state(), CaptureState::Capturing);
        assert_eq!(entries(&fx.log), ["open a", "open a"]);
        assert_eq!(fx.latest(), latest(101, 1));
    }

    #[test]
    fn recoverable_errors_keep_the_source_and_wait() {
        let mut fx = Fixture::new();
        fx.attach();
        let steps = vec![
            Ok(Some(frame(1, 2))),
            Err(CaptureError::WindowMinimized),
            Err(CaptureError::ExclusiveFullscreen),
            Ok(Some(frame(4, 2))),
        ];
        fx.target("a", vec![Ok(steps)]);
        let now = Instant::now();
        fx.pipeline.step(now);
        for _ in 0..2 {
            assert_eq!(fx.pipeline.step(now), Some(RECOVER_INTERVAL));
            assert_eq!(fx.state(), CaptureState::Capturing);
            assert_eq!(fx.latest(), latest(101, 1));
        }
        fx.pipeline.step(now);
        assert_eq!(fx.latest(), latest(102, 4));
        assert_eq!(entries(&fx.log), ["open a"], "the source was kept");
    }

    #[test]
    fn unrecoverable_errors_drop_the_source_and_search_again() {
        let mut fx = Fixture::new();
        fx.attach();
        fx.target(
            "a",
            vec![
                Ok(vec![Ok(Some(frame(1, 2))), Err(CaptureError::WindowClosed)]),
                Err(not_found()),
                Ok(vec![Ok(Some(frame(1, 2)))]),
            ],
        );
        let t0 = Instant::now();
        fx.pipeline.step(t0);
        assert_eq!(fx.pipeline.step(t0), Some(SEARCH_INTERVAL));
        assert_eq!(fx.state(), CaptureState::Searching);
        assert_eq!(entries(&fx.log), ["open a", "drop a"]);
        // 帧环里仍有旧像素，但它们已失效，重新建立的源必须写入新帧才能供输入用。
        assert_eq!(fx.latest(), None);
        assert_eq!(fx.status.captured_window(), None);
        assert!(!fx.status.contains_frame(101));
        let t1 = t0 + SEARCH_INTERVAL;
        assert_eq!(fx.pipeline.step(t1), Some(SEARCH_INTERVAL));
        assert_eq!(fx.state(), CaptureState::Searching);
        fx.pipeline.step(t1 + SEARCH_INTERVAL);
        assert_eq!(fx.state(), CaptureState::Capturing);
        assert_eq!(fx.latest(), latest(102, 1));
        assert!(fx.status.geometry_changed_since(101));
        assert!(!fx.status.contains_frame(101));
        assert_eq!(entries(&fx.log), ["open a", "drop a", "open a", "open a"]);
    }

    #[test]
    fn a_new_target_stops_the_old_source_first() {
        let mut fx = Fixture::new();
        fx.attach();
        fx.target("a", vec![Ok(vec![Ok(Some(frame(1, 2)))])]);
        let now = Instant::now();
        fx.pipeline.step(now);
        fx.target("b", vec![Ok(vec![Ok(Some(frame(1, 2)))])]);
        assert_eq!(entries(&fx.log), ["open a", "drop a"]);
        fx.pipeline.step(now);
        assert_eq!(entries(&fx.log), ["open a", "drop a", "open b"]);
        assert_eq!(fx.state(), CaptureState::Capturing);
        // 新目标立即建立，不等重新识别的间隔。
        fx.target("c", vec![Err(not_found())]);
        fx.pipeline.step(now);
        fx.target("d", vec![Ok(vec![])]);
        fx.pipeline.step(now);
        assert_eq!(fx.state(), CaptureState::Capturing);
        assert_eq!(entries(&fx.log)[3..], ["drop b", "open c", "open d"]);
    }

    #[test]
    fn regression_new_target_invalidates_fresh_frames_with_the_same_geometry() {
        let mut fx = Fixture::new();
        fx.attach();
        let now = Instant::now();
        fx.target("a", vec![Ok(vec![Ok(Some(frame(1, 2)))])]);
        fx.pipeline.step(now);
        assert_eq!(fx.latest(), latest(101, 1));
        assert_eq!(
            fx.status.captured_window().unwrap().0.handle,
            isize::from(b'a')
        );

        fx.target("b", vec![Ok(vec![Ok(Some(frame(1, 2)))])]);
        assert_eq!(fx.latest(), None);
        assert_eq!(fx.status.captured_window(), None);
        assert!(fx.status.geometry_changed_since(101));
        fx.pipeline.step(now);
        assert_eq!(fx.latest(), latest(102, 1));
        assert_eq!(
            fx.status.captured_window().unwrap().0.handle,
            isize::from(b'b')
        );
        assert!(fx.status.geometry_changed_since(101));
        assert!(!fx.status.geometry_changed_since(102));
    }

    #[test]
    fn frames_that_cannot_be_written_leave_the_latest_frame() {
        let mut fx = Fixture::new();
        fx.attach();
        let wide = RecordingSink::MAX_WIDTH + 1;
        fx.target(
            "a",
            vec![Ok(vec![
                Ok(Some(frame(1, 2))),
                Ok(Some(frame(2, wide))),
                Ok(Some(frame(3, 2))),
            ])],
        );
        let now = Instant::now();
        fx.pipeline.step(now);
        fx.pipeline.step(now);
        assert_eq!(fx.latest(), latest(101, 1));
        assert_eq!(fx.state(), CaptureState::Capturing);
        fx.pipeline.step(now);
        assert_eq!(fx.latest(), latest(102, 3));
    }

    #[test]
    fn geometry_changes_are_recorded_as_frames_are_written() {
        let mut fx = Fixture::new();
        fx.attach();
        fx.target(
            "a",
            vec![Ok(vec![
                Ok(Some(frame(1, 2))),
                Ok(Some(frame(2, 2))),
                Ok(Some(frame(3, 3))),
            ])],
        );
        let now = Instant::now();
        // 帧环帧号 101、102 宽 2，103 宽 3。
        fx.pipeline.step(now);
        fx.pipeline.step(now);
        assert!(!fx.status.geometry_changed_since(101));
        assert!(!fx.status.geometry_changed_since(102));
        fx.pipeline.step(now);
        assert!(fx.status.geometry_changed_since(102));
        assert!(!fx.status.geometry_changed_since(103));
        assert_eq!(fx.status.latest_capture_ns(), Some(3_000));
    }

    #[test]
    fn the_geometry_of_old_unknown_and_future_frames_counts_as_changed() {
        let status = FrameStatus::default();
        let at = |frame_number, width, dpi| {
            status.record(
                LatestFrame {
                    frame_number,
                    capture_ns: frame_number * 10,
                },
                Geometry {
                    width,
                    height: 10,
                    dpi,
                },
                None,
            );
        };
        assert!(status.geometry_changed_since(1), "no frames yet");
        assert_eq!(status.latest_capture_ns(), None);
        at(5, 100, 96);
        at(6, 100, 96);
        assert_eq!(status.latest_capture_ns(), Some(60));
        assert!(!status.geometry_changed_since(5));
        assert!(!status.geometry_changed_since(6));
        assert!(
            status.geometry_changed_since(4),
            "before this instance's first frame"
        );
        assert!(status.geometry_changed_since(7), "not written yet");
        at(7, 100, 144);
        assert!(status.geometry_changed_since(6), "the DPI changed");
        assert!(!status.geometry_changed_since(7));
        let mut last = 7;
        for width in 0..u32::try_from(super::GEOMETRY_HISTORY).unwrap() {
            last += 1;
            at(last, 200 + width, 96);
        }
        // 最早记下的变化被挤掉了：更早的基准帧查不到，算作变了。
        assert!(status.geometry_changed_since(7));
        assert!(!status.geometry_changed_since(last));
        status.forget();
        assert!(status.geometry_changed_since(last));
        assert_eq!(status.latest_capture_ns(), None);
    }

    #[test]
    fn attaching_another_ring_forgets_the_latest_frame() {
        let mut fx = Fixture::new();
        fx.attach();
        fx.target("a", vec![Ok(vec![Ok(Some(frame(1, 2)))])]);
        fx.pipeline.step(Instant::now());
        assert!(fx.latest().is_some());
        fx.attach();
        assert_eq!(fx.latest(), None);
        assert_eq!(entries(&fx.log), ["open a"], "the source keeps running");
    }

    #[test]
    fn stop_drops_the_source() {
        let mut fx = Fixture::new();
        fx.attach();
        fx.target("a", vec![Ok(vec![])]);
        fx.pipeline.step(Instant::now());
        assert!(!fx.pipeline.apply(Command::Stop));
        assert_eq!(entries(&fx.log), ["open a", "drop a"]);
    }

    #[test]
    fn the_capture_thread_writes_frames_and_stops_promptly() {
        let status = Arc::new(FrameStatus::default());
        let invalidate = Arc::clone(&status);
        let (capture, worker) =
            Capture::spawn(Arc::clone(&status), move || invalidate.forget()).unwrap();
        let log = Log::default();
        let written = Arc::default();
        let steps = (1..=3).map(|n| Ok(Some(frame(n, 2)))).collect();
        assert!(
            capture
                .set_target(ScriptedOpener::boxed("a", vec![Ok(steps)], &log))
                .is_ok()
        );
        assert!(
            capture
                .send(Command::Attach(RecordingSink::boxed(&written)))
                .is_ok()
        );
        let deadline = Instant::now() + Duration::from_secs(5);
        while status.snapshot() != (CaptureState::Capturing, latest(103, 3)) {
            assert!(Instant::now() < deadline, "{:?}", status.snapshot());
            thread::sleep(Duration::from_millis(1));
        }
        assert!(worker.stop(Duration::from_secs(5)));
        assert_eq!(entries(&log), ["open a", "drop a"]);
        assert!(
            capture
                .set_target(ScriptedOpener::boxed("b", vec![], &log))
                .is_err()
        );
    }

    fn window(process: &str, class: Option<&str>, title: Option<&str>) -> WindowTarget {
        WindowTarget {
            process_name: process.into(),
            window_class: class.map(Into::into),
            title: title.map(Into::into),
        }
    }

    #[test]
    fn window_targets_need_a_process_and_a_class_or_title() {
        assert!(check_window(&window("game.exe", Some("Main"), None)).is_ok());
        assert!(check_window(&window("game.exe", None, Some("Game"))).is_ok());
        for bad in [
            window("game.exe", None, None),
            window("", Some("Main"), Some("Game")),
        ] {
            let error = opener_for(CaptureTarget::Window(bad)).err().unwrap();
            assert!(matches!(error, TargetError::Invalid(_)), "{error}");
        }
    }

    #[cfg(not(windows))]
    #[test]
    fn window_targets_are_unsupported_off_windows() {
        let target = CaptureTarget::Window(window("game.exe", None, Some("Game")));
        let error = opener_for(target).err().unwrap();
        assert!(matches!(error, TargetError::Unsupported(_)), "{error}");
    }
}
