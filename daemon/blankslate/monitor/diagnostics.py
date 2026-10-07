"""Proactive diagnostics — crash watching, anomaly triage, propose-and-confirm.

(Phase 2, six-feature expansion.)

Anomaly inputs
--------------
1. CPU/GPU spike ``monitor_alert`` events from :mod:`blankslate.monitor.system`
   — forwarded through SystemMonitor's optional ``on_alert`` hook. The monitor's
   emit lambda in main.py stays byte-identical (hw2 T8 pins the literal
   ``emit=lambda msg: broadcast(state, msg)``); the hook is a separate,
   optional parameter (``monitor/system.py`` emit wiring only).
2. Application event-log crash watching: ``win32evtlog.EvtQuery`` +
   ``EvtNext`` poll every ``cfg.monitor_poll_interval`` (interval from
   config), filtered to Event IDs 1000/1001, parsed from the event XML
   (crashed exe = AppPath/AppName, faulting module = ModuleName). A
   ``TimeCreated`` boundary captured at service start excludes historical
   events; EventRecordID dedup drops poll-window overlaps. Per-image-path
   cooldown between proposals uses ``cfg.diagnosis_cooldown_s`` (60 s).

Diagnosis call
--------------
Each anomaly goes to the local LLM (``llm.generate``, num_ctx=512) with a
FIXED instruction block demanding a single JSON object
``{summary, cause, proposal, action, action_params}``. ``action`` must match
the v1 whitelist — ``relaunch_app`` ONLY — else the diagnosis is logged and
dropped (never broadcast). LLM failure/timeout/parse failure: logged, no
broadcast, the monitor loop never breaks (same hook pattern as RAG in
judge.py).

Broadcast (proposal)::

    {"type": "monitor_alert", "category": "diagnosis", "id", "anomaly",
     "summary", "cause", "proposal", "action", "action_params",
     "status": "awaiting_confirmation"}

Confirm flow — propose-and-confirm ONLY
---------------------------------------
``diagnose_confirm {id}`` / ``diagnose_dismiss {id}`` are handled in
ipc/server.py, which is the ONLY caller of :meth:`confirm` — nothing executes
at proposal time. State machine: ``awaiting_confirmation -> executed |
dismissed``; illegal transitions are rejected with an error. Both transitions
are broadcast so every HUD can update its cards.

Relaunch target: for crash anomalies the exe path is daemon-authoritative
(parsed from the event XML ground truth), never LLM prose — the LLM supplies
summary/cause/proposal only. For spike anomalies the LLM's ``action_params``
is used as-is and re-validated (existing file) at confirm time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from blankslate.config import DaemonConfig

logger = logging.getLogger("blankslate.diagnostics")

EmitFn = Callable[[dict[str, Any]], Awaitable[Any]]

# v1 action whitelist — relaunch_app only (safe, observable). Anything else
# is rejected before broadcast AND re-checked at confirm time (defense in depth).
ALLOWED_ACTIONS = frozenset({"relaunch_app"})

# Fixed instruction block prepended to every diagnosis call (plan: fixed
# system prompt returning JSON; generate() carries a single user message, so
# the instructions ride at the top of the prompt byte-for-byte the same way).
SYSTEM_INSTRUCTIONS = (
    "You are BlankSlate's local diagnostics engine. You receive ONE anomaly "
    "observation. Respond with ONLY a JSON object (no markdown, no code fences, "
    "no prose outside the JSON) with exactly these keys: "
    '"summary" (one user-facing line), "cause" (one line), '
    '"proposal" (one line describing what Confirm would do), '
    '"action" (must be exactly "relaunch_app"), '
    '"action_params" (object with key "path", an absolute executable path; '
    'use the anomaly\'s "image_path" verbatim when present, otherwise "").'
)

_PENDING_CAP = 500          # completed diagnoses retained for transition checks
_SEEN_CAP = 5000            # EventRecordIDs remembered for poll dedup
_CREATE_NO_WINDOW = 0x08000000


def _utc_boundary(seconds_back: float) -> str:
    """EvtQuery TimeCreated boundary, ISO8601 UTC with 'Z' (verified format)."""
    t = datetime.now(timezone.utc) - timedelta(seconds=seconds_back)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _extract_json(content: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of an LLM reply; None if unparseable."""
    text = content.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class DiagnosticsService:
    """Crash watcher + anomaly triage + confirm-gated execution."""

    def __init__(self, cfg: DaemonConfig, emit: EmitFn, llm: Any) -> None:
        self._cfg = cfg
        self._emit = emit
        self._llm = llm
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        self._pending: dict[str, dict[str, Any]] = {}
        self._cooldowns: dict[str, float] = {}
        self._seen: dict[int, None] = {}          # ordered set (record IDs)
        self._boundary = _utc_boundary(2.0)       # excludes pre-start history
        self._worker: asyncio.Task | None = None
        self._crash: asyncio.Task | None = None
        self._seq = 0

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self._cfg.diagnostics_enabled:
            logger.info("diagnostics: disabled (diagnostics_enabled=False)")
            return
        self._worker = asyncio.create_task(self._worker_loop())
        self._crash = asyncio.create_task(self._crash_loop())
        logger.info(
            "diagnostics: started (poll=%.1fs cooldown=%.0fs llm_timeout=%.0fs)",
            self._cfg.monitor_poll_interval,
            self._cfg.diagnosis_cooldown_s,
            self._cfg.diagnosis_llm_timeout_s,
        )

    async def stop(self) -> None:
        for task in (self._worker, self._crash):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._worker = None
        self._crash = None

    # ── anomaly intake ───────────────────────────────────────────────────

    def on_anomaly(self, msg: dict[str, Any]) -> None:
        """SystemMonitor alert hook (cpu/gpu spikes only; battery excluded).

        Called on the event loop from ``SystemMonitor._safe_emit``. Never
        raises — a diagnostics problem must not break the monitor.
        """
        if not self._cfg.diagnostics_enabled:
            return
        if msg.get("type") != "monitor_alert":
            return
        category = msg.get("category")
        if category not in ("cpu", "gpu"):
            return
        anomaly = {
            "kind": "spike",
            "category": category,
            "value": msg.get("value"),
            "threshold": msg.get("threshold"),
            "samples": msg.get("samples"),
            "ts": msg.get("ts", time.time()),
        }
        self._enqueue(anomaly)

    def _enqueue(self, anomaly: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait(anomaly)
        except asyncio.QueueFull:
            logger.warning("diagnostics: anomaly queue full — dropping %s", anomaly.get("kind"))

    # ── crash watcher (EvtQuery + EvtNext poll) ──────────────────────────

    async def _crash_loop(self) -> None:
        interval = max(0.1, float(self._cfg.monitor_poll_interval))
        logger.info("diagnostics: crash watcher polling every %.1fs (IDs 1000/1001)", interval)
        while True:
            await asyncio.sleep(interval)
            try:
                events = await asyncio.to_thread(self._poll_crash_events)
                for anomaly in events:
                    self._enqueue(anomaly)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("diagnostics: crash poll failed (loop continues)")

    def _poll_crash_events(self) -> list[dict[str, Any]]:
        """Thread-safe sync poll: new 1000/1001 events since service start."""
        import pywintypes
        import win32evtlog

        xpath = (
            "*[System[(EventID=1000 or EventID=1001) and "
            f"TimeCreated[@SystemTime>='{self._boundary}']]]"
        )
        query = win32evtlog.EvtQuery(
            "Application", win32evtlog.EvtQueryForwardDirection, xpath
        )
        out: list[dict[str, Any]] = []
        while True:
            try:
                handles = win32evtlog.EvtNext(query, 64, 2000)
            except pywintypes.error:
                break
            if not handles:
                break
            for handle in handles:
                try:
                    xml = win32evtlog.EvtRender(handle, win32evtlog.EvtRenderEventXml)
                except Exception:
                    continue
                event = self._parse_crash_event(xml)
                if event is None:
                    continue
                rid = event["record_id"]
                if rid in self._seen:
                    continue
                self._seen[rid] = None
                while len(self._seen) > _SEEN_CAP:
                    self._seen.pop(next(iter(self._seen)))
                out.append(event)
        return out

    @staticmethod
    def _parse_crash_event(xml: str) -> dict[str, Any] | None:
        """Event XML -> anomaly dict; None when no relaunchable app path exists."""
        import xml.etree.ElementTree as ET

        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return None
        ns = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
        event_id_s = root.findtext(".//e:System/e:EventID", default="", namespaces=ns)
        record_s = root.findtext(".//e:System/e:EventRecordID", default="", namespaces=ns)
        data = {
            (d.get("Name") or ""): (d.text or "")
            for d in root.findall(".//e:EventData/e:Data", ns)
        }
        image_path = (data.get("AppPath") or data.get("AppName") or "").strip()
        if not image_path:
            # e.g. LiveKernelEvent 1001 reports carry no user-mode exe —
            # nothing to diagnose or relaunch. Logged by caller at debug level.
            logger.debug("diagnostics: event %s has no AppPath — skipped", event_id_s)
            return None
        try:
            record_id = int(record_s)
        except ValueError:
            return None
        return {
            "kind": "crash",
            "event_id": int(event_id_s) if event_id_s.isdigit() else 0,
            "record_id": record_id,
            "image_path": image_path,
            "faulting_module": (data.get("ModuleName") or data.get("ModulePath") or "").strip(),
            "ts": time.time(),
        }

    # ── diagnosis worker ─────────────────────────────────────────────────

    async def _worker_loop(self) -> None:
        while True:
            anomaly = await self._queue.get()
            try:
                await self._diagnose(anomaly)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("diagnostics: diagnosis failed (loop continues)")

    async def _diagnose(self, anomaly: dict[str, Any]) -> None:
        # Per-image-path cooldown (crash events); spike cooldown is per-category.
        if anomaly.get("kind") == "crash":
            key = "img:" + str(anomaly.get("image_path", "")).lower()
        else:
            key = str(anomaly.get("category", anomaly.get("kind", "?")))
        now = time.time()
        last = self._cooldowns.get(key)
        if last is not None and (now - last) < self._cfg.diagnosis_cooldown_s:
            logger.info("diagnostics: cooldown active for %s — skipping", key)
            return
        self._cooldowns[key] = now

        prompt = SYSTEM_INSTRUCTIONS + "\n\n" + json.dumps(anomaly, ensure_ascii=False)
        try:
            resp = await asyncio.wait_for(
                self._llm.generate(prompt, num_ctx=512),
                timeout=self._cfg.diagnosis_llm_timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "diagnostics: LLM diagnosis timed out after %.0fs — no broadcast",
                self._cfg.diagnosis_llm_timeout_s,
            )
            return
        except Exception:
            logger.exception("diagnostics: LLM diagnosis failed — no broadcast")
            return

        data = _extract_json(getattr(resp, "content", "") or "")
        if data is None:
            logger.warning("diagnostics: LLM reply not JSON — no broadcast")
            return

        action = str(data.get("action", "")).strip()
        if action not in ALLOWED_ACTIONS:
            logger.warning(
                "diagnostics: action %r not in whitelist %s — no broadcast",
                action, sorted(ALLOWED_ACTIONS),
            )
            return

        params = data.get("action_params")
        params = dict(params) if isinstance(params, dict) else {}
        if anomaly.get("kind") == "crash" and anomaly.get("image_path"):
            # Daemon ground truth: the relaunch target is the faulting exe
            # parsed from the event XML, never LLM prose.
            params["path"] = str(anomaly["image_path"])

        self._seq += 1
        did = f"diag-{int(now * 1000)}-{self._seq}"
        record: dict[str, Any] = {
            "id": did,
            "anomaly": anomaly,
            "summary": str(data.get("summary", "")).strip()[:300],
            "cause": str(data.get("cause", "")).strip()[:300],
            "proposal": str(data.get("proposal", "")).strip()[:300],
            "action": action,
            "action_params": params,
            "status": "awaiting_confirmation",
            "created": now,
        }
        self._pending[did] = record
        self._prune_pending()
        await self._emit(self._record_msg(record))
        logger.info("diagnostics: proposed %s (%s)", did, anomaly.get("kind"))

    @staticmethod
    def _record_msg(record: dict[str, Any]) -> dict[str, Any]:
        msg = {
            "type": "monitor_alert",
            "category": "diagnosis",
            "id": record["id"],
            "anomaly": record["anomaly"],
            "summary": record["summary"],
            "cause": record["cause"],
            "proposal": record["proposal"],
            "action": record["action"],
            "action_params": record["action_params"],
            "status": record["status"],
            "created": record["created"],
        }
        if "relaunched_pid" in record:
            msg["relaunched_pid"] = record["relaunched_pid"]
        return msg

    def _prune_pending(self) -> None:
        if len(self._pending) <= _PENDING_CAP:
            return
        for did in list(self._pending):
            if len(self._pending) <= _PENDING_CAP:
                break
            if self._pending[did]["status"] != "awaiting_confirmation":
                del self._pending[did]

    # ── confirm / dismiss (the ONLY execution path) ──────────────────────

    async def confirm(self, did: str) -> dict[str, Any]:
        """Execute a proposal. Called only from ipc/server.py's confirm handler."""
        record = self._pending.get(did)
        if record is None:
            return {"error": f"unknown diagnosis id: {did or '(empty)'}"}
        if record["status"] != "awaiting_confirmation":
            return {"error": f"illegal transition: {record['status']} -> executed"}
        if record["action"] not in ALLOWED_ACTIONS:
            return {"error": f"action not whitelisted: {record['action']}"}

        path = str((record.get("action_params") or {}).get("path") or "")
        if not path or not os.path.isfile(path):
            return {"error": f"invalid relaunch path: {path!r}"}
        try:
            proc = subprocess.Popen(
                [path],
                cwd=os.path.dirname(path) or None,
                creationflags=_CREATE_NO_WINDOW,
            )
        except Exception as exc:
            return {"error": f"relaunch failed: {exc}"}

        record["status"] = "executed"
        record["relaunched_pid"] = proc.pid
        await self._emit(self._record_msg(record))
        logger.info("diagnostics: %s executed — relaunched pid=%d", did, proc.pid)
        return {"id": did, "status": "executed", "relaunched_pid": proc.pid}

    async def dismiss(self, did: str) -> dict[str, Any]:
        """Close a proposal without executing. Called only from ipc/server.py."""
        record = self._pending.get(did)
        if record is None:
            return {"error": f"unknown diagnosis id: {did or '(empty)'}"}
        if record["status"] != "awaiting_confirmation":
            return {"error": f"illegal transition: {record['status']} -> dismissed"}
        record["status"] = "dismissed"
        await self._emit(self._record_msg(record))
        logger.info("diagnostics: %s dismissed", did)
        return {"id": did, "status": "dismissed"}
