"""HW1 GATE - Phase 1: Hardware & Media Control Toolsuite.

Tests:
T1  endpoint identity: render flow=eRender, capture flow=eCapture proven by the
    OS data-flow API (IMMEndpoint.GetDataFlow via pycaw), capture target == the
    default capture endpoint, render/capture distinct, singleton reuse.
T2  volume: set 0.37 through the registry (wiring proof), COM readback 0.37,
    restore previous level, structured-error contract for out-of-range input.
T3  mute isolation: capture mute via handler; capture_flow==1; render mute and
    render volume unchanged by independent COM readbacks; toggle path; restore.
T4  media key -> WM_APPCOMMAND: foreground logger window receives APPCOMMAND
    for play_pause/next/previous with matching command ids (deterministic).
T5  real-app scripted media key: generated local WAV opened in the default
    player; SMTC PlaybackStatus transitions via our media_control
    (Playing -> Paused -> Playing) read through PowerShell WinRT.
T6  regressions: prior 18 registry tools intact, 3 new present, F1 extractors
    unchanged, routing selects set_volume / media_control for their queries.

Ground truth: OS data-flow API, COM volume/mute readbacks, foreground window
handle, logger file lines, SMTC status transitions, registry/router outputs.
Zero network. Scratch files removed at the end.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import struct
import subprocess
import sys
import time
import wave

import psutil
import win32gui

from pycaw.pycaw import AudioUtilities

from blankslate.config import DaemonConfig
from blankslate.tools import text_injector as ti
from blankslate.tools.hardware import (
    get_capture_endpoint,
    get_render_endpoint,
    media_control,
    mute_microphone,
)
from blankslate.tools.registry import ToolRegistry
from blankslate.tools.router import ToolRouter
from blankslate.tools.window_manager import switch_window

RESULTS: list[tuple[str, bool, str]] = []

# State kept for guaranteed restoration in cleanup.
_ORIG: dict[str, float | bool | None] = {"vol": None, "mic_mute": None}
_CHILD: subprocess.Popen | None = None
_SCRATCH: list[str] = []

# Logger child: captures WM_APPCOMMAND / raw media VK messages.
LOGGER_SRC = '''\
import sys, time
import win32api, win32gui

OUT, NONCE, LIFE = sys.argv[1], sys.argv[2], float(sys.argv[3])
TITLE = "BSHW1 LOGGER " + NONCE
CLS = "BSHW1LoggerClass" + NONCE
MEDIA_VKS = {176, 177, 179}  # VK_MEDIA_NEXT_TRACK, VK_MEDIA_PREV_TRACK, VK_MEDIA_PLAY_PAUSE

def log(s):
    with open(OUT, "a") as f:
        f.write(s + "\\n")

def wndproc(hwnd, msg, wp, lp):
    if msg == 0x0319:  # WM_APPCOMMAND
        hi = (lp >> 16) & 0xFFFF
        c8 = (hi & 0xFF00) >> 8
        c0 = hi & 0xFF
        log("APPCOMMAND hi=0x%04X c8=%d c0=%d lo=0x%04X" % (hi, c8, c0, lp & 0xFFFF))
        return 0
    if msg in (0x100, 0x101) and wp in MEDIA_VKS:  # WM_KEYDOWN / WM_KEYUP
        log("KEYVK wp=%d msg=0x%X" % (wp, msg))
    return win32gui.DefWindowProc(hwnd, msg, wp, lp)

wc = win32gui.WNDCLASS()
wc.hInstance = win32api.GetModuleHandle(None)
wc.lpszClassName = CLS
wc.lpfnWndProc = wndproc
win32gui.RegisterClass(wc)
hwnd = win32gui.CreateWindow(CLS, TITLE, 0x00CF0000, 120, 120, 460, 260, 0, 0, wc.hInstance, None)
win32gui.ShowWindow(hwnd, 1)
win32gui.UpdateWindow(hwnd)
try:
    win32gui.SetForegroundWindow(hwnd)
except Exception as e:
    log("SETFG_ERR %s" % e)
log("READY hwnd=%d" % hwnd)
end = time.time() + LIFE
while time.time() < end:
    win32gui.PumpWaitingMessages()
    time.sleep(0.02)
'''

# SMTC query via PowerShell WinRT (sync status per session: "aumid|Status").
SMTC_PS = r"""$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType=WindowsRuntime]
$asTaskGeneric = @([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await-Op($op, $type) {
    $task = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($op))
    [void]$task.Wait()
    return $task.Result
}
$mgrType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType=WindowsRuntime]
$manager = Await-Op ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) $mgrType
foreach ($s in $manager.GetSessions()) {
    $pi = $s.GetPlaybackInfo()
    $aumid = $s.SourceAppUserModelId
    if (-not $aumid) { $aumid = 'unknown' }
    Write-Output ($aumid + '|' + [string]$pi.PlaybackStatus)
}
"""

PLAYER_NEEDLES = ("mediaplayer", "media.player", "zunemusic", "zunevideo", "wmplayer", "musicap")


def rec(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {note}" if note else ""))


def enum_window(nonce: str) -> int:
    found: list[int] = []

    def cb(h, acc):
        try:
            if win32gui.IsWindowVisible(h) and nonce in win32gui.GetWindowText(h):
                acc.append(h)
        except Exception:
            pass

    win32gui.EnumWindows(cb, found)
    return found[0] if found else 0


def player_pids() -> set[int]:
    out: set[int] = set()
    for p in psutil.process_iter(["name"]):
        n = p.info["name"]
        if n and any(k in n.lower() for k in PLAYER_NEEDLES):
            out.add(p.pid)
    return out


def kill_players_since(ts: float) -> list[int]:
    killed = []
    for p in player_pids():
        try:
            proc = psutil.Process(p)
            if proc.create_time() >= ts:
                proc.kill()
                killed.append(p)
        except Exception:
            pass
    return killed


def smtc(ps1_path: str) -> list[tuple[str, str]]:
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Sta", "-File", ps1_path],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(f"SMTC ps1 rc={r.returncode}: {r.stderr.strip()[:400]}")
    out: list[tuple[str, str]] = []
    for line in r.stdout.splitlines():
        if "|" in line:
            a, s = line.split("|", 1)
            out.append((a.strip(), s.strip()))
    return out


# ── T1: endpoint identity ─────────────────────────────────────────────────────

def t1_endpoint_identity() -> None:
    print("\n--- T1: endpoint identity (capture vs render proven) ---")
    render = get_render_endpoint()
    capture = get_capture_endpoint()
    same = get_render_endpoint() is render and get_capture_endpoint() is capture
    rec("T1 endpoint singletons (one interface per flow)", same,
        f"render id reuse={get_render_endpoint() is render}")

    spk = AudioUtilities.GetSpeakers()
    mic = AudioUtilities.GetMicrophone()
    rf = AudioUtilities.GetEndpointDataFlow(render.id, 1)
    cf = AudioUtilities.GetEndpointDataFlow(capture.id, 1)
    rec("T1 OS data-flow: render=eRender(0), capture=eCapture(1)", rf == 0 and cf == 1,
        f"render flow={rf} capture flow={cf}")

    ids_ok = render.id == spk.id and capture.id == mic.GetId() and capture.id != render.id
    rec("T1 targets = OS defaults, render != capture", ids_ok,
        f"render='{render.FriendlyName}' capture='{capture.FriendlyName}'")


# ── T2: volume ────────────────────────────────────────────────────────────────

async def t2_volume(reg: ToolRegistry) -> None:
    print("\n--- T2: volume set / readback / restore (via registry wiring) ---")
    vol = get_render_endpoint().EndpointVolume
    orig = vol.GetMasterVolumeLevelScalar()
    _ORIG["vol"] = orig

    r1 = await reg.call("set_volume", level=0.37)
    rb = vol.GetMasterVolumeLevelScalar()
    ok1 = r1.get("tool") == "set_volume" and "error" not in r1 and abs(rb - 0.37) < 1e-3
    rec("T2 set_volume 0.37 (registry) -> COM readback", ok1,
        f"readback={rb:.4f} result={r1.get('result')}")

    r_err = await reg.call("set_volume", level=1.5)
    rec("T2 out-of-range -> structured error", "error" in r_err, str(r_err.get("error")))

    r2 = await reg.call("set_volume", percent=orig * 100)
    rb2 = vol.GetMasterVolumeLevelScalar()
    rec("T2 restore previous volume", abs(rb2 - orig) < 1e-6,
        f"orig={orig:.6f} now={rb2:.6f} resp={r2.get('result')}")


# ── T3: mute isolation ────────────────────────────────────────────────────────

async def t3_mute_isolation() -> None:
    print("\n--- T3: capture mute with render untouched (COM readbacks) ---")
    cap = get_capture_endpoint()
    ren = get_render_endpoint()
    cap_orig = bool(cap.EndpointVolume.GetMute())
    _ORIG["mic_mute"] = cap_orig
    rm0 = bool(ren.EndpointVolume.GetMute())
    rv0 = ren.EndpointVolume.GetMasterVolumeLevelScalar()

    r = await mute_microphone({"muted": True})
    cap_now = bool(cap.EndpointVolume.GetMute())
    rm1 = bool(ren.EndpointVolume.GetMute())
    rv1 = ren.EndpointVolume.GetMasterVolumeLevelScalar()
    ok = (
        r.get("capture_mute_readback") is True
        and cap_now is True
        and r.get("render_mute_unchanged") is True
        and rm1 == rm0
        and abs(rv1 - rv0) < 1e-9
        and r.get("capture_flow") == 1
        and r.get("capture_device") == cap.FriendlyName
    )
    rec("T3 capture muted, render mute+volume unchanged, flow=1", ok,
        f"cap={cap_now} render_mute {rm0}->{rm1} vol {rv0:.4f}->{rv1:.4f} "
        f"flow={r.get('capture_flow')} dev='{r.get('capture_device')}'")

    r2 = await mute_microphone({"action": "toggle"})
    t_now = bool(cap.EndpointVolume.GetMute())
    rec("T3 toggle flips capture state (handler + COM agree)",
        t_now is (not cap_now) and r2.get("capture_mute_readback") == t_now,
        f"after toggle COM={t_now} handler={r2.get('capture_mute_readback')}")

    await mute_microphone({"muted": cap_orig})
    rec("T3 original capture mute restored",
        bool(cap.EndpointVolume.GetMute()) == cap_orig,
        f"orig={cap_orig} now={bool(cap.EndpointVolume.GetMute())}")


# ── T4: media key -> WM_APPCOMMAND (deterministic) ────────────────────────────

async def t4_appcommand() -> None:
    global _CHILD
    print("\n--- T4: media key -> WM_APPCOMMAND on foreground logger ---")
    pid = os.getpid()
    lgpath = os.path.abspath(f"scratch_hw1_logger_{pid}.py")
    applog = os.path.abspath(f"scratch_hw1_appcmds_{pid}.log")
    _SCRATCH.extend([lgpath, applog])
    with open(lgpath, "w") as f:
        f.write(LOGGER_SRC)
    if os.path.exists(applog):
        os.remove(applog)

    nonce = f"n{pid}"
    _CHILD = subprocess.Popen([sys.executable, lgpath, applog, nonce, "35"])
    hwnd = 0
    t0 = time.time()
    while time.time() - t0 < 10:
        hwnd = enum_window(nonce)
        if hwnd:
            break
        time.sleep(0.2)
    rec("T4 logger window created", hwnd != 0, f"hwnd={hwnd}")
    if not hwnd:
        return

    sw = await switch_window({"title": nonce})
    time.sleep(0.6)
    fgtitle = win32gui.GetWindowText(win32gui.GetForegroundWindow())
    ok_fg = nonce in fgtitle
    rec("T4 logger is foreground (product switch_window)", ok_fg,
        f"fg='{fgtitle}' switch='{str(sw.get('result'))[:60]}'")
    if not ok_fg:
        return

    expected = [("play_pause", 14), ("next", 11), ("previous", 12)]
    sends = []
    for action, _cmd in expected:
        r = await media_control({"action": action})
        sends.append((action, r.get("appcommand"), r.get("error")))
        time.sleep(0.5)
    ok_contract = all(err is None and cmd == exp
                      for (_a, cmd, err), (_a2, exp) in zip(sends, expected))
    rec("T4 media_control contract (appcommand ids)", ok_contract, f"sends={sends}")

    lines: list[str] = []
    t0 = time.time()
    while time.time() - t0 < 6:
        if os.path.exists(applog):
            with open(applog) as f:
                lines = [l.strip() for l in f if l.startswith("APPCOMMAND")]
        if len(lines) >= 3:
            break
        time.sleep(0.3)
    rec("T4 WM_APPCOMMAND received x3", len(lines) >= 3, f"lines={lines}")

    matches = []
    for i, (_action, cmd) in enumerate(expected):
        if i < len(lines):
            m = re.search(r"c8=(\d+) c0=(\d+)", lines[i])
            if m:
                matches.append(cmd in (int(m.group(1)), int(m.group(2))))
    rec("T4 APPCOMMAND ids match actions in order",
        len(lines) >= 3 and len(matches) == 3 and all(matches),
        f"expected=[{', '.join(str(c) for _a, c in expected)}] matches={matches}")

    if os.path.exists(applog):
        with open(applog) as f:
            kv = [l.strip() for l in f if l.startswith("KEYVK")]
        if kv:
            print(f"  evidence (raw media VK messages also seen): {kv[:6]}")

    if _CHILD is not None and _CHILD.poll() is None:
        _CHILD.terminate()
        try:
            _CHILD.wait(timeout=5)
        except Exception:
            pass
        print("  logger child done (terminated after T4)")


# ── T5: real-app scripted media key (SMTC) ────────────────────────────────────

async def t5_real_app_smtc() -> None:
    print("\n--- T5: real-app media (generated WAV + SMTC PlaybackStatus) ---")
    pid = os.getpid()
    stem = f"bs_hw1_tone_{pid}"
    wav = os.path.abspath(f"{stem}.wav")
    ps1 = os.path.abspath(f"scratch_hw1_smtc_{pid}.ps1")
    _SCRATCH.extend([wav, ps1])

    fr, dur, freq = 22050, 45.0, 440.0
    n = int(fr * dur)
    with wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(fr)
        frames = b"".join(
            struct.pack("<h", int(0.5 * 32767 * math.sin(2 * math.pi * freq * i / fr)))
            for i in range(n)
        )
        w.writeframes(frames)
    with open(ps1, "w") as f:
        f.write(SMTC_PS)

    try:
        baseline = smtc(ps1)
    except Exception as exc:
        rec("T5 SMTC query baseline", False, f"{type(exc).__name__}: {exc}")
        return
    print(f"  baseline sessions={baseline}")
    baseline_playing = {a for a, s in baseline if s == "Playing"}
    start_ts = time.time() - 1.0
    _ORIG["player_since"] = start_ts

    try:
        os.startfile(wav)
    except Exception as exc:
        rec("T5 start default player on WAV", False, f"{type(exc).__name__}: {exc}")
        return

    candidate: tuple[str, str] | None = None
    sessions: list[tuple[str, str]] = []
    t0 = time.time()
    while time.time() - t0 < 20:
        try:
            sessions = smtc(ps1)
        except Exception as exc:
            rec("T5 SMTC poll during playback", False, f"{type(exc).__name__}: {exc}")
            return
        preferred = [(a, s) for a, s in sessions
                     if s == "Playing" and a not in baseline_playing
                     and any(k in a.lower() for k in PLAYER_NEEDLES)]
        others = [(a, s) for a, s in sessions
                  if s == "Playing" and a not in baseline_playing]
        if preferred or others:
            candidate = (preferred or others)[0]
            break
        time.sleep(1.5)
    rec("T5 new session reached Playing", candidate is not None,
        f"candidate={candidate} last={sessions}")
    if not candidate:
        return

    aumid = candidate[0]

    # The media key is a WM_APPCOMMAND to the FOCUSED window — the player
    # itself must be foreground or the key lands on an unrelated window.
    phwnd = 0
    t0 = time.time()
    while time.time() - t0 < 10 and not phwnd:
        phwnd = enum_window(stem) or enum_window("Media Player")
        if not phwnd:
            found: list[int] = []

            def cb(h, acc):
                try:
                    if win32gui.IsWindowVisible(h):
                        _, wpid = win32gui.GetWindowThreadProcessId(h)
                        if wpid in player_pids():
                            acc.append(h)
                except Exception:
                    pass

            win32gui.EnumWindows(cb, found)
            phwnd = found[0] if found else 0
        if not phwnd:
            time.sleep(0.5)

    fg = win32gui.GetForegroundWindow()
    if phwnd and fg != phwnd:
        ptitle = win32gui.GetWindowText(phwnd)
        sw = await switch_window({"title": stem if stem in ptitle else ptitle})
        time.sleep(0.8)
    fg = win32gui.GetForegroundWindow()
    fgtitle = win32gui.GetWindowText(fg)
    ok_fg = fg != 0 and (
        fg == phwnd
        or "media player" in fgtitle.lower()
        or stem.lower() in fgtitle.lower()
    )
    rec("T5 player window is foreground (key targets the player)", ok_fg,
        f"hwnd={phwnd} fg='{fgtitle[:70]}'")

    def cur_status() -> str:
        try:
            for a, s in smtc(ps1):
                if a == aumid:
                    return s
        except Exception as exc:
            return f"ERR:{exc}"
        return "gone"

    if not ok_fg:
        rec("T5 media_control: Playing -> Paused (real player)", False,
            "skipped: player not foreground")
        rec("T5 media_control: Paused -> Playing (real player)", False,
            "skipped: player not foreground")
        return

    r1 = await media_control({"action": "play_pause"})
    st, t0 = "?", time.time()
    while time.time() - t0 < 6:
        st = cur_status()
        if st == "Paused":
            break
        time.sleep(0.7)
    rec("T5 media_control: Playing -> Paused (real player)",
        st == "Paused", f"status={st} send={r1.get('result')}")

    r2 = await media_control({"action": "play_pause"})
    st2, t0 = "?", time.time()
    while time.time() - t0 < 6:
        st2 = cur_status()
        if st2 == "Playing":
            break
        time.sleep(0.7)
    rec("T5 media_control: Paused -> Playing (real player)",
        st2 == "Playing", f"status={st2} send={r2.get('result')}")

    # Cleanup for T5: kill only player processes we introduced.
    killed = kill_players_since(start_ts)
    if killed:
        print(f"  cleanup: killed new player pids {killed}")


# ── T6: regressions ───────────────────────────────────────────────────────────

async def t6_regressions(reg: ToolRegistry) -> None:
    print("\n--- T6: regressions (registry / extractors / routing) ---")
    prior = [
        "get_current_time", "web_search", "calculator", "note_taking", "weather",
        "app_launch", "app_kill", "list_windows", "switch_window",
        "get_foreground_window", "type_text", "press_key", "hotkey",
        "focus_snapshot", "focus_restore", "screen_xray", "clipboard_read",
        "run_macro",
    ]
    names = set(reg.names())
    missing = [n for n in prior if n not in names]
    rec("T6 prior 18 tools intact", not missing, f"missing={missing}")

    new3 = [n for n in ("set_volume", "mute_microphone", "media_control") if n not in names]
    rec("T6 3 new tools registered", not new3 and len(names) == 34,
        f"missing={new3} count={len(names)}")

    e1 = ti._extract_text_to_type('type "hi there"') == "hi there"
    e2 = ti._extract_text_to_type("type hello") == "hello"
    e3 = ti._extract_key("press enter") == "enter"
    e4 = ti._extract_combo("press ctrl shift t") == "ctrl+shift+t"
    rec("T6 F1 extractors unchanged", all((e1, e2, e3, e4)),
        f"quoted={e1} verb={e2} key={e3} combo={e4}")

    cfg = DaemonConfig()
    routed = await ToolRouter(cfg, reg).route("set the volume to 50 percent")
    sel = [t.name for t in routed.selected_tools]
    rec("T6 routing 'set volume' -> set_volume", "set_volume" in sel,
        f"selected={sel} by_keyword={routed.by_keyword}")

    routed2 = await ToolRouter(cfg, reg).route("pause the music")
    sel2 = [t.name for t in routed2.selected_tools]
    rec("T6 routing 'pause the music' -> media_control", "media_control" in sel2,
        f"selected={sel2} by_keyword={routed2.by_keyword}")


# ── main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    print("HW1 GATE TESTS - Hardware & Media Control (Phase 1)")
    print("Ground truth: OS data-flow API, COM readbacks, foreground hwnd,")
    print("logger file lines, SMTC status via PowerShell WinRT, registry/router.")
    try:
        t1_endpoint_identity()
        reg = ToolRegistry()
        await t2_volume(reg)
        await t3_mute_isolation()
        await t4_appcommand()
        await t5_real_app_smtc()
        await t6_regressions(reg)
    finally:
        await cleanup()


async def cleanup() -> None:
    print("\n--- cleanup ---")
    try:
        if _ORIG["vol"] is not None:
            vol = get_render_endpoint().EndpointVolume
            vol.SetMasterVolumeLevelScalar(float(_ORIG["vol"]), None)
            print(f"  volume restored to {vol.GetMasterVolumeLevelScalar():.6f}")
        if _ORIG["mic_mute"] is not None:
            await mute_microphone({"muted": bool(_ORIG["mic_mute"])})
            print(f"  mic mute restored to {bool(get_capture_endpoint().EndpointVolume.GetMute())}")
    except Exception as exc:
        print(f"  restore error (non-fatal): {exc}")

    global _CHILD
    if _CHILD is not None and _CHILD.poll() is None:
        _CHILD.terminate()
        try:
            _CHILD.wait(timeout=5)
        except Exception:
            pass
        print("  logger child terminated")

    if _ORIG.get("player_since") is not None:
        killed = kill_players_since(float(_ORIG["player_since"]))
        if killed:
            print(f"  player processes killed: {killed}")

    for path in _SCRATCH:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
    left = [p for p in _SCRATCH if os.path.exists(p)]
    print(f"  scratch removed: {len(_SCRATCH) - len(left)}/{len(_SCRATCH)}"
          + (f" LEFTOVER={left}" if left else ""))


if __name__ == "__main__":
    asyncio.run(main())
    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    total = len(RESULTS)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print("\n" + "=" * 60)
    for n, d in failed:
        print(f"  FAILED: {n} -- {d}")
    print(f"GATE_RESULT: {passed}/{total} passed")
    sys.exit(0 if passed == total and total > 0 else 1)
