"""GST4 GATE - webcam gesture layer (Phase 4 of the 7-phase expansion).

Design: Phase 4 spec — isolated blankslate/gesture/ package, off by
default, camera_state wire events, CameraIndicator in BOTH HUD modes,
gesture->action map {"wave": "hide_overlay"} reusing the Phase 1 overlay.

TEST-DEVIATION (locked decision, recorded here as required): the literal
spec wording ("gate prints WAVE AT THE CAMERA NOW") is replaced by
deterministic detector proof on in-memory synthetic frame sequences (no
human wave required) + an env-gated inject_gesture wire hook (BS_GATE_HOOKS
=1, same pattern as tl5's inject_speech_chunk) that drives the SAME
service code path the camera detector uses — so the action mapping
(wave -> gesture_event + overlay hide) is proven end-to-end over the wire
while the detector itself is proven deterministically in T1.

Ground truth: wire traffic, daemon stdout (camera opened/released log
lines incl. frame-read counts), source scans, filesystem snapshot diffs,
npm typecheck, real cv2 device re-open.

Spec assertion map:
(1) opt-in: default config False + service not running; enable -> camera_state
    {active:true} on wire ........................................ T1/T2/T3
(2) gesture + mapped action on the wire ......................... T4
    (synthetic detector determinism .............................. T1)
(3) indicator: camera_state handled while CameraIndicator source-found in
    CompactWidget AND StatusBar + typecheck 0 ................... T5
(4) no frames on disk: fs snapshot diff + source write-call scan . T1/T6
(5) disable -> camera_state {active:false} + device released +
    re-enable works cleanly ...................................... T7

Frames live in RAM only: no imwrite/VideoWriter/imencode anywhere in
blankslate/gesture/ (T1 source scan).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import psutil
import websockets

# npm/tsc output can contain non-cp1252 glyphs; keep print safe.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DAEMON_DIR = Path(__file__).resolve().parent
HUD_DIR = DAEMON_DIR.parent / "hud"
GESTURE_PKG = DAEMON_DIR / "blankslate" / "gesture"
PORT = 8804

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".avi", ".mp4", ".mov",
             ".webm", ".gif", ".tif", ".tiff"}
CONV = DAEMON_DIR / "data" / "conversation_history.json"
TASKS = DAEMON_DIR / "data" / "tasks.sqlite3"
TIMELINE_DB = DAEMON_DIR / "data" / "timeline.sqlite3"
RULES_FILE = DAEMON_DIR / "data" / "watch_rules.json"

# Synthetic-frame constants (probe-tuned: wave@1.57s, palm@0.53s).
W, H, FPS = 640, 480, 30
DT = 1.0 / FPS
SKIN_BGR = np.array([120, 150, 200], dtype=np.uint8)

RESULTS: list[tuple[str, bool, str]] = []
DAEMON: subprocess.Popen | None = None
LINES: list[str] = []
WIRE: list[tuple[float, dict[str, Any]]] = []
DRAIN_TASK: asyncio.Task | None = None
BACKUP: dict[str, object] = {}
SNAP_PRE: set[str] = set()
DAEMON_LOG = Path(tempfile.gettempdir()) / "st4_daemon_stdout.log"


def rec(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {note}" if note else ""))


def line_count(substr: str) -> int:
    return sum(1 for l in LINES if substr in l)


async def wait_line(substr: str, n: int = 1, deadline: float = 10.0) -> bool:
    end = time.time() + deadline
    while time.time() < end:
        if line_count(substr) >= n:
            return True
        await asyncio.sleep(0.2)
    return False


async def wait_wire(pred: Any, timeout: float, start: int = 0) -> dict[str, Any] | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for _ts, m in WIRE[start:]:
            if pred(m):
                return m
        await asyncio.sleep(0.15)
    return None


async def send_expect(ws: Any, payload: dict[str, Any], pred: Any,
                      timeout: float = 15) -> dict[str, Any] | None:
    idx = len(WIRE)
    await ws.send(json.dumps(payload))
    deadline = time.time() + timeout
    while time.time() < deadline:
        for _ts, m in WIRE[idx:]:
            if pred(m):
                return m
        await asyncio.sleep(0.15)
    return None


async def ping(ws: Any) -> bool:
    m = await send_expect(ws, {"type": "ping"},
                          lambda x: x.get("type") in ("pong", "error"), 5.0)
    return m is not None and m.get("type") == "pong"


# ── spawn / kill (house pattern) ────────────────────────────────────────────

def spawn() -> bool:
    global DAEMON
    mark = len(LINES)
    DAEMON_LOG.unlink(missing_ok=True)
    env = dict(os.environ)
    env.update({
        "BS_IPC_PORT": str(PORT),
        "BS_TTS_ENABLED": "False",
        "BS_GATE_HOOKS": "1",
        "PYTHONIOENCODING": "utf-8",
        # NOTE: BS_GESTURE_ENABLED deliberately NOT set — T2 proves the
        # default-off opt-in contract.
    })
    env.pop("BS_GESTURE_ENABLED", None)
    DAEMON = subprocess.Popen(
        [sys.executable, "run_daemon.py"],
        cwd=str(DAEMON_DIR), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )

    def reader() -> None:
        assert DAEMON is not None and DAEMON.stdout is not None
        with DAEMON_LOG.open("a", encoding="utf-8") as logf:
            for line in DAEMON.stdout:
                LINES.append(line.rstrip("\n"))
                logf.write(line)
                logf.flush()

    threading.Thread(target=reader, daemon=True).start()

    deadline = time.time() + 150
    ok = False
    while time.time() < deadline:
        if DAEMON.poll() is not None:
            break
        if any("IPC server listening" in l for l in LINES[mark:]):
            ok = True
            break
        time.sleep(0.25)
    time.sleep(0.7)
    ok = ok and DAEMON.poll() is None
    if not ok:
        print(f"  spawn FAILED (exit={DAEMON.poll()}); stdout tail:")
        for l in LINES[mark:][-25:]:
            print(f"    | {l}")
    return ok


def kill_daemon() -> None:
    global DAEMON
    if DAEMON is None:
        return
    DAEMON.terminate()
    try:
        DAEMON.wait(timeout=15)
    except subprocess.TimeoutExpired:
        DAEMON.kill()
        DAEMON.wait(timeout=10)
    print(f"  daemon killed (exit={DAEMON.poll()})")
    DAEMON = None


# ── synthetic frames (in-memory only; never written to disk) ───────────────

_rng = np.random.default_rng(42)


def _base_frame(noise: int = 6) -> np.ndarray:
    f = np.full((H, W, 3), 30, dtype=np.uint8)
    n = _rng.integers(-noise, noise + 1, size=f.shape, dtype=np.int16)
    return np.clip(f.astype(np.int16) + n, 0, 255).astype(np.uint8)


def _draw_blob(f: np.ndarray, cx: float, cy: float, size: int) -> np.ndarray:
    x0, x1 = max(0, int(cx - size / 2)), min(W, int(cx + size / 2))
    y0, y1 = max(0, int(cy - size / 2)), min(H, int(cy + size / 2))
    blob = np.tile(SKIN_BGR, (y1 - y0, x1 - x0, 1)).astype(np.int16)
    blob = np.clip(blob + _rng.integers(-10, 11, size=blob.shape,
                                        dtype=np.int16), 0, 255)
    f[y0:y1, x0:x1] = blob.astype(np.uint8)
    return f


def _run_seq(gen, seconds: float) -> list[tuple[float, str]]:
    from blankslate.gesture.gestures import GestureDetector

    d = GestureDetector()
    events: list[tuple[float, str]] = []
    t = 0.0
    for _ in range(int(seconds * FPS)):
        t += DT
        for e in d.feed(gen(t), ts=t):
            events.append((round(t, 2), e))
    return events


# ── filesystem snapshot ─────────────────────────────────────────────────────

def image_snapshot() -> set[str]:
    found: set[str] = set()
    for root in (DAEMON_DIR / "data", DAEMON_DIR / "logs",
                 Path(tempfile.gettempdir())):
        if not root.exists():
            continue
        for ext in IMAGE_EXT:
            for p in root.rglob(f"*{ext}"):
                found.add(str(p))
    return found


# ── T1: offline units ───────────────────────────────────────────────────────

def t1_units() -> None:
    print("--- T1: offline units (config default + detector + source scan) ---")
    from blankslate.config import DaemonConfig, load_config

    rec("T1 DaemonConfig().gesture_enabled is False",
        DaemonConfig().gesture_enabled is False, "")
    os.environ["BS_GESTURE_ENABLED"] = "1"
    try:
        rec("T1 env override BS_GESTURE_ENABLED=1",
            load_config().gesture_enabled is True, "")
    finally:
        os.environ.pop("BS_GESTURE_ENABLED", None)
    rec("T1 default restored after env pop",
        load_config().gesture_enabled is False, "")

    ev_wave = _run_seq(lambda t: _draw_blob(
        _base_frame(), 320 + 120 * np.sin(2 * np.pi * 2.0 * t), 240, 80), 2.5)
    rec("T1 synthetic wave sequence fires 'wave' (>=3 cycles / 1.5s window)",
        len(ev_wave) >= 1 and ev_wave[0][1] == "wave",
        f"events={ev_wave}")

    ev_static = _run_seq(lambda t: _draw_blob(_base_frame(), 320, 240, 80), 3.0)
    rec("T1 static blob fires nothing", ev_static == [], f"events={ev_static}")

    ev_drift = _run_seq(
        lambda t: _draw_blob(_base_frame(),
                             min(560.0, 150.0 + 12.0 * int(t * FPS)), 240, 80),
        3.0)
    rec("T1 linear drift (motion, no oscillation) fires nothing",
        ev_drift == [], f"events={ev_drift}")

    ev_palm = _run_seq(lambda t: _draw_blob(_base_frame(), 320, 240, 320), 1.5)
    rec("T1 synthetic palm sequence fires 'palm'",
        [e for _ts, e in ev_palm] == ["palm"], f"events={ev_palm}")

    ev_noise = _run_seq(lambda t: _base_frame(noise=25), 2.0)
    rec("T1 noise-only frames (no skin) fire nothing",
        ev_noise == [], f"events={ev_noise}")

    banned = ("imwrite", "imencode", "VideoWriter", ".tofile", "np.save",
              "savetxt", "Image.save")
    src_all = ""
    for p in sorted(GESTURE_PKG.glob("*.py")):
        src_all += p.read_text(encoding="utf-8")
    hits = [b for b in banned if b in src_all]
    rec("T1 gesture package has ZERO frame/file write calls", not hits,
        f"banned hits={hits} files={[p.name for p in sorted(GESTURE_PKG.glob('*.py'))]}")

    global SNAP_PRE
    SNAP_PRE = image_snapshot()
    rec("T1 fs baseline captured (data/, logs/, temp image/video files)",
        True, f"count={len(SNAP_PRE)}")


# ── T2: spawn, default-off proof, on-connect camera_state ──────────────────

async def t2_spawn(spawn_ok: bool) -> Any | None:
    print("\n--- T2: spawn + default OFF (service constructed, camera closed) ---")
    rec("T2 daemon reached IPC listening (port 8804)", spawn_ok,
        f"exit={DAEMON.poll() if DAEMON else 'gone'} lines={len(LINES)}")
    if not spawn_ok:
        return None

    ws = await websockets.connect(f"ws://127.0.0.1:{PORT}", open_timeout=5)

    async def drain() -> None:
        try:
            async for raw in ws:
                try:
                    WIRE.append((time.time(), json.loads(raw)))
                except json.JSONDecodeError:
                    pass
        except websockets.ConnectionClosed:
            pass

    global DRAIN_TASK
    DRAIN_TASK = asyncio.create_task(drain())
    await asyncio.sleep(0.4)

    m = await wait_wire(lambda x: x.get("type") == "camera_state", 5.0, 0)
    rec("T2 on-connect camera_state pushed (service constructed)",
        m is not None, f"msg={m}")
    rec("T2 camera_state active:false at default config",
        m is not None and m.get("active") is False, f"msg={m}")

    await asyncio.sleep(1.0)
    rec("T2 camera NOT opened at default config (stdout)",
        line_count("gesture camera opened") == 0,
        f"opened_lines={line_count('gesture camera opened')}")
    rec("T2 no active:true on wire before opt-in",
        not any(x.get("active") is True for _t, x in WIRE
                if x.get("type") == "camera_state"), "")
    rec("T2 ping -> pong", await ping(ws), "")
    return ws


# ── T3: opt-in via set_gesture_mode ────────────────────────────────────────

async def t3_enable(ws: Any) -> None:
    print("\n--- T3: set_gesture_mode {enabled:true} -> camera opens ---")
    start = len(WIRE)
    ack = await send_expect(
        ws, {"type": "set_gesture_mode", "enabled": True},
        lambda m: m.get("type") in ("ack", "error")
        and (m.get("setting") == "gesture_mode" or m.get("type") == "error"),
        15.0,
    )
    rec("T3 ack setting=gesture_mode, enabled true, active true",
        ack is not None and ack.get("type") == "ack"
        and ack.get("enabled") is True and ack.get("active") is True,
        f"ack={ack}")

    evt = await wait_wire(
        lambda m: m.get("type") == "camera_state"
        and m.get("active") is True, 10.0, start)
    rec("T3 camera_state {active:true} on wire", evt is not None, f"msg={evt}")

    opened = await wait_line("gesture camera opened", 1, 5.0)
    rec("T3 daemon stdout: camera opened", opened,
        next((ln.strip()[-60:] for ln in LINES
              if "gesture camera opened" in ln), "MISSING"))


# ── T4: gesture -> event -> mapped overlay action (inject hook) ────────────

async def t4_actions(ws: Any) -> None:
    print("\n--- T4: inject wave/palm/unknown -> gesture_event + action map ---")
    start = len(WIRE)
    ack = await send_expect(
        ws, {"type": "inject_gesture", "gesture": "wave"},
        lambda m: m.get("type") in ("ack", "error"), 10.0)
    rec("T4 wave ack (action=hide_overlay)",
        ack is not None and ack.get("type") == "ack"
        and ack.get("gesture") == "wave"
        and ack.get("action") == "hide_overlay", f"ack={ack}")

    evt = await wait_wire(
        lambda m: m.get("type") == "gesture_event"
        and m.get("gesture") == "wave", 5.0, start)
    rec("T4 gesture_event {gesture:wave, action:hide_overlay} on wire",
        evt is not None and evt.get("action") == "hide_overlay",
        f"msg={evt}")

    ovl = await wait_wire(
        lambda m: m.get("type") == "overlay"
        and m.get("action") == "hide", 5.0, start)
    rec("T4 wave executed mapped action: Phase-1 overlay hide broadcast",
        ovl is not None, f"msg={ovl}")

    start2 = len(WIRE)
    ack2 = await send_expect(
        ws, {"type": "inject_gesture", "gesture": "palm"},
        lambda m: m.get("type") in ("ack", "error"), 10.0)
    rec("T4 palm ack (unmapped, action null)",
        ack2 is not None and ack2.get("type") == "ack"
        and ack2.get("action") is None, f"ack={ack2}")
    evt2 = await wait_wire(
        lambda m: m.get("type") == "gesture_event"
        and m.get("gesture") == "palm", 5.0, start2)
    rec("T4 palm gesture_event broadcast (no crash on unmapped gesture)",
        evt2 is not None, f"msg={evt2}")
    ovl2 = [m for _t, m in WIRE[start2:] if m.get("type") == "overlay"]
    rec("T4 palm does NOT touch the overlay", not ovl2, f"overlay_msgs={len(ovl2)}")

    err = await send_expect(
        ws, {"type": "inject_gesture", "gesture": "dance"},
        lambda m: m.get("type") in ("ack", "error"), 10.0)
    rec("T4 unknown gesture rejected (structured error)",
        err is not None and err.get("type") == "error"
        and "unknown gesture" in str(err.get("detail", "")), f"msg={err}")
    rec("T4 daemon healthy after inject paths", await ping(ws), "")


# ── T5: HUD indicator in BOTH modes + typecheck ─────────────────────────────

def t5_hud() -> None:
    print("\n--- T5: CameraIndicator in BOTH HUD modes + types + typecheck ---")
    status = (HUD_DIR / "src" / "components" / "StatusBar.tsx").read_text(encoding="utf-8")
    compact = (HUD_DIR / "src" / "components" / "CompactWidget.tsx").read_text(encoding="utf-8")
    types = (HUD_DIR / "src" / "types.ts").read_text(encoding="utf-8")
    app = (HUD_DIR / "src" / "App.tsx").read_text(encoding="utf-8")
    indicator = (HUD_DIR / "src" / "components" / "CameraIndicator.tsx").read_text(encoding="utf-8")

    rec("T5 CameraIndicator source-present in StatusBar",
        'import CameraIndicator' in status and "<CameraIndicator" in status
        and "cameraActive" in status, "import + render + prop")
    rec("T5 CameraIndicator source-present in CompactWidget",
        'import CameraIndicator' in compact and "<CameraIndicator" in compact
        and "cameraActive" in compact, "import + render + prop")
    rec("T5 types.ts carries camera_state + gesture_event",
        '"camera_state"' in types and '"gesture_event"' in types, "")
    rec("T5 App.tsx handles camera_state and passes it to both surfaces",
        'case "camera_state"' in app and "setCameraActive" in app
        and "cameraActive={cameraActive}" in app, "")
    rec("T5 indicator component renders only when active",
        "if (!active) return null" in indicator
        and 'data-testid="camera-indicator"' in indicator, "")

    proc = subprocess.run(
        "npm run typecheck",
        cwd=str(HUD_DIR), capture_output=True, text=True, timeout=180,
        shell=True, encoding="utf-8", errors="replace",
    )
    rec("T5 npm run typecheck exits 0", proc.returncode == 0,
        ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-1][:100]
        if (proc.stdout or proc.stderr) else "no output")


# ── T6: no frames on disk (daemon still running) ───────────────────────────

def t6_fs() -> None:
    print("\n--- T6: filesystem snapshot diff — no frame files anywhere ---")
    new = image_snapshot() - SNAP_PRE
    rec("T6 no new image/video files (data/, logs/, temp) during run",
        not new, f"new={sorted(new)[:5]}")


# ── T7: disable -> released -> re-enable clean -> device free ──────────────

async def t7_disable_reenable(ws: Any) -> None:
    print("\n--- T7: disable -> release (reads>0) -> re-enable -> final off ---")
    start = len(WIRE)
    ack = await send_expect(
        ws, {"type": "set_gesture_mode", "enabled": False},
        lambda m: m.get("type") in ("ack", "error")
        and (m.get("setting") == "gesture_mode" or m.get("type") == "error"),
        15.0)
    rec("T7 disable ack (enabled false, active false)",
        ack is not None and ack.get("type") == "ack"
        and ack.get("enabled") is False and ack.get("active") is False,
        f"ack={ack}")

    evt = await wait_wire(
        lambda m: m.get("type") == "camera_state"
        and m.get("active") is False, 10.0, start)
    rec("T7 camera_state {active:false} on wire", evt is not None, f"msg={evt}")

    await wait_line("gesture camera released", 1, 5.0)
    rel = next((ln.strip() for ln in LINES if "gesture camera released" in ln),
               "MISSING")
    mreads = re.search(r"released \(reads=(\d+)", rel)
    reads = int(mreads.group(1)) if mreads else -1
    rec("T7 camera released + frames actually flowed (reads>0)",
        mreads is not None and reads > 0, rel[-70:])

    ack2 = await send_expect(
        ws, {"type": "set_gesture_mode", "enabled": True},
        lambda m: m.get("type") in ("ack", "error"), 15.0)
    rec("T7 re-enable works cleanly (active true again)",
        ack2 is not None and ack2.get("type") == "ack"
        and ack2.get("active") is True, f"ack={ack2}")
    rec("T7 camera opened twice on stdout",
        line_count("gesture camera opened") >= 2,
        f"count={line_count('gesture camera opened')}")

    ack3 = await send_expect(
        ws, {"type": "set_gesture_mode", "enabled": False},
        lambda m: m.get("type") in ("ack", "error"), 15.0)
    rec("T7 final disable (active false)",
        ack3 is not None and ack3.get("type") == "ack"
        and ack3.get("active") is False, f"ack={ack3}")
    rec("T7 camera released twice on stdout",
        line_count("gesture camera released") >= 2,
        f"count={line_count('gesture camera released')}")

    # Device genuinely free: the gate itself can open + read it.
    try:
        import cv2

        cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        okr = bool(cap.isOpened())
        if okr:
            okr = bool(cap.read()[0])
        cap.release()
    except Exception as exc:
        okr = False
        print(f"    device re-open failed: {exc}")
    rec("T7 device re-openable by gate process after release", okr, "")
    rec("T7 daemon healthy at end of T7", await ping(ws), "")


# ── T8: kill + full restore + final cleanup ────────────────────────────────

async def t8_restore() -> None:
    print("\n--- T8: kill + BACKUP restore + final cleanup ---")
    kill_daemon()
    await asyncio.sleep(0.6)

    for key, path in (("conv", CONV), ("tasks", TASKS),
                      ("timeline", TIMELINE_DB), ("rules", RULES_FILE)):
        if BACKUP.get(key) is not None:
            path.write_bytes(BACKUP[key])  # type: ignore[arg-type]
            rec(f"T8 {key} byte-identical",
                path.read_bytes() == BACKUP[key],
                f"{path.stat().st_size}B")
        else:
            path.unlink(missing_ok=True)
            rec(f"T8 {key} removed (absent pre-gate)", not path.exists(), "")

    if not BACKUP.get("rag_existed"):
        (DAEMON_DIR / "data" / "rag_memory.sqlite3").unlink(missing_ok=True)
        (DAEMON_DIR / "data" / "rag_memory.sqlite3.lock").unlink(missing_ok=True)
        rec("T8 rag db+lock removed (absent pre-gate)",
            not (DAEMON_DIR / "data" / "rag_memory.sqlite3").exists(), "")
    else:
        rec("T8 pre-existing rag left for its own lifecycle", True,
            "existence only")

    new = image_snapshot() - SNAP_PRE
    rec("T8 no image/video files after kill", not new, f"new={sorted(new)[:5]}")

    busy = [c.laddr.port for c in psutil.net_connections(kind="tcp")
            if getattr(c, "laddr", None) and c.status == "LISTEN"
            and c.laddr.port == PORT]
    rec("T8 gate port 8804 free", not busy, f"listening={busy}")

    strays = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        cl = p.info.get("cmdline") or []
        if any("run_daemon.py" in str(a) for a in cl):
            strays.append(p.info.get("pid"))
    rec("T8 no stray daemon processes", not strays, f"pids={strays}")

    leaked = [k for k in ("BS_IPC_PORT", "BS_TTS_ENABLED", "BS_GATE_HOOKS",
                          "BS_GESTURE_ENABLED") if k in os.environ]
    rec("T8 env clean (no gate vars leaked)", not leaked, f"leaked={leaked}")


# ── main ────────────────────────────────────────────────────────────────────

async def main() -> int:
    print("=" * 72)
    print("GST4 GATE — webcam gesture layer (Phase 4)")
    print("  synthetic-detector deviation per locked plan decision; camera")
    print("  session is REAL (device 0), action map proven over the wire.")
    print("=" * 72)

    t1_units()

    conv = CONV
    BACKUP["conv"] = conv.read_bytes() if conv.exists() else None
    BACKUP["tasks"] = TASKS.read_bytes() if TASKS.exists() else None
    BACKUP["timeline"] = TIMELINE_DB.read_bytes() if TIMELINE_DB.exists() else None
    BACKUP["rules"] = RULES_FILE.read_bytes() if RULES_FILE.exists() else None
    BACKUP["rag_existed"] = (DAEMON_DIR / "data" / "rag_memory.sqlite3").exists()

    spawn_ok = spawn()
    ws = await t2_spawn(spawn_ok)
    try:
        if ws is not None:
            await t3_enable(ws)
            await t4_actions(ws)
            t5_hud()
            t6_fs()
            await t7_disable_reenable(ws)
        else:
            for name in ("T3 opt-in", "T4 action map", "T5 HUD",
                         "T6 fs", "T7 disable/re-enable"):
                rec(f"{name} (skipped: spawn failed)", False, "")
    finally:
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if DRAIN_TASK is not None:
            DRAIN_TASK.cancel()
        await t8_restore()

    total = len(RESULTS)
    passed = sum(1 for _n, ok, _note in RESULTS if ok)
    print("=" * 72)
    for name, ok, note in RESULTS:
        if not ok:
            print(f"  FAILED: {name}" + (f" -- {note}" if note else ""))
    print(f"GATE_RESULT: {passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
