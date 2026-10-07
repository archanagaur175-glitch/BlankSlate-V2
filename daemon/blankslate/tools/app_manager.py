"""Desktop automation — app launcher and process killer.

Security:
- Launcher resolves apps via Windows shell (PATH, Start Menu, registered handlers).
- Killer checks a hard-coded protected-process blocklist before terminating.
- All blocking Win32/psutil calls run via loop.run_in_executor().
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil

logger = logging.getLogger("blankslate.tools.app_manager")

# ── Allowlist ───────────────────────────────────────────────────────────────

_APPS_JSON = Path(__file__).parent / "apps.json"


def _load_allowlist() -> dict[str, dict[str, str]]:
    if not _APPS_JSON.exists():
        return {}
    with open(_APPS_JSON, encoding="utf-8") as f:
        return json.load(f)


# ── Protected-process blocklist ─────────────────────────────────────────────

_BLOCKED_NAMES: frozenset[str] = frozenset({
    "explorer.exe",
    "csrss.exe",
    "winlogon.exe",
    "services.exe",
    "svchost.exe",
    "smss.exe",
    "lsass.exe",
    "wininit.exe",
    "fontdrvhost.exe",
    "sihost.exe",
    "dwm.exe",
    "blankslate-daemon.exe",
    "python.exe",
    "python3.exe",
})


def _is_protected(name: str) -> bool:
    return name.lower() in _BLOCKED_NAMES


# ── App name extraction from natural language ────────────────────────────────

def _load_aliases() -> dict[str, dict[str, Any]]:
    """Load alias dictionary from apps.json (alias → launch name mapping)."""
    if not _APPS_JSON.exists():
        return {}
    with open(_APPS_JSON, encoding="utf-8") as f:
        return json.load(f)


def _extract_app_name(query: str) -> str:
    """Extract an app name from a natural language query.

    Checks:
      1. Alias match: known alias (e.g. "browser") → launch name (e.g. "chrome")
      2. Key match: apps.json key (e.g. "notepad") appears in query
      3. Fallback: first word after open/launch/start/run that looks like an app name

    Returns the app name to launch, or the cleaned query if nothing matches.
    """
    import re

    q = query.lower().strip()
    aliases = _load_aliases()

    # 1. Direct key match (longest first to avoid partial matches)
    for key in sorted(aliases.keys(), key=len, reverse=True):
        pattern = r"\b" + re.escape(key) + r"\b"
        if re.search(pattern, q):
            return key

    # 2. Alias match (longest first)
    best: tuple[int, str] = (0, "")
    for key, entry in aliases.items():
        for alias in entry.get("aliases", []):
            pattern = r"\b" + re.escape(alias.lower()) + r"\b"
            if re.search(pattern, q):
                candidate = (len(alias), key)
                if candidate[0] > best[0]:
                    best = candidate
    if best[1]:
        return best[1]

    # 3. Fallback: extract the noun after open/launch/start/run
    match = re.search(
        r"\b(?:open|launch|start|run)\s+(\w+)", q
    )
    if match:
        return match.group(1)

    # 4. Last resort: return the whole query cleaned up
    return q


# ── Blocking helpers (run in executor) ──────────────────────────────────────

def _launch_blocking(app_name: str) -> dict[str, Any]:
    """Launch an app via Windows shell execution.

    Resolution order:
      1. os.startfile() — works for Win32 apps with registered handlers
      2. UWP AUMID via shell:AppsFolder — works for Store/UWP apps
      3. cmd /c start — fallback for everything else
    """
    if not app_name:
        return {
            "tool": "app_launch",
            "error": "no app name provided",
            "result": None,
        }

    # Check if this is a known UWP app with an AUMID
    aliases = _load_aliases()
    entry = aliases.get(app_name.lower())
    uwp_aumid = entry.get("uwp") if entry else None

    # Try os.startfile first (works for most Win32 apps)
    try:
        os.startfile(app_name)
        logger.info("Launched %s via os.startfile", app_name)
        return {
            "tool": "app_launch",
            "app_name": app_name,
            "result": f"Launched {app_name}.",
            "reply": f"Opened {entry.get('name', app_name) if entry else app_name}.",
            "deterministic": True,
        }
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("os.startfile(%s) failed: %s", app_name, exc)

    # Try UWP AUMID if available
    if uwp_aumid:
        try:
            import subprocess
            subprocess.Popen(
                ["cmd", "/c", "start", "", f"shell:AppsFolder\\{uwp_aumid}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info("Launched %s via UWP AUMID: %s", app_name, uwp_aumid)
            return {
                "tool": "app_launch",
                "app_name": app_name,
                "result": f"Launched {entry.get('name', app_name)}.",
                "reply": f"Opened {entry.get('name', app_name)}.",
                "deterministic": True,
            }
        except Exception as exc:
            logger.warning("UWP launch failed for %s: %s", app_name, exc)

    # Fallback: cmd /c start
    try:
        import subprocess
        subprocess.Popen(
            ["cmd", "/c", "start", "", app_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("Launched %s via cmd /c start", app_name)
        return {
            "tool": "app_launch",
            "app_name": app_name,
            "result": f"Launched {app_name}.",
            "reply": f"Opened {app_name}.",
            "deterministic": True,
        }
    except Exception as exc:
        logger.exception("Failed to launch %s", app_name)
        return {
            "tool": "app_launch",
            "error": f"Could not find '{app_name}'. Is it installed?",
            "result": None,
        }


def _kill_blocking(target: str) -> dict[str, Any]:
    lower_target = target.lower()

    # Check protected-process blocklist first
    if _is_protected(lower_target) or _is_protected(target):
        return {
            "tool": "app_kill",
            "error": f"Refused: {target!r} is a protected system process.",
            "result": None,
            "blocked": True,
        }

    # Find matching processes by name
    killed_pids: list[int] = []
    not_found = True
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if proc.info["name"] and proc.info["name"].lower() == lower_target:
                not_found = False
                pid = proc.info["pid"]
                try:
                    p = psutil.Process(pid)
                    p.terminate()
                    try:
                        p.wait(timeout=3)
                    except psutil.TimeoutExpired:
                        p.kill()
                        p.wait(timeout=3)
                    killed_pids.append(pid)
                    logger.info("Killed %s (pid=%d)", target, pid)
                except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
                    logger.warning("Could not kill %s pid=%d: %s", target, pid, exc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not_found:
        return {
            "tool": "app_kill",
            "error": f"No running process found matching {target!r}.",
            "result": None,
        }

    return {
        "tool": "app_kill",
        "target": target,
        "killed_pids": killed_pids,
        "count": len(killed_pids),
        "result": f"Terminated {len(killed_pids)} process(es) for {target}.",
        "reply": f"Closed {target} ({len(killed_pids)} process(es) terminated).",
        "deterministic": True,
    }


# ── Async tool handlers ─────────────────────────────────────────────────────

async def app_launch(args: dict[str, Any]) -> dict[str, Any]:
    """Launch an application by name or natural language query.

    Resolves via Windows shell — works for any installed app.
    """
    raw = str(args.get("app_id", args.get("app", args.get("query", "")))).strip()
    if not raw:
        return {
            "tool": "app_launch",
            "error": "no app name provided",
            "result": None,
        }
    app_name = _extract_app_name(raw)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _launch_blocking, app_name)


async def app_kill(args: dict[str, Any]) -> dict[str, Any]:
    """Kill a process by name, with protected-process safety check."""
    target = str(args.get("target", args.get("process", args.get("query", "")))).strip()
    if not target:
        return {
            "tool": "app_kill",
            "error": "no process target provided",
            "result": None,
        }
    # Add .exe extension if missing
    if not target.lower().endswith(".exe"):
        target += ".exe"
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _kill_blocking, target)
