"""Proposal registry — design doc §5 state machine (dg2 pattern).

proposal  --confirm--> executed      (spawns, runner called by server)
proposal  --dismiss--> dismissed
proposal  --TTL----->  timed_out     (nothing spawned; observed lazily on
                                      the next confirm/dismiss — no timer)
*         --confirm/dismiss on unknown/settled id--> rejected: illegal transition

Single process, single event loop: no locks. confirm()/dismiss() return
(code, detail, proposal) where code ∈ ok | unknown | illegal | timed_out;
on ok the state is already settled (single spawn point stays single).
"""

from __future__ import annotations

import secrets
import time
from typing import Any

_REGISTRY: "ProposalRegistry | None" = None


class ProposalRegistry:
    def __init__(self, default_ttl_s: float = 120.0) -> None:
        self.default_ttl_s = default_ttl_s
        self._proposals: dict[str, dict[str, Any]] = {}

    def propose(
        self,
        script: str,
        purpose: str,
        ttl_s: float | None = None,
        limits: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ttl = float(ttl_s if ttl_s is not None else self.default_ttl_s)
        pid = f"sbx-{secrets.token_hex(8)}"
        proposal: dict[str, Any] = {
            "id": pid,
            "script": script,
            "purpose": purpose,
            "limits": dict(limits or {}),
            "ttl_s": ttl,
            "expires_at": time.time() + ttl,
            "state": "proposed",
            "created_at": time.time(),
        }
        self._proposals[pid] = proposal
        return proposal

    def get(self, pid: str) -> dict[str, Any] | None:
        return self._proposals.get(pid)

    def confirm(self, pid: str) -> tuple[str, str, dict[str, Any] | None]:
        return self._settle(pid, "executed")

    def dismiss(self, pid: str) -> tuple[str, str, dict[str, Any] | None]:
        return self._settle(pid, "dismissed")

    def release(self, pid: str) -> None:
        """Spawn failed before a process existed (e.g. setup missing):
        allow a later retry — proposed -> proposed, internal use only."""
        prop = self._proposals.get(pid)
        if prop is not None and prop["state"] == "executed":
            prop["state"] = "proposed"

    def _settle(self, pid: str, target: str) -> tuple[str, str, dict[str, Any] | None]:
        prop = self._proposals.get(pid)
        if prop is None:
            return "unknown", f"unknown proposal: {pid or '(empty id)'}", None
        if prop["state"] != "proposed":
            if prop["state"] == "executed" and target == "executed":
                return "illegal", "illegal transition: already executed", None
            return "illegal", f"illegal transition: already {prop['state']}", None
        if time.time() > prop["expires_at"]:
            prop["state"] = "timed_out"
            return "timed_out", "proposal timed out (nothing spawned)", None
        prop["state"] = target
        return "ok", "", prop


def get_registry() -> ProposalRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = ProposalRegistry()
    return _REGISTRY
