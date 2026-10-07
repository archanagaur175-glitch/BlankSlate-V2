use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tauri::Manager;

const CREATE_NO_WINDOW: u32 = 0x0800_0000;
pub const OLLAMA_PORT: u16 = 11434;
const DEFAULT_DAEMON_PORT: u16 = 8765;

#[derive(Default)]
pub struct AutostartState {
  pub daemon_pid: Option<u32>,
  pub ollama_pid: Option<u32>,
}

pub type SharedState = Arc<Mutex<AutostartState>>;

fn opt_out() -> bool {
  matches!(
    std::env::var("BS_HUD_AUTOSTART"),
    Ok(v) if v == "0" || v.eq_ignore_ascii_case("false")
  )
}

fn exe_dir() -> PathBuf {
  std::env::current_exe()
    .ok()
    .and_then(|p| p.parent().map(|d| d.to_path_buf()))
    .unwrap_or_else(|| PathBuf::from("."))
}

fn daemon_dir() -> PathBuf {
  if let Ok(d) = std::env::var("BS_DAEMON_DIR") {
    if !d.is_empty() {
      return PathBuf::from(d);
    }
  }
  let sibling = exe_dir().join("daemon");
  if sibling.join("run_daemon.py").exists() || sibling.join("blankslate-daemon.exe").exists() {
    return sibling;
  }
  let dev = PathBuf::from(r"C:\Users\archa\Documents\BlankSlate_V2\daemon");
  if dev.join("run_daemon.py").exists() {
    return dev;
  }
  sibling
}

fn frozen_launcher(dir: &std::path::Path) -> Option<PathBuf> {
  let flat = dir.join("blankslate-daemon.exe");
  if flat.exists() {
    return Some(flat);
  }
  let dev = dir
    .join("dist")
    .join("blankslate-daemon")
    .join("blankslate-daemon.exe");
  if dev.exists() {
    return Some(dev);
  }
  None
}

pub fn daemon_port() -> u16 {
  std::env::var("BS_DAEMON_PORT")
    .ok()
    .and_then(|v| v.parse().ok())
    .or_else(|| std::env::var("BS_IPC_PORT").ok().and_then(|v| v.parse().ok()))
    .unwrap_or(DEFAULT_DAEMON_PORT)
}

/// Per-session IPC token file for the launcher `spawn_daemon` would use:
/// frozen exe first (installed layout `<dir>\blankslate-daemon.exe` or dev
/// layout `dist/blankslate-daemon/blankslate-daemon.exe`, token at
/// `<exe_dir>/_internal/data/ipc_token`), dev run_daemon.py fallback
/// (`data/ipc_token`). The frozen daemon issues auth for every handshake,
/// so the bridge must read the token from HERE — not a hardcoded dev path.
pub fn token_file() -> PathBuf {
  let dir = daemon_dir();
  if let Some(exe) = frozen_launcher(&dir) {
    let base = exe.parent().map(|p| p.to_path_buf()).unwrap_or_else(|| dir.clone());
    return base.join("_internal").join("data").join("ipc_token");
  }
  dir.join("data").join("ipc_token")
}

/// Publish the token path to the bridge (BS_IPC_TOKEN_FILE is honored by
/// bridge.rs `hud_token()`; explicit BS_IPC_TOKEN / BS_IPC_TOKEN_FILE
/// env wins over this default). Called synchronously before the bridge
/// task starts so the first connect attempt already sees it.
pub fn set_token_env() {
  if let Ok(t) = std::env::var("BS_IPC_TOKEN") {
    if !t.is_empty() {
      return;
    }
  }
  if std::env::var("BS_IPC_TOKEN_FILE").is_ok() {
    return;
  }
  std::env::set_var("BS_IPC_TOKEN_FILE", token_file());
}

async fn port_open(port: u16) -> bool {
  matches!(
    tokio::time::timeout(
      Duration::from_millis(800),
      tokio::net::TcpStream::connect(("127.0.0.1", port))
    )
    .await,
    Ok(Ok(_))
  )
}

fn open_log(name: &str) -> (Stdio, Stdio) {
  let dir = daemon_dir().join("logs");
  let _ = std::fs::create_dir_all(&dir);
  let path = dir.join(name);
  let file = std::fs::OpenOptions::new()
    .create(true)
    .append(true)
    .open(&path);
  match file {
    Ok(f) => match f.try_clone() {
      Ok(g) => (Stdio::from(f), Stdio::from(g)),
      Err(_) => (Stdio::null(), Stdio::null()),
    },
    Err(_) => (Stdio::null(), Stdio::null()),
  }
}

fn spawn_pid(cmd: &mut Command) -> Option<u32> {
  use std::os::windows::process::CommandExt;
  cmd.creation_flags(CREATE_NO_WINDOW);
  match cmd.spawn() {
    Ok(child) => Some(child.id()),
    Err(e) => {
      log::warn!("autostart: spawn failed: {e}");
      None
    }
  }
}

fn spawn_daemon(port: u16) -> Option<u32> {
  let dir = daemon_dir();
  let (out, err) = open_log("hud_autostart_daemon.log");
  let explicit = std::env::var("BS_IPC_PORT").is_ok()
    || std::env::var("BS_DAEMON_PORT").is_ok();
  let mut cmd = if let Some(frozen) = frozen_launcher(&dir) {
    let mut c = Command::new(&frozen);
    if let Some(p) = frozen.parent() {
      c.current_dir(p);
    }
    log::info!("autostart: launching frozen daemon {}", frozen.display());
    c
  } else {
    let script = dir.join("run_daemon.py");
    if !script.exists() {
      log::warn!(
        "autostart: no daemon launcher found (no blankslate-daemon.exe and no run_daemon.py under {})",
        dir.display()
      );
      return None;
    }
    let mut c = Command::new("python");
    c.arg(&script).current_dir(&dir);
    log::info!("autostart: launching dev daemon {}", script.display());
    c
  };
  if explicit {
    cmd.env("BS_IPC_PORT", port.to_string());
  }
  cmd.stdout(out).stderr(err);
  spawn_pid(&mut cmd)
}

fn ollama_exe() -> Option<PathBuf> {
  if let Ok(p) = std::env::var("BS_OLLAMA_EXE") {
    if !p.is_empty() {
      let pb = PathBuf::from(&p);
      if pb.exists() {
        return Some(pb);
      }
    }
  }
  let bundled = exe_dir().join("ollama").join("ollama.exe");
  if bundled.exists() {
    return Some(bundled);
  }
  let base = std::env::var("LOCALAPPDATA").ok()?;
  if base.is_empty() {
    log::warn!("autostart: LOCALAPPDATA unset, cannot locate ollama.exe");
    return None;
  }
  let installed = PathBuf::from(base)
    .join("Programs")
    .join("Ollama")
    .join("ollama.exe");
  if installed.exists() {
    return Some(installed);
  }
  log::warn!("autostart: ollama.exe not found (checked BS_OLLAMA_EXE, {}, {})",
             bundled.display(), installed.display());
  None
}

fn spawn_ollama() -> Option<u32> {
  let exe = ollama_exe()?;
  let (out, err) = open_log("hud_autostart_ollama.log");
  let mut cmd = Command::new(&exe);
  cmd.arg("serve")
    .env("CUDA_VISIBLE_DEVICES", "0")
    .env("OLLAMA_FLASH_ATTENTION", "1");
  let pre = std::env::var("OLLAMA_MODELS").unwrap_or_default();
  if pre.is_empty() {
    if let Ok(up) = std::env::var("USERPROFILE") {
      if !up.is_empty() {
        cmd.env("OLLAMA_MODELS", PathBuf::from(up).join(".ollama").join("models"));
      }
    }
  } else {
    cmd.env("OLLAMA_MODELS", pre);
  }
  if let Some(p) = exe.parent() {
    cmd.current_dir(p);
  }
  cmd.stdout(out).stderr(err);
  log::info!("autostart: launching ollama serve {}", exe.display());
  spawn_pid(&mut cmd)
}

async fn wait_port(port: u16, seconds: u64) -> bool {
  let deadline = tokio::time::Instant::now() + Duration::from_secs(seconds);
  loop {
    if port_open(port).await {
      return true;
    }
    if tokio::time::Instant::now() >= deadline {
      return false;
    }
    tokio::time::sleep(Duration::from_secs(1)).await;
  }
}

fn record(app: &tauri::AppHandle, f: impl FnOnce(&mut AutostartState)) {
  let state = app.state::<SharedState>();
  let guard = state.lock();
  if let Ok(mut s) = guard {
    f(&mut s);
  }
}

pub async fn run(app: tauri::AppHandle) {
  if opt_out() {
    log::info!("autostart: disabled via BS_HUD_AUTOSTART");
    return;
  }

  let dport = daemon_port();
  if port_open(dport).await {
    log::info!("autostart: daemon already listening on :{dport}");
  } else {
    match spawn_daemon(dport) {
      Some(pid) => {
        log::info!("autostart: daemon spawned pid={pid} port={dport}");
        record(&app, |s| s.daemon_pid = Some(pid));
        if wait_port(dport, 90).await {
          log::info!("autostart: daemon listening on :{dport}");
        } else {
          log::warn!("autostart: daemon not listening on :{dport} after 90s");
        }
      }
      None => log::warn!("autostart: daemon not launched"),
    }
  }

  if port_open(OLLAMA_PORT).await {
    log::info!("autostart: ollama already listening on :{OLLAMA_PORT}");
  } else {
    match spawn_ollama() {
      Some(pid) => {
        log::info!("autostart: ollama spawned pid={pid}");
        record(&app, |s| s.ollama_pid = Some(pid));
        if wait_port(OLLAMA_PORT, 60).await {
          log::info!("autostart: ollama listening on :{OLLAMA_PORT}");
        } else {
          log::warn!("autostart: ollama not listening after 60s");
        }
      }
      None => log::warn!("autostart: ollama not launched"),
    }
  }
}

pub fn kill_pid_tree(pid: u32) -> bool {
  use std::os::windows::process::CommandExt;
  let status = Command::new("taskkill")
    .args(["/F", "/T", "/PID", &pid.to_string()])
    .creation_flags(CREATE_NO_WINDOW)
    .status();
  matches!(status, Ok(s) if s.success())
}

pub fn listeners(port: u16) -> Vec<u32> {
  use std::os::windows::process::CommandExt;
  let out = Command::new("netstat")
    .args(["-ano"])
    .creation_flags(CREATE_NO_WINDOW)
    .output();
  let needle = format!(":{port}");
  let mut pids: Vec<u32> = Vec::new();
  if let Ok(out) = out {
    let text = String::from_utf8_lossy(&out.stdout);
    for line in text.lines() {
      let parts: Vec<&str> = line.split_whitespace().collect();
      if parts.len() >= 5
        && parts[0].eq_ignore_ascii_case("TCP")
        && parts[3].eq_ignore_ascii_case("LISTENING")
        && parts[1].ends_with(&needle)
      {
        if let Ok(pid) = parts[4].parse::<u32>() {
          if !pids.contains(&pid) {
            pids.push(pid);
          }
        }
      }
    }
  }
  pids
}

pub fn is_listening(port: u16) -> bool {
  std::net::TcpStream::connect_timeout(
    &format!("127.0.0.1:{port}").parse().unwrap(),
    Duration::from_millis(500),
  )
  .is_ok()
}
