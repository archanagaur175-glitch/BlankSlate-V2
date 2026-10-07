"""File/log change watcher (Phase 2) — watchdog triggers on the existing IPC broadcast.

Reuse contract (approved plan):
- Rules live in ``data/watch_rules.json``; startup creates the file with a
  DISABLED example rule when missing and NEVER overwrites existing user
  edits — only the add/remove tools rewrite it, on explicit command.
- Matched alerts leave through the same injected ``emit`` coroutine main.py
  wires to ``ipc.server.broadcast`` — identical channel/shape as the
  SystemMonitor (``monitor_alert``, category ``filewatch``). The observer
  thread bridges onto the asyncio loop exactly like SystemMonitor's COM
  callback (``run_coroutine_threadsafe``).
- Amendment 3: when a rule carries a content pattern, the matched snippet
  passes through ``guardrail.redact_text()`` BEFORE it can appear in any
  broadcast payload — a matched log line may contain a secret and gets the
  same redaction treatment as conversation content.

Rule schema: {id, path, glob, regex, label, enabled}
- path: a file OR directory (relative → daemon cwd). File paths are watched
  via their parent directory with glob defaulted to the basename; directory
  paths glob-default to "*".
- regex: optional; evaluated against the last 4 KiB of the file at event
  time. No match → no alert for that rule.
- Debounce: one alert per file path per cfg.watch_debounce_s (default 0.5 s).

Resilience: watchdog import is guarded (missing → warn once, no watching,
daemon lives); bad rules are skipped with warnings; ``start()`` never
raises into main.py.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from blankslate.config import DaemonConfig
from blankslate.guardrail.redact import redact_text

logger = logging.getLogger("blankslate.monitor.watcher")

EmitFn = Callable[[dict[str, Any]], Awaitable[Any]]

_TAIL_BYTES = 4096

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer

    HAVE_WATCHDOG = True
except Exception:  # pragma: no cover - environment without watchdog
    HAVE_WATCHDOG = False
    FileSystemEventHandler = object  # type: ignore[misc,assignment]
    Observer = None  # type: ignore[assignment]


def rules_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "data" / "watch_rules.json"


def _default_rules_doc() -> dict[str, Any]:
    return {
        "rules": [
            {
                "id": "example",
                "path": "logs/example.log",
                "glob": "*.log",
                "regex": None,
                "label": "example rule",
                "enabled": False,
                "note": "disabled example — enable via add_watch_rule or edit this file",
            }
        ]
    }


def load_rules() -> list[dict[str, Any]]:
    p = rules_path()
    if not p.exists():
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(_default_rules_doc(), indent=2), encoding="utf-8")
            logger.info("watch rules: created example at %s", p)
        except OSError:
            logger.exception("watch rules: could not create example file")
        return []
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        rules = doc.get("rules") if isinstance(doc, dict) else doc
        if not isinstance(rules, list):
            raise ValueError("rules key missing/not a list")
        return [r for r in rules if isinstance(r, dict)]
    except Exception:
        logger.exception("watch rules: %s unreadable — watching nothing", p)
        return []


def save_rules(rules: list[dict[str, Any]]) -> None:
    p = rules_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"rules": rules}, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _rule_id(path: str, glob_pat: str) -> str:
    h = hashlib.sha256(f"{path}|{glob_pat}".encode("utf-8")).hexdigest()
    return "w_" + h[:8]


def _resolve(path: str, glob_pat: str | None) -> tuple[Path, str] | None:
    """(watch_dir, effective_glob) for a rule path; None if unusable."""
    raw = str(path or "").strip().strip('"')
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = Path.cwd() / p
    if p.is_dir():
        return p, (glob_pat or "*")
    # Treat as file path (may not exist yet — extension heuristic for dirs).
    parent = p.parent
    if not parent.exists():
        return None
    return parent, (glob_pat or p.name)


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: "FileWatcher") -> None:
        super().__init__()
        self._w = watcher

    def on_created(self, event: FileSystemEvent) -> None:
        self._w._on_event("created", event)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._w._on_event("modified", event)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._w._on_event("moved", event)


class FileWatcher:
    def __init__(self, cfg: DaemonConfig, emit: EmitFn) -> None:
        self._cfg = cfg
        self._emit = emit
        self._loop: asyncio.AbstractEventLoop | None = None
        self._observer: Any = None
        self._rules: list[dict[str, Any]] = []
        self._resolved: dict[str, tuple[Path, str]] = {}  # rule id → (dir, glob)
        self._last_fire: dict[str, float] = {}
        self._lock = threading.Lock()
        self._started = False

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if not getattr(self._cfg, "watch_enabled", True):
            logger.warning("watcher: disabled by config (watch_enabled=False)")
            return
        if not HAVE_WATCHDOG:
            logger.warning(
                "watcher: watchdog not installed — file watching DISABLED "
                "(install watchdog to enable)"
            )
            return
        try:
            self._load_and_launch()
            self._started = True
        except Exception:
            logger.exception("watcher: failed to start — file watching inactive")

    async def stop(self) -> None:
        self._stop_observer()
        self._started = False

    async def reload(self) -> None:
        """Re-read rules file and re-schedule (called by add/remove tools)."""
        if not HAVE_WATCHDOG or not getattr(self._cfg, "watch_enabled", True):
            return
        try:
            self._stop_observer()
            self._load_and_launch()
        except Exception:
            logger.exception("watcher: reload failed")

    def _load_and_launch(self) -> None:
        rules: list[dict[str, Any]] = []
        resolved: dict[str, tuple[Path, str]] = {}
        for raw in load_rules():
            if not raw.get("enabled", True):
                continue
            path = str(raw.get("path", "")).strip()
            glob_pat = str(raw.get("glob", "")).strip() or None
            rx = raw.get("regex") or None
            if rx:
                try:
                    re.compile(rx)
                except re.error as exc:
                    logger.warning("watcher: bad regex in rule %r skipped: %s", path, exc)
                    continue
            got = _resolve(path, glob_pat)
            if got is None:
                logger.warning("watcher: rule path unusable, skipped: %r", path)
                continue
            directory, eff_glob = got
            rid = str(raw.get("id") or _rule_id(path, eff_glob))
            rule = dict(raw)
            rule["id"] = rid
            rule["path"] = path
            rule["glob"] = eff_glob
            rules.append(rule)
            resolved[rid] = (directory, eff_glob)

        self._rules, self._resolved = rules, resolved

        self._observer = Observer()
        handler = _Handler(self)
        # One schedule per unique directory: the handler evaluates every rule.
        scheduled_dirs: set[str] = set()
        for directory in sorted({str(d) for d, _g in resolved.values()}):
            try:
                self._observer.schedule(handler, directory, recursive=False)
                scheduled_dirs.add(directory)
            except Exception:
                logger.exception("watcher: schedule failed for %s", directory)
        scheduled = len(scheduled_dirs)
        try:
            self._observer.start()
        except Exception:
            logger.exception("watcher: observer start failed")
            self._observer = None
            return
        dirs = sorted({str(d) for d, _g in resolved.values()})
        logger.info(
            "watcher: active — %d rule(s), %d dir(s), debounce=%.2fs: %s",
            len(rules), scheduled,
            max(0.05, float(getattr(self._cfg, "watch_debounce_s", 0.5))),
            ", ".join(dirs) or "none",
        )

    def _stop_observer(self) -> None:
        obs, self._observer = self._observer, None
        if obs is not None:
            try:
                obs.stop()
                obs.join(timeout=5)
            except Exception:
                logger.exception("watcher: observer stop failed")

    # ── event path (watchdog thread) ─────────────────────────────────────

    def _on_event(self, kind: str, event: Any) -> None:
        if getattr(event, "is_directory", False):
            return
        src = getattr(event, "dest_path", None) or getattr(event, "src_path", "")
        if not src:
            return
        path = str(src)

        debounce = max(0.05, float(getattr(self._cfg, "watch_debounce_s", 0.5)))
        now = time.time()
        with self._lock:
            last = self._last_fire.get(path, 0.0)
            if now - last < debounce:
                return
            self._last_fire[path] = now
            rules = list(self._rules)
            resolved = dict(self._resolved)

        name = Path(path).name
        for rule in rules:
            try:
                directory, eff_glob = resolved.get(rule["id"], (None, None))
                if directory is None:
                    continue
                if not Path(path).is_relative_to(directory):
                    continue
                if not fnmatch.fnmatch(name, eff_glob):
                    continue
                snippet = None
                rx = rule.get("regex") or None
                if rx:
                    m = re.search(rx, _tail(path))
                    if not m:
                        continue
                    # Amendment 3: redact BEFORE the snippet can enter a payload.
                    snippet = redact_text(m.group(0))
                msg = {
                    "type": "monitor_alert",
                    "category": "filewatch",
                    "rule": rule["id"],
                    "label": str(rule.get("label") or ""),
                    "path": path,
                    "event": kind,
                    "ts": now,
                }
                if snippet is not None:
                    msg["snippet"] = snippet
                self._schedule_emit(msg)
            except Exception:
                logger.exception("watcher: rule %s failed on %s", rule.get("id"), path)

    def _schedule_emit(self, msg: dict[str, Any]) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._safe_emit(msg), self._loop)

    async def _safe_emit(self, msg: dict[str, Any]) -> None:
        try:
            await self._emit(msg)
        except Exception:
            logger.exception("watcher emit failed: %s", msg.get("type"))


def _tail(path: str) -> str:
    try:
        with open(path, "rb") as f:
            try:
                f.seek(-_TAIL_BYTES, os.SEEK_END)
            except OSError:
                pass
            return f.read(_TAIL_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""


# ── module singleton (handlers call reload() after rule changes) ────────────

_WATCHER: FileWatcher | None = None


def set_watcher(w: FileWatcher | None) -> None:
    global _WATCHER
    _WATCHER = w


def get_watcher() -> FileWatcher | None:
    return _WATCHER


# ── tool handlers (standard dispatcher contract) ────────────────────────────

_WATCH_ADD_RE = re.compile(
    r"^\s*watch(?:ing)?\s+(?:file|log|path)?\s*(?P<path>\S+?)"
    r"(?:\s+matching\s+(?P<glob>\S+))?(?:\s+pattern\s+(?P<regex>.+))?\s*$",
    re.I,
)
_WATCH_DEL_RE = re.compile(
    r"^\s*(?:remove|delete|stop)\s+(?:the\s+)?watch(?:ing)?\s+"
    r"(?:rule\s+)?(?:for\s+)?(?P<path>\S+)\s*$",
    re.I,
)


async def _reload() -> None:
    w = get_watcher()
    if w is not None:
        await w.reload()


async def add_watch_rule(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()
    path = str(args.get("path", "")).strip()
    glob_pat = str(args.get("glob", "")).strip() or None
    rx = args.get("regex") or None

    if not path:
        m = _WATCH_ADD_RE.match(query)
        if m:
            path = m.group("path")
            glob_pat = glob_pat or m.group("glob")
            rx = rx or m.group("regex")
    if not path:
        return {
            "tool": "add_watch_rule", "deterministic": True,
            "result": "No path given.",
            "reply": "What should I watch? e.g. 'watch file logs/app.log' "
                     "or 'watch file logs/app.log pattern ERROR'.",
        }
    if rx:
        try:
            re.compile(rx)
        except re.error as exc:
            return {
                "tool": "add_watch_rule", "deterministic": True, "path": path,
                "result": f"bad pattern: {exc}",
                "reply": f"That pattern isn't valid regex: {exc}",
            }
    got = _resolve(path, glob_pat)
    if got is None:
        return {
            "tool": "add_watch_rule", "deterministic": True, "path": path,
            "result": "unusable path",
            "reply": f"I can't watch '{path}' — the parent directory doesn't exist yet.",
        }
    directory, eff_glob = got
    rid = _rule_id(path, eff_glob)

    rules = load_rules()
    rule = {
        "id": rid, "path": path, "glob": eff_glob,
        "regex": rx, "label": str(args.get("label", "")), "enabled": True,
    }
    rules = [r for r in rules if str(r.get("id")) != rid] + [rule]
    try:
        save_rules(rules)
    except OSError as exc:
        return {
            "tool": "add_watch_rule", "deterministic": True,
            "result": f"save failed: {exc}",
            "reply": f"I couldn't save that rule: {exc}",
        }
    await _reload()

    desc = f"Watching '{path}' (file pattern '{eff_glob}')"
    if rx:
        desc += f", content pattern /{rx}/"
    return {
        "tool": "add_watch_rule", "deterministic": True,
        "rule": rule, "result": desc + ".", "reply": desc + ".",
    }


async def remove_watch_rule(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()
    ref = str(args.get("id", args.get("path", ""))).strip()
    if not ref:
        m = _WATCH_DEL_RE.match(query)
        if m:
            ref = m.group("path")
    if not ref:
        return {
            "tool": "remove_watch_rule", "deterministic": True,
            "result": "No rule given.",
            "reply": "Which watch rule? e.g. 'remove watch rule logs/app.log'.",
        }

    rules = load_rules()
    kept, removed = [], []
    for r in rules:
        rp = str(r.get("path", ""))
        rid = str(r.get("id", ""))
        same = rp == ref or rid == ref
        if not same and rp:
            try:
                a, b = _resolve(rp, None), _resolve(ref, None)
                same = a is not None and a == b
            except Exception:
                same = False
        (removed if same else kept).append(r)

    if not removed:
        return {
            "tool": "remove_watch_rule", "deterministic": True, "ref": ref,
            "result": f"no rule for {ref}",
            "reply": f"No watch rule matches '{ref}'.",
        }
    try:
        save_rules(kept)
    except OSError as exc:
        return {
            "tool": "remove_watch_rule", "deterministic": True,
            "result": f"save failed: {exc}",
            "reply": f"I couldn't update the rules file: {exc}",
        }
    await _reload()
    reply = f"Stopped watching '{ref}'."
    return {
        "tool": "remove_watch_rule", "deterministic": True, "ref": ref,
        "removed": len(removed), "result": reply, "reply": reply,
    }


async def list_watch_rules(_args: dict[str, Any]) -> dict[str, Any]:
    rules = load_rules()
    active = [r for r in rules if r.get("enabled", True)]
    if not rules:
        reply = "No watch rules configured."
        return {
            "tool": "list_watch_rules", "deterministic": True,
            "rules": [], "count": 0, "result": reply, "reply": reply,
        }
    parts = [
        f"{i}) {r.get('path')} [{r.get('glob')}]"
        + (f" /{r['regex']}/" if r.get("regex") else "")
        + ("" if r.get("enabled", True) else " (disabled)")
        for i, r in enumerate(rules, 1)
    ]
    reply = f"{len(active)} active watch rule" + ("s: " if len(active) != 1 else ": ") + " ".join(parts)
    if len(active) != len(rules):
        reply += f" ({len(rules) - len(active)} disabled)"
    return {
        "tool": "list_watch_rules", "deterministic": True,
        "rules": rules, "count": len(rules), "result": reply, "reply": reply,
    }
