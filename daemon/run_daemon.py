#!/usr/bin/env python3
"""run_daemon.py — Top-level wrapper. ALWAYS use this to launch the daemon.

Handles:
  1. UTF-8 encoding enforcement (before ANY other import)
  2. Proper package import regardless of CWD or PyInstaller context
"""

# ── UTF-8 enforcement (must be first, before any print/log) ──────────
import sys
import io

if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Daemon entry ─────────────────────────────────────────────────────
from blankslate.main import main

if __name__ == "__main__":
    main()
