"""HUD Phase 0 theme probe — CDP against the real built bundle.

Serves hud/dist over HTTP on an ephemeral port, launches headless Edge with
remote debugging, waits for the React compact view, then:

  snapshot : record computed styles of body / .hud-root / injected probe
             (bg-emerald-500/20) / injected .overlay-pill + screenshot.
  repaint  : record dark, set data-theme=light on <html> (no reload, marker
             checked across separate CDP calls), record light, flip back to
             dark, record again. Assert live repaint of the opacity-modifier
             utility. Screenshots both states.

Ground truth: CDP Runtime.evaluate getComputedStyle — never assumptions.
"""
from __future__ import annotations

import argparse
import base64
import http.server
import json
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests
from websockets.sync.client import connect as ws_connect

HUD_DIST = Path(r"C:\Users\archa\Documents\BlankSlate_V2\hud\dist")
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Quiet(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HUD_DIST), **kw)

    def log_message(self, *a):
        pass


class CDP:
    def __init__(self, port: int):
        self.port = port
        self.ws = None
        self.next_id = 0

    def connect(self, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        target = None
        while time.time() < deadline:
            try:
                r = requests.get(
                    f"http://127.0.0.1:{self.port}/json", timeout=2
                ).json()
                pages = [
                    t for t in r
                    if t.get("type") == "page"
                    and ("index.html" in t.get("url", "")
                         or "tauri.localhost" in t.get("url", ""))
                ]
                if pages:
                    target = pages[0]
                    break
            except Exception:
                pass
            time.sleep(0.25)
        if not target:
            raise RuntimeError("no CDP page target appeared")
        self.ws = ws_connect(target["webSocketDebuggerUrl"], open_timeout=10)
        self.next_id = 0

    def cmd(self, method: str, params: dict | None = None, timeout: float = 20.0):
        self.next_id += 1
        mid = self.next_id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv(timeout=max(0.1, deadline - time.time())))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(f"{method} no response in {timeout}s")

    def eval_js(self, expr: str, timeout: float = 20.0):
        r = self.cmd(
            "Runtime.evaluate",
            {
                "expression": expr,
                "returnByValue": True,
                "awaitPromise": True,
            },
            timeout=timeout,
        )
        if r.get("exceptionDetails"):
            raise RuntimeError(json.dumps(r["exceptionDetails"], indent=2)[:500])
        return r["result"].get("value")

    def shot(self, path: Path) -> None:
        r = self.cmd("Page.captureScreenshot", {"format": "png"})
        path.write_bytes(base64.b64decode(r["data"]))

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


WAIT_READY = """
(new Promise((resolve, reject) => {
  const t0 = Date.now();
  const tick = () => {
    if (document.querySelector('.hud-root')) return resolve('hud-root');
    if (Date.now() - t0 > 15000) return reject(new Error('hud-root never appeared'));
    setTimeout(tick, 100);
  };
  tick();
}))
"""

MEASURE = """
(() => {
  let probe = document.getElementById('bs-probe');
  if (!probe) {
    probe = document.createElement('div');
    probe.id = 'bs-probe';
    probe.className = 'bg-emerald-500/20';
    document.body.appendChild(probe);
  }
  let pill = document.getElementById('bs-pill-probe');
  if (!pill) {
    pill = document.createElement('div');
    pill.id = 'bs-pill-probe';
    pill.className = 'overlay-pill';
    pill.style.position = 'fixed';
    pill.style.left = '-9999px';
    document.body.appendChild(pill);
  }
  const root = document.querySelector('.hud-root');
  const rs = root ? getComputedStyle(root) : null;
  const spans = [...document.querySelectorAll('.hud-root span')].slice(0, 4)
    .map(s => ({ bg: getComputedStyle(s).backgroundColor,
                 color: getComputedStyle(s).color }));
  const bar = document.querySelector('.hud-root [style*="height"]');
  return JSON.stringify({
    theme: document.documentElement.getAttribute('data-theme'),
    bodyBg: getComputedStyle(document.body).backgroundColor,
    bodyColor: getComputedStyle(document.body).color,
    rootBgImage: rs ? rs.backgroundImage : null,
    rootBorder: rs ? rs.borderColor : null,
    rootShadow: rs ? rs.boxShadow : null,
    probeBg: getComputedStyle(probe).backgroundColor,
    pillBg: getComputedStyle(pill).backgroundColor,
    pillBorder: getComputedStyle(pill).borderColor,
    pillColor: getComputedStyle(pill).color,
    spans: spans,
    barGradient: bar ? getComputedStyle(bar).backgroundImage : null,
  });
})()
"""


def measure(cdp: CDP) -> dict:
    return json.loads(cdp.eval_js(MEASURE))


def fmt(d: dict) -> str:
    return json.dumps(d, indent=2)


def diff(a: dict, b: dict) -> list[str]:
    out = []
    keys = sorted(set(a) | set(b))
    for k in keys:
        if a.get(k) != b.get(k):
            out.append(f"{k}:\n  A={a.get(k)}\n  B={b.get(k)}")
    return out


def launch(http_port: int, cdp_port: int, profile: Path):
    return subprocess.Popen(
        [
            EDGE,
            "--headless=new",
            f"--remote-debugging-port={cdp_port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--window-size=400,300",
            "--force-device-scale-factor=1",
            f"http://127.0.0.1:{http_port}/index.html",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["snapshot", "repaint"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--shots", required=True)
    args = ap.parse_args()

    out = Path(args.out)
    shots = Path(args.shots)
    shots.mkdir(parents=True, exist_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)

    http_port = free_port()
    cdp_port = free_port()
    profile = out.parent / f"edge_profile_{os.getpid()}_{int(time.time() * 1000)}"
    profile.mkdir(parents=True, exist_ok=True)

    httpd = socketserver.TCPServer(("127.0.0.1", http_port), Quiet)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    edge = launch(http_port, cdp_port, profile)
    cdp = CDP(cdp_port)
    result: dict = {}
    try:
        cdp.connect()
        cdp.cmd("Page.enable")
        ready = None
        for _ in range(5):
            try:
                ready = cdp.eval_js(WAIT_READY, timeout=25)
                break
            except RuntimeError as e:
                if "Execution context was destroyed" in str(e):
                    time.sleep(0.5)
                    continue
                raise
        if ready is None:
            raise RuntimeError("could not establish execution context")
        result["ready"] = ready
        print(f"ready: {ready}")

        if args.mode == "snapshot":
            dark = measure(cdp)
            result["dark"] = dark
            cdp.shot(shots / "dark.png")
            print(fmt(dark))
        else:
            # repaint: dark -> light (attribute flip, NO reload) -> back to dark.
            dark1 = measure(cdp)
            cdp.eval_js("window.__bs_marker = 42; document.documentElement.setAttribute('data-theme','light'); 'flipped'")
            light = measure(cdp)
            cdp.shot(shots / "light.png")
            marker = cdp.eval_js("window.__bs_marker")
            cdp.eval_js("document.documentElement.setAttribute('data-theme','dark'); 'flipped'")
            dark2 = measure(cdp)
            cdp.shot(shots / "dark_restored.png")
            result.update(dark1=dark1, light=light, dark2=dark2, marker=marker)
            print("dark1:", dark1["probeBg"], "|", dark1["bodyBg"])
            print("light:", light["probeBg"], "|", light["bodyBg"])
            print("dark2:", dark2["probeBg"], "|", dark2["bodyBg"])
            print(f"marker (no-reload proof): {marker}")

            checks = {
                "probe bg-emerald-500/20 repaints live (light != dark1)":
                    light["probeBg"] != dark1["probeBg"],
                "body background repaints live": light["bodyBg"] != dark1["bodyBg"],
                "hud-root gradient repaints live": light["rootBgImage"] != dark1["rootBgImage"],
                "overlay-pill repaints live": light["pillBg"] != dark1["pillBg"],
                "waveform gradient repaints live": light["barGradient"] != dark1["barGradient"],
                "no page reload (marker survives across CDP calls)": marker == 42,
                "dark restored identically (probe)": dark2["probeBg"] == dark1["probeBg"],
                "dark restored identically (body)": dark2["bodyBg"] == dark1["bodyBg"],
                "dark restored identically (root gradient)":
                    dark2["rootBgImage"] == dark1["rootBgImage"],
            }
            result["checks"] = checks
            for name, ok in checks.items():
                print(f"  {'OK  ' if ok else 'FAIL'} {name}")
            result["ok"] = all(checks.values())

        out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"wrote {out}")
        return 0 if result.get("ok", True) else 1
    finally:
        cdp.close()
        edge.terminate()
        try:
            edge.wait(timeout=5)
        except Exception:
            edge.kill()
        httpd.shutdown()


if __name__ == "__main__":
    sys.exit(main())
