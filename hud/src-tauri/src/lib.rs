mod autostart;
mod bridge;

use tauri::{Emitter, Manager};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

#[tauri::command]
fn quit_app(app: tauri::AppHandle, kill_all: bool) {
  if kill_all {
    // Stop the Ollama desktop supervisor FIRST: it respawns `ollama serve`
    // ~3s after a listener kill, which would defeat quit-all entirely.
    {
      use std::os::windows::process::CommandExt;
      let _ = std::process::Command::new("taskkill")
        .args(["/F", "/IM", "ollama app.exe"])
        .creation_flags(0x0800_0000)
        .status();
    }
    let mut pids = autostart::listeners(autostart::daemon_port());
    for pid in autostart::listeners(autostart::OLLAMA_PORT) {
      if !pids.contains(&pid) {
        pids.push(pid);
      }
    }
    for pid in pids {
      log::info!("quit_app: stopping service pid={pid}");
      autostart::kill_pid_tree(pid);
    }
  }
  app.exit(0);
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  tauri::Builder::default()
    .plugin(tauri_plugin_log::Builder::default().level(log::LevelFilter::Info).build())
    .plugin(tauri_plugin_global_shortcut::Builder::new().build())
    .on_window_event(|window, event| {
      if let tauri::WindowEvent::CloseRequested { api, .. } = event {
        api.prevent_close();
        if window.label() == "overlay" {
          return;
        }
        let prompt_off =
          matches!(std::env::var("BS_HUD_QUIT_PROMPT"), Ok(v) if v == "0");
        let daemon_running = autostart::is_listening(autostart::daemon_port());
        let ollama_running = autostart::is_listening(autostart::OLLAMA_PORT);
        if prompt_off || (!daemon_running && !ollama_running) {
          window.app_handle().exit(0);
        } else {
          let _ = window.emit_to(
            "main",
            "close_requested",
            serde_json::json!({
              "daemon_running": daemon_running,
              "ollama_running": ollama_running,
            }),
          );
        }
      }
    })
    .setup(|app| {
      // Register global Ctrl+B shortcut
      let shortcut = Shortcut::new(Some(Modifiers::CONTROL | Modifiers::ALT), Code::KeyB);
      app.global_shortcut().on_shortcut(shortcut, |app, shortcut, event| {
        if event.state == ShortcutState::Pressed {
          app.emit("toggle-expand", ()).ok();
        }
      }).ok();

      // Floating overlay pill window (six-feature expansion, Phase 1):
      // separate from the main HUD, created hidden and shown/hidden +
      // positioned by the daemon's `overlay` IPC messages. Click-through
      // from birth — the pill must never eat input meant for the window
      // underneath.
      match tauri::WebviewWindowBuilder::new(
        app.handle(),
        "overlay",
        tauri::WebviewUrl::App("index.html".into()),
      )
      .title("BlankSlate Overlay")
      .inner_size(240.0, 56.0)
      .position(120.0, 120.0)
      .visible(false)
      .transparent(true)
      .decorations(false)
      .always_on_top(true)
      .skip_taskbar(true)
      .focused(false)
      .resizable(false)
      // Borderless pill with no native shadow: the window frame must be
      // EXACTLY (x, y, 240, 56) so the daemon's coordinates position the
      // visible content precisely (a shadow-enabled frame adds invisible
      // resize borders that offset content by ~7px).
      .shadow(false)
      .build()
      {
        Ok(overlay) => {
          if let Err(e) = overlay.set_ignore_cursor_events(true) {
            log::error!("overlay click-through failed: {e}");
          }
        }
        Err(e) => log::error!("overlay window creation failed: {e}"),
      }

      autostart::set_token_env();

      let handle = app.handle().clone();
      tauri::async_runtime::spawn(async move {
        if let Err(e) = bridge::run(&handle).await {
          log::error!("BlankSlate bridge error: {e}");
        }
      });

      app.manage(std::sync::Arc::new(std::sync::Mutex::new(
        autostart::AutostartState::default(),
      )));
      let autostart_app = app.handle().clone();
      tauri::async_runtime::spawn(async move {
        autostart::run(autostart_app).await;
      });
      Ok(())
    })
    .invoke_handler(tauri::generate_handler![
      bridge::send_daemon,
      bridge::get_daemon_status,
      bridge::resize_window,
      bridge::position_window,
      bridge::start_window_drag,
      bridge::set_tool_window,
      quit_app
    ])
    .run(tauri::generate_context!())
    .expect("error while running tauri application");
}
