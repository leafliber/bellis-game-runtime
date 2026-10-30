//! WGC 采集源（`docs/modules/realtime-core.md` 的“管线与线程”）。
//!
//! D3D11 设备 → `IGraphicsCaptureItemInterop::CreateForWindow` → 自由线程的 BGRA8 帧池 → 会话。帧池在系统
//! 线程上触发 `FrameArrived`，回调只记数并唤醒；取帧、裁剪、复制与读回都在调用 [`CaptureSource::next_frame`]
//! 的线程上完成，D3D11 的立即上下文只在这一个线程上使用。

use super::dpi::thread_is_per_monitor_aware_v2;
use super::os;
use super::window::{TopLevelWindow, check_capturable, find_window, query_geometry};
use crate::{
    BYTES_PER_PIXEL, CaptureError, CaptureSource, CropBox, CropError, Frame, FrameMeta, Size,
    TransformTracker, WindowSelector, client_crop, hundred_ns_to_ns,
};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant};
use windows::Foundation::{TimeSpan, TypedEventHandler};
use windows::Graphics::Capture::{
    Direct3D11CaptureFrame, Direct3D11CaptureFramePool, GraphicsCaptureAccess,
    GraphicsCaptureAccessKind, GraphicsCaptureItem, GraphicsCaptureSession,
};
use windows::Graphics::DirectX::Direct3D11::IDirect3DDevice;
use windows::Graphics::DirectX::DirectXPixelFormat;
use windows::Graphics::SizeInt32;
use windows::Security::Authorization::AppCapabilityAccess::AppCapabilityAccessStatus;
use windows::Win32::Foundation::HMODULE;
use windows::Win32::Graphics::Direct3D::D3D_DRIVER_TYPE_HARDWARE;
use windows::Win32::Graphics::Direct3D11::{
    D3D11_BOX, D3D11_CPU_ACCESS_READ, D3D11_CREATE_DEVICE_BGRA_SUPPORT, D3D11_MAP_READ,
    D3D11_MAPPED_SUBRESOURCE, D3D11_SDK_VERSION, D3D11_TEXTURE2D_DESC, D3D11_USAGE_STAGING,
    D3D11CreateDevice, ID3D11Device, ID3D11DeviceContext, ID3D11Texture2D,
};
use windows::Win32::Graphics::Dxgi::Common::{DXGI_FORMAT_B8G8R8A8_UNORM, DXGI_SAMPLE_DESC};
use windows::Win32::Graphics::Dxgi::{IDXGIAdapter, IDXGIDevice};
use windows::Win32::System::WinRT::Direct3D11::{
    CreateDirect3D11DeviceFromDXGIDevice, IDirect3DDxgiInterfaceAccess,
};
use windows::Win32::System::WinRT::Graphics::Capture::IGraphicsCaptureItemInterop;
use windows::core::{HRESULT, Interface, factory};

const PIXEL_FORMAT: DirectXPixelFormat = DirectXPixelFormat::B8G8R8A8UIntNormalized;
/// 帧池的缓冲数。每次只取最新的一帧并立即读回，两个缓冲足以让 WGC 在读回期间继续写下一帧。
const POOL_BUFFERS: i32 = 2;
/// 等帧期间检查窗口状态（关闭、最小化、独占全屏）的间隔。
const STATE_POLL_INTERVAL: Duration = Duration::from_millis(100);

/// WGC 采集源的配置。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct WgcOptions {
    /// 要采集的窗口。
    pub selector: WindowSelector,
    /// 黄色采集边框的处理。
    pub border: BorderRequest,
    /// 两帧之间的最小间隔（`GraphicsCaptureSession.MinUpdateInterval`）；`None` 时用系统缺省。
    pub min_update_interval: Option<Duration>,
}

/// 黄色采集边框的处理。
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum BorderRequest {
    /// 请求去掉边框：先请求 Borderless 访问，再把会话设为不需要边框。
    #[default]
    Remove,
    /// 保留系统的缺省行为。只用于对照。
    Keep,
}

/// 黄色采集边框的处理结果。API 读回的值不等于屏幕上看不到边框，后者须目测确认。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BorderStatus {
    /// 请求的处理。
    pub request: BorderRequest,
    /// `GraphicsCaptureAccess::RequestAccessAsync(Borderless)` 的结果；没有请求时为 `None`。
    pub access: Option<String>,
    /// `SetIsBorderRequired(false)` 失败时系统的说明。
    pub set_error: Option<String>,
    /// 设置之后读回的 `IsBorderRequired`；读取失败时为 `None`。
    pub border_required: Option<bool>,
}

/// 采集源的计数，供探测程序报告丢帧。
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct WgcStats {
    /// 从帧池取到的帧数，即最近一帧的帧号。
    pub taken: u64,
    /// 交出的帧数。
    pub delivered: u64,
    /// 为交出最新帧而丢弃的旧帧数。
    pub superseded: u64,
    /// 因画面尺寸与当前窗口几何对不上（窗口正在改变尺寸）而丢弃的帧数。
    pub stale: u64,
    /// 因客户区不在画面内或为空而丢弃的帧数。
    pub uncroppable: u64,
    /// 帧池按新尺寸重建的次数。
    pub pool_recreations: u64,
}

/// 按窗口采集客户区的 WGC 采集源。在哪个线程上创建，就在哪个线程上使用。
pub struct WgcCapture {
    window: TopLevelWindow,
    device: ID3D11Device,
    context: ID3D11DeviceContext,
    winrt_device: IDirect3DDevice,
    item: GraphicsCaptureItem,
    pool: Direct3D11CaptureFramePool,
    session: GraphicsCaptureSession,
    pool_size: SizeInt32,
    signal: Arc<Signal>,
    frame_arrived_token: i64,
    closed_token: i64,
    staging: Option<Staging>,
    tracker: TransformTracker,
    border: BorderStatus,
    stats: WgcStats,
}

struct Staging {
    texture: ID3D11Texture2D,
    width: u32,
    height: u32,
}

impl WgcCapture {
    /// 找到窗口并开始采集。
    ///
    /// # Errors
    ///
    /// 系统不支持 WGC 时返回 [`CaptureError::Unsupported`]；调用线程不是按显示器感知 DPI v2 时返回
    /// [`CaptureError::NotPerMonitorDpiAwareV2`]；窗口找不到、不唯一、已关闭、最小化或处于独占全屏时返回
    /// 对应的错误；其余失败返回 [`CaptureError::Os`]。
    pub fn new(options: &WgcOptions) -> Result<Self, CaptureError> {
        if !is_supported()? {
            return Err(CaptureError::Unsupported("Windows Graphics Capture"));
        }
        if !thread_is_per_monitor_aware_v2() {
            return Err(CaptureError::NotPerMonitorDpiAwareV2);
        }
        let window = find_window(&options.selector)?;
        check_capturable(window.hwnd)?;

        let (device, context) = create_device()?;
        let winrt_device = winrt_device(&device)?;
        let interop = factory::<GraphicsCaptureItem, IGraphicsCaptureItemInterop>()
            .map_err(os("IGraphicsCaptureItemInterop"))?;
        // SAFETY: 句柄来自刚才的枚举；窗口若已关闭，调用返回错误而不访问无效内存。
        let item: GraphicsCaptureItem = unsafe { interop.CreateForWindow(window.hwnd) }
            .map_err(os("IGraphicsCaptureItemInterop::CreateForWindow"))?;
        let pool_size = item.Size().map_err(os("GraphicsCaptureItem::Size"))?;
        let pool = Direct3D11CaptureFramePool::CreateFreeThreaded(
            &winrt_device,
            PIXEL_FORMAT,
            POOL_BUFFERS,
            pool_size,
        )
        .map_err(os("Direct3D11CaptureFramePool::CreateFreeThreaded"))?;
        let session = pool
            .CreateCaptureSession(&item)
            .map_err(os("Direct3D11CaptureFramePool::CreateCaptureSession"))?;
        session
            .SetIsCursorCaptureEnabled(false)
            .map_err(os("GraphicsCaptureSession::SetIsCursorCaptureEnabled"))?;
        let border = configure_border(&session, options.border);
        if let Some(interval) = options.min_update_interval {
            let hundred_ns = i64::try_from(interval.as_nanos() / 100).unwrap_or(i64::MAX);
            session
                .SetMinUpdateInterval(TimeSpan {
                    Duration: hundred_ns,
                })
                .map_err(os("GraphicsCaptureSession::SetMinUpdateInterval"))?;
        }

        let signal = Arc::new(Signal::default());
        let on_frame = Arc::clone(&signal);
        let frame_arrived_token = pool
            .FrameArrived(&TypedEventHandler::new(move |_, _| {
                on_frame.frame_arrived();
                Ok(())
            }))
            .map_err(os("Direct3D11CaptureFramePool::FrameArrived"))?;
        let on_closed = Arc::clone(&signal);
        let closed_token = item
            .Closed(&TypedEventHandler::new(move |_, _| {
                on_closed.item_closed();
                Ok(())
            }))
            .map_err(os("GraphicsCaptureItem::Closed"))?;

        // 先组装好，启动失败时由 Drop 注销回调并关闭会话。
        let capture = Self {
            window,
            device,
            context,
            winrt_device,
            item,
            pool,
            session,
            pool_size,
            signal,
            frame_arrived_token,
            closed_token,
            staging: None,
            tracker: TransformTracker::default(),
            border,
            stats: WgcStats::default(),
        };
        capture
            .session
            .StartCapture()
            .map_err(os("GraphicsCaptureSession::StartCapture"))?;
        Ok(capture)
    }

    /// 正在采集的窗口。
    #[must_use]
    pub fn window(&self) -> &TopLevelWindow {
        &self.window
    }

    /// 黄色采集边框的处理结果。
    #[must_use]
    pub fn border(&self) -> &BorderStatus {
        &self.border
    }

    /// 会话现在的最小帧间隔；系统不支持读取时为 `None`。
    #[must_use]
    pub fn min_update_interval(&self) -> Option<Duration> {
        let interval = self.session.MinUpdateInterval().ok()?;
        let hundred_ns = u64::try_from(interval.Duration).ok()?;
        Some(Duration::from_nanos(hundred_ns.saturating_mul(100)))
    }

    /// 到目前为止的计数。
    #[must_use]
    pub fn stats(&self) -> WgcStats {
        self.stats
    }

    /// 取出帧池中积压的全部帧，只留最新的一帧，其余关闭。
    fn take_latest(&mut self) -> Result<Option<Direct3D11CaptureFrame>, CaptureError> {
        let mut latest: Option<Direct3D11CaptureFrame> = None;
        loop {
            match self.pool.TryGetNextFrame() {
                Ok(frame) => {
                    self.stats.taken += 1;
                    if let Some(older) = latest.replace(frame) {
                        let _ = older.Close();
                        self.stats.superseded += 1;
                    }
                }
                // 帧池为空时返回空指针，windows-rs 把它表示为错误码为 S_OK 的错误。
                Err(error) if error.code() == HRESULT(0) => return Ok(latest),
                Err(error) => return Err(os("Direct3D11CaptureFramePool::TryGetNextFrame")(error)),
            }
        }
    }

    /// 把一帧的客户区读回到 `out`；这帧与当前窗口几何对不上时丢弃它并返回 `Ok(false)`。
    fn read_client(
        &mut self,
        wgc_frame: &Direct3D11CaptureFrame,
        content: SizeInt32,
        out: &mut Frame,
    ) -> Result<bool, CaptureError> {
        let number = self.stats.taken;
        let relative_time = wgc_frame
            .SystemRelativeTime()
            .map_err(os("Direct3D11CaptureFrame::SystemRelativeTime"))?;
        let captured_at_ns = hundred_ns_to_ns(relative_time.Duration).ok_or(CaptureError::Os {
            context: "Direct3D11CaptureFrame::SystemRelativeTime",
            code: 0,
            message: format!("out of range: {}", relative_time.Duration),
        })?;
        let texture = frame_texture(wgc_frame)?;
        let mut desc = D3D11_TEXTURE2D_DESC::default();
        // SAFETY: 只写入局部的描述结构。
        unsafe { texture.GetDesc(&raw mut desc) };
        let content = Size {
            width: u32::try_from(content.Width).unwrap_or(0),
            height: u32::try_from(content.Height).unwrap_or(0),
        };
        // 画面比帧池的纹理大时被截断了：窗口刚变大，帧池还没按新尺寸重建。
        if content.width > desc.Width || content.height > desc.Height {
            self.stats.stale += 1;
            return Ok(false);
        }

        // 几何在取到帧之后立即查询，与这帧最接近。
        let geometry = query_geometry(self.window.hwnd)?;
        let crop = match client_crop(&geometry, content) {
            Ok(crop) => crop,
            Err(CropError::StaleContent { .. }) => {
                self.stats.stale += 1;
                return Ok(false);
            }
            Err(CropError::ClientOutsideBounds | CropError::EmptyClient) => {
                self.stats.uncroppable += 1;
                return Ok(false);
            }
        };
        self.copy_out(&texture, crop, out)?;

        let transform = geometry.transform();
        out.meta = FrameMeta {
            number,
            captured_at_ns,
            transform_version: self.tracker.observe(transform),
            width: crop.width,
            height: crop.height,
            stride: crop.width * BYTES_PER_PIXEL,
            client_origin: transform.client_origin,
            dpi: transform.dpi,
        };
        self.stats.delivered += 1;
        Ok(true)
    }

    /// 把 `texture` 中的 `crop` 复制到 staging 纹理，映射后逐行复制到 `out.pixels`，行跨度为 `width * 4`。
    fn copy_out(
        &mut self,
        texture: &ID3D11Texture2D,
        crop: CropBox,
        out: &mut Frame,
    ) -> Result<(), CaptureError> {
        let staging = self.staging(crop.width, crop.height)?;
        let source_box = D3D11_BOX {
            left: crop.x,
            top: crop.y,
            front: 0,
            right: crop.x + crop.width,
            bottom: crop.y + crop.height,
            back: 1,
        };
        // SAFETY: 源框在纹理之内（client_crop 保证客户区在画面内，上面核对过画面不大于纹理）；目标是同格式、
        // 与源框同尺寸的 staging 纹理。
        unsafe {
            self.context.CopySubresourceRegion(
                &staging,
                0,
                0,
                0,
                0,
                texture,
                0,
                Some(&raw const source_box),
            );
        }
        let mut mapped = D3D11_MAPPED_SUBRESOURCE::default();
        // SAFETY: staging 纹理允许 CPU 读取；Map 等 GPU 复制完成后才返回，映射结果写入局部变量。
        unsafe {
            self.context
                .Map(&staging, 0, D3D11_MAP_READ, 0, Some(&raw mut mapped))
        }
        .map_err(os("ID3D11DeviceContext::Map"))?;

        let row_bytes = crop.width as usize * BYTES_PER_PIXEL as usize;
        let rows = crop.height as usize;
        let pitch = mapped.RowPitch as usize;
        out.pixels.resize(row_bytes * rows, 0);
        let source = mapped.pData.cast::<u8>().cast_const();
        for (y, row) in out.pixels.chunks_exact_mut(row_bytes).enumerate() {
            // SAFETY: 映射的内存有 `rows` 行，每行 `pitch >= row_bytes` 字节，`y < rows`；解除映射之前一直有效，
            // 且不与 `out.pixels` 重叠。
            let line = unsafe { std::slice::from_raw_parts(source.add(y * pitch), row_bytes) };
            row.copy_from_slice(line);
        }
        // SAFETY: 与上面的 Map 配对；之后不再使用映射的指针。
        unsafe { self.context.Unmap(&staging, 0) };
        Ok(())
    }

    /// 尺寸为 `width × height` 的 staging 纹理；尺寸变化时重建。
    fn staging(&mut self, width: u32, height: u32) -> Result<ID3D11Texture2D, CaptureError> {
        if let Some(staging) = &self.staging
            && staging.width == width
            && staging.height == height
        {
            return Ok(staging.texture.clone());
        }
        let desc = D3D11_TEXTURE2D_DESC {
            Width: width,
            Height: height,
            MipLevels: 1,
            ArraySize: 1,
            Format: DXGI_FORMAT_B8G8R8A8_UNORM,
            SampleDesc: DXGI_SAMPLE_DESC {
                Count: 1,
                Quality: 0,
            },
            Usage: D3D11_USAGE_STAGING,
            BindFlags: 0,
            CPUAccessFlags: D3D11_CPU_ACCESS_READ.0.cast_unsigned(),
            MiscFlags: 0,
        };
        let mut texture = None;
        // SAFETY: 描述结构与输出参数都是局部变量；不提供初始数据。
        unsafe {
            self.device
                .CreateTexture2D(&raw const desc, None, Some(&raw mut texture))
        }
        .map_err(os("ID3D11Device::CreateTexture2D"))?;
        let texture = texture.ok_or_else(|| missing("ID3D11Device::CreateTexture2D"))?;
        self.staging = Some(Staging {
            texture: texture.clone(),
            width,
            height,
        });
        Ok(texture)
    }

    /// 窗口改变了尺寸：按新尺寸重建帧池，之后的帧是新尺寸。
    fn recreate_pool(&mut self, size: SizeInt32) -> Result<(), CaptureError> {
        if size.Width <= 0 || size.Height <= 0 {
            return Ok(());
        }
        self.pool
            .Recreate(&self.winrt_device, PIXEL_FORMAT, POOL_BUFFERS, size)
            .map_err(os("Direct3D11CaptureFramePool::Recreate"))?;
        self.pool_size = size;
        self.stats.pool_recreations += 1;
        Ok(())
    }
}

impl CaptureSource for WgcCapture {
    fn next_frame(&mut self, timeout: Duration, frame: &mut Frame) -> Result<bool, CaptureError> {
        let deadline = Instant::now().checked_add(timeout);
        loop {
            let seen = self.signal.arrivals();
            if self.signal.is_closed() {
                return Err(CaptureError::WindowClosed);
            }
            check_capturable(self.window.hwnd)?;

            if let Some(wgc_frame) = self.take_latest()? {
                let content = wgc_frame
                    .ContentSize()
                    .map_err(os("Direct3D11CaptureFrame::ContentSize"))?;
                let delivered = self.read_client(&wgc_frame, content, frame);
                let _ = wgc_frame.Close();
                if content != self.pool_size {
                    self.recreate_pool(content)?;
                }
                if delivered? {
                    return Ok(true);
                }
            }

            let now = Instant::now();
            let slice = match deadline {
                Some(deadline) if deadline <= now => return Ok(false),
                Some(deadline) => (deadline - now).min(STATE_POLL_INTERVAL),
                None => STATE_POLL_INTERVAL,
            };
            self.signal.wait(seen, slice);
        }
    }
}

impl Drop for WgcCapture {
    fn drop(&mut self) {
        let _ = self.pool.RemoveFrameArrived(self.frame_arrived_token);
        let _ = self.item.RemoveClosed(self.closed_token);
        let _ = self.session.Close();
        let _ = self.pool.Close();
    }
}

/// 帧池与采集项的回调到取帧线程的信号：到达的帧数与窗口是否已关闭。
#[derive(Default)]
struct Signal {
    state: Mutex<SignalState>,
    changed: Condvar,
}

#[derive(Default)]
struct SignalState {
    arrivals: u64,
    closed: bool,
}

impl Signal {
    fn lock(&self) -> MutexGuard<'_, SignalState> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn frame_arrived(&self) {
        self.lock().arrivals += 1;
        self.changed.notify_all();
    }

    fn item_closed(&self) {
        self.lock().closed = true;
        self.changed.notify_all();
    }

    fn arrivals(&self) -> u64 {
        self.lock().arrivals
    }

    fn is_closed(&self) -> bool {
        self.lock().closed
    }

    /// 等到有新帧到达（到达数不再是 `seen`）、窗口关闭或超时。
    fn wait(&self, seen: u64, timeout: Duration) {
        let guard = self.lock();
        let _ = self
            .changed
            .wait_timeout_while(guard, timeout, |state| {
                state.arrivals == seen && !state.closed
            })
            .unwrap_or_else(PoisonError::into_inner);
    }
}

/// 系统是否支持 WGC。在任何线程上都可以调用，不建设备也不找窗口。
///
/// # Errors
///
/// 查询失败时返回 [`CaptureError::Os`]。
pub fn is_supported() -> Result<bool, CaptureError> {
    GraphicsCaptureSession::IsSupported().map_err(os("GraphicsCaptureSession::IsSupported"))
}

fn create_device() -> Result<(ID3D11Device, ID3D11DeviceContext), CaptureError> {
    let mut device = None;
    let mut context = None;
    // SAFETY: 输出参数都是局部变量；不指定适配器与软件光栅模块。
    unsafe {
        D3D11CreateDevice(
            None::<&IDXGIAdapter>,
            D3D_DRIVER_TYPE_HARDWARE,
            HMODULE::default(),
            D3D11_CREATE_DEVICE_BGRA_SUPPORT,
            None,
            D3D11_SDK_VERSION,
            Some(&raw mut device),
            None,
            Some(&raw mut context),
        )
    }
    .map_err(os("D3D11CreateDevice"))?;
    Ok((
        device.ok_or_else(|| missing("D3D11CreateDevice"))?,
        context.ok_or_else(|| missing("D3D11CreateDevice"))?,
    ))
}

fn winrt_device(device: &ID3D11Device) -> Result<IDirect3DDevice, CaptureError> {
    let dxgi: IDXGIDevice = device.cast().map_err(os("ID3D11Device as IDXGIDevice"))?;
    // SAFETY: 参数是有效的 DXGI 设备；返回的 WinRT 设备持有它的引用。
    let inspectable = unsafe { CreateDirect3D11DeviceFromDXGIDevice(&dxgi) }
        .map_err(os("CreateDirect3D11DeviceFromDXGIDevice"))?;
    inspectable.cast().map_err(os(
        "CreateDirect3D11DeviceFromDXGIDevice as IDirect3DDevice",
    ))
}

fn frame_texture(frame: &Direct3D11CaptureFrame) -> Result<ID3D11Texture2D, CaptureError> {
    let surface = frame
        .Surface()
        .map_err(os("Direct3D11CaptureFrame::Surface"))?;
    let access: IDirect3DDxgiInterfaceAccess = surface
        .cast()
        .map_err(os("IDirect3DSurface as IDirect3DDxgiInterfaceAccess"))?;
    // SAFETY: 表面由 WGC 给出；GetInterface 返回它底层的 D3D11 纹理的新引用。
    unsafe { access.GetInterface::<ID3D11Texture2D>() }
        .map_err(os("IDirect3DDxgiInterfaceAccess::GetInterface"))
}

/// 去掉黄色采集边框：先请求 Borderless 访问，再把会话设为不需要边框；结果如实记录。
fn configure_border(session: &GraphicsCaptureSession, request: BorderRequest) -> BorderStatus {
    let mut status = BorderStatus {
        request,
        access: None,
        set_error: None,
        border_required: None,
    };
    if request == BorderRequest::Remove {
        let access =
            GraphicsCaptureAccess::RequestAccessAsync(GraphicsCaptureAccessKind::Borderless)
                .and_then(|operation| operation.join());
        status.access = Some(match access {
            Ok(access) => access_status_name(access).to_owned(),
            Err(error) => describe(&error),
        });
        if let Err(error) = session.SetIsBorderRequired(false) {
            status.set_error = Some(describe(&error));
        }
    }
    status.border_required = session.IsBorderRequired().ok();
    status
}

fn access_status_name(status: AppCapabilityAccessStatus) -> &'static str {
    match status {
        AppCapabilityAccessStatus::DeniedBySystem => "DeniedBySystem",
        AppCapabilityAccessStatus::NotDeclaredByApp => "NotDeclaredByApp",
        AppCapabilityAccessStatus::DeniedByUser => "DeniedByUser",
        AppCapabilityAccessStatus::UserPromptRequired => "UserPromptRequired",
        AppCapabilityAccessStatus::Allowed => "Allowed",
        _ => "Unknown",
    }
}

fn describe(error: &windows::core::Error) -> String {
    format!("0x{:08X}: {}", error.code().0, error.message())
}

fn missing(context: &'static str) -> CaptureError {
    CaptureError::Os {
        context,
        code: 0,
        message: "the call succeeded but returned no object".into(),
    }
}
