// React hook bridging the webview to the daemon through the Rust WS bridge.
// The Rust side owns the daemon WebSocket connection; the webview only uses
// Tauri IPC (invoke + events), so no daemon endpoint ever appears in the bundle.
import { useCallback, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import type { DaemonEvent, DaemonStatus } from "../types";

export function useDaemon(
  onEvent: (evt: DaemonEvent) => void,
  onStatus: (s: DaemonStatus) => void,
) {
  const [connected, setConnected] = useState(false);
  const onEventRef = useRef(onEvent);
  const onStatusRef = useRef(onStatus);
  onEventRef.current = onEvent;
  onStatusRef.current = onStatus;

  useEffect(() => {
    let disposed = false;
    const unlisteners: UnlistenFn[] = [];

    (async () => {
      try {
        const em = await listen<string>("daemon_message", (e) => {
          if (disposed) return;
          try {
            const parsed = JSON.parse(e.payload) as DaemonEvent;
            onEventRef.current(parsed);
          } catch {
            // non-JSON daemon chatter — ignore
          }
        });
        unlisteners.push(em);
      } catch (err) {
        console.error("daemon_message listen failed", err);
      }

      try {
        const st = await listen<DaemonStatus>("daemon_status", (e) => {
          if (disposed) return;
          const s = e.payload;
          setConnected(s.connected);
          onStatusRef.current(s);
        });
        unlisteners.push(st);
      } catch (err) {
        console.error("daemon_status listen failed", err);
      }

      try {
        const s = await invoke<DaemonStatus>("get_daemon_status");
        setConnected(s.connected);
        onStatusRef.current(s);
      } catch (err) {
        console.error("get_daemon_status failed", err);
      }
    })();

    return () => {
      disposed = true;
      unlisteners.forEach((u) => u());
    };
  }, []);

  const send = useCallback((text: string) => {
    const msg = JSON.stringify({ type: "send_text_message", text });
    invoke("send_daemon", { message: msg }).catch((err) =>
      console.error("send_daemon failed", err),
    );
  }, []);

  const sendRaw = useCallback((msg: Record<string, unknown>) => {
    invoke("send_daemon", { message: JSON.stringify(msg) }).catch((err) =>
      console.error("send_daemon failed", err),
    );
  }, []);

  return { connected, send, sendRaw };
}
