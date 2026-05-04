#!/usr/bin/env python3
"""
claude-dejavu MCP launcher — cross-platform, venv-aware.

Resolves the bundled venv python (created by scripts/install.py) and execs
the MCP server with it. Works on Linux, macOS, and Windows native.

This file is the canonical entry for the plugin's .mcp.json:

    {
      "mcpServers": {
        "claude-dejavu": {
          "type": "stdio",
          "command": "python",
          "args": ["${CLAUDE_PLUGIN_ROOT}/bin/claude-dejavu-mcp.py"]
        }
      }
    }

Why Python instead of a shell wrapper:
  - .mcp.json doesn't support per-OS dispatch — a single command must work
    on Linux, macOS, AND Windows.
  - A bash script (#!/bin/bash) cannot execute on Windows native (no
    /bin/bash). Claude Code reports "MCP error -32000: Connection closed"
    because the spawn fails immediately.
  - Python is already a hard dependency for every part of the plugin,
    and it's in PATH on every platform after `python scripts/install.py`
    has been run at least once.

Failure modes & user-facing diagnostics:
  - Venv missing → tells the user to run scripts/install.py.
  - Server crashes on startup (PG/Weaviate down, deps missing) → the
    server's own error reaches Claude Code via stderr. Claude Code
    surfaces it next to the "Connection closed" message in the MCP UI.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _data_root() -> Path:
    """Single source of truth for the per-OS data dir. Mirrors
    scripts/install.py / hooks/stop.py / mcp/server.py.
    """
    override = os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "claude-dejavu"
    if sys.platform.startswith("win"):
        base = (
            os.environ.get("LOCALAPPDATA")
            or os.environ.get("APPDATA")
            or str(Path.home() / "AppData" / "Local")
        )
        return Path(base) / "claude-dejavu"
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg) / "claude-dejavu" if xdg else Path.home() / ".local" / "share" / "claude-dejavu"


def _resolve_venv_python(data_root: Path) -> Path | None:
    """Find the bundled venv's python interpreter, or None if missing."""
    candidates = [
        data_root / "venv" / "Scripts" / "python.exe",   # Windows
        data_root / "venv" / "bin" / "python3",          # Linux / macOS
        data_root / "venv" / "bin" / "python",           # fallback
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def main() -> int:
    data_root = _data_root()
    venv_py = _resolve_venv_python(data_root)
    plugin_root = Path(
        os.environ.get("CLAUDE_PLUGIN_ROOT")
        or Path(__file__).resolve().parent.parent
    )

    if venv_py is None:
        sys.stderr.write(
            f"claude-dejavu MCP: bundled venv not found at {data_root}/venv\n\n"
            "Run the installer first:\n"
            f"  python {plugin_root}{os.sep}scripts{os.sep}install.py\n\n"
            "Or set CLAUDE_DEJAVU_DATA_ROOT to an existing data dir.\n"
        )
        return 1

    server = plugin_root / "mcp" / "server.py"
    if not server.exists():
        sys.stderr.write(
            f"claude-dejavu MCP: server.py not found at {server}\n"
            "Plugin install may be corrupt — try `claude plugin update claude-dejavu`.\n"
        )
        return 1

    args = [str(venv_py), str(server), *sys.argv[1:]]
    # On Windows, os.execv replaces the process — same on POSIX. This
    # keeps the parent process count flat and makes Claude Code's stdio
    # wiring direct.
    try:
        os.execv(str(venv_py), args)
    except OSError as e:
        sys.stderr.write(f"claude-dejavu MCP: failed to exec venv python: {e}\n")
        return 1
    return 0  # unreachable


if __name__ == "__main__":
    sys.exit(main())
