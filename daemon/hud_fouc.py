"""FOUC capture for the BlankSlate HUD exe.

Launches the exe, locates the 250x100 'BlankSlate' window, samples frames for
~2s at ~50ms cadence, computes mean/std luminance of the window interior, and
reports any near-white low-variance frame (the flash-of-white signature).

Usage: python hud_fouc.py --exe <path> --out <json> [--sample-ms 2000]
Exit 0 always; verdict lives in the JSON (ok / white_frames / frames).
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pyautogui
from PIL import Image, ImageStat


def set_dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def find_window(title: str, timeout: float):
    import win32gui

    deadline = time.time() + timeout
    hwnds: list[int] = []
    while time.time() < deadline:
        hwnds.clear()

        def cb(h, _):
            try:
                if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h) == title:
                    hwnds.append(h)
            except Exception:
                pass

        win32gui.EnumWindows(cb, None)
        if hwnds:
            return hwnds[0]
        time.sleep(0.05)
    return None


def visible_rect(hwnd):
    import ctypes

    class RECT(ctypes.Structure):
        _fields_ = [
            ("l", ctypes.c_long),
            ("t", ctypes.c_long),
            ("r", ctypes.c_long),
            ("b", ctypes.c_long),
        ]

    rc = RECT()
    hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
        hwnd, 9, ctypes.byref(rc), ctypes.sizeof(rc)
    )
    if hr != 0:
        import win32gui

        return win32gui.GetWindowRect(hwnd)
    return (rc.l, rc.t, rc.r, rc.b)


def kill_window(hwnd) -> None:
    import win32con
    import win32gui

    try:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception:
        pass


def frame_stats(img: Image.Image) -> tuple[float, float]:
    w, h = img.size
    inset = 3
    if w > inset * 4 and h > inset * 4:
        img = img.crop((inset, inset, w - inset, h - inset))
    gray = img.convert("L")
    st = ImageStat.Stat(gray)
    return float(st.mean[0]), float(st.stddev[0])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--sample-ms", type=int, default=2000)
    ap.add_argument("--title", default="BlankSlate")
    args = ap.parse_args()

    set_dpi_aware()
    exe = Path(args.exe)
    if not exe.exists():
        print(f"exe missing: {exe}")
        return 2

    env = dict(os.environ)
    env["BS_HUD_AUTOSTART"] = "0"
    proc = subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env)
    hwnd = find_window(args.title, timeout=20)
    result: dict = {"exe": str(exe), "hwnd": hwnd}
    if hwnd is None:
        print("window never appeared")
        result["ok"] = False
        result["error"] = "window not found"
        Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
        proc.kill()
        return 1

    import win32gui

    frames = []
    deadline = time.time() + args.sample_ms / 1000.0
    idx = 0
    t_first = time.time()
    l = t = r = b = 0
    while time.time() < deadline:
        try:
            l, t, r, b = visible_rect(hwnd)
            region = (l, t, max(1, r - l), max(1, b - t))
            img = pyautogui.screenshot(region=region)
            mean, std = frame_stats(img)
            frames.append({
                "i": idx,
                "t_ms": round((time.time() - t_first) * 1000, 1),
                "mean": round(mean, 1),
                "std": round(std, 1),
            })
            idx += 1
        except Exception as e:
            frames.append({"i": idx, "err": str(e)})
            idx += 1

    with_mean = [f for f in frames if "mean" in f]
    settled = 0.0
    if with_mean:
        tail = [f["mean"] for f in with_mean[-5:]]
        settled = sum(tail) / len(tail)
    flash = [
        f for f in with_mean
        if f is not with_mean[-1] and abs(f["mean"] - settled) > 80
    ]
    white = [f for f in with_mean
             if f["mean"] > 235 and f["std"] < 25]
    result.update(
        frames=frames,
        frame_count=len(frames),
        settled_mean=round(settled, 1),
        flash_frames=flash,
        white_frames=white,
        ok=len(flash) == 0,
        rect=(l, t, r, b),
    )
    print(f"frames={len(frames)} settled={settled:.1f} flash={len(flash)} "
          f"white={len(white)}")
    for f in frames[:6]:
        print(f"  {f}")
    if len(frames) > 6:
        print(f"  ... ({len(frames) - 6} more)")

    kill_window(hwnd)
    time.sleep(0.4)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
