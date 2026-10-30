//! 集成测试用的服务端：在真实的命名管道（Windows）或 UDS（其它平台）上接受一个连接，
//! 按测试脚本收发原始帧。只用本 crate 的公开接口实现服务端一侧的握手。

#![allow(dead_code)] // 各测试文件只用到其中一部分

use std::io::{Read, Write};
use std::thread::{self, JoinHandle};

use bellis_channel::{
    FrameDecoder, FrameKind, PROOF_DOMAIN, SessionKey, create_session_key, encode_frame,
    encode_session_key, mac,
};
use serde_json::{Value, json};

pub const ROLE: &str = "game-runtime.core";

/// 服务端一侧的原始字节流。
pub struct ServerStream {
    stream: platform::Stream,
    decoder: FrameDecoder,
}

impl ServerStream {
    /// 读一帧，返回（类型，负载）；对端关闭时返回 None。
    pub fn read_frame(&mut self) -> Option<(FrameKind, Vec<u8>)> {
        loop {
            if let Some(frame) = self.decoder.read().expect("合法帧") {
                return Some((frame.kind, frame.payload));
            }
            let mut chunk = [0u8; 4096];
            let count = self.stream.read(&mut chunk).unwrap_or(0);
            if count == 0 {
                return None;
            }
            self.decoder.push(&chunk[..count]);
        }
    }

    pub fn read_json(&mut self) -> Value {
        let (kind, payload) = self.read_frame().expect("对端未关闭");
        assert_ne!(kind, FrameKind::Binary);
        serde_json::from_slice(&payload).expect("JSON")
    }

    pub fn write_raw(&mut self, bytes: &[u8]) {
        self.stream.write_all(bytes).expect("写入");
        self.stream.flush().expect("刷新");
    }

    pub fn write_frame(&mut self, kind: FrameKind, payload: &[u8]) {
        self.write_raw(&encode_frame(kind, payload));
    }

    pub fn write_json(&mut self, kind: FrameKind, message: &Value) {
        self.write_frame(kind, message.to_string().as_bytes());
    }

    /// 读 hello，回一个 challenge；`forge` 可以改写要发出的 challenge。返回（角色，客户端随机数，服务端随机数）。
    pub fn challenge(
        &mut self,
        key: &SessionKey,
        forge: impl FnOnce(&mut Value),
    ) -> (String, String, String) {
        let hello = self.read_json();
        let role = hello["role"].as_str().expect("角色").to_owned();
        let client_nonce = hello["nonce"].as_str().expect("随机数").to_owned();
        let server_nonce = encode_session_key(&create_session_key());
        let proof = mac(
            key,
            PROOF_DOMAIN,
            "server",
            &role,
            &client_nonce,
            &server_nonce,
        );
        let mut challenge = json!({
            "v": 1, "type": "challenge", "nonce": server_nonce, "proof": encode_session_key(&proof),
        });
        forge(&mut challenge);
        self.write_json(FrameKind::Handshake, &challenge);
        (role, client_nonce, server_nonce)
    }

    /// 完成服务端一侧的握手，返回经认证的角色。
    pub fn handshake(&mut self, key: &SessionKey) -> String {
        let (role, client_nonce, server_nonce) = self.challenge(key, |_| {});
        let proof = self.read_json();
        let expected = mac(
            key,
            PROOF_DOMAIN,
            "client",
            &role,
            &client_nonce,
            &server_nonce,
        );
        assert_eq!(proof["proof"], encode_session_key(&expected), "客户端证明");
        self.write_json(FrameKind::Handshake, &json!({"v": 1, "type": "accept"}));
        role
    }

    /// 等对端关闭，返回期间收到的帧数。
    pub fn frames_until_closed(&mut self) -> usize {
        let mut count = 0;
        while self.read_frame().is_some() {
            count += 1;
        }
        count
    }
}

/// 在随机地址上监听，在线程里接受一个连接并运行脚本。返回地址与线程。
pub fn serve<T: Send + 'static>(
    script: impl FnOnce(ServerStream) -> T + Send + 'static,
) -> (String, JoinHandle<T>) {
    let listener = platform::Listener::new();
    let path = listener.path().to_owned();
    let handle = thread::spawn(move || {
        let stream = listener.accept();
        script(ServerStream {
            stream,
            decoder: FrameDecoder::new(usize::MAX),
        })
    });
    (path, handle)
}

/// 一个没有人监听的随机地址。
pub fn unused_path() -> String {
    platform::Listener::new().path().to_owned()
}

fn session_id() -> String {
    let random = create_session_key();
    let id = u32::from_be_bytes([random[0], random[1], random[2], random[3]]);
    format!("t{id:08x}")
}

#[cfg(unix)]
mod platform {
    use std::os::unix::net::{UnixListener, UnixStream};
    use std::path::PathBuf;

    pub type Stream = UnixStream;

    pub struct Listener {
        socket: UnixListener,
        directory: PathBuf,
        path: String,
    }

    impl Listener {
        pub fn new() -> Self {
            let directory = std::env::temp_dir().join(format!("bgr-{}", super::session_id()));
            std::fs::create_dir(&directory).expect("临时目录");
            let path = bellis_channel::create_channel_path(&super::session_id(), &directory)
                .expect("地址");
            let socket = UnixListener::bind(&path).expect("监听");
            Self {
                socket,
                directory,
                path,
            }
        }

        pub fn path(&self) -> &str {
            &self.path
        }

        pub fn accept(self) -> Stream {
            self.socket.accept().expect("接受连接").0
        }
    }

    impl Drop for Listener {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.directory);
        }
    }
}

#[cfg(windows)]
mod platform {
    use std::io;

    use windows::Win32::Foundation::{
        CloseHandle, ERROR_BROKEN_PIPE, ERROR_NO_DATA, ERROR_PIPE_CONNECTED,
        ERROR_PIPE_NOT_CONNECTED, HANDLE, WIN32_ERROR,
    };
    use windows::Win32::Storage::FileSystem::{
        FILE_FLAG_FIRST_PIPE_INSTANCE, PIPE_ACCESS_DUPLEX, ReadFile, WriteFile,
    };
    use windows::Win32::System::Pipes::{
        ConnectNamedPipe, CreateNamedPipeW, PIPE_READMODE_BYTE, PIPE_REJECT_REMOTE_CLIENTS,
        PIPE_TYPE_BYTE, PIPE_WAIT,
    };
    use windows::core::HSTRING;

    /// 阻塞模式的管道服务端句柄。
    pub struct Stream(HANDLE);

    // SAFETY: 内核句柄可以在线程间传递；测试中只有一个线程使用它。
    unsafe impl Send for Stream {}

    pub struct Listener {
        pipe: Stream,
        path: String,
    }

    impl Listener {
        pub fn new() -> Self {
            let path =
                bellis_channel::create_channel_path(&super::session_id(), std::path::Path::new(""))
                    .expect("地址");
            // SAFETY: 名字以 NUL 结尾，其余参数都是合法常量。
            let handle = unsafe {
                CreateNamedPipeW(
                    &HSTRING::from(path.as_str()),
                    PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE,
                    PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                    1,
                    65536,
                    65536,
                    0,
                    None,
                )
            };
            assert!(!handle.is_invalid(), "建管道失败");
            Self {
                pipe: Stream(handle),
                path,
            }
        }

        pub fn path(&self) -> &str {
            &self.path
        }

        pub fn accept(self) -> Stream {
            // SAFETY: 句柄有效；阻塞直到客户端连上。
            match unsafe { ConnectNamedPipe(self.pipe.0, None) } {
                Ok(()) => {}
                // 客户端在 ConnectNamedPipe 之前已连上（或连上又断开），之后的读返回 0。
                Err(error)
                    if matches!(
                        WIN32_ERROR::from_error(&error),
                        Some(ERROR_PIPE_CONNECTED | ERROR_NO_DATA)
                    ) => {}
                Err(error) => panic!("接受连接失败：{error}"),
            }
            self.pipe
        }
    }

    impl io::Read for Stream {
        fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
            let mut count = 0u32;
            // SAFETY: 缓冲区与计数在调用期间有效；同步句柄。
            match unsafe { ReadFile(self.0, Some(buf), Some(&raw mut count), None) } {
                Ok(()) => Ok(count as usize),
                Err(error)
                    if matches!(
                        WIN32_ERROR::from_error(&error),
                        Some(ERROR_BROKEN_PIPE | ERROR_NO_DATA | ERROR_PIPE_NOT_CONNECTED)
                    ) =>
                {
                    Ok(0)
                }
                Err(error) => Err(io::Error::other(error)),
            }
        }
    }

    impl io::Write for Stream {
        fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
            let mut count = 0u32;
            // SAFETY: 同 read。
            unsafe { WriteFile(self.0, Some(buf), Some(&raw mut count), None) }
                .map_err(io::Error::other)?;
            Ok(count as usize)
        }

        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    impl Drop for Stream {
        fn drop(&mut self) {
            // SAFETY: 句柄由本结构独占。
            let _ = unsafe { CloseHandle(self.0) };
        }
    }
}
