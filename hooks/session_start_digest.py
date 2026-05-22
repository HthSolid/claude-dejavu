#!/usr/bin/env python3
"""claude-dejavu SessionStart digest hook (v0.8.4 G1.c).

Reads `DATA_ROOT/versions/*.md` (written by `claude-dejavu memory
record-version`) and emits a compact `systemMessage` JSON blob with
the most-recent release one-liners across all projects we have a
record for. Up to 3 versions per project, hard-capped at ~30 output
lines / ~1500 tokens (≈6000 chars). Truncate-with-marker if exceeded.

Wired into hooks.json as a second SessionStart hook. The existing
hooks/session_start.py keeps doing prior-session preamble; this one
adds the cross-project release context strictly additively.

Deliberately stdlib-only — runs even before the venv is built, since
the input data is just a flat directory of markdown files.

Token budget enforcement is hard: if we exceed CHAR_CAP, we
truncate-and-mark. The cap matches the SessionStart hook's existing
implicit budget (one-screen preamble) so two SessionStart hooks
together still fit in the IDE's compact preamble area.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


CHAR_CAP = 6000           # ~1500 tokens hard cap
MAX_PROJECTS = 20         # don't blow the budget on someone with 100 repos
PER_PROJECT_MAX = 3       # last 3 releases per project


def _resolve_data_root() -> Path:
    override = os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support"
                / "claude-dejavu")
    if sys.platform.startswith("win"):
        base = (os.environ.get("LOCALAPPDATA")
                or os.environ.get("APPDATA")
                or str(Path.home() / "AppData" / "Local"))
        return Path(base) / "claude-dejavu"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) / "claude-dejavu" if xdg
            else Path.home() / ".local" / "share" / "claude-dejavu")


def _load_cfg() -> dict[str, str]:
    cfg: dict[str, str] = {}
    f = _resolve_data_root() / "config.env"
    if not f.exists():
        return cfg
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    except (OSError, UnicodeDecodeError):
        pass
    return cfg


# `- v0.8.3: anything` (allows leading whitespace before the dash).
_LINE_RE = re.compile(r"^\s*-\s+(v\d+\.\d+\.\d+[a-z]?:.*)$")
# Match the `project_<slug>_versions.md` filename pattern emitted by
# changelog_distill.record_version so we can pull the slug back out
# for display. Anything else in the dir is ignored.
_FNAME_RE = re.compile(r"^project_(?P<slug>.+)_versions\.md$")


def _collect_entries(versions_dir: Path) -> list[tuple[str, list[str]]]:
    """Return [(repo_slug, [line, line, ...])] sorted by the mtime of
    the per-project file, newest project first. Each line list is
    already truncated to PER_PROJECT_MAX and newest-first (input file
    is already in that order — see changelog_distill.record_version).
    """
    if not versions_dir.is_dir():
        return []
    items: list[tuple[float, str, list[str]]] = []
    try:
        files = list(versions_dir.iterdir())
    except OSError:
        return []
    for fp in files:
        m = _FNAME_RE.match(fp.name)
        if not m:
            continue
        try:
            mtime = fp.stat().st_mtime
            text = fp.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines: list[str] = []
        for ln in text.splitlines():
            mm = _LINE_RE.match(ln)
            if not mm:
                continue
            lines.append(mm.group(1).strip())
            if len(lines) >= PER_PROJECT_MAX:
                break
        if not lines:
            continue
        items.append((mtime, m.group("slug"), lines))
    items.sort(key=lambda x: x[0], reverse=True)
    return [(slug, lines) for _, slug, lines in items[:MAX_PROJECTS]]


def _format_digest(entries: list[tuple[str, list[str]]]) -> str:
    """Render the digest, hard-capped at CHAR_CAP. Truncate-and-mark
    if needed. Empty input → empty string (caller decides what to do
    with that)."""
    if not entries:
        return ""
    header = ("[claude-dejavu — recent releases across your projects "
                "(passive context priming)]")
    out_lines: list[str] = [header]
    overflow = False
    for slug, lines in entries:
        block: list[str] = [f"  {slug}:"]
        for ln in lines:
            block.append(f"    - {ln}")
        proposed = "\n".join(out_lines + block)
        if len(proposed) > CHAR_CAP:
            overflow = True
            break
        out_lines.extend(block)
    if overflow:
        out_lines.append("  … (truncated — token budget hit)")
    body = "\n".join(out_lines)
    # Final belt-and-suspenders cap. Should never trigger because we
    # check incrementally above, but guards against pathological
    # single-line oversize entries.
    if len(body) > CHAR_CAP:
        body = body[:CHAR_CAP - 30].rstrip() + "\n  … (truncated)"
    return body


def main() -> None:
    # Discard stdin — Claude Code hooks pass {session_id, transcript_path}
    # but we don't need either; the digest is project-agnostic.
    try:
        sys.stdin.read()
    except Exception:
        pass

    cfg = _load_cfg()
    # Honor the same global mute as session_start.py — if the user has
    # turned off SessionStart injection, don't surprise them with an
    # extra block from a second hook.
    if cfg.get("INJECT_SESSION_START", "true").lower() in (
            "0", "false", "no", "off"):
        print(json.dumps({"systemMessage": ""}))
        return

    versions_dir = _resolve_data_root() / "versions"
    entries = _collect_entries(versions_dir)
    digest = _format_digest(entries)
    print(json.dumps({"systemMessage": digest}))


if __name__ == "__main__":
    main()
