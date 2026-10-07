"""Reminder delivery (Phase 1) — rides the existing monitor package.

The poller runs as an asyncio task next to SystemMonitor's poll loop and
the vault purge timer. Due tasks are emitted as `monitor_alert`
(category=reminder) through the SAME injected `emit` coroutine main.py
wires to ipc.server.broadcast — no new transport (approved reuse rule).

Each due task fires exactly once: store.mark_reminded() sets reminded_at
inside the sealed payload after a successful emit. Titles pass through
guardrail redact_text() — a reminder title is user content and gets the
same secret-shaping treatment as conversation content.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from blankslate.guardrail.redact import redact_text

logger = logging.getLogger("blankslate.monitor.reminders")

EmitFn = Callable[[dict[str, Any]], Awaitable[Any]]


async def reminder_loop(cfg: Any, store: Any, emit: EmitFn) -> None:
    """Sleep-first loop (no fire at t=0, mirror of purge_loop)."""
    interval = max(0.05, float(getattr(cfg, "reminder_interval", 30.0)))
    logger.info(
        "reminders: sweeping every %.1fs over %d task(s)",
        interval, len(store.all()),
    )
    while True:
        await asyncio.sleep(interval)
        try:
            now = time.time()
            for task in store.due_tasks(now):
                try:
                    await emit({
                        "type": "monitor_alert",
                        "category": "reminder",
                        "task_id": task.id,
                        "title": redact_text(task.title),
                        "due_at": task.due_at,
                        "ts": now,
                    })
                    store.mark_reminded(task.id)
                    logger.info("reminder fired: %s", task.id)
                except Exception:
                    logger.exception("reminder emit failed: %s", getattr(task, "id", "?"))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("reminder sweep failed")
