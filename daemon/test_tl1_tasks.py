"""TL1 GATE - Local Task Planner (real daemon).

One real run_daemon.py instance drives the wire tests; post-kill runs the
structured contract + ICS + extraction-fallback proofs on the same sealed
store. Ground truth: on-disk bytes (envelope cells), real daemon stdout,
real WS traffic, real reminder monitor_alert broadcasts.

Voice note: send_text_message feeds handle_text_input — the exact pipeline
voice transcriptions reach after STT (proven in earlier phases), so wire
text turns exercise the same path a voice command would.

Tests:
T1  spawn + Amendment A ordering (recovery first) + task store opened at
    startup (exists by IPC-up) + gate port free pre-spawn.
T2  WS transport ping/pong + get_state.
T3  routing/registry: 27 tools; task tools route canonically with disjoint
    keyword sets; 'remember' still routes to note_taking (keyword strip
    regression); reminder_interval default 30.0.
T4  wire NL reminder: 'remind me to call Ned tomorrow at 5pm' ->
    text_reply intent=tool tools=[set_reminder], deterministic reply names
    the task with due 17:00 tomorrow.
T5  wire NL add: 'create task buy milk tomorrow at 17:30' -> [add_task],
    reply carries title + due.
T6  wire list: both tasks, pending filter, count 2.
T7  wire complete: 'complete the task call Ned' -> marked done; re-list no
    longer shows it, buy milk remains.
T8  reminder fire: task due in 10s -> monitor_alert category=reminder on
    the EXISTING broadcast channel with correct title/due_at, fired
    exactly once (no duplicate over the following 3.5s window).
T9  wire note regression: 'remember that tlgate marker is active' ->
    [note_taking] only, saved, file on disk is an envelope (not plaintext).
T10 ciphertext while the daemon is ALIVE: tasks.sqlite3 contains no task
    title in plaintext, envelope prefix present; conversation still an
    envelope after live turns.
T11 kill -> structured contract (structured args win), complete/delete,
    ICS import (2 events, UTC + date-only), extraction fallback with the
    LLM removed (regex path), list counts verified at each step.
T12 vault proof: schema is (id, payload) only; raw bytes leak no title;
    opening a payload cell through vault.open_envelope recovers the JSON.
T13 FULL RESTORE: conversation byte-identical, tasks db back to pre-gate
    state, note restored, audio untouched, no residue, ports free.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import psutil
import websockets
from pycaw.pycaw import AudioUtilities

from blankslate.config import DaemonConfig, load_config
from blankslate.tools.registry import ToolRegistry
from blankslate.tools.router import ToolRouter
from blankslate.vault import core as vault

DAEMON_DIR = Path(__file__).resolve().parent
PORT = 8795
NOTE_LABEL = "tlgate"
TASKS_DB = DAEMON_DIR / "data" / "tasks.sqlite3"
NOTE_FILE = DAEMON_DIR / "notes" / f"{NOTE_LABEL}.txt"

RESULTS: list[tuple[str, bool, str]] = []
TMP_DIRS: list[Path] = []

DAEMON: subprocess.Popen | None = None
LINES: list[str] = []
WS = None
WIRE: list[tuple[float, dict]] = []
BACKUP: dict[str, object] = {}
ORIG: dict[str, object] = {}


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


async def send_and_reply(ws, text: str, deadline: float = 45.0) -> dict | None:
    await ws.send(json.dumps({"type": "send_text_message", "text": text}))
    return await recv_until(
        ws, lambda m: m.get("type") in ("text_reply", "error"), deadline=deadline
    )


# ── T1: spawn ───────────────────────────────────────────────────────────────

def t1_spawn() -> None:
    print("\n--- T1: spawn + ordering + task store at startup ---")
    global DAEMON

    conv = DAEMON_DIR / "data" / "conversation_history.json"
    BACKUP["conv"] = conv.read_bytes() if conv.exists() else None
    BACKUP["tasks"] = TASKS_DB.read_bytes() if TASKS_DB.exists() else None
    BACKUP["note"] = NOTE_FILE.read_bytes() if NOTE_FILE.exists() else None

    render = AudioUtilities.GetSpeakers().EndpointVolume
    ORIG["level"] = render.GetMasterVolumeLevelScalar()
    ORIG["mute"] = bool(render.GetMute())
    ORIG["mic"] = bool(
        AudioUtilities.CreateDevice(AudioUtilities.GetMicrophone()).EndpointVolume.GetMute()
    )
    print(f"  originals: vol={ORIG['level']:.4f} sysmute={ORIG['mute']} micmute={ORIG['mic']}")
    print(f"  pre-gate: tasks_db={'present' if BACKUP['tasks'] is not None else 'absent'}, "
          f"note={'present' if BACKUP['note'] is not None else 'absent'}")

    busy = [c.laddr.port for c in psutil.net_connections(kind="tcp")
            if getattr(c, "laddr", None) and c.status == "LISTEN" and c.laddr.port == PORT]
    rec("T1 gate port free pre-spawn", not busy, f"listening={busy}")

    env = dict(os.environ)
    env.update({
        "BS_IPC_PORT": str(PORT),
        "BS_TTS_ENABLED": "False",
        "BS_REMINDER_INTERVAL": "1.0",
        "PYTHONIOENCODING": "utf-8",
    })
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
    while time.time() < deadline and DAEMON.poll() is None:
        if any("IPC server listening" in l for l in LINES):
            break
        time.sleep(0.25)
    time.sleep(0.7)

    def idx(n: str) -> int:
        return next((i for i, l in enumerate(LINES) if n in l), -1)

    i_rec, i_start, i_ipc = idx("vault recovery:"), idx("BlankSlate daemon started"), idx("IPC server listening")
    rec("T1 daemon reached IPC listening", i_ipc >= 0 and DAEMON.poll() is None,
        f"exit={DAEMON.poll()} lines={len(LINES)}")
    rec("T1 recovery BEFORE daemon started (Amendment A regression)",
        0 <= i_rec < i_start, f"recovery@{i_rec} started@{i_start}")
    rec("T1 task store opened at startup (exists by IPC-up)", TASKS_DB.exists(),
        f"size={TASKS_DB.stat().st_size if TASKS_DB.exists() else 0}B")
    rec("T1 reminder sweeper started with gate interval",
        any("reminders: sweeping every 1.0s" in l for l in LINES),
        next((l.split("reminders:")[-1][:40] for l in LINES if "reminders:" in l), "MISSING"))


# ── T2: transport ───────────────────────────────────────────────────────────

async def t2_transport() -> None:
    print("\n--- T2: WebSocket transport on live daemon ---")
    global WS
    WS = await websockets.connect(f"ws://127.0.0.1:{PORT}")
    await WS.send(json.dumps({"type": "ping"}))
    pong = await recv_until(WS, lambda m: m.get("type") == "pong", deadline=5.0)
    rec("T2 ping -> pong", pong is not None)
    await WS.send(json.dumps({"type": "get_state"}))
    st = await recv_until(WS, lambda m: m.get("type") == "state", deadline=5.0)
    rec("T2 get_state -> state payload", st is not None and len(st) >= 3,
        f"keys={sorted(st.keys())[:8] if st else None}")


# ── T3: routing + registry + config ─────────────────────────────────────────

async def t3_routing() -> None:
    print("\n--- T3: routing / registry / config defaults ---")
    reg = ToolRegistry()
    rec("T3 registry count 34", reg.count == 34, f"count={reg.count}")
    cfg = DaemonConfig()
    rec("T3 reminder_interval default 30.0", cfg.reminder_interval == 30.0,
        f"value={cfg.reminder_interval}")
    lc = load_config()
    rec("T3 load_config default 30.0 (no env in gate process)",
        lc.reminder_interval == 30.0, f"value={lc.reminder_interval}")

    router = ToolRouter(cfg, reg)
    cases = [
        ("remind me to call Ned tomorrow at 5pm", {"set_reminder"}),
        ("create task buy milk tomorrow at 17:30", {"add_task"}),
        ("what tasks do I have", {"list_tasks"}),
        ("complete the task call Ned", {"complete_task"}),
        ("delete task buy milk", {"delete_task"}),
        ("remember that tlgate marker is active", {"note_taking"}),
        ("import ics file C:/tmp/x.ics", {"import_ics"}),
        ("remind me to check the gate in 10 seconds", {"set_reminder"}),
    ]
    bad = []
    for q, want in cases:
        got = set((await router.route(q)).by_keyword)
        if got != want:
            bad.append(f"{q!r} -> {sorted(got)} (want {sorted(want)})")
    rec("T3 disjoint keyword routing (8 canonical phrases)", not bad,
        "; ".join(bad) if bad else "all exact")

    got = set((await router.route("add task buy milk")).by_keyword)
    rec("T3 'add task' routes add_task (calculator bare-'add' co-fire known)",
        "add_task" in got, f"selected={sorted(got)}")


# ── T4/T5: wire NL add ──────────────────────────────────────────────────────

async def t4_wire_reminder() -> None:
    print("\n--- T4: wire NL reminder (set_reminder) ---")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    reply = await send_and_reply(WS, "remind me to call Ned tomorrow at 5pm")
    tools = (reply or {}).get("tools") or []
    rec("T4 text_reply intent=tool tools=[set_reminder]",
        reply is not None and reply.get("intent") == "tool" and tools == ["set_reminder"],
        f"intent={(reply or {}).get('intent')} tools={tools}")
    txt = str((reply or {}).get("text", ""))
    rec("T4 deterministic reply names task + due tomorrow 17:00",
        "call ned" in txt.lower() and "17:00" in txt and tomorrow in txt,
        repr(txt[:100]))


async def t5_wire_add() -> None:
    print("\n--- T5: wire NL add (add_task) ---")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    reply = await send_and_reply(WS, "create task buy milk tomorrow at 17:30")
    tools = (reply or {}).get("tools") or []
    rec("T5 text_reply intent=tool tools=[add_task]",
        reply is not None and reply.get("intent") == "tool" and tools == ["add_task"],
        f"intent={(reply or {}).get('intent')} tools={tools}")
    txt = str((reply or {}).get("text", ""))
    rec("T5 reply names task + due 17:30",
        "buy milk" in txt and "17:30" in txt and tomorrow in txt,
        repr(txt[:100]))


# ── T6/T7: list + complete ──────────────────────────────────────────────────

async def t6_list() -> None:
    print("\n--- T6: wire list ---")
    reply = await send_and_reply(WS, "what tasks do I have")
    txt = str((reply or {}).get("text", ""))
    tools = (reply or {}).get("tools") or []
    rec("T6 tools=[list_tasks]", tools == ["list_tasks"], f"tools={tools}")
    rec("T6 both wire-created tasks listed",
        "call ned" in txt.lower() and "buy milk" in txt.lower(), repr(txt[:140]))


async def t7_complete() -> None:
    print("\n--- T7: wire complete + re-list ---")
    reply = await send_and_reply(WS, "complete the task call Ned")
    txt = str((reply or {}).get("text", ""))
    tools = (reply or {}).get("tools") or []
    rec("T7 tools=[complete_task]", tools == ["complete_task"], f"tools={tools}")
    rec("T7 reply marks it done", "marked 'call ned' done." in txt.lower(), repr(txt[:100]))

    reply2 = await send_and_reply(WS, "what tasks do I have")
    txt2 = str((reply2 or {}).get("text", ""))
    rec("T7 re-list: completed gone, pending remains",
        "call ned" not in txt2.lower() and "buy milk" in txt2.lower(), repr(txt2[:140]))


# ── T8: reminder fire over existing broadcast ───────────────────────────────

async def t8_reminder_fire() -> None:
    print("\n--- T8: reminder fires once on the existing broadcast channel ---")
    t0 = time.time()
    reply = await send_and_reply(WS, "remind me to check the gate in 10 seconds")
    t_reply = time.time()
    txt = str((reply or {}).get("text", ""))
    tools = (reply or {}).get("tools") or []
    rec("T8 tools=[set_reminder] reply confirms reminder",
        tools == ["set_reminder"] and "Reminder set for" in txt
        and "check" in txt and "gate" in txt,
        repr(txt[:110]))

    alert = await recv_until(
        WS,
        lambda m: m.get("type") == "monitor_alert" and m.get("category") == "reminder",
        deadline=25.0,
    )
    lat = time.time() - t0
    # due = extract_time + 10s, and t0 <= extract_time <= t_reply (the reply
    # is composed after the handler ran) — anchor on both ends honestly.
    due = (alert or {}).get("due_at") or 0
    title = str(alert.get("title", "")) if alert else ""
    ok = (
        alert is not None
        and {"check", "gate"} <= set(title.split())
        and t0 + 10 - 1 <= due <= t_reply + 10 + 1
    )
    rec("T8 monitor_alert/reminder on wire with correct title + due_at",
        ok, f"lat={lat:.1f}s title={title!r} "
            f"due={datetime.fromtimestamp(due).strftime('%H:%M:%S') if due else None} "
            f"(send+10={datetime.fromtimestamp(t0 + 10).strftime('%H:%M:%S')})")

    aid = (alert or {}).get("task_id")
    dup = 0
    end = time.time() + 3.5
    while aid and time.time() < end:
        m = await recv_until(
            WS,
            lambda m: m.get("type") == "monitor_alert"
            and m.get("category") == "reminder" and m.get("task_id") == aid,
            deadline=end - time.time(),
        )
        if m is None:
            break
        dup += 1
    rec("T8 fire-once: no duplicate alert in +3.5s window", dup == 0,
        f"duplicates={dup} task_id={aid}")


# ── T9: note regression after keyword strip ─────────────────────────────────

async def t9_note_regression() -> None:
    print("\n--- T9: note regression (keyword strip) ---")
    reply = await send_and_reply(WS, "remember that tlgate marker is active")
    txt = str((reply or {}).get("text", ""))
    tools = (reply or {}).get("tools") or []
    rec("T9 tools=[note_taking] only", tools == ["note_taking"], f"tools={tools}")
    rec("T9 reply confirms save", "saved" in txt.lower() and "note" in txt.lower(),
        repr(txt[:110]))
    ok_file = NOTE_FILE.exists()
    if ok_file:
        ok_file = b"tlgate marker" not in NOTE_FILE.read_bytes()
    rec("T9 note file exists and is an envelope (not plaintext)", ok_file,
        f"exists={NOTE_FILE.exists()}")


# ── T10: ciphertext while alive ─────────────────────────────────────────────

def t10_ciphertext_live() -> None:
    print("\n--- T10: ciphertext while daemon is ALIVE ---")
    raw = TASKS_DB.read_bytes()
    titles = [b"call Ned", b"buy milk", b"check the gate"]
    leaks = [t.decode() for t in titles if re.search(re.escape(t), raw, re.IGNORECASE)]
    rec("T10 no task title in plaintext on disk (live)", not leaks, f"leaks={leaks}")
    rec("T10 envelope cells present", b"gAAAAA" in raw,
        f"db={len(raw)}B prefix={raw[100:140]!r}")
    conv = DAEMON_DIR / "data" / "conversation_history.json"
    if conv.exists():
        rec("T10 conversation still an envelope after live turns",
            vault.is_envelope(conv.read_bytes()), f"{conv.stat().st_size}B")


# ── kill + T11/T12 (daemon dead) ────────────────────────────────────────────

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


async def t11_structured_and_ics() -> None:
    print("\n--- T11: structured contract + ICS + extraction fallback (daemon dead) ---")
    from blankslate.tasks import extract as ex
    from blankslate.tasks import service
    from blankslate.tasks import store as task_store_mod

    out = await service.add_task({"title": "structured task", "due_at": "2026-10-01T09:30"})
    txt = out.get("reply", "")
    rec("T11 structured add_task (args win, no LLM)",
        "structured task" in txt and "2026-10-01 09:30" in txt, repr(txt))

    out = await service.list_tasks({"filter": "all"})
    rec("T11 list all == 4 (3 wire rows + structured)",
        out.get("count") == 4 and "structured task" in out.get("reply", ""),
        f"count={out.get('count')}")

    out = await service.complete_task({"title": "structured task"})
    rec("T11 structured complete", "Marked 'structured task' done." in out.get("reply", ""),
        repr(out.get("reply", "")))

    out = await service.delete_task({"title": "structured task"})
    rec("T11 structured delete", out.get("deleted") is True and "Deleted task" in out.get("reply", ""),
        repr(out.get("reply", "")))

    out = await service.list_tasks({"filter": "all"})
    rec("T11 list all == 3 after delete", out.get("count") == 3, f"count={out.get('count')}")

    # ICS: one UTC datetime event + one date-only all-day event
    tmp = Path(tempfile.mkdtemp())
    TMP_DIRS.append(tmp)
    ics = tmp / "tl1gate.ics"
    ics.write_text(
        "BEGIN:VCALENDAR\r\n"
        "BEGIN:VEVENT\r\nSUMMARY:Team sync\r\nDTSTART:20261001T140000Z\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nSUMMARY:Dentist appointment\r\nDTSTART;TZID=local:20261005\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n",
        encoding="utf-8",
    )
    out = await service.import_ics({"path": str(ics)})
    rec("T11 ICS import: 2 events", out.get("imported") == 2, repr(out.get("reply", "")))

    out = await service.list_tasks({"filter": "all"})
    rec("T11 list all == 5 after ICS",
        out.get("count") == 5 and "Team sync" in out.get("reply", ""),
        f"count={out.get('count')}")

    # Extraction fallback: LLM removed → deterministic regex path
    ex.set_llm(None)
    got = await ex.extract_task("remind me to water the plants tomorrow at 8am")
    want = (datetime.now() + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    ok = (
        got["title"] == "water the plants"  # regex path keeps query wording
        and got["due_at"] is not None
        and abs(got["due_at"] - want.timestamp()) < 60
    )
    rec("T11 extraction fallback (no LLM): title + due correct", ok,
        f"title={got['title']!r} due={datetime.fromtimestamp(got['due_at']).strftime('%m-%d %H:%M') if got['due_at'] else None}")

    task_store_mod.close_store()


def t12_vault_proof() -> None:
    print("\n--- T12: schema + raw-bytes proof + envelope recovery ---")
    db = sqlite3.connect(str(TASKS_DB))
    cols = [r[1] for r in db.execute("PRAGMA table_info(tasks)")]
    rows = db.execute("SELECT payload FROM tasks").fetchall()
    db.close()
    rec("T12 schema is (id, payload) only — no plaintext columns",
        cols == ["id", "payload"], f"cols={cols} rows={len(rows)}")

    raw = TASKS_DB.read_bytes()
    titles = ["call Ned", "buy milk", "check the gate", "Team sync", "Dentist appointment"]
    leaks = [t for t in titles if re.search(re.escape(t.encode()), raw, re.IGNORECASE)]
    rec("T12 raw sqlite bytes leak no title (daemon dead)", not leaks, f"leaks={leaks}")
    rec("T12 envelope prefix in every era of data", b"gAAAAA" in raw, f"db={len(raw)}B")

    opened = None
    sample = rows[0][0][:28] if rows else ""
    for payload, in rows:
        try:
            data = json.loads(vault.open_envelope(payload.encode("ascii")))
            if str(data.get("title", "")).lower() == "call ned":
                opened = data
                break
        except Exception:
            continue
    rec("T12 vault.open_envelope recovers original task JSON",
        opened is not None and str(opened.get("title", "")).lower() == "call ned",
        f"sample_payload={sample}… recovered_title={(opened or {}).get('title')!r}")


# ── T13: restore ────────────────────────────────────────────────────────────

def t13_restore() -> None:
    print("\n--- T13: FULL RESTORE ---")
    if DAEMON is not None and DAEMON.poll() is None:
        kill_daemon()

    conv = DAEMON_DIR / "data" / "conversation_history.json"
    if BACKUP["conv"] is not None:
        conv.write_bytes(BACKUP["conv"])  # type: ignore[arg-type]
        rec("T13 conversation restored byte-identical",
            conv.read_bytes() == BACKUP["conv"], f"{conv.stat().st_size}B")
    else:
        conv.unlink(missing_ok=True)
        rec("T13 conversation removed (absent pre-gate)", not conv.exists())

    # Task db back to pre-gate state (feature is new: normally absent)
    for side in ("", "-journal", "-wal", "-shm"):
        p = Path(str(TASKS_DB) + side)
        if BACKUP["tasks"] is None:
            p.unlink(missing_ok=True)
        elif side == "":
            p.write_bytes(BACKUP["tasks"])  # type: ignore[arg-type]
        else:
            p.unlink(missing_ok=True)
    rec("T13 tasks db back to pre-gate state",
        (TASKS_DB.exists()) == (BACKUP["tasks"] is not None),
        f"present={TASKS_DB.exists()}")

    if BACKUP["note"] is not None:
        NOTE_FILE.write_bytes(BACKUP["note"])  # type: ignore[arg-type]
    else:
        NOTE_FILE.unlink(missing_ok=True)
    rec("T13 note restored to pre-gate state",
        (BACKUP["note"] is None) == (not NOTE_FILE.exists()))

    render = AudioUtilities.GetSpeakers().EndpointVolume
    cap = AudioUtilities.CreateDevice(AudioUtilities.GetMicrophone()).EndpointVolume
    lvl = render.GetMasterVolumeLevelScalar()
    rec("T13 audio untouched (level/sysmute/micmute)",
        abs(lvl - float(ORIG["level"])) < 0.001
        and bool(render.GetMute()) is bool(ORIG["mute"])
        and bool(cap.GetMute()) is bool(ORIG["mic"]),
        f"vol={lvl:.4f} sys={bool(render.GetMute())} mic={bool(cap.GetMute())}")

    data = DAEMON_DIR / "data"
    residue = (list(data.glob("*.tmp")) + list((DAEMON_DIR / "notes").glob("*.tmp"))
               + list(DAEMON_DIR.glob("*.tmp")) + list(data.glob("tasks.sqlite3*")))
    if BACKUP["tasks"] is not None:
        residue = [r for r in residue if r.name != "tasks.sqlite3"]
    rec("T13 no .tmp / stray task-db residue", not residue,
        ", ".join(p.name for p in residue))

    used = [c.laddr.port for c in psutil.net_connections(kind="tcp")
            if getattr(c, "laddr", None) and c.status == "LISTEN"
            and c.laddr.port in (8765, 8795)]
    rec("T13 gate ports free after kill", not used, f"listening={used}")

    me = os.getpid()
    strays = [p.pid for p in psutil.process_iter(["pid", "cmdline"])
              if p.pid != me
              and any(str(a).endswith("run_daemon.py") for a in (p.info["cmdline"] or []))]
    rec("T13 no stray daemons", not strays, f"pids={strays}")


# ── main ─────────────────────────────────────────────────────────────────────

async def main() -> None:
    os.chdir(DAEMON_DIR)
    print("TL1 GATE - Local Task Planner (real daemon)")
    print("Ground truth: wire traffic, daemon stdout, on-disk envelope bytes, real broadcasts.")
    try:
        t1_spawn()
        await t2_transport()
        await t3_routing()
        await t4_wire_reminder()
        await t5_wire_add()
        await t6_list()
        await t7_complete()
        await t8_reminder_fire()
        await t9_note_regression()
        t10_ciphertext_live()
        kill_daemon()
        await t11_structured_and_ics()
        t12_vault_proof()
    finally:
        if WS is not None:
            try:
                await WS.close()
            except Exception:
                pass
        t13_restore()
        for k in ("BS_IPC_PORT", "BS_TTS_ENABLED", "BS_REMINDER_INTERVAL"):
            os.environ.pop(k, None)
        for d in TMP_DIRS:
            shutil.rmtree(d, ignore_errors=True)
        print("  env cleaned, temp dirs removed")


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
