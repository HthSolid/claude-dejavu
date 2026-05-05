"""claude-dejavu hook dispatcher — cross-platform.

Single entry point for all plugin hooks (Setup, SessionStart, Stop,
SessionEnd, PreToolUse). Takes the hook name as the first argument
and execs the corresponding handler with the right Python.

Why this file exists:
  hooks.json was previously calling `python3 SCRIPT.py` directly.
  That fails on Windows where the canonical command is `python` or
  `py.exe` — `python3` typically isn't on PATH. The Setup hook
  (which calls install.py) silently failed on every Windows
  install, leaving the plugin "installed" but with no venv, no MCP
  server, no working tools.

  This dispatcher is invoked via `python` (which IS available on
  every supported platform: Windows installer creates it, modern
  Linux/macOS have it as a symlink to python3, and Ubuntu users
  can `apt install python-is-python3` if missing).

  The dispatcher then re-execs the requested hook script using
  the same `sys.executable` that started us — so subsequent calls
  don't depend on PATH lookup.

Usage (called from hooks.json):

    python "${CLAUDE_PLUGIN_ROOT}/bin/dejavu-hook.py" <hook-name> [args]

Supported hook names:
    setup           → scripts/install.py --auto
    session-start   → hooks/session_start.py
    stop            → hooks/stop.py
    session-end     → hooks/session_end.py
    pre-tool-use    → hooks/pre_tool_use.py

Any extra args are forwarded.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


HOOK_TARGETS: dict[str, tuple[str, ...]] = {
    "setup":         ("scripts", "install.py"),
    "session-start": ("hooks", "session_start.py"),
    "stop":          ("hooks", "stop.py"),
    "session-end":   ("hooks", "session_end.py"),
    "pre-tool-use":  ("hooks", "pre_tool_use.py"),
}

EXTRA_ARGS_BY_HOOK: dict[str, tuple[str, ...]] = {
    "setup": ("--auto",),
}


def _plugin_root() -> Path:
    """Resolve the plugin root from this file's location.
    bin/dejavu-hook.py → parent.parent."""
    return Path(__file__).resolve().parents[1]


def _resolve_target(hook_name: str) -> Path:
    if hook_name not in HOOK_TARGETS:
        sys.stderr.write(
            f"[dejavu-hook] unknown hook `{hook_name}`. "
            f"Valid: {', '.join(HOOK_TARGETS)}\n")
        sys.exit(2)
    parts = HOOK_TARGETS[hook_name]
    return _plugin_root().joinpath(*parts)


def main() -> int:
    if len(sys.argv) < 2:
        sys.stderr.write(
            "[dejavu-hook] usage: dejavu-hook.py <hook-name> [extra args]\n"
            f"  hook names: {', '.join(HOOK_TARGETS)}\n")
        return 2

    hook = sys.argv[1].lower()
    target = _resolve_target(hook)
    if not target.is_file():
        sys.stderr.write(
            f"[dejavu-hook] target not found: {target}\n"
            "The plugin install may be corrupt — re-run install.py "
            "or reinstall the plugin from the marketplace.\n")
        return 2

    fixed_args = list(EXTRA_ARGS_BY_HOOK.get(hook, ()))
    forwarded = list(sys.argv[2:])
    cmd = [sys.executable, str(target), *fixed_args, *forwarded]

    if os.name == "nt":
        # Windows: subprocess.call returns the child's exit code; we
        # forward stdout/stderr through inheritance.
        import subprocess
        return subprocess.call(cmd)
    # POSIX: replace the current process so the child inherits stdio
    # and signals cleanly. No fork overhead.
    os.execv(cmd[0], cmd)
    return 0  # unreachable


if __name__ == "__main__":
    sys.exit(main())
