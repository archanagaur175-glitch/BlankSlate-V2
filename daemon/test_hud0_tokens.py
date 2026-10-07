"""Phase 0 gate — HUD theme token infrastructure (visual overhaul).

T1  source: tokens.css dual-theme blocks (:root/:data-theme dark + light),
    all 15 surface/pill/radius tokens, dark block byte-identical Tailwind
    oklch spot values, light block differs where required; index.css has
    @import order (tailwindcss then tokens.css), @theme inline mapping for
    all 36 used color tokens, var(--bs-*) everywhere, zero leftover
    hardcoded sentinels; index.html synchronous bs-theme/data-theme
    bootstrap script positioned BEFORE the module entry script.
T2  npm run typecheck exit 0; npm run build (tsc && vite build) exit 0;
    dist bundle carries the live-repaint mechanism (.bg-emerald-500/20 ->
    color-mix with var(--bs-emerald-500) 20% inside @supports), tokens
    inlined (var(--bs-bg-app)), and dist/index.html keeps the bootstrap
    before the hashed module script.
T3  live repaint — headless Edge + CDP against the fresh dist: flip
    data-theme on <html> at runtime; bg-emerald-500/20 (opacity-modifier
    utility) AND body bg, hud-root gradient, overlay-pill, waveform
    gradient must repaint WITHOUT reload (window.__msr marker survives a
    separate CDP call); dark state restored byte-identically.
T4  fresh exe (npm run tauri build -- --no-bundle) cold-launch capture:
    native dark backgroundColor (#0a0a16 in tauri.conf, flagged+approved)
    + inline bootstrap => zero white frames, zero flash frames, first
    frame dark, settled dark, max frame mean < 120.
T5  dark parity — post-edit computed styles byte-identical to the T0
    baseline captured BEFORE any Phase 0 edit (11 fields, embedded below);
    screenshots archived (dark/light/dark_restored).

BASELINE CHANGELOG:
    * Original capture: pre-Phase-0 (T0), frozen to prove P0 changed no
      visual value.
    * Re-frozen at Phase 4 (approved): `spans` and `barGradient` updated
      to the approved Phase 2 CompactWidget restyle (frosted status chip
      wrapper + conditional label color; waveform from-emerald-400/80
      to-cyan-400/90). Drift was introduced by P2 and surfaced by the
      Phase 4 re-run because P4 is the first phase to touch index.css.
      The other 9 fields (incl. pillBg/pillBorder/pillColor, which P4
      must preserve) remain byte-identical to the original T0 capture.
T6  backups: hud_backup_p0/index.css.bak + index.html.bak exist, are
    non-empty, contain original sentinels (restorable), and the html bak
    predates the bootstrap script.
    restore: leftover blankslate-hud processes killed, edge profiles
    removed, GATE_RESULT + exit.

Ground truth: CDP Runtime.evaluate getComputedStyle, Win32/DWM window
frames via screenshot statistics, disk reads, exit codes — never assumed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DAEMON_DIR = Path(__file__).resolve().parent
HUD_DIR = DAEMON_DIR.parent / "hud"
PROBE = DAEMON_DIR / "hud_p0_probe.py"
FOUC = DAEMON_DIR / "hud_fouc.py"
EXE = HUD_DIR / "src-tauri" / "target" / "release" / "blankslate-hud.exe"
ARTIFACTS = Path(r"C:\Users\archa\AppData\Local\Temp\opencode\hud0_artifacts")
BACKUP_DIR = Path(r"C:\Users\archa\AppData\Local\Temp\opencode\hud_backup_p0")

T0_BASELINE = {
    "bodyBg": "rgba(10, 10, 22, 0.9)",
    "bodyColor": "rgb(224, 224, 224)",
    "rootBgImage": "linear-gradient(rgba(10, 10, 22, 0.55), rgba(10, 10, 22, 0.35))",
    "rootBorder": "rgba(120, 180, 255, 0.08)",
    "rootShadow": "rgba(34, 211, 238, 0.04) 0px 0px 60px 0px inset",
    "probeBg": "oklab(0.696 -0.162114 0.0511766 / 0.2)",
    "pillBg": "rgba(10, 10, 22, 0.88)",
    "pillBorder": "rgba(120, 180, 255, 0.28)",
    "pillColor": "rgb(207, 232, 255)",
    "spans": [
        {"bg": "oklab(0.645 0.235944 0.0696166 / 0.1)",
         "color": "rgb(224, 224, 224)"},
        {"bg": "oklch(0.645 0.246 16.439)", "color": "rgb(224, 224, 224)"},
        {"bg": "rgba(0, 0, 0, 0)", "color": "oklch(0.81 0.117 11.638)"},
    ],
    "barGradient": (
        "linear-gradient(to top, oklab(0.765 -0.169466 0.0510906 / 0.8) 0%, "
        "oklab(0.789 -0.131264 -0.0805336 / 0.9) 100%)"
    ),
}

SURFACE_TOKENS = [
    "--bs-bg-app",
    "--bs-text-primary",
    "--bs-glass-from",
    "--bs-glass-to",
    "--bs-border-subtle",
    "--bs-glow-inset",
    "--bs-scrollbar-thumb",
    "--bs-pill-bg",
    "--bs-pill-border",
    "--bs-pill-text",
    "--bs-shadow-pill",
    "--bs-radius-card",
    "--bs-radius-pill",
]

COLOR_MAP = {
    "white": "white",
    "black": "black",
    "amber-200": "amber-200",
    "amber-300": "amber-300",
    "amber-400": "amber-400",
    "amber-500": "amber-500",
    "cyan-200": "cyan-200",
    "cyan-300": "cyan-300",
    "cyan-400": "cyan-400",
    "cyan-500": "cyan-500",
    "cyan-950": "cyan-950",
    "emerald-200": "emerald-200",
    "emerald-300": "emerald-300",
    "emerald-400": "emerald-400",
    "emerald-500": "emerald-500",
    "emerald-950": "emerald-950",
    "fuchsia-300": "fuchsia-300",
    "fuchsia-400": "fuchsia-400",
    "fuchsia-500": "fuchsia-500",
    "rose-200": "rose-200",
    "rose-300": "rose-300",
    "rose-400": "rose-400",
    "rose-500": "rose-500",
    "teal-300": "teal-300",
    "teal-400": "teal-400",
    "teal-500": "teal-500",
    "violet-200": "violet-200",
    "violet-300": "violet-300",
    "violet-400": "violet-400",
    "violet-500": "violet-500",
    "zinc-100": "zinc-100",
    "zinc-300": "zinc-300",
    "zinc-400": "zinc-400",
    "zinc-500": "zinc-500",
    "zinc-600": "zinc-600",
    "zinc-700": "zinc-700",
}

SENTINELS_ABSENT = [
    "rgba(10, 10, 22",
    "#e0e0e0",
    "rgba(120, 180, 255",
    "#cfe8ff",
    "rgba(34, 211, 238",
]

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {note}" if note else ""))


def _run(cmd: str, cwd: Path, timeout: int) -> tuple[int, str]:
    proc = subprocess.run(
        cmd, cwd=str(cwd), shell=True,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )
    out = (proc.stdout + proc.stderr).strip()
    return proc.returncode, out


def _kill_leftover_hud() -> int:
    import psutil

    killed = 0
    for p in psutil.process_iter(["name"]):
        name = (p.info.get("name") or "").lower()
        if "blankslate-hud" in name:
            try:
                p.kill()
                killed += 1
            except Exception:
                pass
    return killed


def _hud_running() -> int:
    import psutil

    n = 0
    for p in psutil.process_iter(["name"]):
        name = (p.info.get("name") or "").lower()
        if "blankslate-hud" in name:
            n += 1
    return n


# ---- T1: source asserts -------------------------------------------------

def t1_source() -> None:
    print("\n--- T1: tokens.css / index.css / index.html source asserts ---")
    tokens = (HUD_DIR / "src" / "styles" / "tokens.css").read_text(encoding="utf-8")
    css = (HUD_DIR / "src" / "index.css").read_text(encoding="utf-8")
    html = (HUD_DIR / "index.html").read_text(encoding="utf-8")

    dark_idx = tokens.find(':root[data-theme="dark"]')
    light_idx = tokens.find(':root[data-theme="light"]')
    rec("T1 tokens.css dual-theme blocks present",
        tokens.startswith(":root,") and dark_idx > 0 and light_idx > dark_idx,
        f"dark@{dark_idx} light@{light_idx}")

    rec("T1 all surface/pill/radius tokens defined",
        all(t in tokens for t in SURFACE_TOKENS),
        f"missing={[t for t in SURFACE_TOKENS if t not in tokens]}")

    dark_part = tokens[:light_idx]
    light_part = tokens[light_idx:]
    rec("T1 dark block byte-identical Tailwind oklch spot values",
        "--bs-emerald-500: oklch(69.6% 0.17 162.48)" in dark_part
        and "--bs-rose-500: oklch(64.5% 0.246 16.439)" in dark_part
        and "--bs-zinc-400: oklch(70.5% 0.015 286.067)" in dark_part
        and "--bs-white: #fff" in dark_part
        and "--bs-black: #000" in dark_part
        and "color-scheme: dark" in dark_part,
        "emerald-500/rose-500/zinc-400/white/black/color-scheme")

    rec("T1 light block defines divergent palette + light scheme",
        "color-scheme: light" in light_part
        and "--bs-emerald-500: oklch(59.6% 0.145 163.225)" in light_part
        and "--bs-emerald-500: oklch(69.6% 0.17 162.48)" not in light_part
        and "--bs-white: #111827" in light_part
        and "--bs-bg-app: rgba(241, 245, 249, 0.95)" in light_part,
        "emerald-500 flips, ink/bg-app light values")

    tail_i = css.find('@import "tailwindcss"')
    tok_i = css.find('@import "./styles/tokens.css"')
    rec("T1 index.css import order: tailwindcss then tokens.css",
        0 <= tail_i < tok_i, f"tailwind@{tail_i} tokens@{tok_i}")

    theme_i = css.find("@theme inline {")
    mappings_ok = theme_i > 0 and all(
        f"--color-{k}: var(--bs-{v})" in css for k, v in COLOR_MAP.items()
    )
    missing = [
        k for k, v in COLOR_MAP.items()
        if f"--color-{k}: var(--bs-{v})" not in css
    ]
    rec(f"T1 @theme inline maps all {len(COLOR_MAP)} used color tokens",
        mappings_ok, f"missing={missing}")

    rec("T1 index.css uses var(--bs-*) for shell/pill/scrollbar",
        all(
            s in css
            for s in [
                "var(--bs-bg-app)", "var(--bs-text-primary)",
                "var(--bs-glass-from)", "var(--bs-glass-to)",
                "var(--bs-border-subtle)", "var(--bs-radius-card)",
                "var(--bs-glow-inset)", "var(--bs-scrollbar-thumb)",
                "var(--bs-pill-bg)", "var(--bs-pill-border)",
                "var(--bs-pill-text)", "var(--bs-shadow-pill)",
                "var(--bs-radius-pill)",
            ]
        ),
        "all indexed")

    rec("T1 zero leftover hardcoded sentinels in index.css",
        not any(s in css for s in SENTINELS_ABSENT),
        f"found={[s for s in SENTINELS_ABSENT if s in css]}")

    boot_i = html.find('localStorage.getItem("bs-theme")')
    entry_i = html.find('src="/src/main.tsx"')
    rec("T1 index.html synchronous bs-theme bootstrap before module entry",
        boot_i > 0 and entry_i > boot_i
        and 'setAttribute("data-theme"' in html
        and "r.style.backgroundColor" in html
        and "prefers-color-scheme" not in html,
        f"boot@{boot_i} entry@{entry_i}")


# ---- T2: typecheck + build + bundle mechanism --------------------------

def t2_build() -> None:
    print("\n--- T2: typecheck + vite build + dist mechanism ---")
    rc, out = _run("npm run typecheck", HUD_DIR, 300)
    rec("T2 npm run typecheck exit 0", rc == 0,
        f"rc={rc} tail={out.strip().splitlines()[-2:]}")

    rc, out = _run("npm run build", HUD_DIR, 600)
    rec("T2 npm run build (tsc && vite build) exit 0", rc == 0,
        f"rc={rc} tail={out.strip().splitlines()[-2:]}")

    dist_index = HUD_DIR / "dist" / "index.html"
    css_files = sorted((HUD_DIR / "dist" / "assets").glob("*.css"))
    js_files = sorted((HUD_DIR / "dist" / "assets").glob("*.js"))
    dist_css = css_files[0].read_text(encoding="utf-8") if css_files else ""
    html = dist_index.read_text(encoding="utf-8") if dist_index.exists() else ""

    rec("T2 dist css carries opacity-modifier var mechanism",
        ".bg-emerald-500\\/20" in dist_css
        and "var(--bs-emerald-500) 20%" in dist_css
        and "--bs-bg-app" in dist_css,
        f"css={[p.name for p in css_files]}")
    rec("T2 dist index.html keeps bootstrap before hashed module script",
        'localStorage.getItem("bs-theme")' in html
        and html.find('localStorage.getItem("bs-theme")')
        < html.find("<script type=\"module\""),
        f"js={[p.name for p in js_files]}")


# ---- T3: live repaint via CDP -------------------------------------------

def t3_repaint() -> None:
    print("\n--- T3: live repaint (CDP data-theme flip, no reload) ---")
    out_json = ARTIFACTS / "hud0_repaint.json"
    shots = ARTIFACTS / "hud0_shots_repaint"
    rc, out = _run(
        f'"{sys.executable}" "{PROBE}" repaint --out "{out_json}" --shots "{shots}"',
        DAEMON_DIR, 120,
    )
    rec("T3 probe repaint exit 0", rc == 0,
        f"rc={rc} tail={out.strip().splitlines()[-6:]}")

    data = {}
    if out_json.exists():
        data = json.loads(out_json.read_text(encoding="utf-8"))
    checks = data.get("checks", {})
    rec("T3 all live-repaint checks true", bool(checks) and all(checks.values()),
        f"checks={ {k: v for k, v in checks.items()} }")
    rec("T3 opacity-modifier probe repaints (light != dark)",
        checks.get(
            "probe bg-emerald-500/20 repaints live (light != dark1)", False
        ),
        data.get("light", {}).get("probeBg", "?"))
    rec("T3 no-reload marker survives across CDP calls",
        data.get("marker") == 42, f"marker={data.get('marker')}")
    rec("T3 archived repaint screenshots exist",
        (shots / "dark_restored.png").exists()
        and (shots / "light.png").exists(),
        f"dir={shots}")


# ---- T4: fresh exe FOUC frames ------------------------------------------

def t4_fouc() -> None:
    print("\n--- T4: fresh exe build + cold-launch FOUC frames ---")
    _kill_leftover_hud()
    rc, out = _run("npm run tauri build -- --no-bundle", HUD_DIR, 900)
    rec("T4 npm run tauri build -- --no-bundle exit 0", rc == 0,
        f"rc={rc} tail={out.strip().splitlines()[-2:]}")
    rec("T4 fresh exe present", EXE.exists(), f"path={EXE}")
    if not EXE.exists():
        rec("T4 FOUC capture skipped (no exe)", False, "build failed")
        return

    out_json = ARTIFACTS / "hud0_fouc.json"
    rc, out = _run(
        f'"{sys.executable}" "{FOUC}" --exe "{EXE}" --out "{out_json}"',
        DAEMON_DIR, 120,
    )
    data = {}
    if out_json.exists():
        data = json.loads(out_json.read_text(encoding="utf-8"))

    frames = [f for f in data.get("frames", []) if "mean" in f]
    means = [f["mean"] for f in frames]
    rec("T4 FOUC capture exit 0 + frames collected", rc == 0 and len(frames) >= 10,
        f"rc={rc} frames={len(frames)}")
    rec("T4 zero white frames (mean>235 & std<25)",
        len(data.get("white_frames", [])) == 0,
        f"white={len(data.get('white_frames', []))}")
    rec("T4 zero flash frames (|mean - settled| > 80)",
        len(data.get("flash_frames", [])) == 0
        and data.get("ok") is True,
        f"flash={len(data.get('flash_frames', []))} ok={data.get('ok')}")
    rec("T4 first frame dark + settled dark",
        bool(frames)
        and frames[0]["mean"] < 100
        and data.get("settled_mean", 999) < 60,
        f"first={frames[0]['mean'] if frames else '?'} "
        f"settled={data.get('settled_mean')}")
    rec("T4 every frame mean < 120 (no bright phase at all)",
        bool(means) and max(means) < 120, f"max={max(means) if means else '?'}")


# ---- T5: dark parity vs embedded T0 baseline ----------------------------

def t5_parity() -> None:
    print("\n--- T5: dark parity vs pre-edit T0 baseline (11 fields) ---")
    out_json = ARTIFACTS / "hud0_snapshot.json"
    shots = ARTIFACTS / "hud0_shots_snapshot"
    rc, out = _run(
        f'"{sys.executable}" "{PROBE}" snapshot --out "{out_json}" --shots "{shots}"',
        DAEMON_DIR, 120,
    )
    rec("T5 probe snapshot exit 0", rc == 0,
        f"rc={rc} tail={out.strip().splitlines()[-4:]}")

    dark = {}
    if out_json.exists():
        dark = json.loads(out_json.read_text(encoding="utf-8")).get("dark", {})
    diffs = {
        k: {"t0": T0_BASELINE[k], "now": dark.get(k)}
        for k in T0_BASELINE
        if dark.get(k) != T0_BASELINE[k]
    }
    rec("T5 all 11 dark fields byte-identical to T0 baseline",
        not diffs and len(dark) >= len(T0_BASELINE),
        f"diffs={diffs}")
    rec("T5 dark theme attribute active on snapshot",
        dark.get("theme") == "dark", f"theme={dark.get('theme')}")
    rec("T5 archived dark screenshot exists",
        (shots / "dark.png").exists(), f"dir={shots}")


# ---- T6: backups + restore ----------------------------------------------

def t6_backups() -> None:
    print("\n--- T6: BACKUP files restorable ---")
    css_bak = BACKUP_DIR / "index.css.bak"
    html_bak = BACKUP_DIR / "index.html.bak"
    css_ok = css_bak.exists() and css_bak.stat().st_size > 0
    html_ok = html_bak.exists() and html_bak.stat().st_size > 0
    rec("T6 backups exist and non-empty",
        css_ok and html_ok,
        f"css={css_bak.exists()} html={html_bak.exists()}")
    if css_ok and html_ok:
        css_txt = css_bak.read_text(encoding="utf-8")
        html_txt = html_bak.read_text(encoding="utf-8")
        rec("T6 css bak carries original sentinels (restorable)",
            "rgba(10, 10, 22, 0.9)" in css_txt and "#cfe8ff" in css_txt,
            f"size={css_bak.stat().st_size}")
        rec("T6 html bak predates bootstrap script (restorable)",
            'bs-theme' not in html_txt and '<div id="root">' in html_txt,
            f"size={html_bak.stat().st_size}")


def t7_restore() -> None:
    print("\n--- T7: process/profile hygiene ---")
    killed = _kill_leftover_hud()
    time.sleep(0.3)
    profiles = list(ARTIFACTS.glob("edge_profile_*"))
    for p in profiles:
        shutil.rmtree(p, ignore_errors=True)
    rec("T7 no leftover blankslate-hud process",
        _hud_running() == 0, f"killed={killed} remaining={_hud_running()}")
    rec("T7 edge profiles cleaned", not list(ARTIFACTS.glob("edge_profile_*")),
        f"removed={len(profiles)}")


def main() -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    try:
        t1_source()
        t2_build()
        t3_repaint()
        t4_fouc()
        t5_parity()
        t6_backups()
    finally:
        t7_restore()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\nGATE_RESULT: {passed}/{total} passed")
    if passed != total:
        failed = [n for n, ok, _ in RESULTS if not ok]
        print("FAILED: " + "; ".join(failed))
        print("RESTORE (only if sources must revert):")
        print(
            f'  Copy-Item "{BACKUP_DIR}\\index.css.bak" '
            f'"{HUD_DIR}\\src\\index.css" -Force'
        )
        print(
            f'  Copy-Item "{BACKUP_DIR}\\index.html.bak" '
            f'"{HUD_DIR}\\index.html" -Force'
        )
    return 0 if passed == total and total > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
