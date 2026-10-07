"""TL3 GATE - Local Timeline (real daemon, two spawns).

Design: daemon/docs/TIMELINE_SECURITY_DESIGN.md (approved Phase 3).
Ground truth: wire traffic, daemon stdout, raw sqlite reads while the
daemon is ALIVE (attack sim), on-disk HUD source, npm typecheck.

Spawn A (no exclusions): session lifecycle, live search, Amendment 4
mid-session capture, Amendment 2 indicator (daemon state + both HUD
surfaces), attack sim, TTL purge proof.
Spawn B (BS_TIMELINE_EXCLUDE_PROC=notepad.exe): exclusion-before-capture
proof — the same foreground Notepad yields zero new rows.

Safety: types only into our scratch Notepad (foreground-verified, user
Notepad never touched), HUD edits proven by tsc, machine state restored.

Tests:
T1  spawn A + recovery ordering + watcher regression line + timeline absent
    pre-gate.
T2  transport + initial state carries timeline {active:false, frames:0}.
T3  registry 34; timeline config defaults; 9 exact routing sets (incl.
    documented co-fires: start->app_launch, stop->app_kill,
    search->web_search offline stub).
T4  HUD indicator (Amendment 2): StatusBar + CompactWidget both render on
    timeline.active with REC marker; types.ts/App.tsx carry the field;
    `npm run typecheck` exits 0.
T5  scratch Notepad setup (no marker yet), foreground verified.
T6  wire 'begin timeline' -> [start_timeline]; pushed state with
    active:true arrives; get_state confirms.
T7  search BEFORE marker exists -> 0 matches (index live, empty).
T8  marker typed MID-SESSION -> polled search finds it (Amendment 4):
    reply carries notepad.exe + timestamp + snippet; frames >= 1.
T9  state while active: active, frames>=1, started_at set.
T10 ATTACK SIM (daemon running): raw sqlite in a separate process ->
    >=1 row, zero plaintext (marker/process/screen text), created_at
    epochs only, no FTS files on disk.
T11 TTL purge proof: seed row at now-25h -> live sweep deletes it
    (stdout 'vault purge' shows timeline:1), fresh marker row survives.
T12 wire 'end timeline' -> [stop_timeline]; pushed state active:false;
    frames stable afterwards.
T13 spawn B (notepad excluded): same foreground Notepad -> frames==0,
    row count unchanged after >=3 ticks.
T14 FULL RESTORE: conv byte-identical, tasks db, timeline db removed,
    watch_rules byte-equal, Notepad closed (only ours), audio, ports,
    no residue, no strays, env cleaned.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import psutil
import websockets
from pycaw.pycaw import AudioUtilities

import win32gui
from blankslate.config import DaemonConfig, load_config
from blankslate.tools.registry import ToolRegistry
from blankslate.tools.router import ToolRouter
from blankslate.vault import core as vault

DAEMON_DIR = Path(__file__).resolve().parent
HUD_DIR = DAEMON_DIR.parent / "hud"
PORT = 8797
SCRATCH_PREFIX = "blankslate_tl3"
MARKER = "TLMARKER42"
TIMELINE_DB = DAEMON_DIR / "data" / "timeline.sqlite3"
RULES_FILE = DAEMON_DIR / "data" / "watch_rules.json"
SCRATCH_FILE = Path(tempfile.gettempdir()) / f"{SCRATCH_PREFIX}.txt"

RESULTS: list[tuple[str, bool, str]] = []

DAEMON: subprocess.Popen | None = None
LINES: list[str] = []
WS = None
WIRE: list[tuple[float, dict]] = []
BACKUP: dict[str, object] = {}
ORIG: dict[str, object] = {}
REG = ToolRegistry()
USER_NOTEPAD_OPEN = False


def rec(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {note}" if note else ""))


async def recv_until(ws, pred, deadline: float) -> dict | None:
    end = time.time() + deadline
    while time.time() < end:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.05, end - time.time()))
        except asyncio.TimeoutError:
            return None
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        WIRE.append((time.time(), msg))
        if pred(msg):
            return msg
    return None


async def send_and_reply(ws, text: str, deadline: float = 60.0) -> dict | None:
    await ws.send(json.dumps({"type": "send_text_message", "text": text}))
    return await recv_until(
        ws, lambda m: m.get("type") in ("text_reply", "error"), deadline=deadline
    )


async def get_state(ws) -> dict | None:
    await ws.send(json.dumps({"type": "get_state"}))
    return await recv_until(ws, lambda m: m.get("type") == "state", deadline=5.0)


async def _h(name: str, args: dict) -> dict:
    return await REG.get(name).handler(args)


def fg_title() -> str:
    return win32gui.GetWindowText(win32gui.GetForegroundWindow())


def notepad_titles() -> list[str]:
    titles: list[str] = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            t = win32gui.GetWindowText(hwnd)
            if "notepad" in t.lower():
                titles.append(t)
        return True

    win32gui.EnumWindows(cb, None)
    return titles


def spawn(exclude_proc: str | None = None) -> bool:
    global DAEMON
    mark = len(LINES)  # only match THIS daemon's stdout, not prior spawns
    env = dict(os.environ)
    env.update({
        "BS_IPC_PORT": str(PORT),
        "BS_TTS_ENABLED": "False",
        "BS_TIMELINE_INTERVAL": "1.0",
        "BS_VAULT_PURGE_INTERVAL": "2.0",
        "PYTHONIOENCODING": "utf-8",
    })
    if exclude_proc:
        env["BS_TIMELINE_EXCLUDE_PROC"] = exclude_proc
    DAEMON = subprocess.Popen(
        [sys.executable, "run_daemon.py"],
        cwd=str(DAEMON_DIR), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )

    def reader() -> None:
        assert DAEMON is not None and DAEMON.stdout is not None
        for line in DAEMON.stdout:
            LINES.append(line.rstrip("\n"))

    threading.Thread(target=reader, daemon=True).start()

    deadline = time.time() + 120
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
        print(f"  spawn FAILED (exit={DAEMON.poll()}); stdout tail of this daemon:")
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


def raw_count() -> int:
    import sqlite3

    if not TIMELINE_DB.exists():
        return 0
    conn = sqlite3.connect(str(TIMELINE_DB), timeout=10)
    try:
        row = conn.execute("SELECT COUNT(*) FROM timeline").fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


# ── T1: spawn A ─────────────────────────────────────────────────────────────

def t1_spawn_a() -> None:
    print("\n--- T1: spawn A + ordering + pre-gate state ---")
    conv = DAEMON_DIR / "data" / "conversation_history.json"
    BACKUP["conv"] = conv.read_bytes() if conv.exists() else None
    tasks_db = DAEMON_DIR / "data" / "tasks.sqlite3"
    BACKUP["tasks"] = tasks_db.read_bytes() if tasks_db.exists() else None
    BACKUP["rules"] = RULES_FILE.read_bytes() if RULES_FILE.exists() else None
    BACKUP["timeline"] = TIMELINE_DB.read_bytes() if TIMELINE_DB.exists() else None

    render = AudioUtilities.GetSpeakers().EndpointVolume
    ORIG["level"] = render.GetMasterVolumeLevelScalar()
    ORIG["mute"] = bool(render.GetMute())
    ORIG["mic"] = bool(
        AudioUtilities.CreateDevice(AudioUtilities.GetMicrophone()).EndpointVolume.GetMute()
    )
    print(f"  originals: vol={ORIG['level']:.4f} sysmute={ORIG['mute']} micmute={ORIG['mic']}")
    print(f"  pre-gate: timeline_db={'present' if BACKUP['timeline'] is not None else 'absent'}, "
          f"conv={'present' if BACKUP['conv'] is not None else 'absent'}, "
          f"rules={'present' if BACKUP['rules'] is not None else 'absent'}")

    busy = [c.laddr.port for c in psutil.net_connections(kind="tcp")
            if getattr(c, "laddr", None) and c.status == "LISTEN" and c.laddr.port == PORT]
    rec("T1 gate port free pre-spawn", not busy, f"listening={busy}")

    spawn_ok = spawn()

    def idx(n: str) -> int:
        return next((i for i, l in enumerate(LINES) if n in l), -1)

    i_rec, i_start, i_ipc = (idx("vault recovery:"), idx("BlankSlate daemon started"),
                             idx("IPC server listening"))
    rec("T1 daemon reached IPC listening", spawn_ok and i_ipc >= 0
        and DAEMON is not None and DAEMON.poll() is None,
        f"exit={DAEMON.poll() if DAEMON else 'gone'} lines={len(LINES)}")
    rec("T1 recovery BEFORE daemon started (flagged-edit regression)",
        0 <= i_rec < i_start, f"recovery@{i_rec} started@{i_start}")
    rec("T1 watcher still active on stdout (Phase 2 regression)",
        any("watcher: active" in l for l in LINES), "watcher line present")


# ── T2: transport + initial state ───────────────────────────────────────────

async def t2_transport() -> None:
    print("\n--- T2: transport + initial timeline state ---")
    global WS
    WS = await websockets.connect(f"ws://127.0.0.1:{PORT}")
    await WS.send(json.dumps({"type": "ping"}))
    pong = await recv_until(WS, lambda m: m.get("type") == "pong", deadline=5.0)
    rec("T2 ping -> pong", pong is not None)
    st = await get_state(WS)
    tl = (st or {}).get("timeline")
    rec("T2 state carries timeline {active:false, frames:0}",
        isinstance(tl, dict) and tl.get("active") is False and tl.get("frames") == 0,
        f"timeline={tl}")


# ── T3: registry / config / routing ─────────────────────────────────────────

async def t3_routing() -> None:
    print("\n--- T3: registry / config / routing ---")
    rec("T3 registry count 34", REG.count == 34, f"count={REG.count}")

    cfg = DaemonConfig()
    rec("T3 config defaults (off at boot, 3s, 24h, hamming 8, no excludes)",
        cfg.timeline_enabled is False and cfg.timeline_interval_s == 3.0
        and cfg.timeline_ttl_hours == 24.0 and cfg.timeline_hamming_threshold == 8
        and cfg.timeline_exclude_processes == () and cfg.timeline_exclude_titles == (),
        f"enabled={cfg.timeline_enabled} interval={cfg.timeline_interval_s} "
        f"ttl={cfg.timeline_ttl_hours}")
    lc = load_config()
    rec("T3 load_config defaults (no timeline env in gate process)",
        lc.timeline_enabled is False and lc.timeline_ttl_hours == 24.0,
        f"enabled={lc.timeline_enabled} ttl={lc.timeline_ttl_hours}")

    router = ToolRouter(cfg, REG)
    cases = [
        ("begin timeline", {"start_timeline"}),
        # documented co-fires: bare 'start' also matches app_launch
        ("start timeline", {"app_launch", "start_timeline"}),
        ("end timeline", {"stop_timeline"}),
        # bare 'stop' also matches app_kill (harmless: no such process)
        ("stop timeline", {"app_kill", "stop_timeline"}),
        # bare 'search' matches the deterministic offline web_search stub
        ("search timeline for TLMARKER42", {"search_timeline", "web_search"}),
        ("remember that tl3 marker is active", {"note_taking"}),
        ("watch file logs/tl3.log", {"add_watch_rule"}),
        ("list watch rules", {"list_watch_rules"}),
        ("remind me to call Ned tomorrow at 5pm", {"set_reminder"}),
    ]
    bad = []
    for q, want in cases:
        got = set((await router.route(q)).by_keyword)
        if got != want:
            bad.append(f"{q!r} -> {sorted(got)} (want {sorted(want)})")
    rec("T3 routing exact incl. documented co-fires (9 phrases)", not bad,
        "; ".join(bad) if bad else "all exact")


# ── T4: HUD indicator (Amendment 2) ─────────────────────────────────────────

def t4_hud() -> None:
    print("\n--- T4: HUD indicator in BOTH surfaces + typecheck (Amendment 2) ---")
    status = (HUD_DIR / "src" / "components" / "StatusBar.tsx").read_text(encoding="utf-8")
    compact = (HUD_DIR / "src" / "components" / "CompactWidget.tsx").read_text(encoding="utf-8")
    types = (HUD_DIR / "src" / "types.ts").read_text(encoding="utf-8")
    app = (HUD_DIR / "src" / "App.tsx").read_text(encoding="utf-8")

    rec("T4 StatusBar renders REC indicator on timeline.active",
        "timeline?.active" in status and "REC" in status and "Timeline" in status,
        "daemonState?.timeline?.active + REC found")
    rec("T4 CompactWidget renders REC indicator on timeline.active",
        "timeline?.active" in compact and "REC" in compact,
        "daemonState?.timeline?.active + REC found")
    rec("T4 types + App carry the timeline field",
        "TimelineState" in types and "timeline?: TimelineState" in types
        and "timeline: evt.timeline" in app,
        "types.ts TimelineState + App.tsx state pass-through")

    proc = subprocess.run(
        "npm run typecheck",
        cwd=str(HUD_DIR), capture_output=True, text=True, timeout=180, shell=True,
    )
    rec("T4 npm run typecheck exits 0", proc.returncode == 0,
        (proc.stdout + proc.stderr).strip().splitlines()[-1][:100] if proc.stdout or proc.stderr
        else "no output")


# ── T5: scratch Notepad (no marker yet) ─────────────────────────────────────

async def t5_setup_notepad() -> None:
    print("\n--- T5: scratch Notepad setup (marker NOT typed yet) ---")
    global USER_NOTEPAD_OPEN
    existing = notepad_titles()
    USER_NOTEPAD_OPEN = bool(existing and not all(SCRATCH_PREFIX in t for t in existing))
    if existing and not USER_NOTEPAD_OPEN:
        await _h("app_kill", {"query": "notepad"})
        await asyncio.sleep(1.0)
    elif existing:
        print(f"  note: leaving user Notepad open: {existing}")

    SCRATCH_FILE.write_text("tl3 scratch line one\n", encoding="utf-8")
    os.startfile(str(SCRATCH_FILE))
    await asyncio.sleep(2.5)
    if SCRATCH_PREFIX not in fg_title().lower():
        await _h("switch_window", {"title": SCRATCH_PREFIX})
        await asyncio.sleep(0.6)
    focused = SCRATCH_PREFIX in fg_title().lower()
    rec("T5 scratch Notepad open and foreground (no marker yet)", focused,
        f"fg={fg_title()!r} marker_absent={MARKER not in fg_title()}")


# ── T6: wire start ──────────────────────────────────────────────────────────

async def t6_start() -> None:
    print("\n--- T6: wire start session + pushed indicator state ---")
    wmark = len(WIRE)
    reply = await send_and_reply(WS, "begin timeline")
    await recv_until(WS, lambda m: False, deadline=2.0)  # drain async state pushes
    tools = (reply or {}).get("tools") or []
    txt = str((reply or {}).get("text", ""))
    rec("T6 tools=[start_timeline], reply confirms start",
        tools == ["start_timeline"] and "started" in txt,
        f"tools={tools} text={txt[:90]!r}")

    pushed = any(
        m.get("type") == "state" and (m.get("timeline") or {}).get("active") is True
        for _t, m in WIRE[wmark:]
    )
    push_err = any("timeline state push failed" in l for l in LINES)
    rec("T6 pushed state message carries timeline.active=true",
        pushed, f"wire_msgs={len(WIRE) - wmark}"
        + (" push_err_logged=True" if push_err else ""))

    st = await get_state(WS)
    tl = (st or {}).get("timeline") or {}
    rec("T6 get_state confirms active session",
        tl.get("active") is True and bool(tl.get("started_at")),
        f"timeline={tl}")


# ── T7: search before marker (empty) ────────────────────────────────────────

async def t7_search_empty() -> None:
    print("\n--- T7: search BEFORE marker exists -> 0 matches ---")
    reply = await send_and_reply(WS, f"search timeline for {MARKER}")
    tools = (reply or {}).get("tools") or []
    txt = str((reply or {}).get("text", ""))
    rec("T7 routing {search_timeline, web_search} (documented stub co-fire)",
        set(tools) == {"search_timeline", "web_search"}, f"tools={tools}")
    rec("T7 reply: no matches for marker yet",
        f"no matches for '{MARKER}'" in txt, repr(txt[:140]))


# ── T8: marker typed mid-session -> immediately searchable (Amendment 4) ────

async def t8_live_search() -> None:
    print("\n--- T8: mid-session capture immediately searchable (Amendment 4) ---")
    if SCRATCH_PREFIX not in fg_title().lower():
        await _h("switch_window", {"title": SCRATCH_PREFIX})
        await asyncio.sleep(0.5)
    fg_before = fg_title()
    typed = await _h("type_text", {"text": f" {MARKER} alpha"})
    await asyncio.sleep(0.5)
    fg_after = fg_title()
    rec("T8 marker typed into OUR foreground Notepad (not stolen)",
        SCRATCH_PREFIX in fg_after.lower() and typed.get("error") in (None, ""),
        f"fg_before={fg_before[:50]!r} fg_after={fg_after[:50]!r} err={typed.get('error')}")

    hit_reply = None
    deadline = time.time() + 12.0
    while time.time() < deadline:
        r = await send_and_reply(WS, f"search timeline for {MARKER}")
        txt = str((r or {}).get("text", ""))
        if "match(es)" in txt:
            hit_reply = txt
            break
        await asyncio.sleep(1.0)
    rec("T8 marker captured mid-session and immediately searchable",
        hit_reply is not None,
        repr(hit_reply[:170]) if hit_reply else "no hit within 12s (OCR/frames failed)")
    if hit_reply:
        rec("T8 hit carries process + timestamp + bracketed snippet",
            "notepad" in hit_reply.lower() and f"[{MARKER}]" in hit_reply
            and re.search(r"\d{2}:\d{2}:\d{2}", hit_reply) is not None,
            repr(hit_reply[:300]))


# ── T9: state while active ──────────────────────────────────────────────────

async def t9_state_active() -> None:
    print("\n--- T9: state while session active ---")
    st = await get_state(WS)
    tl = (st or {}).get("timeline") or {}
    rec("T9 active + frames>=1 + started_at set",
        tl.get("active") is True and int(tl.get("frames") or 0) >= 1
        and bool(tl.get("started_at")),
        f"timeline={tl}")


# ── T10: attack sim (daemon ALIVE) ──────────────────────────────────────────

def t10_attack_sim() -> None:
    print("\n--- T10: ATTACK SIM — raw sqlite read in a separate process ---")
    script = r'''
import sqlite3, sys, time
from pathlib import Path

db = Path(sys.argv[1])
raw = db.read_bytes()
checks = []
leaks = []

conn = sqlite3.connect(str(db), timeout=10)
rows = conn.execute("SELECT id, created_at, payload FROM timeline").fetchall()
conn.close()

checks.append(("rows >= 1", len(rows) >= 1, f"n={len(rows)}"))

for needle in (b"TLMARKER42", b"notepad", b"tl3 scratch"):
    if needle.lower() in raw.lower():
        leaks.append(needle.decode())
checks.append(("zero plaintext (marker/process/screen text)", not leaks, f"leaks={leaks}"))

now = time.time()
sane = all(0 < r[1] <= now + 60 for r in rows)
checks.append(("created_at epochs only (documented leak surface)", sane,
               f"max={max((r[1] for r in rows), default=0):.0f} now={now:.0f}"))

fts_files = [p.name for p in db.parent.glob("*fts*")]
checks.append(("no FTS files on disk", not fts_files, f"files={fts_files}"))

ok = all(c[1] for c in checks)
for name, passed, note in checks:
    print(("ATTACK_OK " if passed else "ATTACK_FAIL ") + name + " -- " + note)
sys.exit(0 if ok else 1)
'''
    tmp = Path(tempfile.mkdtemp()) / "tl3_attack.py"
    tmp.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(tmp), str(TIMELINE_DB)],
        capture_output=True, text=True, timeout=60, cwd=str(DAEMON_DIR),
    )
    shutil.rmtree(tmp.parent, ignore_errors=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    for line in out.strip().splitlines():
        print(f"  {line}")
    rec("T10 attack sim passes (raw db while daemon running)",
        proc.returncode == 0 and "ATTACK_FAIL" not in out,
        f"exit={proc.returncode}")


# ── T11: TTL purge proof (live sweep) ───────────────────────────────────────

def t11_ttl_purge() -> None:
    print("\n--- T11: TTL purge — seeded 25h-old row dies on the live sweep ---")
    from blankslate.timeline import store as tl_store

    fresh_before = raw_count()
    aged_payload = vault.seal(json.dumps({
        "text": "TTL-EXPIRED-SENTINEL tl3",
        "ts": time.time() - 25 * 3600,
        "process": "expired.exe",
        "title": "expired",
    }).encode("utf-8"))
    aged_id = tl_store.insert(aged_payload, created_at=time.time() - 25 * 3600)
    rec("T11 seeded 25h-old row in live db", aged_id > 0 and raw_count() == fresh_before + 1,
        f"id={aged_id} count={raw_count()}")

    gone = False
    deadline = time.time() + 9.0
    while time.time() < deadline:
        import sqlite3

        conn = sqlite3.connect(str(TIMELINE_DB), timeout=10)
        try:
            row = conn.execute("SELECT COUNT(*) FROM timeline WHERE id = ?",
                               (aged_id,)).fetchone()
        finally:
            conn.close()
        if row and row[0] == 0:
            gone = True
            break
        time.sleep(0.5)
    rec("T11 live sweep deleted the aged row", gone, f"id={aged_id}")

    purge_line = next((l for l in LINES if "vault purge:" in l and "'timeline': 1" in l), "")
    rec("T11 stdout shows vault purge with timeline:1",
        bool(purge_line), purge_line[-90:] if purge_line else "no matching line")

    still_fresh = raw_count() >= 1
    rec("T11 fresh marker rows survive", still_fresh, f"count={raw_count()}")


# ── T12: wire stop ──────────────────────────────────────────────────────────

async def t12_stop() -> None:
    print("\n--- T12: wire stop + indicator clears + frames frozen ---")
    wmark = len(WIRE)
    reply = await send_and_reply(WS, "end timeline")
    await recv_until(WS, lambda m: False, deadline=2.0)  # drain async state pushes
    tools = (reply or {}).get("tools") or []
    txt = str((reply or {}).get("text", ""))
    rec("T12 tools=[stop_timeline], reply confirms stop",
        tools == ["stop_timeline"] and "stopped" in txt, f"tools={tools} text={txt[:90]!r}")

    pushed = any(
        m.get("type") == "state" and (m.get("timeline") or {}).get("active") is False
        for _t, m in WIRE[wmark:]
    )
    rec("T12 pushed state clears timeline.active",
        pushed, f"wire_msgs={len(WIRE) - wmark}")

    st1 = await get_state(WS)
    frames1 = int(((st1 or {}).get("timeline") or {}).get("frames") or 0)
    await asyncio.sleep(2.5)
    st2 = await get_state(WS)
    tl2 = (st2 or {}).get("timeline") or {}
    rec("T12 inactive + frames frozen after stop",
        tl2.get("active") is False and int(tl2.get("frames") or 0) == frames1,
        f"frames {frames1} -> {tl2.get('frames')}")


# ── T13: spawn B — exclusion-before-capture proof ───────────────────────────

async def t13_exclusion() -> None:
    print("\n--- T13: spawn B with notepad excluded — zero new rows ---")
    global WS
    if WS is not None:
        try:
            await WS.close()
        except Exception:
            pass
        WS = None
    kill_daemon()

    if SCRATCH_PREFIX not in fg_title().lower():
        await _h("switch_window", {"title": SCRATCH_PREFIX})
        await asyncio.sleep(0.6)
    rec("T13 scratch Notepad foreground before B", SCRATCH_PREFIX in fg_title().lower(),
        f"fg={fg_title()!r}")

    count_before = raw_count()
    spawn_ok = spawn(exclude_proc="notepad.exe")

    ipc_lines = sum(1 for l in LINES if "IPC server listening" in l)
    up = spawn_ok and ipc_lines >= 2 and DAEMON is not None and DAEMON.poll() is None
    rec("T13 daemon B reached IPC listening", up,
        f"exit={DAEMON.poll() if DAEMON else 'gone'} ipc_lines={ipc_lines}")
    if not up:
        return

    try:
        WS = await websockets.connect(f"ws://127.0.0.1:{PORT}")
    except Exception as exc:
        rec("T13 connect to B", False, f"{type(exc).__name__}: {exc}")
        return
    reply = await send_and_reply(WS, "begin timeline")
    tools = (reply or {}).get("tools") or []
    rec("T13 session started on B", tools == ["start_timeline"], f"tools={tools}")

    await asyncio.sleep(3.6)  # >=3 ticks at BS_TIMELINE_INTERVAL=1.0
    fg_during = fg_title()
    st = await get_state(WS)
    tl = (st or {}).get("timeline") or {}
    count_after = raw_count()
    fg_ok = SCRATCH_PREFIX in fg_during.lower()
    rec("T13 excluded foreground -> frames==0 (capture skipped pre-pixel)",
        fg_ok and tl.get("active") is True and int(tl.get("frames") or 0) == 0,
        f"fg={fg_during[:50]!r} timeline={tl}")
    rec("T13 row count unchanged (excluded app produced no row)",
        count_after == count_before, f"before={count_before} after={count_after}")


# ── T14: restore ────────────────────────────────────────────────────────────

async def t14_restore() -> None:
    print("\n--- T14: FULL RESTORE ---")
    if WS is not None:
        try:
            await WS.close()
        except Exception:
            pass
    if DAEMON is not None and DAEMON.poll() is None:
        kill_daemon()

    conv = DAEMON_DIR / "data" / "conversation_history.json"
    if BACKUP["conv"] is not None:
        conv.write_bytes(BACKUP["conv"])  # type: ignore[arg-type]
        rec("T14 conversation restored byte-identical",
            conv.read_bytes() == BACKUP["conv"], f"{conv.stat().st_size}B")
    else:
        conv.unlink(missing_ok=True)
        rec("T14 conversation removed (absent pre-gate)", not conv.exists())

    for side in ("", "-journal", "-wal", "-shm"):
        p = Path(str(TIMELINE_DB) + side)
        if BACKUP["timeline"] is None:
            p.unlink(missing_ok=True)
        elif side == "":
            p.write_bytes(BACKUP["timeline"])  # type: ignore[arg-type]
        else:
            p.unlink(missing_ok=True)
    rec("T14 timeline db back to pre-gate state",
        TIMELINE_DB.exists() == (BACKUP["timeline"] is not None),
        f"present={TIMELINE_DB.exists()}")

    tasks_db = DAEMON_DIR / "data" / "tasks.sqlite3"
    for side in ("", "-journal", "-wal", "-shm"):
        p = Path(str(tasks_db) + side)
        if BACKUP["tasks"] is None:
            p.unlink(missing_ok=True)
        elif side == "":
            p.write_bytes(BACKUP["tasks"])  # type: ignore[arg-type]
        else:
            p.unlink(missing_ok=True)
    rec("T14 tasks db back to pre-gate state",
        tasks_db.exists() == (BACKUP["tasks"] is not None),
        f"present={tasks_db.exists()}")

    if BACKUP["rules"] is not None:
        rec("T14 watch_rules.json byte-equal (never touched by timeline)",
            RULES_FILE.exists() and RULES_FILE.read_bytes() == BACKUP["rules"],
            f"{RULES_FILE.stat().st_size if RULES_FILE.exists() else 0}B")

    nt = notepad_titles()
    if nt and not USER_NOTEPAD_OPEN and all(SCRATCH_PREFIX in t for t in nt):
        await _h("app_kill", {"query": "notepad"})
        await asyncio.sleep(0.5)
        nt = notepad_titles()
    rec("T14 scratch Notepad closed (user Notepad, if any, untouched)",
        not any(SCRATCH_PREFIX in t for t in nt),
        f"remaining={nt}")
    SCRATCH_FILE.unlink(missing_ok=True)
    rec("T14 scratch file removed", not SCRATCH_FILE.exists(), str(SCRATCH_FILE))

    render = AudioUtilities.GetSpeakers().EndpointVolume
    cap = AudioUtilities.CreateDevice(AudioUtilities.GetMicrophone()).EndpointVolume
    lvl = render.GetMasterVolumeLevelScalar()
    rec("T14 audio untouched (level/sysmute/micmute)",
        abs(lvl - float(ORIG["level"])) < 0.001
        and bool(render.GetMute()) is bool(ORIG["mute"])
        and bool(cap.GetMute()) is bool(ORIG["mic"]),
        f"vol={lvl:.4f} sys={bool(render.GetMute())} mic={bool(cap.GetMute())}")

    data = DAEMON_DIR / "data"
    residue = (list(data.glob("*.tmp")) + list((DAEMON_DIR / "notes").glob("*.tmp"))
               + list(DAEMON_DIR.glob("*.tmp"))
               + list(data.glob("timeline.sqlite3*"))
               + list(data.glob("tasks.sqlite3*")))
    if BACKUP["timeline"] is not None:
        residue = [r for r in residue if not r.name.startswith("timeline.sqlite3")
                   or r.name == "timeline.sqlite3"]
    if BACKUP["tasks"] is not None:
        residue = [r for r in residue if r.name != "tasks.sqlite3"]
    rec("T14 no .tmp / stray db residue", not residue,
        ", ".join(p.name for p in residue))

    used = [c.laddr.port for c in psutil.net_connections(kind="tcp")
            if getattr(c, "laddr", None) and c.status == "LISTEN"
            and c.laddr.port in (8765, PORT)]
    rec("T14 gate ports free after kill", not used, f"listening={used}")

    me = os.getpid()
    strays = [p.pid for p in psutil.process_iter(["pid", "cmdline"])
              if p.pid != me
              and any(str(a).endswith("run_daemon.py") for a in (p.info["cmdline"] or []))]
    rec("T14 no stray daemons", not strays, f"pids={strays}")


# ── main ─────────────────────────────────────────────────────────────────────

async def main() -> None:
    os.chdir(DAEMON_DIR)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # OCR text is utf-8
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("TL3 GATE - Local Timeline (real daemon, two spawns)")
    print("Ground truth: wire traffic, stdout, raw sqlite while alive, HUD tsc.")
    try:
        t1_spawn_a()
        await t2_transport()
        await t3_routing()
        t4_hud()
        await t5_setup_notepad()
        await t6_start()
        await t7_search_empty()
        await t8_live_search()
        await t9_state_active()
        t10_attack_sim()
        t11_ttl_purge()
        await t12_stop()
        await t13_exclusion()
    finally:
        await t14_restore()
        for k in ("BS_IPC_PORT", "BS_TTS_ENABLED", "BS_TIMELINE_INTERVAL",
                  "BS_TIMELINE_EXCLUDE_PROC", "BS_VAULT_PURGE_INTERVAL",
                  "BS_TIMELINE_ENABLED", "BS_TIMELINE_TTL_HOURS"):
            os.environ.pop(k, None)
        print("  env cleaned")


if __name__ == "__main__":
    asyncio.run(main())
    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    total = len(RESULTS)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print("\n" + "=" * 60)
    for n, d in failed:
        print(f"  FAILED: {n} -- {d}")
    print(f"GATE_RESULT: {passed}/{total} passed")
    raise SystemExit(0 if passed == total and total > 0 else 1)
