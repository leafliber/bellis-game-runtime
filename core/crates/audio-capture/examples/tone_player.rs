//! 真机测试用的测试进程，只用于测试：开一个带给定标题的窗口，在缺省的播放设备上播放静音或已知频率的
//! 正弦波。由 `runtime/tests/core_link/test_audio_process.py`（手动）编译并拉起，只在 Windows 上可用。
//!
//! 用法：`tone_player --title <窗口标题> --frequency <Hz> [--amplitude <0 到 1>]`。
//!
//! - 窗口已建、播放已开始（静音）后，标准输出写 `ready <进程号>`；
//! - 标准输入的一行 `tone`：从下一个缓冲起写正弦波，写出后输出 `tone <时刻>`；
//! - 一行 `silence`：从下一个缓冲起写静音，输出 `silence <时刻>`；
//! - 标准输入关闭即关窗口、停止播放并退出。
//!
//! 时刻是含第一个新采样点的缓冲交给系统（`ReleaseBuffer`）之后的 `mono_clock::now_ns()`，与主进程
//! `time.perf_counter_ns()` 同一时钟；声音实际播出还要晚一个播放缓冲左右。

#[cfg(windows)]
fn main() -> std::process::ExitCode {
    match player::run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(message) => {
            eprintln!("tone_player: {message}");
            std::process::ExitCode::FAILURE
        }
    }
}

#[cfg(not(windows))]
fn main() -> std::process::ExitCode {
    eprintln!("tone_player: WASAPI is only available on Windows");
    std::process::ExitCode::FAILURE
}

#[cfg(windows)]
mod player {
    use std::f64::consts::TAU;
    use std::io::{BufRead, Write};
    use std::sync::Arc;
    use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
    use std::thread;

    use windows::Win32::Foundation::{CloseHandle, HWND, LPARAM, LRESULT, WPARAM};
    use windows::Win32::Media::Audio::{
        AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM,
        AUDCLNT_STREAMFLAGS_EVENTCALLBACK, AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY, IAudioClient,
        IAudioRenderClient, IMMDeviceEnumerator, MMDeviceEnumerator, WAVEFORMATEX, eConsole,
        eRender,
    };
    use windows::Win32::Media::Multimedia::WAVE_FORMAT_IEEE_FLOAT;
    use windows::Win32::System::Com::{
        CLSCTX_ALL, COINIT_MULTITHREADED, CoCreateInstance, CoInitializeEx, CoUninitialize,
    };
    use windows::Win32::System::LibraryLoader::GetModuleHandleW;
    use windows::Win32::System::Threading::{CreateEventW, WaitForSingleObject};
    use windows::Win32::UI::WindowsAndMessaging::{
        CreateWindowExW, DefWindowProcW, DispatchMessageW, GetMessageW, MSG, PostMessageW,
        PostQuitMessage, RegisterClassExW, SW_SHOWNOACTIVATE, ShowWindow, TranslateMessage,
        WINDOW_EX_STYLE, WM_CLOSE, WM_DESTROY, WNDCLASSEXW, WS_OVERLAPPEDWINDOW,
    };
    use windows::core::{HSTRING, w};

    const SAMPLE_RATE: u32 = 48_000;
    const CHANNELS: u16 = 2;
    /// 播放缓冲的时长（100 ns 单位）：20 ms。
    const BUFFER_HNS: i64 = 200_000;
    const SILENCE: u8 = 0;
    const TONE: u8 = 1;

    struct Args {
        title: String,
        frequency: f64,
        amplitude: f64,
    }

    fn parse_args() -> Result<Args, String> {
        let mut title = None;
        let mut frequency = None;
        let mut amplitude = 0.05;
        let mut args = std::env::args().skip(1);
        while let Some(flag) = args.next() {
            let value = args.next().ok_or(format!("{flag} needs a value"))?;
            match flag.as_str() {
                "--title" => title = Some(value),
                "--frequency" => frequency = Some(value.parse().map_err(|_| "bad --frequency")?),
                "--amplitude" => amplitude = value.parse().map_err(|_| "bad --amplitude")?,
                _ => return Err(format!("unknown option {flag}")),
            }
        }
        Ok(Args {
            title: title.ok_or("--title is required")?,
            frequency: frequency.ok_or("--frequency is required")?,
            amplitude,
        })
    }

    fn say(line: &str) {
        let mut stdout = std::io::stdout().lock();
        let _ = writeln!(stdout, "{line}");
        let _ = stdout.flush();
    }

    pub(super) fn run() -> Result<(), String> {
        let args = parse_args()?;
        let hwnd = create_window(&args.title)?;
        let mode = Arc::new(AtomicU8::new(SILENCE));
        let quit = Arc::new(AtomicBool::new(false));
        let (ready_tx, ready_rx) = std::sync::mpsc::channel();
        let render = {
            let (mode, quit) = (Arc::clone(&mode), Arc::clone(&quit));
            thread::spawn(move || render(&args, &mode, &quit, &ready_tx))
        };
        ready_rx
            .recv()
            .map_err(|_| "the render thread ended".to_owned())??;
        say(&format!("ready {}", std::process::id()));
        // 窗口句柄不是 Send；以整数交给读标准输入的线程，只用它投递 WM_CLOSE。
        let window = hwnd.0 as isize;
        thread::spawn(move || {
            for line in std::io::stdin().lock().lines() {
                match line.as_deref().map(str::trim) {
                    Ok("tone") => mode.store(TONE, Ordering::Release),
                    Ok("silence") => mode.store(SILENCE, Ordering::Release),
                    Ok(_) => {}
                    Err(_) => break,
                }
            }
            quit.store(true, Ordering::Release);
            // SAFETY: 投递消息是线程安全的；窗口已关闭时调用失败，无妨。
            let _ =
                unsafe { PostMessageW(Some(HWND(window as _)), WM_CLOSE, WPARAM(0), LPARAM(0)) };
        });
        let mut message = MSG::default();
        // SAFETY: 标准的消息循环，只处理本线程的窗口。
        while unsafe { GetMessageW(&raw mut message, None, 0, 0) }.as_bool() {
            // SAFETY: 同上。
            unsafe {
                let _ = TranslateMessage(&raw const message);
                DispatchMessageW(&raw const message);
            }
        }
        render.join().map_err(|_| "the render thread panicked")?
    }

    extern "system" fn window_proc(
        hwnd: HWND,
        msg: u32,
        wparam: WPARAM,
        lparam: LPARAM,
    ) -> LRESULT {
        if msg == WM_DESTROY {
            // SAFETY: 在窗口线程上结束消息循环。
            unsafe { PostQuitMessage(0) };
            return LRESULT(0);
        }
        // SAFETY: 其余消息交给系统的缺省处理。
        unsafe { DefWindowProcW(hwnd, msg, wparam, lparam) }
    }

    fn create_window(title: &str) -> Result<HWND, String> {
        // SAFETY: 取本程序的模块句柄。
        let instance =
            unsafe { GetModuleHandleW(None) }.map_err(|e| format!("GetModuleHandleW: {e}"))?;
        let class = WNDCLASSEXW {
            cbSize: u32::try_from(size_of::<WNDCLASSEXW>()).expect("small"),
            lpfnWndProc: Some(window_proc),
            hInstance: instance.into(),
            lpszClassName: w!("BellisTonePlayer"),
            ..Default::default()
        };
        // SAFETY: 类结构在调用期间有效。
        if unsafe { RegisterClassExW(&raw const class) } == 0 {
            return Err("RegisterClassExW failed".into());
        }
        // SAFETY: 类已注册；标题由 HSTRING 以 NUL 结尾传入。
        let hwnd = unsafe {
            CreateWindowExW(
                WINDOW_EX_STYLE(0),
                w!("BellisTonePlayer"),
                &HSTRING::from(title),
                WS_OVERLAPPEDWINDOW,
                0,
                0,
                360,
                120,
                None,
                None,
                Some(instance.into()),
                None,
            )
        }
        .map_err(|e| format!("CreateWindowExW: {e}"))?;
        // SAFETY: 显示而不抢焦点，不打扰前台的程序。
        let _ = unsafe { ShowWindow(hwnd, SW_SHOWNOACTIVATE) };
        Ok(hwnd)
    }

    fn render(
        args: &Args,
        mode: &AtomicU8,
        quit: &AtomicBool,
        ready: &std::sync::mpsc::Sender<Result<(), String>>,
    ) -> Result<(), String> {
        // SAFETY: 以 MTA 初始化本线程，结束时反初始化。
        unsafe { CoInitializeEx(None, COINIT_MULTITHREADED) }
            .ok()
            .map_err(|e| format!("CoInitializeEx: {e}"))?;
        let result = play(args, mode, quit, ready);
        if let Err(message) = &result {
            let _ = ready.send(Err(message.clone()));
        }
        // SAFETY: 与上面的初始化配对。
        unsafe { CoUninitialize() };
        result
    }

    #[allow(clippy::cast_possible_truncation, reason = "样本值在 [-1, 1] 内")]
    fn play(
        args: &Args,
        mode: &AtomicU8,
        quit: &AtomicBool,
        ready: &std::sync::mpsc::Sender<Result<(), String>>,
    ) -> Result<(), String> {
        let error = |what: &str| {
            let what = what.to_owned();
            move |e: windows::core::Error| format!("{what}: {e}")
        };
        // SAFETY: 标准的 COM 调用，参数都是局部值。
        let client: IAudioClient = unsafe {
            let devices: IMMDeviceEnumerator =
                CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL)
                    .map_err(error("enumerator"))?;
            let device = devices
                .GetDefaultAudioEndpoint(eRender, eConsole)
                .map_err(error("GetDefaultAudioEndpoint"))?;
            device
                .Activate(CLSCTX_ALL, None)
                .map_err(error("Activate"))?
        };
        let block_align = CHANNELS * 4;
        let wave = WAVEFORMATEX {
            wFormatTag: u16::try_from(WAVE_FORMAT_IEEE_FLOAT).expect("small"),
            nChannels: CHANNELS,
            nSamplesPerSec: SAMPLE_RATE,
            nAvgBytesPerSec: SAMPLE_RATE * u32::from(block_align),
            nBlockAlign: block_align,
            wBitsPerSample: 32,
            cbSize: 0,
        };
        let flags = AUDCLNT_STREAMFLAGS_EVENTCALLBACK
            | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
            | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY;
        // SAFETY: 格式在调用期间有效。
        unsafe {
            client.Initialize(
                AUDCLNT_SHAREMODE_SHARED,
                flags,
                BUFFER_HNS,
                0,
                &raw const wave,
                None,
            )
        }
        .map_err(error("Initialize"))?;
        // SAFETY: 自动复位的无名事件，结束时关闭。
        let event =
            unsafe { CreateEventW(None, false, false, None) }.map_err(error("CreateEventW"))?;
        // SAFETY: 事件在播放期间有效。
        unsafe { client.SetEventHandle(event) }.map_err(error("SetEventHandle"))?;
        // SAFETY: 客户端已初始化。
        let buffer_frames = unsafe { client.GetBufferSize() }.map_err(error("GetBufferSize"))?;
        // SAFETY: 同上。
        let renderer: IAudioRenderClient =
            unsafe { client.GetService() }.map_err(error("GetService"))?;
        // SAFETY: 同上。
        unsafe { client.Start() }.map_err(error("Start"))?;
        let _ = ready.send(Ok(()));

        let step = TAU * args.frequency / f64::from(SAMPLE_RATE);
        let mut phase = 0.0f64;
        let mut playing = SILENCE;
        while !quit.load(Ordering::Acquire) {
            // SAFETY: 事件句柄有效；至多等 100 ms，以便及时看到退出。
            let _ = unsafe { WaitForSingleObject(event, 100) };
            // SAFETY: 客户端已开始播放。
            let padding =
                unsafe { client.GetCurrentPadding() }.map_err(error("GetCurrentPadding"))?;
            let frames = buffer_frames - padding;
            if frames == 0 {
                continue;
            }
            let wanted = mode.load(Ordering::Acquire);
            // SAFETY: 请求的帧数不超过空闲的部分；返回的缓冲在 ReleaseBuffer 之前有效。
            let buffer = unsafe { renderer.GetBuffer(frames) }.map_err(error("GetBuffer"))?;
            let values = frames as usize * usize::from(CHANNELS);
            #[allow(clippy::cast_ptr_alignment, reason = "WASAPI 的缓冲按采样点对齐")]
            // SAFETY: 缓冲有 `frames` 个采样点，每个 `block_align` 字节，按 f32 对齐。
            let out = unsafe { std::slice::from_raw_parts_mut(buffer.cast::<f32>(), values) };
            for sample in out.chunks_exact_mut(usize::from(CHANNELS)) {
                let value = if wanted == TONE {
                    let value = (phase.sin() * args.amplitude) as f32;
                    phase = (phase + step) % TAU;
                    value
                } else {
                    0.0
                };
                sample.fill(value);
            }
            // SAFETY: 交出刚写好的全部采样点。
            unsafe { renderer.ReleaseBuffer(frames, 0) }.map_err(error("ReleaseBuffer"))?;
            if wanted != playing {
                let at = mono_clock::now_ns();
                say(&format!(
                    "{} {at}",
                    if wanted == TONE { "tone" } else { "silence" }
                ));
                playing = wanted;
            }
        }
        // SAFETY: 停止播放并关闭事件。
        unsafe {
            let _ = client.Stop();
            let _ = CloseHandle(event);
        }
        Ok(())
    }
}
