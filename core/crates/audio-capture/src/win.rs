//! WASAPI 进程回环（`docs/modules/realtime-core.md` 的“音频采集”）：按进程 ID 只采这个进程及其子进程
//! 的声音。Windows 10 build 20348 起才有。
//!
//! 1. `ActivateAudioInterfaceAsync` 以 `AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK` 与
//!    `PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE` 激活虚拟设备 `VAD\Process_Loopback` 上的
//!    `IAudioClient`，在完成回调里只发一个信号，调用线程等到它再取结果；
//! 2. 以共享模式、事件驱动初始化，格式为调用方给的 F32 采样率与声道数，由系统转换（`AUTOCONVERTPCM`）；
//! 3. `next_packet` 在调用线程上等缓冲事件或目标进程退出，按顺序取出每个包：样本值、包里第一个采样点的
//!    QPC 时刻（100 ns 单位，乘 100 换成纳秒）与不连续标志；
//! 4. 目标进程以 `SYNCHRONIZE` 打开并持有句柄，进程退出即报 [`AudioError::ProcessExited`]，持有句柄也让
//!    进程号在此期间不被复用。
//!
//! COM 在创建音频源的线程上以 MTA 初始化，音频源在哪个线程创建就在哪个线程使用，drop 时对称地反初始化。

use std::mem::ManuallyDrop;
use std::ptr;
use std::sync::Mutex;
use std::sync::mpsc;
use std::time::{Duration, Instant};

use windows::Win32::Foundation::{
    CloseHandle, ERROR_INVALID_PARAMETER, HANDLE, WAIT_EVENT, WAIT_OBJECT_0, WAIT_TIMEOUT,
};
use windows::Win32::Media::Audio::{
    AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY, AUDCLNT_BUFFERFLAGS_SILENT,
    AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR, AUDCLNT_SHAREMODE_SHARED,
    AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM, AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
    AUDCLNT_STREAMFLAGS_LOOPBACK, AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
    AUDIOCLIENT_ACTIVATION_PARAMS, AUDIOCLIENT_ACTIVATION_PARAMS_0,
    AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK, AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS,
    ActivateAudioInterfaceAsync, IActivateAudioInterfaceCompletionHandler, IAudioCaptureClient,
    IAudioClient, PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE,
    VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, WAVEFORMATEX,
};
use windows::Win32::Media::Multimedia::WAVE_FORMAT_IEEE_FLOAT;
use windows::Win32::System::Com::StructuredStorage::{
    PROPVARIANT, PROPVARIANT_0, PROPVARIANT_0_0, PROPVARIANT_0_0_0,
};
use windows::Win32::System::Com::{BLOB, COINIT_MULTITHREADED, CoInitializeEx, CoUninitialize};
use windows::Win32::System::Threading::{
    CreateEventW, OpenProcess, PROCESS_SYNCHRONIZE, WaitForMultipleObjects, WaitForSingleObject,
};
use windows::Win32::System::Variant::VT_BLOB;
use windows::core::{HRESULT, IUnknown, Interface};

use crate::{AudioError, AudioFormat, AudioPacket, AudioSource};

/// 等激活完成的时限。激活通常在几毫秒内完成。
const ACTIVATE_TIMEOUT: Duration = Duration::from_secs(5);
/// 共享模式缓冲的时长（100 ns 单位）：200 ms。音频线程卡住不超过这么久就不丢声音。
const BUFFER_DURATION_HNS: i64 = 2_000_000;

/// 把系统调用的错误包装为 [`AudioError::Os`]。
fn os(context: &'static str) -> impl FnOnce(windows::core::Error) -> AudioError {
    move |error| AudioError::Os {
        context,
        code: error.code().0,
        message: error.message(),
    }
}

/// 激活的完成回调。单独成模块，以便放过 `implement` 宏生成的代码里的 pedantic 提示。
mod completion {
    #![allow(
        clippy::inline_always,
        clippy::ref_as_ptr,
        reason = "windows crate 的 implement 宏生成的代码"
    )]

    use std::sync::mpsc::Sender;
    use std::sync::{Mutex, PoisonError};

    use windows::Win32::Media::Audio::{
        IActivateAudioInterfaceAsyncOperation, IActivateAudioInterfaceCompletionHandler,
        IActivateAudioInterfaceCompletionHandler_Impl,
    };
    use windows::core::{Ref, implement};

    /// 只发一个信号。回调在系统的线程上运行，`IAgileObject` 由 `implement` 提供。
    #[implement(IActivateAudioInterfaceCompletionHandler)]
    pub(super) struct Activated {
        pub(super) done: Mutex<Option<Sender<()>>>,
    }

    impl IActivateAudioInterfaceCompletionHandler_Impl for Activated_Impl {
        fn ActivateCompleted(
            &self,
            _operation: Ref<'_, IActivateAudioInterfaceAsyncOperation>,
        ) -> windows::core::Result<()> {
            let sender = self
                .done
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .take();
            if let Some(sender) = sender {
                let _ = sender.send(());
            }
            Ok(())
        }
    }
}

use completion::Activated;

/// `WAVE_FORMAT_IEEE_FLOAT` 作为 `WAVEFORMATEX` 的 16 位格式标签。
#[allow(clippy::cast_possible_truncation, reason = "格式标签是 3")]
const FORMAT_TAG_FLOAT: u16 = WAVE_FORMAT_IEEE_FLOAT as u16;

/// 一个内核对象句柄，drop 时关闭。
struct OwnedHandle(HANDLE);

impl Drop for OwnedHandle {
    fn drop(&mut self) {
        // SAFETY: 句柄由本模块创建或打开，只关闭一次。关闭失败时没有可做的补救。
        let _ = unsafe { CloseHandle(self.0) };
    }
}

/// 本线程上的一次 COM 初始化，drop 时反初始化。
struct Apartment;

impl Apartment {
    fn enter() -> Result<Self, AudioError> {
        // SAFETY: 以 MTA 初始化本线程；成功（含已初始化的 S_FALSE）时须对称地反初始化。
        let result = unsafe { CoInitializeEx(None, COINIT_MULTITHREADED) };
        result.ok().map_err(os("CoInitializeEx"))?;
        Ok(Self)
    }
}

impl Drop for Apartment {
    fn drop(&mut self) {
        // SAFETY: 与 `enter` 中成功的初始化配对。
        unsafe { CoUninitialize() };
    }
}

/// WASAPI 进程回环音频源：只采目标进程及其子进程的声音。
pub struct ProcessLoopback {
    capture: IAudioCaptureClient,
    client: IAudioClient,
    buffer_event: OwnedHandle,
    process: OwnedHandle,
    format: AudioFormat,
    process_id: u32,
    // 最后丢弃：COM 对象须在反初始化之前释放。
    _apartment: Apartment,
}

impl ProcessLoopback {
    /// 采进程 `process_id` 及其子进程的声音，以 `format` 交出（F32，系统负责转换）。
    ///
    /// # Errors
    ///
    /// 进程不存在或已退出时返回 [`AudioError::ProcessExited`]；系统不支持进程回环时返回
    /// [`AudioError::Unsupported`]；无权打开进程（如提权的进程）等其余失败返回 [`AudioError::Os`]。
    pub fn new(process_id: u32, format: AudioFormat) -> Result<Self, AudioError> {
        let channels = u16::try_from(format.channels)
            .ok()
            .filter(|&channels| channels > 0)
            .ok_or_else(|| AudioError::Unsupported(format!("{} channels", format.channels)))?;
        if format.sample_rate == 0 {
            return Err(AudioError::Unsupported("a sample rate of 0 Hz".into()));
        }
        let apartment = Apartment::enter()?;
        // SAFETY: 只请求等待权限；进程不存在时失败（ERROR_INVALID_PARAMETER），无权打开时也失败。
        let process = unsafe { OpenProcess(PROCESS_SYNCHRONIZE, false, process_id) }
            .map(OwnedHandle)
            .map_err(|error| {
                if error.code() == ERROR_INVALID_PARAMETER.to_hresult() {
                    AudioError::ProcessExited
                } else {
                    os("OpenProcess")(error)
                }
            })?;
        if exited(&process) {
            return Err(AudioError::ProcessExited);
        }
        let client = activate(process_id)?;
        let block_align = channels * 4;
        let wave = WAVEFORMATEX {
            wFormatTag: FORMAT_TAG_FLOAT,
            nChannels: channels,
            nSamplesPerSec: format.sample_rate,
            nAvgBytesPerSec: format.sample_rate * u32::from(block_align),
            nBlockAlign: block_align,
            wBitsPerSample: 32,
            cbSize: 0,
        };
        let flags = AUDCLNT_STREAMFLAGS_LOOPBACK
            | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
            | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
            | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY;
        // SAFETY: `wave` 在调用期间有效；进程回环的周期参数须为 0。
        unsafe {
            client.Initialize(
                AUDCLNT_SHAREMODE_SHARED,
                flags,
                BUFFER_DURATION_HNS,
                0,
                &raw const wave,
                None,
            )
        }
        .map_err(os("IAudioClient::Initialize"))?;
        // SAFETY: 自动复位、初始无信号、无名的事件。
        let buffer_event = unsafe { CreateEventW(None, false, false, None) }
            .map(OwnedHandle)
            .map_err(os("CreateEventW"))?;
        // SAFETY: 事件句柄在音频源的整个生命期内有效。
        unsafe { client.SetEventHandle(buffer_event.0) }
            .map_err(os("IAudioClient::SetEventHandle"))?;
        // SAFETY: 客户端已初始化。
        let capture: IAudioCaptureClient =
            unsafe { client.GetService() }.map_err(os("IAudioClient::GetService"))?;
        // SAFETY: 客户端已初始化并设了事件。
        unsafe { client.Start() }.map_err(os("IAudioClient::Start"))?;
        Ok(Self {
            capture,
            client,
            buffer_event,
            process,
            format,
            process_id,
            _apartment: apartment,
        })
    }

    /// 目标进程的 ID。
    #[must_use]
    pub fn process_id(&self) -> u32 {
        self.process_id
    }

    /// 取出一个包；没有积压的包时返回 `Ok(false)`。
    fn take_packet(&mut self, packet: &mut AudioPacket) -> Result<bool, AudioError> {
        // SAFETY: 客户端已开始采集。
        let pending =
            unsafe { self.capture.GetNextPacketSize() }.map_err(os("GetNextPacketSize"))?;
        if pending == 0 {
            return Ok(false);
        }
        let mut data = ptr::null_mut();
        let mut frames = 0u32;
        let mut flags = 0u32;
        let mut qpc_position = 0u64;
        // SAFETY: 输出参数都是局部变量；成功后 `data` 指向 `frames` 个采样点，直到 ReleaseBuffer。
        unsafe {
            self.capture.GetBuffer(
                &raw mut data,
                &raw mut frames,
                &raw mut flags,
                None,
                Some(&raw mut qpc_position),
            )
        }
        .map_err(os("IAudioCaptureClient::GetBuffer"))?;
        let bytes = frames as usize * self.format.bytes_per_sample();
        packet.data.clear();
        if flags & flag(AUDCLNT_BUFFERFLAGS_SILENT) != 0 || data.is_null() {
            packet.data.resize(bytes, 0);
        } else {
            // SAFETY: WASAPI 保证 `data` 起有 `frames` 个采样点（每个 `nBlockAlign` 字节），
            // 在 ReleaseBuffer 之前有效。
            packet
                .data
                .extend_from_slice(unsafe { std::slice::from_raw_parts(data, bytes) });
        }
        // SAFETY: 释放刚取出的全部采样点。
        unsafe { self.capture.ReleaseBuffer(frames) }
            .map_err(os("IAudioCaptureClient::ReleaseBuffer"))?;
        packet.sample_count = frames;
        packet.discontinuity = flags & flag(AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY) != 0;
        packet.capture_ns =
            if flags & flag(AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR) != 0 || qpc_position == 0 {
                // 时刻不可信：按现在减去包的时长估计。
                mono_clock::now_ns().saturating_sub(self.format.duration_ns(u64::from(frames)))
            } else {
                qpc_position.saturating_mul(100)
            };
        Ok(true)
    }
}

impl AudioSource for ProcessLoopback {
    fn next_packet(
        &mut self,
        timeout: Duration,
        packet: &mut AudioPacket,
    ) -> Result<bool, AudioError> {
        let deadline = Instant::now() + timeout;
        loop {
            if self.take_packet(packet)? {
                return Ok(true);
            }
            if exited(&self.process) {
                return Err(AudioError::ProcessExited);
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Ok(false);
            }
            let millis = u32::try_from(remaining.as_millis().max(1)).unwrap_or(u32::MAX);
            let handles = [self.buffer_event.0, self.process.0];
            // SAFETY: 两个句柄在音频源的生命期内都有效。
            let woke = unsafe { WaitForMultipleObjects(&handles, false, millis) };
            match woke {
                WAIT_OBJECT_0 => {}
                woke if woke == WAIT_EVENT(WAIT_OBJECT_0.0 + 1) => {
                    return Err(AudioError::ProcessExited);
                }
                WAIT_TIMEOUT => {}
                _ => {
                    return Err(AudioError::Os {
                        context: "WaitForMultipleObjects",
                        code: woke.0.cast_signed(),
                        message: std::io::Error::last_os_error().to_string(),
                    });
                }
            }
        }
    }
}

impl Drop for ProcessLoopback {
    fn drop(&mut self) {
        // SAFETY: 客户端已初始化；停止失败时没有可做的补救。
        let _ = unsafe { self.client.Stop() };
    }
}

fn flag(value: windows::Win32::Media::Audio::_AUDCLNT_BUFFERFLAGS) -> u32 {
    value.0.cast_unsigned()
}

fn exited(process: &OwnedHandle) -> bool {
    // SAFETY: 句柄有效，带等待权限；超时为 0，只查看状态。
    let state = unsafe { WaitForSingleObject(process.0, 0) };
    state == WAIT_OBJECT_0
}

/// 激活进程回环的 `IAudioClient`，等完成回调后取结果。
fn activate(process_id: u32) -> Result<IAudioClient, AudioError> {
    let params = AUDIOCLIENT_ACTIVATION_PARAMS {
        ActivationType: AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
        Anonymous: AUDIOCLIENT_ACTIVATION_PARAMS_0 {
            ProcessLoopbackParams: AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS {
                TargetProcessId: process_id,
                ProcessLoopbackMode: PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE,
            },
        },
    };
    let blob = BLOB {
        cbSize: u32::try_from(size_of::<AUDIOCLIENT_ACTIVATION_PARAMS>())
            .expect("the activation parameters are small"),
        pBlobData: (&raw const params).cast_mut().cast(),
    };
    // windows crate 的 PROPVARIANT 在 drop 时调 PropVariantClear，会以 CoTaskMemFree 释放 blob 指向的
    // 栈上数据；这里的 blob 不是 COM 分配的，所以不让它 drop。
    let variant = ManuallyDrop::new(PROPVARIANT {
        Anonymous: PROPVARIANT_0 {
            Anonymous: ManuallyDrop::new(PROPVARIANT_0_0 {
                vt: VT_BLOB,
                wReserved1: 0,
                wReserved2: 0,
                wReserved3: 0,
                Anonymous: PROPVARIANT_0_0_0 { blob },
            }),
        },
    });
    let (sender, done) = mpsc::channel();
    let handler: IActivateAudioInterfaceCompletionHandler = Activated {
        done: Mutex::new(Some(sender)),
    }
    .into();
    // SAFETY: `variant` 与它指向的 `params` 在这个函数返回之前都有效，激活在此之前完成或超时；
    // 回调对象是 agile 的。
    let operation = unsafe {
        ActivateAudioInterfaceAsync(
            VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
            &IAudioClient::IID,
            Some(&raw const *variant),
            &handler,
        )
    }
    .map_err(unsupported_or(os("ActivateAudioInterfaceAsync")))?;
    if done.recv_timeout(ACTIVATE_TIMEOUT).is_err() {
        return Err(AudioError::Os {
            context: "ActivateAudioInterfaceAsync",
            code: 0,
            message: format!(
                "activation did not complete within {}s",
                ACTIVATE_TIMEOUT.as_secs()
            ),
        });
    }
    let mut result = HRESULT(0);
    let mut activated: Option<IUnknown> = None;
    // SAFETY: 激活已完成；输出参数都是局部变量。
    unsafe { operation.GetActivateResult(&raw mut result, &raw mut activated) }
        .map_err(os("GetActivateResult"))?;
    result
        .ok()
        .map_err(unsupported_or(os("ActivateAudioInterfaceAsync")))?;
    activated
        .ok_or_else(|| AudioError::Os {
            context: "GetActivateResult",
            code: 0,
            message: "no interface".into(),
        })?
        .cast()
        .map_err(os("IUnknown::QueryInterface(IAudioClient)"))
}

/// 系统不认识进程回环时（旧版 Windows）报 [`AudioError::Unsupported`]，其余照 `otherwise` 处理。
fn unsupported_or(
    otherwise: impl FnOnce(windows::core::Error) -> AudioError,
) -> impl FnOnce(windows::core::Error) -> AudioError {
    use windows::Win32::Foundation::{E_INVALIDARG, E_NOTIMPL};
    move |error| {
        if error.code() == E_NOTIMPL || error.code() == E_INVALIDARG {
            AudioError::Unsupported(format!("process loopback capture: {}", error.message()))
        } else {
            otherwise(error)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::ProcessLoopback;
    use crate::{AudioError, AudioFormat};

    const FORMAT: AudioFormat = AudioFormat {
        sample_rate: 48_000,
        channels: 2,
    };

    #[test]
    fn a_missing_process_is_reported_as_exited() {
        // 进程号 0 是系统空闲进程，不能以等待权限打开。
        assert!(matches!(
            ProcessLoopback::new(0, FORMAT),
            Err(AudioError::ProcessExited)
        ));
    }

    #[test]
    fn unsupported_formats_are_refused_before_touching_the_system() {
        let mono_zero = AudioFormat {
            sample_rate: 0,
            channels: 1,
        };
        assert!(matches!(
            ProcessLoopback::new(std::process::id(), mono_zero),
            Err(AudioError::Unsupported(_))
        ));
        let none = AudioFormat {
            sample_rate: 48_000,
            channels: 0,
        };
        assert!(matches!(
            ProcessLoopback::new(std::process::id(), none),
            Err(AudioError::Unsupported(_))
        ));
    }
}
