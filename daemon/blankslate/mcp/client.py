"""Lightweight MCP (Model Context Protocol) client — asyncio native.

Uses stdio transport: spawns the MCP server as a subprocess and speaks
JSON-RPC over its stdin/stdout. Session persists across calls; an idle
timeout closes the session to avoid leaking the subprocess.

No external MCP SDK dependency — the protocol (JSON-RPC 2.0 over stdio)
is simple enough to implement directly and keeps the daemon lean.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("blankslate.mcp.client")

MCP_INIT_METHOD = "initialize"
MCP_TOOLS_LIST = "tools/list"
MCP_TOOLS_CALL = "tools/call"
PROTOCOL_VERSION = "2024-11-05"


@dataclass(frozen=True)
class MCPSessionConfig:
    name: str
    command: list[str] | None = None  # e.g. ["python", "-m", "mcp_server"]
    idle_timeout_s: float = 60.0
    tool_prefix: str = "mcp."


class MCPClient:
    """MCP client that keeps a session alive across calls via stdio."""

    def __init__(self, session_cfg: MCPSessionConfig) -> None:
        self._cfg = session_cfg
        self._proc: asyncio.subprocess.Process | None = None
        self._connected = False
        self._last_used: float = 0.0
        self._idle_task: asyncio.Task | None = None
        self._closing = False
        self._id_counter = 0

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        if self._connected:
            return True

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._cfg.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            # Initialize handshake
            await self._send({
                "jsonrpc": "2.0",
                "id": self._new_id(),
                "method": MCP_INIT_METHOD,
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "blankslate", "version": "0.1.0"},
                },
            })
            await self._recv()

            self._connected = True
            self._last_used = time.time()
            self._start_idle_monitor()
            logger.info("MCP: connected to '%s'", self._cfg.name)
            return True

        except Exception as exc:
            logger.error("MCP: connect failed: %s", exc)
            await self._kill_proc()
            return False

    def _new_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    def _start_idle_monitor(self) -> None:
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.ensure_future(self._idle_close_loop())

    async def _idle_close_loop(self) -> None:
        while self._connected and not self._closing:
            await asyncio.sleep(5)
            idle = time.time() - self._last_used
            if idle >= self._cfg.idle_timeout_s:
                logger.info("MCP: '%s' idle %.0fs → closing session", self._cfg.name, idle)
                await self.close()
                return

    async def _send(self, payload: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise ConnectionError("MCP subprocess not running")
        data = (json.dumps(payload) + "\n").encode("utf-8")
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    async def _recv(self) -> dict:
        if self._proc is None or self._proc.stdout is None:
            raise ConnectionError("MCP subprocess not running")
        line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=30)
        if not line:
            raise ConnectionError("MCP subprocess closed stdout")
        return json.loads(line.decode("utf-8"))

    async def list_tools(self) -> list[dict]:
        if not self._connected:
            if not await self.connect():
                return []
        try:
            await self._send({
                "jsonrpc": "2.0",
                "id": self._new_id(),
                "method": MCP_TOOLS_LIST,
                "params": {},
            })
            resp = await self._recv()
            self._last_used = time.time()
            tools = resp.get("result", {}).get("tools", [])
            return [
                {
                    "name": f"{self._cfg.tool_prefix}{t.get('name')}",
                    "description": t.get("description", ""),
                }
                for t in tools
            ]
        except Exception as exc:
            logger.error("MCP: list_tools failed: %s", exc)
            self._connected = False
            return []

    async def call_tool(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        bare = tool_name[len(self._cfg.tool_prefix):] if tool_name.startswith(self._cfg.tool_prefix) else tool_name

        if not self._connected:
            if not await self.connect():
                return {"error": "MCP session unavailable"}

        try:
            await self._send({
                "jsonrpc": "2.0",
                "id": self._new_id(),
                "method": MCP_TOOLS_CALL,
                "params": {"name": bare, "arguments": args},
            })
            resp = await self._recv()
            self._last_used = time.time()
            if "error" in resp:
                return {"tool": tool_name, "error": str(resp["error"])}
            result = resp.get("result", {})
            # Extract text from content blocks
            text_parts = []
            for content in result.get("content", []):
                if content.get("type") == "text":
                    text_parts.append(content.get("text", ""))
            logger.info("MCP: called %s on session '%s'", tool_name, self._cfg.name)
            return {"tool": tool_name, "result": "\n".join(text_parts)}
        except Exception as exc:
            logger.error("MCP: call_tool %s failed: %s", bare, exc)
            self._connected = False
            return {"error": str(exc)}

    async def close(self) -> None:
        self._closing = True
        self._connected = False
        if self._idle_task:
            self._idle_task.cancel()
            self._idle_task = None
        await self._kill_proc()
        logger.info("MCP: session for '%s' closed", self._cfg.name)

    async def _kill_proc(self) -> None:
        if self._proc:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError):
                pass
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
