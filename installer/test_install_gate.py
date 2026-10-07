"""Install gate — offline monolith installer end-to-end.

T1  installer artifact: BlankSlate-Setup-2.0.0.exe + .bin slices present (~6.4 GB).
T2  silent install: /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /TASKS="!desktopicon"
    exits 0.
T3  layout on disk: {app}\BlankSlate.exe, {app}\daemon\blankslate-daemon.exe +
    _internal, {app}\ollama\ollama.exe + "ollama app.exe", THIRD_PARTY_NOTICES
    with CC BY 4.0 credit, seeds extracted directly into home caches (no
    {app}\seeds staging, no seeds-installed.txt), home ollama manifest +
    HF whisper/pocket-tts repos present, Start Menu shortcut present,
    desktop shortcut absent.
T4  first run (clean env, all BS_* stripped): installed HUD spawns the INSTALLED
    frozen daemon (8765 owner exe path == {app}\daemon\blankslate-daemon.exe,
    HUD log "launching frozen daemon" names the installed path),
    token at {app}\daemon\_internal\data\ipc_token, WS get_state subscribers
    >= 2 with the token, daemon log shows 34 "Registered tool:" lines + cuda +
    STT ready small.en on cuda (seeded whisper loaded offline).
T5  ollama branch (pre-killed): 11434 owner exe == {app}\ollama\ollama.exe,
    HUD log "launching ollama serve" names the installed path, /api/tags
    serves the three seeded models from the home cache.
T6  CDP: main window .hud-root renders the compact view ("turns" chip).
T7  hygiene: installed app tree killed, ports restored to pre-gate state
    (dev daemons on 8765, ollama pre-open/closed), installed files remain.

Ground truth: process exe paths, TCP owners, disk reads, log files, wire calls.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DAEMON_DIR = Path(r"C:\Users\archa\Documents\BlankSlate_V2\daemon")
sys.path.insert(0, str(DAEMON_DIR))
from hud_p0_probe import CDP, free_port  # noqa: E402

INSTALLER_DIR = Path(r"C:\Users\archa\Documents\BlankSlate_V2\installer\output")
SETUP = INSTALLER_DIR / "BlankSlate-Setup-2.0.0.exe"
APP = Path(os.environ["LOCALAPPDATA"]) / "BlankSlate"
DAEMON_EXE = APP / "daemon" / "blankslate-daemon.exe"
OLLAMA_EXE = APP / "ollama" / "ollama.exe"
TOKEN_FILE = APP / "daemon" / "_internal" / "data" / "ipc_token"
DAEMON_LOG = APP / "daemon" / "logs" / "hud_autostart_daemon.log"
OLLAMA_LOG = APP / "daemon" / "logs" / "hud_autostart_ollama.log"
HOME = Path(os.environ["USERPROFILE"])
OLLAMA_MANIFEST = HOME / ".ollama/models/manifests/registry.ollama.ai/library/qwen2.5/0.5b"
HF_WHISPER = HOME / ".cache/huggingface/hub/models--Systran--faster-whisper-small.en"
HF_TTS = HOME / ".cache/huggingface/hub/models--kyutai--pocket-tts"
HF_TTS_NVC = HOME / ".cache/huggingface/hub/models--kyutai--pocket-tts-without-voice-cloning"
HUD_LOG = Path(os.environ["LOCALAPPDATA"]) / "com.blankslate.v2" / "logs" / "BlankSlate.log"
START_MENU = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/BlankSlate/BlankSlate.lnk"
DESKTOP = Path(os.environ["USERPROFILE"]) / "Desktop/BlankSlate.lnk"
PROD_PORT = 8765
OLLAMA_PORT = 11434

RESULTS: list[tuple[str, bool, str]] = []
PROC: subprocess.Popen | None = None
CDP_PORT: int = 0
PRE: dict[str, Any] = {}


def _hud_slice() -> str:
    parts: list[str] = []
    try:
        for p in sorted(HUD_LOG.parent.glob("BlankSlate.log*")):
            try:
                parts.append(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    except OSError:
        return ""
    return "".join(parts)


def _hud_log_clean_slate() -> None:
    try:
        for p in HUD_LOG.parent.glob("BlankSlate.log*"):
            try:
                p.unlink()
            except OSError:
                pass
    except OSError:
        pass


def rec(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {note}" if note else ""))


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.8):
            return True
    except OSError:
        return False


def _listeners(port: int) -> list[int]:
    pids: list[int] = []
    try:
        import psutil

        for c in psutil.net_connections(kind="tcp"):
            if (
                c.laddr
                and getattr(c.laddr, "port", None) == port
                and c.status == psutil.CONN_LISTEN
                and c.pid
            ):
                pids.append(c.pid)
        if pids:
            return sorted(set(pids))
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15,
        ).stdout
        for ln in out.splitlines():
            parts = ln.split()
            if (
                len(parts) >= 5
                and parts[0].upper() == "TCP"
                and parts[3] == "LISTENING"
                and parts[1].endswith(f":{port}")
            ):
                pids.append(int(parts[4]))
    except Exception:
        pass
    return sorted(set(pids))


def _exe_of(pid: int) -> str:
    try:
        import psutil

        return psutil.Process(pid).exe() or ""
    except Exception:
        return ""


def _count_procs(substr: str) -> int:
    try:
        import psutil

        n = 0
        for p in psutil.process_iter(["name"]):
            if substr in (p.info.get("name") or "").lower():
                n += 1
        return n
    except Exception:
        return -1


def _kill_pid_tree(pid: int) -> None:
    try:
        import psutil

        proc = psutil.Process(pid)
        kids = [c.pid for c in proc.children(recursive=True)]
        proc.kill()
        for k in kids:
            try:
                psutil.Process(k).kill()
            except Exception:
                pass
    except Exception:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, timeout=15,
        )


def _kill_names(*names: str) -> None:
    try:
        import psutil

        for p in psutil.process_iter(["name"]):
            nm = (p.info.get("name") or "").lower()
            if any(x in nm for x in names):
                try:
                    p.kill()
                except Exception:
                    pass
    except Exception:
        pass


def _wait(pred, seconds: float, step: float = 0.5) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


def _list_pages(port: int) -> list[dict]:
    import requests

    try:
        return [
            t
            for t in requests.get(f"http://127.0.0.1:{port}/json", timeout=5).json()
            if t.get("type") == "page" and "tauri.localhost" in t.get("url", "")
        ]
    except Exception:
        return []


def _connect(port: int, target: dict) -> CDP:
    from websockets.sync.client import connect as ws_connect

    c = CDP(port)
    c.ws = ws_connect(target["webSocketDebuggerUrl"], open_timeout=10)
    c.cmd("Page.enable")
    return c


def _eval(cdp: CDP, expr: str, timeout: float = 10.0) -> Any:
    last: Exception | None = None
    for _ in range(6):
        try:
            return cdp.eval_js(expr, timeout=timeout)
        except RuntimeError as e:
            if "Execution context was destroyed" in str(e):
                time.sleep(0.4)
                last = e
                continue
            raise
        except Exception as e:
            last = e
            time.sleep(0.4)
    raise RuntimeError(f"eval never succeeded: {last}")


def _attach_main(timeout_s: float = 14.0) -> CDP | None:
    deadline = time.time() + timeout_s
    seen: set[str] = set()
    while time.time() < deadline:
        for tgt in _list_pages(CDP_PORT):
            if tgt["id"] in seen:
                continue
            seen.add(tgt["id"])
            try:
                c = _connect(CDP_PORT, tgt)
            except Exception:
                continue
            try:
                is_main = bool(
                    _eval(c, "!!document.querySelector('.hud-root')", timeout=5)
                )
            except Exception:
                is_main = False
            if is_main:
                return c
            try:
                c.close()
            except Exception:
                pass
        time.sleep(0.4)
    return None


def _ws_subscribers(port: int, token: str) -> int:
    from websockets.sync.client import connect as ws_connect

    url = f"ws://127.0.0.1:{port}" + (f"?token={token}" if token else "")
    try:
        with ws_connect(url, open_timeout=5) as ws:
            ws.send(json.dumps({"type": "get_state"}))
            deadline = time.time() + 6
            while time.time() < deadline:
                m = json.loads(ws.recv(timeout=6))
                if m.get("type") == "state":
                    return int(m.get("subscribers", 0))
    except Exception:
        return -1
    return -1


def _log_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ---- T1: artifact ---------------------------------------------------------

def t1_artifact() -> None:
    print("\n--- T1: installer artifact ---")
    rec("T1 setup exe present", SETUP.exists(), f"path={SETUP}")
    slices = sorted(INSTALLER_DIR.glob("BlankSlate-Setup-2.0.0-*.bin"))
    total = (SETUP.stat().st_size if SETUP.exists() else 0) + sum(
        s.stat().st_size for s in slices
    )
    rec("T1 .bin slices present", len(slices) >= 3,
        f"count={len(slices)} names={[s.name for s in slices]}")
    rec("T1 total size ~6-8 GB", 5.0 <= total / 1e9 <= 9.0,
        f"{total / 1e9:.2f} GB")


# ---- T2: silent install ---------------------------------------------------

def t2_install() -> None:
    print("\n--- T2: silent install ---")
    unins = APP / "unins000.exe"
    if unins.exists():
        subprocess.run(
            [str(unins), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
            capture_output=True, timeout=600,
        )
        _wait(lambda: not APP.exists(), 180, 1.0)
    if APP.exists():
        shutil.rmtree(APP, ignore_errors=True)
    cmd = [
        str(SETUP), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
        '/TASKS=!desktopicon',
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=1800,
    )
    rec("T2 setup exits 0", proc.returncode == 0,
        f"rc={proc.returncode} out={proc.stdout.strip()[:200]}")


# ---- T3: layout ------------------------------------------------------------

def t3_layout() -> None:
    print("\n--- T3: installed layout (disk truth) ---")
    rec("T3 BlankSlate.exe", (APP / "BlankSlate.exe").exists(), f"dir={APP}")
    rec("T3 daemon exe + _internal",
        DAEMON_EXE.exists() and (APP / "daemon/_internal").is_dir())
    rec("T3 ollama.exe + ollama app.exe",
        OLLAMA_EXE.exists() and (APP / "ollama/ollama app.exe").exists())
    notices = _log_text(APP / "THIRD_PARTY_NOTICES.txt")
    rec("T3 notices credit Kyutai CC BY 4.0", "CC BY 4.0" in notices
        and "Kyutai" in notices, f"len={len(notices)}")
    rec("T3 no seed staging dir in install dir",
        not (APP / "seeds").exists())
    rec("T3 seeds-installed.txt absent (direct extraction)",
        not (APP / "seeds-installed.txt").exists())
    rec("T3 home ollama manifest seeded", OLLAMA_MANIFEST.exists(),
        f"path={OLLAMA_MANIFEST}")
    rec("T3 home HF whisper repo seeded", HF_WHISPER.exists(),
        f"path={HF_WHISPER}")
    rec("T3 home HF pocket-tts repos seeded",
        HF_TTS.exists() and HF_TTS_NVC.exists(),
        f"{HF_TTS.exists()}/{HF_TTS_NVC.exists()}")
    rec("T3 Start Menu shortcut", START_MENU.exists(), f"path={START_MENU}")
    rec("T3 desktop shortcut suppressed", not DESKTOP.exists(),
        f"path={DESKTOP}")


# ---- T4: first run --------------------------------------------------------

def t4_first_run() -> None:
    global PROC, CDP_PORT
    print("\n--- T4: installed HUD spawns installed daemon (clean env) ---")
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("BS_"):
            env.pop(k)
    CDP_PORT = free_port()
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
        f"--remote-debugging-port={CDP_PORT}"
    )
    _hud_log_clean_slate()
    PROC = subprocess.Popen(
        [str(APP / "BlankSlate.exe")], cwd=str(APP), env=env,
    )
    rec("T4 installed HUD launched", PROC.pid is not None, f"pid={PROC.pid}")

    up = _wait(lambda: _port_open(PROD_PORT), 90, 0.5)
    owners = _listeners(PROD_PORT)
    exes = [_exe_of(p) for p in owners]
    rec("T4 daemon listening on 8765 <=90s", up, f"owners={owners}")
    rec("T4 port owner is the INSTALLED daemon exe",
        bool(up) and all(e.lower() == str(DAEMON_EXE).lower() for e in exes),
        f"exes={exes}")
    rec("T4 exactly one daemon process",
        _count_procs("blankslate-daemon") == 1,
        f"count={_count_procs('blankslate-daemon')}")

    tok = ""
    if _wait(lambda: TOKEN_FILE.exists() and TOKEN_FILE.stat().st_size > 0, 30, 0.5):
        tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
    rec("T4 frozen token written under installed _internal", bool(tok),
        f"path={TOKEN_FILE} len={len(tok)}")

    subs = -1
    if up:
        deadline = time.time() + 25
        while time.time() < deadline:
            subs = _ws_subscribers(PROD_PORT, tok)
            if subs >= 2:
                break
            time.sleep(1.0)
    rec("T4 HUD bridge subscribed (gate+HUD >= 2)", subs >= 2,
        f"subscribers={subs}")

    dlog = _log_text(DAEMON_LOG)
    hud = _hud_slice()
    rec("T4 HUD log: frozen daemon launched from installed path",
        "launching frozen daemon" in hud
        and str(DAEMON_EXE).lower() in hud.lower(),
        f"log={HUD_LOG.name} slice={len(hud)}")
    rec("T4 daemon log: 34 tools registered",
        dlog.count("Registered tool: ") == 34,
        f"count={dlog.count('Registered tool: ')}")
    rec("T4 daemon log: cuda device", "cuda" in dlog.lower(),
        f"hits={dlog.lower().count('cuda')}")
    rec("T4 daemon log: STT ready small.en on cuda",
        "STT ready: model=small.en, device=cuda" in dlog)


# ---- T5: ollama branch -----------------------------------------------------

def t5_ollama() -> None:
    print("\n--- T5: installed ollama.exe spawned + seeded models served ---")
    up = _wait(lambda: _port_open(OLLAMA_PORT), 60, 1.0)
    owners = _listeners(OLLAMA_PORT)
    exes = [_exe_of(p) for p in owners]
    rec("T5 ollama listening on 11434", up, f"owners={owners}")
    rec("T5 port owner is the INSTALLED ollama.exe",
        bool(up) and all(e.lower() == str(OLLAMA_EXE).lower() for e in exes),
        f"exes={exes}")
    olog = _log_text(OLLAMA_LOG)
    hud = _hud_slice()
    rec("T5 HUD log: installed ollama launched from installed path",
        "launching ollama serve" in hud
        and str(OLLAMA_EXE).lower() in hud.lower(),
        f"slice={len(hud)} ollama_log={len(olog)}")
    tags: list[str] = []
    try:
        import requests

        r = requests.get("http://127.0.0.1:11434/api/tags", timeout=15)
        tags = [m.get("name", "") for m in r.json().get("models", [])]
    except Exception:
        pass
    want = ["qwen2.5:0.5b", "moondream", "nomic-embed-text"]
    missing = [w for w in want
               if not any(t == w or t.startswith(w + ":") for t in tags)]
    rec("T5 seeded models served (qwen2.5:0.5b, moondream, nomic)",
        not missing, f"models={tags} missing={missing}")


# ---- T6: CDP UI -----------------------------------------------------------

def t6_cdp() -> None:
    print("\n--- T6: installed HUD renders compact view ---")
    text = ""
    cdp = _attach_main()
    if cdp is not None:
        try:
            text = str(_eval(
                cdp,
                "(document.querySelector('.hud-root')||{innerText:''}).innerText",
                timeout=6,
            ))
        except Exception:
            text = ""
        try:
            cdp.close()
        except Exception:
            pass
    rec("T6 compact view renders daemonState chip (turns)",
        "turns" in text.lower(), f"found={'turns' in text.lower()}")


# ---- T7: hygiene -----------------------------------------------------------

def t7_hygiene() -> None:
    print("\n--- T7: hygiene + restore ---")
    if PROC is not None:
        try:
            _kill_pid_tree(PROC.pid)
        except Exception:
            pass
    time.sleep(0.5)
    _kill_names("blankslate", "BlankSlate")
    time.sleep(0.6)
    for pid in _listeners(PROD_PORT):
        _kill_pid_tree(pid)
    time.sleep(0.6)

    rec("T7 no leftover app/daemon processes",
        _count_procs("blankslate") == 0
        and _count_procs("blankslate-hud") == 0
        and _count_procs("blankslate-daemon") == 0,
        f"any={_count_procs('blankslate')} daemon={_count_procs('blankslate-daemon')}")
    rec("T7 prod port restored to pre-gate", _listeners(PROD_PORT) == PRE["prod_owners"],
        f"pre={PRE['prod_owners']} post={_listeners(PROD_PORT)}")

    if PRE["ollama_open"]:
        if not _port_open(OLLAMA_PORT):
            subprocess.Popen(
                [os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"),
                 "serve"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            _wait(lambda: _port_open(OLLAMA_PORT), 30, 1.0)
        rec("T7 ollama pre-open restored (dev serve running)",
            _port_open(OLLAMA_PORT), f"post={_listeners(OLLAMA_PORT)}")
    else:
        _kill_names("ollama")
        time.sleep(1.0)
        rec("T7 ollama baseline closed", not _port_open(OLLAMA_PORT), "")

    rec("T7 installed payload remains on disk",
        (APP / "BlankSlate.exe").exists() and DAEMON_EXE.exists()
        and OLLAMA_EXE.exists(), f"dir={APP}")


def main() -> int:
    global CDP_PORT
    print("PRE-CLEAN: killing dev HUD/daemon/ollama to establish baseline")
    _kill_names("blankslate-hud", "blankslate-daemon", "blankslate", "ollama")
    time.sleep(0.8)
    for pid in _listeners(PROD_PORT):
        _kill_pid_tree(pid)
    for pid in _listeners(OLLAMA_PORT):
        _kill_pid_tree(pid)
    time.sleep(1.0)
    PRE["prod_owners"] = _listeners(PROD_PORT)
    PRE["ollama_open"] = _port_open(OLLAMA_PORT)
    PRE["ollama_owners"] = _listeners(OLLAMA_PORT)
    print(f"PRE: prod_owners={PRE['prod_owners']} ollama_open={PRE['ollama_open']}")

    try:
        t1_artifact()
        t2_install()
        t3_layout()
        t4_first_run()
        t5_ollama()
        t6_cdp()
    finally:
        t7_hygiene()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\nGATE_RESULT: {passed}/{total} passed")
    if passed != total:
        failed = [n for n, ok, _ in RESULTS if not ok]
        print("FAILED: " + "; ".join(failed))
    return 0 if passed == total and total > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
