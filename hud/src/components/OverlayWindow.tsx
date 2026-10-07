import { useEffect, useState } from "react";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWebviewWindow } from "@tauri-apps/api/webviewWindow";
import { PhysicalPosition, PhysicalSize } from "@tauri-apps/api/dpi";

/**
 * Overlay pill surface (six-feature expansion, Phase 1).
 *
 * Runs inside the dedicated `overlay` webview window (created hidden by
 * Rust at startup). Listens for the daemon's `overlay` broadcast and
 * shows itself at the daemon-computed coordinates, always click-through
 * (setIgnoreCursorEvents), and hides on command. The main HUD window
 * never mounts this component — the two windows are independent.
 */
export default function OverlayWindow() {
  const [text, setText] = useState("");

  useEffect(() => {
    // Per-webview DOM: make this window's chrome fully transparent so the
    // rounded pill floats over whatever is underneath.
    document.body.classList.add("overlay-mode");
    const unlisten = listen<string>("daemon_message", (e) => {
      let evt: {
        type?: string;
        action?: string;
        x?: number;
        y?: number;
        w?: number;
        h?: number;
        text?: string;
      };
      try {
        evt = JSON.parse(e.payload);
      } catch {
        return;
      }
      if (evt.type !== "overlay") return;
      const win = getCurrentWebviewWindow();
      if (evt.action === "show") {
        setText(String(evt.text ?? ""));
        win
          .setPosition(new PhysicalPosition(Math.round(evt.x ?? 0), Math.round(evt.y ?? 0)))
          .catch(() => {});
        win
          .setSize(new PhysicalSize(Math.round(evt.w ?? 240), Math.round(evt.h ?? 56)))
          .catch(() => {});
        // Spec'd click-through: calls pass through to the window below.
        win.setIgnoreCursorEvents(true).catch(() => {});
        win.show().catch(() => {});
      } else if (evt.action === "hide") {
        win.hide().catch(() => {});
      }
    });
    return () => {
      document.body.classList.remove("overlay-mode");
      unlisten.then((fn) => fn());
    };
  }, []);

  return (
    <div className="overlay-pill" data-testid="overlay-pill">
      <span data-testid="overlay-pill-text">{text}</span>
    </div>
  );
}
