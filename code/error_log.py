"""Centralized error log for claude-dejavu.

Why: when something goes wrong inside the plugin, the user (or you,
asking the user to forward a log) needs a single, predictable file
that captures what failed, where, and with what context — not just
ephemeral stderr that vanishes when the MCP server is restarted.

Design:
  - Append-only JSON Lines file at the platform's data dir:
      Linux:   $XDG_DATA_HOME/claude-dejavu/errors.log (default
               $HOME/.local/share/claude-dejavu/errors.log)
      macOS:   $HOME/Library/Application Support/claude-dejavu/errors.log
      Windows: %APPDATA%\\claude-dejavu\\errors.log
  - One JSON record per line so it's grep-able + parseable.
  - Auto-rotated at 5 MB, last 3 generations kept (.1, .2, .3).
  - Every record: timestamp, component, message, context dict,
    optional traceback string, dejavu version.
  - Programmatic read via `read_recent(limit, component=None,
    since=None)` for the CLI + MCP tool surface.

Failure mode: if the log file can't be written (permission, disk
full, etc.), `log_error()` falls back to stderr — never raises.
That way an error in the error-logger never masks the original
problem."""
from __future__ import annotations

import json
import os
import platform
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

LOG_FILE_NAME = "errors.log"
MAX_BYTES = 5 * 1024 * 1024
KEEP_GENERATIONS = 3

_VERSION = "unknown"
try:
    # Lazy: read from .claude-plugin/plugin.json if reachable.
    _root = Path(__file__).resolve().parents[1]
    _pj = _root / ".claude-plugin" / "plugin.json"
    if _pj.is_file():
        _VERSION = json.loads(_pj.read_text()).get("version", "unknown")
except Exception:  # noqa: BLE001
    pass


def _data_dir() -> Path:
    """Resolve the per-user dejavu data directory (where the log
    lives, alongside the database fingerprints etc.).

    Honors CLAUDE_DEJAVU_DATA_ROOT first — that's the sandbox-
    isolation env var the smoke + install pipeline use to keep test
    runs from clobbering the user's real data dir. Falls back to
    XDG_DATA_HOME on Linux, the standard per-platform locations
    elsewhere."""
    override = os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
    if override:
        return Path(override)
    if platform.system() == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    elif platform.system() == "Windows":
        base = Path(os.environ.get("APPDATA",
                                       Path.home() / "AppData"
                                                  / "Roaming"))
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg) if xdg else (Path.home() / ".local" / "share")
    return base / "claude-dejavu"


def _log_path() -> Path:
    return _data_dir() / LOG_FILE_NAME


def _rotate_if_needed(path: Path) -> None:
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size < MAX_BYTES:
        return
    # Shift .2 → .3, .1 → .2, current → .1
    for i in range(KEEP_GENERATIONS, 0, -1):
        src = path.with_suffix(path.suffix + f".{i}")
        dst = path.with_suffix(path.suffix + f".{i + 1}")
        if src.exists():
            if i == KEEP_GENERATIONS:
                src.unlink(missing_ok=True)
            else:
                src.rename(dst)
    path.rename(path.with_suffix(path.suffix + ".1"))


def log_error(component: str, message: str, *,
                context: Optional[dict[str, Any]] = None,
                exc_info: bool = False,
                level: str = "error") -> None:
    """Write a structured record to the error log.

    Args:
        component: stable identifier of the failing subsystem
            (e.g. "mcp.server", "indexer.env", "rewriter.cascade").
        message: short human-readable description.
        context: optional dict of relevant fields (project, file,
            input args). Anything JSON-serializable is fine.
        exc_info: if True, captures the current sys.exc_info() and
            stores its traceback string in the record.
        level: "error" | "warning" | "info" — for filtering only.

    Never raises. On disk failure, falls back to stderr."""
    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "component": component,
        "message": message,
        "version": _VERSION,
    }
    if context:
        # Filter to JSON-serializable values; coerce others to str.
        safe_ctx: dict[str, Any] = {}
        for k, v in context.items():
            try:
                json.dumps(v)
                safe_ctx[k] = v
            except (TypeError, ValueError):
                safe_ctx[k] = str(v)
        record["context"] = safe_ctx
    if exc_info:
        et, ev, tb = sys.exc_info()
        if et is not None:
            record["exception_type"] = et.__name__
            record["exception"] = str(ev) if ev else ""
            record["traceback"] = "".join(
                traceback.format_exception(et, ev, tb))

    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        # Last-resort: never let logging itself break the caller.
        sys.stderr.write(f"[error_log: write failed: {e}] {line}")


def read_recent(limit: int = 50, *,
                  component: Optional[str] = None,
                  level: Optional[str] = None,
                  since_ts: Optional[str] = None
                  ) -> list[dict[str, Any]]:
    """Return up to `limit` most-recent matching records.

    Reads the active log + .1/.2/.3 generations, newest first."""
    paths: list[Path] = []
    base = _log_path()
    if base.is_file():
        paths.append(base)
    for i in range(1, KEEP_GENERATIONS + 1):
        p = base.with_suffix(base.suffix + f".{i}")
        if p.is_file():
            paths.append(p)

    out: list[dict[str, Any]] = []
    for p in paths:
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        # Iterate newest-first within each file
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if component and rec.get("component", "").startswith(
                    component) is False:
                continue
            if level and rec.get("level") != level:
                continue
            if since_ts and rec.get("ts", "") < since_ts:
                continue
            out.append(rec)
            if len(out) >= limit:
                return out
    return out


def install_excepthook(component: str = "uncaught") -> None:
    """Install a sys.excepthook that logs every uncaught exception
    to the error log before the default handler runs. Call this
    once at the entry point of any long-running dejavu process
    (MCP server, CLI commands that do indexing work, etc.)."""
    prev = sys.excepthook

    def _hook(exc_type, exc_value, tb):
        try:
            log_error(component, f"uncaught {exc_type.__name__}: "
                                     f"{exc_value}",
                        exc_info=True, level="error")
        finally:
            prev(exc_type, exc_value, tb)

    sys.excepthook = _hook


def format_for_human(records: list[dict[str, Any]],
                       *, with_traceback: bool = False) -> str:
    """Render records for terminal display."""
    if not records:
        return "No errors logged."
    lines = []
    for r in records:
        ts = r.get("ts", "?")
        level = r.get("level", "?").upper()
        comp = r.get("component", "?")
        msg = r.get("message", "")
        head = f"[{ts}] {level} {comp}: {msg}"
        ver = r.get("version")
        if ver and ver != "unknown":
            head += f"  (v{ver})"
        lines.append(head)
        ctx = r.get("context")
        if ctx:
            for k, v in ctx.items():
                lines.append(f"    {k}: {v}")
        if r.get("exception_type"):
            lines.append(f"    {r['exception_type']}: "
                          f"{r.get('exception', '')}")
        if with_traceback and r.get("traceback"):
            for tl in r["traceback"].splitlines():
                lines.append(f"    | {tl}")
        lines.append("")
    return "\n".join(lines)


def log_path_str() -> str:
    """Where is the log? Used by `dejavu doctor` + the MCP tool's
    instruction text."""
    return str(_log_path())
