"""Tool registry — single catalog of available tools.

Each tool declares:
  name:        unique identifier
  description: human-readable, used for routing
  keywords:    lexical hints for keyword routing
  handler:     async callable (kwargs: dict) -> dict result

The registry powers the router: only relevant tools (by embedding
and/or keyword similarity) get selected — never the whole catalog.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger("blankslate.tools.registry")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    keywords: tuple[str, ...] = ()
    handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        # Import handlers lazily to avoid circular imports at module load.
        from blankslate.tools.handlers import HANDLERS

        def h(name: str) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
            return HANDLERS[name]

        self.register(
            ToolSpec(
                name="get_current_time",
                description="Get the current date and time.",
                keywords=("time", "date", "clock", "now", "when is it"),
                handler=h("get_current_time"),
            )
        )
        self.register(
            ToolSpec(
                name="web_search",
                description="Search the web for up-to-date information.",
                keywords=("search", "web", "look up", "online", "find", "google"),
                handler=h("web_search"),
            )
        )
        self.register(
            ToolSpec(
                name="calculator",
                description="Perform arithmetic and mathematical calculations.",
                keywords=("calculate", "math", "add", "subtract", "multiply", "divide", "sum", "total"),
                handler=h("calculator"),
            )
        )
        self.register(
            ToolSpec(
                name="note_taking",
                description="Create, read, and manage local notes.",
                # Phase 1: 'remind'/'todo'/'schedule' moved to the task tools —
                # they double-fired here (note_taking defaults to READ) and
                # routed reminder intent away from set_reminder/add_task.
                keywords=("note", "remember"),
                handler=h("note_taking"),
            )
        )
        self.register(
            ToolSpec(
                name="weather",
                description="Get current weather and forecasts.",
                keywords=("weather", "temperature", "rain", "forecast", "hot", "cold"),
                handler=h("weather"),
            )
        )
        self.register(
            ToolSpec(
                name="app_launch",
                description="Launch an application by name (notepad, chrome, code, explorer).",
                keywords=("open", "launch", "start", "run", "app", "application"),
                handler=h("app_launch"),
            )
        )
        self.register(
            ToolSpec(
                name="app_kill",
                description="Terminate a running process by name.",
                keywords=("close", "kill", "stop", "quit", "terminate", "process"),
                handler=h("app_kill"),
            )
        )
        self.register(
            ToolSpec(
                name="list_windows",
                description="List all open visible windows with title and process name.",
                keywords=("windows", "list windows", "open windows", "show windows"),
                handler=h("list_windows"),
            )
        )
        self.register(
            ToolSpec(
                name="switch_window",
                description="Switch focus to a window by title substring.",
                keywords=("switch", "focus", "go to", "bring", "foreground"),
                handler=h("switch_window"),
            )
        )
        self.register(
            ToolSpec(
                name="get_foreground_window",
                description="Get info about the current foreground (active) window.",
                keywords=("active window", "current window", "foreground"),
                handler=h("get_foreground_window"),
            )
        )
        self.register(
            ToolSpec(
                name="type_text",
                description="Type text into the currently focused window.",
                keywords=("type", "write", "text", "input", "dictate", "enter text"),
                handler=h("type_text"),
            )
        )
        self.register(
            ToolSpec(
                name="press_key",
                description="Press a single key (enter, tab, escape, backspace, etc.).",
                keywords=("press", "key", "hit", "enter", "tab", "backspace"),
                handler=h("press_key"),
            )
        )
        self.register(
            ToolSpec(
                name="hotkey",
                description="Press a keyboard shortcut (e.g. Ctrl+C, Alt+F4).",
                keywords=("shortcut", "hotkey", "combo", "ctrl", "alt", "shift"),
                handler=h("hotkey"),
            )
        )
        self.register(
            ToolSpec(
                name="focus_snapshot",
                description="Save the current foreground window so focus can be restored later.",
                keywords=("snapshot", "save this window", "save my window"),
                handler=h("focus_snapshot"),
            )
        )
        self.register(
            ToolSpec(
                name="focus_restore",
                description="Return focus to the previously saved window.",
                keywords=("go back", "where was i", "previous window", "come back"),
                handler=h("focus_restore"),
            )
        )
        self.register(
            ToolSpec(
                name="screen_xray",
                description="Capture the active window (image + optional describe/answer).",
                keywords=("screen", "screenshot", "xray", "capture"),
                handler=h("screen_xray"),
            )
        )
        self.register(
            ToolSpec(
                name="clipboard_read",
                description="Read the clipboard: copied text (redacted) or pasted image.",
                keywords=("clipboard", "copied", "what did i copy"),
                handler=h("clipboard_read"),
            )
        )
        self.register(
            ToolSpec(
                name="run_macro",
                description="Run a saved multi-step voice macro (JSON) from the macros folder.",
                keywords=("macro", "macros", "routine"),
                handler=h("run_macro"),
            )
        )
        self.register(
            ToolSpec(
                name="set_volume",
                description="Set the system speaker output volume (level 0.0-1.0 or percent 0-100).",
                keywords=("volume", "louder", "quieter", "mute", "unmute", "sound"),
                handler=h("set_volume"),
            )
        )
        self.register(
            ToolSpec(
                name="mute_microphone",
                description="Mute or unmute the microphone (capture device), without touching speakers.",
                keywords=("microphone", "mic", "mute", "unmute", "input"),
                handler=h("mute_microphone"),
            )
        )
        self.register(
            ToolSpec(
                name="media_control",
                description="Send a global media key: play/pause, next track, or previous track.",
                keywords=("play", "pause", "music", "track", "song", "next", "previous", "media"),
                handler=h("media_control"),
            )
        )
        # ── local task planner (Phase 1) ────────────────────────────────────
        # Keyword sets are kept disjoint from note_taking ('remember' stays a
        # note, 'remind' is a reminder) — every router-selected tool executes,
        # so overlap would double-fire side by side.
        self.register(
            ToolSpec(
                name="add_task",
                description="Create a new task (optionally with a due date).",
                keywords=("add task", "create task", "new task", "todo", "i need to"),
                handler=h("add_task"),
            )
        )
        self.register(
            ToolSpec(
                name="set_reminder",
                description="Remind the user about something at a specific date/time.",
                keywords=("remind", "reminder", "alert me", "notify me"),
                handler=h("set_reminder"),
            )
        )
        self.register(
            ToolSpec(
                name="list_tasks",
                description="List tasks: all, pending, due today, overdue, or completed.",
                keywords=("tasks", "checklist", "due today", "overdue"),
                handler=h("list_tasks"),
            )
        )
        self.register(
            ToolSpec(
                name="complete_task",
                description="Mark a task as done.",
                keywords=("complete", "finish", "done with", "mark done"),
                handler=h("complete_task"),
            )
        )
        self.register(
            ToolSpec(
                name="delete_task",
                description="Delete a task by title or id.",
                keywords=(
                    "delete task", "remove task", "cancel task",
                    "delete the task", "remove the task",
                ),
                handler=h("delete_task"),
            )
        )
        self.register(
            ToolSpec(
                name="import_ics",
                description="Import events from a local .ics calendar file (no remote sync).",
                keywords=("import ics", "import calendar", "ics file", "calendar file", "load ics"),
                handler=h("import_ics"),
            )
        )
        # ── proactive background event agents (Phase 2) ─────────────────────
        self.register(
            ToolSpec(
                name="add_watch_rule",
                description="Watch a file or directory for changes and alert on matches.",
                keywords=("watch file", "watch log", "watch path", "add watch", "start watching"),
                handler=h("add_watch_rule"),
            )
        )
        self.register(
            ToolSpec(
                name="remove_watch_rule",
                description="Stop watching a previously configured file or path.",
                keywords=("remove watch", "delete watch", "stop watching", "unwatch"),
                handler=h("remove_watch_rule"),
            )
        )
        self.register(
            ToolSpec(
                name="list_watch_rules",
                description="List configured file-watch rules.",
                keywords=("list watch", "show watch", "watch rules"),
                handler=h("list_watch_rules"),
            )
        )
        # ── local timeline (Phase 4) ─────────────────────────────────────────
        # Known co-fires (deterministic, documented): bare 'start' also matches
        # app_launch, bare 'stop' matches app_kill, bare 'search' matches
        # web_search's offline stub. Gate phrases are chosen accordingly.
        self.register(
            ToolSpec(
                name="start_timeline",
                description="Start an opt-in timeline session (local screen indexing).",
                keywords=("start timeline", "begin timeline", "timeline on",
                          "enable timeline"),
                handler=h("start_timeline"),
            )
        )
        self.register(
            ToolSpec(
                name="stop_timeline",
                description="Stop the active timeline session.",
                keywords=("stop timeline", "end timeline", "timeline off",
                          "disable timeline"),
                handler=h("stop_timeline"),
            )
        )
        self.register(
            ToolSpec(
                name="search_timeline",
                description="Search the local timeline (screen text indexed this session).",
                keywords=("search timeline", "timeline search"),
                handler=h("search_timeline"),
            )
        )
        # ── floating overlay pill (six-feature expansion, Phase 1) ──────────
        # Keywords are deliberately narrow ("pill"-family only): the pill is a
        # side surface, never a router match for generic show/display queries
        # (routing regression suites in tl3/tl5 depend on that separation).
        self.register(
            ToolSpec(
                name="show_context_pill",
                description="Show a small floating contextual pill next to a window, by title substring.",
                keywords=("pill", "context pill", "overlay pill", "hud pill"),
                handler=h("show_context_pill"),
            )
        )

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec
        logger.info("Registered tool: %s", spec.name)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def all(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return list(self._tools.keys())

    @property
    def count(self) -> int:
        return len(self._tools)

    async def call(self, name: str, **kwargs: Any) -> dict[str, Any]:
        """Invoke a tool handler with graceful error boundary."""
        spec = self._tools.get(name)
        if spec is None:
            return {"error": f"unknown tool: {name}"}
        if spec.handler is None:
            # Default stub handler if not provided
            return {
                "tool": name,
                "result": f"[stub] {name} called with {kwargs}",
                "stub": True,
            }
        try:
            return await spec.handler(kwargs)
        except Exception as exc:
            logger.exception("Tool %s failed", name)
            return {"error": str(exc)}
