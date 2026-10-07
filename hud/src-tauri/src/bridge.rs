//! BlankSlate HUD bridge — maintains a WebSocket client to the local daemon
//! and relays messages to/from the webview via Tauri events + commands.
//!
//! Why a Rust-side WS client? In release the webview runs on
//! `tauri://localhost`, which cannot open raw sockets to an arbitrary port.
//! The browser-like sandbox also blocks direct calls to `ws://127.0.0.1:8765`.
//! Keeping the WS connection in Rust means the daemon endpoint ("localhost")
//! never leaks into the shipped JS bundle.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager, PhysicalPosition, PhysicalSize, State, Window};
use tokio::net::TcpStream;
use tokio::sync::Mutex;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::{MaybeTlsStream, WebSocketStream};

type DaemonStream = WebSocketStream<MaybeTlsStream<TcpStream>>;

/// Config our bridge needs (injected from tauri state).
#[derive(Clone)]
struct BridgeConfig {
    daemon_url: String,
    reconnect_delay: Duration,
}

/// Shared runtime bridge state (kept alive across reconnects).
pub(crate) struct BridgeState {
    daemon_url: String,
    connected: AtomicBool,
    writer: Mutex<Option<futures_util::stream::SplitSink<DaemonStream, Message>>>,
}

impl BridgeState {
    fn new(cfg: &BridgeConfig) -> Arc<Self> {
        Arc::new(BridgeState {
            daemon_url: cfg.daemon_url.clone(),
            connected: AtomicBool::new(false),
            writer: Mutex::new(None),
        })
    }
}

/// Webview-facing payload describing current bridge/daemon status.
#[derive(Serialize, Clone)]
pub struct DaemonStatus {
    connected: bool,
    daemon_url: String,
}

/// Send a JSON command (as a string) from the webview to the daemon over WS.
#[tauri::command]
pub async fn send_daemon(state: State<'_, Arc<BridgeState>>, message: String) -> Result<(), String> {
    let mut writer = state.writer.lock().await;
    let Some(ws) = writer.as_mut() else {
        return Err("daemon not connected".into());
    };
    ws.send(Message::Text(message))
        .await
        .map_err(|e| format!("send failed: {e}"))
}

/// Query the bridge's connection status.
#[tauri::command]
pub fn get_daemon_status(state: State<'_, Arc<BridgeState>>) -> DaemonStatus {
    DaemonStatus {
        connected: state.connected.load(Ordering::SeqCst),
        daemon_url: state.daemon_url.clone(),
    }
}

/// The daemon URL. Kept in Rust so it never needs to live in the frontend bundle.
fn daemon_url(_app: &AppHandle) -> String {
    let host = std::env::var("BS_DAEMON_HOST").unwrap_or_else(|_| "127.0.0.1".into());
    let port = std::env::var("BS_DAEMON_PORT").unwrap_or_else(|_| "8765".into());
    format!("ws://{host}:{port}")
}

/// Per-session IPC auth token, read fresh on every connect attempt so a
/// daemon restart (new token) is picked up by the reconnect loop.
/// BS_IPC_TOKEN wins over BS_IPC_TOKEN_FILE wins over the default path.
fn hud_token() -> String {
    if let Ok(t) = std::env::var("BS_IPC_TOKEN") {
        if !t.is_empty() {
            return t;
        }
    }
    let path = std::env::var("BS_IPC_TOKEN_FILE").unwrap_or_else(|_| {
        r"C:\Users\archa\Documents\BlankSlate_V2\daemon\data\ipc_token".into()
    });
    std::fs::read_to_string(path)
        .map(|s| s.trim().to_string())
        .unwrap_or_default()
}

/// Main loop: connect (with reconnect/backoff), pump messages to the webview.
pub async fn run(app: &AppHandle) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let cfg = BridgeConfig {
        daemon_url: daemon_url(app),
        reconnect_delay: Duration::from_secs(3),
    };
    let state = BridgeState::new(&cfg);
    app.manage(state.clone());

    loop {
        match connect_and_pump(app, &state).await {
            Ok(()) => log::info!("daemon connection closed cleanly"),
            Err(e) => log::warn!("daemon connection error: {e}"),
        }
        state.connected.store(false, Ordering::SeqCst);
        broadcast_status(app, &state);
        log::info!("reconnecting to daemon in {:?}", cfg.reconnect_delay);
        tokio::time::sleep(cfg.reconnect_delay).await;
    }
}

async fn connect_and_pump(
    app: &AppHandle,
    state: &Arc<BridgeState>,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let token = hud_token();
    let url = if token.is_empty() {
        state.daemon_url.clone()
    } else {
        format!("{}?token={}", state.daemon_url, token)
    };
    let (ws, _resp) = tokio_tungstenite::connect_async(&url).await?;
    log::info!("connected to daemon at {}", state.daemon_url);
    state.connected.store(true, Ordering::SeqCst);

    // Split so we can hold the writer separately from the read loop.
    let (sink, mut stream) = ws.split();
    {
        *state.writer.lock().await = Some(sink);
    }
    broadcast_status(app, state);

    // Greet the daemon and request state so the HUD starts with real status.
    {
        let mut guard = state.writer.lock().await;
        if let Some(sink) = guard.as_mut() {
            let _ = sink
                .send(Message::Text(r#"{"type":"get_state"}"#.into()))
                .await;
        }
    }

    while let Some(msg) = stream.next().await {
        match msg? {
            Message::Text(text) => {
                // Relay raw daemon message to the webview as an event.
                let _ = app.emit("daemon_message", text);
            }
            Message::Binary(bytes) => {
                if let Ok(text) = String::from_utf8(bytes.to_vec()) {
                    let _ = app.emit("daemon_message", text);
                }
            }
            Message::Close(_) | Message::Ping(_) | Message::Pong(_) | Message::Frame(_) => {}
        }
    }
    // Clear the stored writer on disconnect.
    *state.writer.lock().await = None;
    Ok(())
}

fn broadcast_status(app: &AppHandle, state: &Arc<BridgeState>) {
    let status = DaemonStatus {
        connected: state.connected.load(Ordering::SeqCst),
        daemon_url: state.daemon_url.clone(),
    };
    let _ = app.emit("daemon_status", status);
}

#[tauri::command]
pub fn resize_window(window: Window, width: f64, height: f64, center: bool) -> Result<(), String> {
    let size = PhysicalSize::new(width, height);
    window.set_size(size).map_err(|e| e.to_string())?;
    if center {
        if let Some(monitor) = window.primary_monitor().map_err(|e| e.to_string())? {
            let m_size = monitor.size();
            let m_pos = monitor.position();
            let x = m_pos.x + ((m_size.width as f64 - width) / 2.0) as i32;
            let y = m_pos.y + ((m_size.height as f64 - height) / 2.0) as i32;
            window.set_position(PhysicalPosition::new(x, y)).map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}

#[tauri::command]
pub fn position_window(window: Window, width: f64, height: f64, anchor: String) -> Result<(), String> {
    let size = PhysicalSize::new(width, height);
    window.set_size(size).map_err(|e| e.to_string())?;
    if let Some(monitor) = window.primary_monitor().map_err(|e| e.to_string())? {
        let m_size = monitor.size();
        let m_pos = monitor.position();
        let margin: i32 = 12;
        let taskbar_h: i32 = 48;
        let (x, y) = match anchor.as_str() {
            "bottom_right" => (
                m_pos.x + m_size.width as i32 - width as i32 - margin,
                m_pos.y + m_size.height as i32 - height as i32 - taskbar_h - margin,
            ),
            _ => {
                // center
                (
                    m_pos.x + ((m_size.width as f64 - width) / 2.0) as i32,
                    m_pos.y + ((m_size.height as f64 - height) / 2.0) as i32,
                )
            }
        };
        window.set_position(PhysicalPosition::new(x, y)).map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
pub fn start_window_drag(window: Window) -> Result<(), String> {
    window.start_dragging().map_err(|e| e.to_string())
}

#[tauri::command]
pub fn set_tool_window(window: Window) -> Result<(), String> {
    use windows::Win32::UI::WindowsAndMessaging::*;

    let hwnd_raw = window.hwnd().map_err(|e| e.to_string())?;
    let hwnd_ptr = hwnd_raw.0 as isize;
    unsafe {
        let hwnd = windows::Win32::Foundation::HWND(hwnd_ptr as *mut _);
        let ex_style = GetWindowLongW(hwnd, GWL_EXSTYLE);
        SetWindowLongW(hwnd, GWL_EXSTYLE,
            ex_style | WS_EX_TOOLWINDOW.0 as i32 | WS_EX_NOACTIVATE.0 as i32,
        );
        SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        ).ok();
    }

    std::thread::spawn(move || {
        use windows::Win32::UI::WindowsAndMessaging::*;
        loop {
            std::thread::sleep(std::time::Duration::from_millis(500));
            unsafe {
                let hwnd = windows::Win32::Foundation::HWND(hwnd_ptr as *mut _);
                if IsIconic(hwnd).as_bool() {
                    let _ = ShowWindow(hwnd, SW_RESTORE);
                }
                SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
                ).ok();
            }
        }
    });
    Ok(())
}


