"""TL2 GATE - Proactive background event agents (real daemon).

One real run_daemon.py instance: FileWatcher rules are created over the
existing wire; matched file changes arrive as monitor_alert / category
=filewatch on the SAME broadcast channel the SystemMonitor already uses.
Ground truth: real WS traffic, real daemon stdout, on-disk
data/watch_rules.json bytes.

Voice note: send_text_message feeds handle_text_input — the exact pipeline
voice transcriptions reach after STT, so wire text turns exercise the same
path a voice command would.

Tests:
T1  spawn + recovery-first ordering (main.py flagged edit) + watcher active
    line on stdout + gate port free + test logs created pre-spawn.
T2  WS transport ping/pong + get_state.
T3  routing/registry: 30 tools; the 4 watch phrases route canonically
    (exact, single-tool); Phase 1 regression phrases still exact;
    watch_enabled=True / watch_debounce_s=0.5 defaults in both
    DaemonConfig() and load_config().
T4  wire NL add: 'watch file logs/tl2_watch.log' -> tools=[add_watch_rule],
    deterministic reply, rules file on disk gains the enabled rule
    (pre-existing disabled example is preserved), watcher reschedules.
T5  append -> monitor_alert/category=filewatch on the existing channel
    <=3s, correct path + rule id + event kind.
T6  NEGATIVE: append to an unmatched file -> no filewatch alert (1.5s).
T7  wire add rule with content pattern -> append password=hunter2secret99
    -> alert snippet REDACTED (Amendment 3): contains [REDACTED:...],
    the raw secret appears nowhere in the whole alert payload.
T8  30s soak: process alive, no Traceback in stdout, CPU sane (reported),
    watcher STILL fires after the idle window.
T9  wire list (2 rules) -> wire remove rule A -> disk shows exactly 1
    enabled -> append A -> NO alert; append B -> still alerts;
    final list no longer mentions A.
T10 FULL RESTORE: conversation byte-identical, watch_rules.json back to
    pre-gate bytes, test logs back to pre-gate state, tasks db back,
    audio untouched, no .tmp residue, ports free, no stray daemons.

Known wart (documented, pre-existing pattern): the phrase 'stop watching
...' also matches app_kill's bare 'stop' keyword — every selected tool
executes, but app_kill finds no process and replies harmlessly. The gate
uses 'remove watch rule ...' (exact single-tool routing).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import websockets
from pycaw.pycaw import AudioUtilities

from blankslate.config import DaemonConfig, load_config
from blankslate.tools.registry import ToolRegistry
from blankslate.tools.router import ToolRouter

DAEMON_DIR = Path(__file__).resolve().parent
PORT = 8796
RULES_FILE = DAEMON_DIR / "data" / "watch_rules.json"
LOGS = DAEMON_DIR / "logs"
WATCH_LOG = LOGS / "tl2_watch.log"
OTHER_LOG = LOGS / "tl2_other.log"
SECRET_LOG = LOGS / "tl2_secret.log"
TEST_LOGS = (WATCH_LOG, OTHER_LOG, SECRET_LOG)
SECRET_VALUE = "hunter2secret99"

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


def fw_alert(path_suffix: str):
    return lambda m: (
        m.get("type") == "monitor_alert"
        and m.get("category") == "filewatch"
        and str(m.get("path", "")).endswith(path_suffix)
    )


def append_fsync(path: Path, text: str) -> float:
    """Append + flush + fsync; returns the write-completion timestamp."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    return time.time()


def read_rules() -> list[dict]:
    if not RULES_FILE.exists():
        return []
    doc = json.loads(RULES_FILE.read_text(encoding="utf-8"))
    return doc.get("rules", doc) if isinstance(doc, dict) else doc


def enabled_rules() -> list[dict]:
    return [r for r in read_rules() if r.get("enabled", True)]


def find_rule(path: str) -> dict | None:
    return next((r for r in enabled_rules() if r.get("path") == path), None)


# ── T1: spawn ───────────────────────────────────────────────────────────────

def t1_spawn() -> None:
    print("\n--- T1: spawn + ordering + watcher active on stdout ---")
    global DAEMON

    conv = DAEMON_DIR / "data" / "conversation_history.json"
    BACKUP["conv"] = conv.read_bytes() if conv.exists() else None
    tasks_db = DAEMON_DIR / "data" / "tasks.sqlite3"
    BACKUP["tasks"] = tasks_db.read_bytes() if tasks_db.exists() else None
    BACKUP["rules"] = RULES_FILE.read_bytes() if RULES_FILE.exists() else None
    for p in TEST_LOGS:
        BACKUP[p.name] = p.read_bytes() if p.exists() else None
        p.parent.mkdir(exist_ok=True)
        p.write_text("", encoding="utf-8")  # empty pre-spawn: no missed events

    render = AudioUtilities.GetSpeakers().EndpointVolume
    ORIG["level"] = render.GetMasterVolumeLevelScalar()
    ORIG["mute"] = bool(render.GetMute())
    ORIG["mic"] = bool(
        AudioUtilities.CreateDevice(AudioUtilities.GetMicrophone()).EndpointVolume.GetMute()
    )
    print(f"  originals: vol={ORIG['level']:.4f} sysmute={ORIG['mute']} micmute={ORIG['mic']}")
    print(
        f"  pre-gate: rules={'present' if BACKUP['rules'] is not None else 'absent'}, "
        f"tasks_db={'present' if BACKUP['tasks'] is not None else 'absent'}, "
        f"conv={'present' if BACKUP['conv'] is not None else 'absent'}"
    )

    busy = [c.laddr.port for c in psutil.net_connections(kind="tcp")
            if getattr(c, "laddr", None) and c.status == "LISTEN" and c.laddr.port == PORT]
    rec("T1 gate port free pre-spawn", not busy, f"listening={busy}")
    rec("T1 test logs created empty pre-spawn", all(p.exists() and p.stat().st_size == 0
                                                    for p in TEST_LOGS))

    env = dict(os.environ)
    env.update({
        "BS_IPC_PORT": str(PORT),
        "BS_TTS_ENABLED": "False",
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

    i_rec, i_start, i_ipc = (idx("vault recovery:"), idx("BlankSlate daemon started"),
                             idx("IPC server listening"))
    rec("T1 daemon reached IPC listening", i_ipc >= 0 and DAEMON.poll() is None,
        f"exit={DAEMON.poll()} lines={len(LINES)}")
    rec("T1 recovery BEFORE daemon started (main.py flagged-edit regression)",
        0 <= i_rec < i_start, f"recovery@{i_rec} started@{i_start}")
    active = [l for l in LINES if "watcher: active" in l]
    rec("T1 watcher active on stdout (startup, before IPC-up)",
        bool(active), next((l.split("watcher:")[-1][:70] for l in active), "MISSING"))


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
    rec("T3 config defaults: watch_enabled=True, debounce=0.5",
        cfg.watch_enabled is True and cfg.watch_debounce_s == 0.5,
        f"enabled={cfg.watch_enabled} debounce={cfg.watch_debounce_s}")
    lc = load_config()
    rec("T3 load_config defaults (no watch env in gate process)",
        lc.watch_enabled is True and lc.watch_debounce_s == 0.5,
        f"enabled={lc.watch_enabled} debounce={lc.watch_debounce_s}")

    router = ToolRouter(cfg, reg)
    cases = [
        ("watch file logs/tl2_watch.log", {"add_watch_rule"}),
        ("watch file logs/tl2_secret.log pattern password=\\S+", {"add_watch_rule"}),
        ("list watch rules", {"list_watch_rules"}),
        ("remove watch rule logs/tl2_watch.log", {"remove_watch_rule"}),
        # Phase 1 regression: keyword strip must still hold
        ("remind me to call Ned tomorrow at 5pm", {"set_reminder"}),
        ("remember that tlgate marker is active", {"note_taking"}),
    ]
    bad = []
    for q, want in cases:
        got = set((await router.route(q)).by_keyword)
        if got != want:
            bad.append(f"{q!r} -> {sorted(got)} (want {sorted(want)})")
    rec("T3 canonical routing exact, single-tool (4 watch + 2 Phase-1)", not bad,
        "; ".join(bad) if bad else "all exact")


# ── T4: wire add rule ───────────────────────────────────────────────────────

async def t4_add_rule() -> None:
    print("\n--- T4: wire NL add watch rule (add_watch_rule) ---")
    reply = await send_and_reply(WS, "watch file logs/tl2_watch.log")
    tools = (reply or {}).get("tools") or []
    rec("T4 text_reply intent=tool tools=[add_watch_rule]",
        reply is not None and reply.get("intent") == "tool" and tools == ["add_watch_rule"],
        f"intent={(reply or {}).get('intent')} tools={tools}")
    txt = str((reply or {}).get("text", ""))
    rec("T4 deterministic reply names the watched path",
        "Watching" in txt and "tl2_watch.log" in txt, repr(txt[:110]))

    rule = find_rule("logs/tl2_watch.log")
    rec("T4 rules file on disk: enabled rule added, example preserved",
        rule is not None and rule.get("glob") == "tl2_watch.log" and bool(rule.get("id"))
        and any(r.get("id") == "example" for r in read_rules()),
        f"rule={rule}")
    rescheduled = [l for l in LINES if "watcher: active" in l and "1 rule(s)" in l]
    rec("T4 watcher rescheduled on stdout after add (1 rule)",
        bool(rescheduled), f"lines={len(rescheduled)}")


# ── T5: append fires on existing channel ────────────────────────────────────

async def t5_alert() -> None:
    print("\n--- T5: append -> monitor_alert/filewatch on existing channel ---")
    t_write = append_fsync(WATCH_LOG, "TL5 marker line\n")
    alert = await recv_until(WS, fw_alert("tl2_watch.log"), deadline=3.0)
    lat = time.time() - t_write
    rule = find_rule("logs/tl2_watch.log") or {}
    rec("T5 filewatch alert on wire <=3s", alert is not None and lat <= 3.0,
        f"lat={lat:.2f}s" if alert else f"NO ALERT in 3.0s (write@{t_write:.3f})")
    rec("T5 alert carries rule id + event kind + correct path",
        alert is not None and alert.get("rule") == rule.get("id")
        and alert.get("event") in ("created", "modified", "moved")
        and str(alert.get("path", "")).endswith("tl2_watch.log"),
        f"rule={alert.get('rule') if alert else None} event={alert.get('event') if alert else None}")


# ── T6: negative — unrelated file ───────────────────────────────────────────

async def t6_negative() -> None:
    print("\n--- T6: NEGATIVE — unmatched file produces no alert ---")
    await asyncio.sleep(1.0)  # let any straggler duplicates land first
    append_fsync(OTHER_LOG, "TL6 unrelated line\n")
    stray = await recv_until(WS, fw_alert("tl2_other.log"), deadline=1.5)
    rec("T6 unrelated append -> no filewatch alert (1.5s window)",
        stray is None, f"stray={stray}")


# ── T7: content pattern + Amendment 3 redaction ─────────────────────────────

async def t7_pattern_redaction() -> None:
    print("\n--- T7: content pattern + snippet redaction (Amendment 3) ---")
    reply = await send_and_reply(
        WS, "watch file logs/tl2_secret.log pattern password=\\S+"
    )
    tools = (reply or {}).get("tools") or []
    rule_b = find_rule("logs/tl2_secret.log")
    rec("T7 wire add rule with pattern: tools=[add_watch_rule], stored on disk",
        reply is not None and reply.get("intent") == "tool"
        and tools == ["add_watch_rule"] and rule_b is not None
        and rule_b.get("regex") == "password=\\S+",
        f"tools={tools} regex={(rule_b or {}).get('regex')!r}")
    rec("T7 exactly 2 enabled rules on disk", len(enabled_rules()) == 2,
        f"enabled={len(enabled_rules())}")

    t_write = append_fsync(SECRET_LOG, f"login attempt password={SECRET_VALUE} denied\n")
    alert = await recv_until(WS, fw_alert("tl2_secret.log"), deadline=3.0)
    lat = time.time() - t_write
    rec("T7 pattern-matched alert arrives <=3s", alert is not None and lat <= 3.0,
        f"lat={lat:.2f}s" if alert else "NO ALERT in 3.0s")

    snippet = str((alert or {}).get("snippet", ""))
    blob = json.dumps(alert or {})
    rec("T7 snippet REDACTED, raw secret absent from whole payload",
        alert is not None and "[REDACTED" in snippet
        and SECRET_VALUE not in snippet and SECRET_VALUE not in blob,
        f"snippet={snippet!r}")


# ── T8: 30s soak + liveness + post-soak alert ───────────────────────────────

async def t8_soak() -> None:
    print("\n--- T8: 30s soak — stability + watcher still alive after idle ---")
    marker = len(LINES)
    proc = psutil.Process(DAEMON.pid)  # type: ignore[union-attr]
    t0 = time.time()
    await asyncio.sleep(30.0)
    alive = DAEMON.poll() is None
    rec("T8 daemon alive after 30s soak", alive, f"exit={DAEMON.poll()}")
    crashes = [l for l in LINES[marker:] if "Traceback" in l or "CRITICAL" in l]
    rec("T8 no Traceback in stdout during soak", not crashes,
        crashes[0][:120] if crashes else "clean")
    try:
        cpu = proc.cpu_percent(interval=1.0)
        rss = proc.memory_info().rss // (1024 * 1024)
        rec("T8 CPU sane during soak (<200%, spin-loop guard)", cpu < 200,
            f"cpu={cpu:.0f}% rss={rss}MB soak={time.time() - t0:.0f}s")
    except psutil.NoSuchProcess:
        rec("T8 CPU sane during soak (<200%, spin-loop guard)", False, "process gone")

    t_write = append_fsync(WATCH_LOG, "TL8 post-soak line\n")
    alert = await recv_until(WS, fw_alert("tl2_watch.log"), deadline=3.0)
    lat = time.time() - t_write
    rec("T8 watcher STILL fires after 30s idle", alert is not None and lat <= 3.0,
        f"lat={lat:.2f}s" if alert else "NO ALERT after soak")


# ── T9: list + remove + negative-after-remove ───────────────────────────────

async def t9_list_remove() -> None:
    print("\n--- T9: wire list + remove + post-remove behavior ---")
    reply = await send_and_reply(WS, "list watch rules")
    tools = (reply or {}).get("tools") or []
    txt = str((reply or {}).get("text", ""))
    rec("T9 list: tools=[list_watch_rules], both rules named",
        tools == ["list_watch_rules"] and "logs/tl2_watch.log" in txt
        and "logs/tl2_secret.log" in txt, repr(txt[:160]))

    reply = await send_and_reply(WS, "remove watch rule logs/tl2_watch.log")
    tools = (reply or {}).get("tools") or []
    txt = str((reply or {}).get("text", ""))
    rec("T9 remove: tools=[remove_watch_rule], reply confirms",
        tools == ["remove_watch_rule"] and "Stopped watching" in txt, repr(txt[:120]))
    enabled = enabled_rules()
    rec("T9 rules file on disk: exactly 1 enabled (the pattern rule)",
        len(enabled) == 1 and enabled[0].get("path") == "logs/tl2_secret.log",
        f"enabled={[r.get('path') for r in enabled]}")

    await asyncio.sleep(1.0)
    append_fsync(WATCH_LOG, "TL9 should not fire\n")
    stray = await recv_until(WS, fw_alert("tl2_watch.log"), deadline=1.5)
    rec("T9 removed rule: append -> NO alert (1.5s)", stray is None,
        f"stray={stray}")

    t_write = append_fsync(SECRET_LOG, "TL9 remaining rule line\n")
    alert = await recv_until(WS, fw_alert("tl2_secret.log"), deadline=3.0)
    rec("T9 remaining rule still fires after remove",
        alert is not None and time.time() - t_write <= 3.0,
        f"lat={time.time() - t_write:.2f}s" if alert else "NO ALERT")

    reply = await send_and_reply(WS, "list watch rules")
    txt = str((reply or {}).get("text", ""))
    rec("T9 final list excludes removed rule, keeps pattern rule",
        "tl2_watch.log" not in txt and "logs/tl2_secret.log" in txt, repr(txt[:160]))


# ── kill + T10: restore ─────────────────────────────────────────────────────

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


def t10_restore() -> None:
    print("\n--- T10: FULL RESTORE ---")
    if DAEMON is not None and DAEMON.poll() is None:
        kill_daemon()

    conv = DAEMON_DIR / "data" / "conversation_history.json"
    if BACKUP["conv"] is not None:
        conv.write_bytes(BACKUP["conv"])  # type: ignore[arg-type]
        rec("T10 conversation restored byte-identical",
            conv.read_bytes() == BACKUP["conv"], f"{conv.stat().st_size}B")
    else:
        conv.unlink(missing_ok=True)
        rec("T10 conversation removed (absent pre-gate)", not conv.exists())

    if BACKUP["rules"] is not None:
        RULES_FILE.write_bytes(BACKUP["rules"])  # type: ignore[arg-type]
        rec("T10 watch_rules.json restored byte-identical",
            RULES_FILE.read_bytes() == BACKUP["rules"], f"{RULES_FILE.stat().st_size}B")
    else:
        RULES_FILE.unlink(missing_ok=True)
        rec("T10 watch_rules.json removed (absent pre-gate)", not RULES_FILE.exists())

    tasks_db = DAEMON_DIR / "data" / "tasks.sqlite3"
    for side in ("", "-journal", "-wal", "-shm"):
        p = Path(str(tasks_db) + side)
        if BACKUP["tasks"] is None:
            p.unlink(missing_ok=True)
        elif side == "":
            p.write_bytes(BACKUP["tasks"])  # type: ignore[arg-type]
        else:
            p.unlink(missing_ok=True)
    rec("T10 tasks db back to pre-gate state",
        tasks_db.exists() == (BACKUP["tasks"] is not None),
        f"present={tasks_db.exists()}")

    for p in TEST_LOGS:
        if BACKUP[p.name] is None:
            p.unlink(missing_ok=True)
        else:
            p.write_bytes(BACKUP[p.name])  # type: ignore[arg-type]
    rec("T10 test logs back to pre-gate state",
        all(p.exists() == (BACKUP[p.name] is not None) for p in TEST_LOGS),
        ", ".join(f"{p.name}={'present' if p.exists() else 'absent'}" for p in TEST_LOGS))

    render = AudioUtilities.GetSpeakers().EndpointVolume
    cap = AudioUtilities.CreateDevice(AudioUtilities.GetMicrophone()).EndpointVolume
    lvl = render.GetMasterVolumeLevelScalar()
    rec("T10 audio untouched (level/sysmute/micmute)",
        abs(lvl - float(ORIG["level"])) < 0.001
        and bool(render.GetMute()) is bool(ORIG["mute"])
        and bool(cap.GetMute()) is bool(ORIG["mic"]),
        f"vol={lvl:.4f} sys={bool(render.GetMute())} mic={bool(cap.GetMute())}")

    data = DAEMON_DIR / "data"
    residue = (list(data.glob("*.tmp")) + list((DAEMON_DIR / "notes").glob("*.tmp"))
               + list(DAEMON_DIR.glob("*.tmp"))
               + [p for p in data.glob("tasks.sqlite3*")])
    if BACKUP["tasks"] is not None:
        residue = [r for r in residue if r.name != "tasks.sqlite3"]
    rec("T10 no .tmp / stray task-db residue", not residue,
        ", ".join(p.name for p in residue))

    used = [c.laddr.port for c in psutil.net_connections(kind="tcp")
            if getattr(c, "laddr", None) and c.status == "LISTEN"
            and c.laddr.port in (8765, PORT)]
    rec("T10 gate ports free after kill", not used, f"listening={used}")

    me = os.getpid()
    strays = [p.pid for p in psutil.process_iter(["pid", "cmdline"])
              if p.pid != me
              and any(str(a).endswith("run_daemon.py") for a in (p.info["cmdline"] or []))]
    rec("T10 no stray daemons", not strays, f"pids={strays}")


# ── main ─────────────────────────────────────────────────────────────────────

async def main() -> None:
    os.chdir(DAEMON_DIR)
    print("TL2 GATE - Proactive background event agents (real daemon)")
    print("Ground truth: wire traffic, daemon stdout, on-disk watch_rules.json bytes.")
    try:
        t1_spawn()
        await t2_transport()
        await t3_routing()
        await t4_add_rule()
        await t5_alert()
        await t6_negative()
        await t7_pattern_redaction()
        await t8_soak()
        await t9_list_remove()
    finally:
        if WS is not None:
            try:
                await WS.close()
            except Exception:
                pass
        t10_restore()
        for k in ("BS_IPC_PORT", "BS_TTS_ENABLED",
                  "BS_WATCH_ENABLED", "BS_WATCH_DEBOUNCE"):
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
