#!/usr/bin/env python3
"""
claude-dejavu PreToolUse hook (v0.3.1).

Wired against Claude Code's PreToolUse hook for Edit / Write / MultiEdit.
Reads the tool's input JSON from stdin, extracts the proposed file content,
runs `lint_proposed_edit` against the symbol index, and emits a JSON
response that either:

  - allows the write silently (verdict 'ok'),
  - emits a systemMessage warning (verdict 'warn' — likely typo), or
  - emits a systemMessage block (verdict 'block' — likely fabricated name).

Hook input contract (Claude Code PreToolUse JSON):
  {
    "tool_name": "Edit" | "Write" | "MultiEdit",
    "tool_input": {
      "file_path": "...",
      "content": "..."           # for Write
      "new_string": "...",       # for Edit
      "old_string": "...",
      "edits": [...],            # for MultiEdit
    },
    "session_id": "...",
    "cwd": "..."
  }

Hook response contract:
  {"systemMessage": "...", "decision": "approve"|"block"} (block only when
  configured in autofix mode; for v0.3.1 default is warn-only — never blocks
  the user, just surfaces).

Configuration knobs (env, can also live in ~/.local/share/claude-dejavu/config.env):
  CLAUDE_DEJAVU_LINT_MODE:
    'off'     — disable hook entirely.
    'warn'    — (default) never blocks, only annotates with systemMessage.
    'suggest' — never blocks; systemMessage explicitly lists the proposed
                rename(s) so Claude can re-issue the write with corrections.
    'autofix' — blocks on any non-ok verdict and tells Claude to rewrite
                with the corrections applied. The hook protocol doesn't
                allow us to rewrite tool input directly, so 'autofix'
                = "block + emit the exact replacement table". Claude reads
                the systemMessage, fixes, and re-issues.
    'block'   — block on verdict='block' (fabricated names) only.
                Doesn't block on warn-level (low-confidence typo) cases.

  CLAUDE_DEJAVU_LINT_TIMEOUT_S: max seconds the hook should spend (default 5).

Recursion guard: CLAUDE_DEJAVU_NESTED=1 skips, same as the other hooks.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _silent_pass():
    """Print empty JSON and exit. The hook never blocks Claude on its own
    failures — symbol-grounding is a best-effort assist, not a gate."""
    print(json.dumps({}))
    sys.exit(0)


def main():
    if os.environ.get("CLAUDE_DEJAVU_NESTED") == "1":
        _silent_pass()
    mode = (os.environ.get("CLAUDE_DEJAVU_LINT_MODE") or "warn").strip().lower()
    if mode == "off":
        _silent_pass()
    timeout_s = float(os.environ.get("CLAUDE_DEJAVU_LINT_TIMEOUT_S") or 5.0)

    raw = sys.stdin.read()
    if not raw.strip():
        _silent_pass()
    try:
        payload = json.loads(raw)
    except Exception:
        _silent_pass()

    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    if tool_name not in ("Write", "Edit", "MultiEdit"):
        _silent_pass()

    file_path = tool_input.get("file_path") or ""
    if not file_path:
        _silent_pass()

    # Build the *proposed full content* for the linter.
    proposed: str | None = None
    if tool_name == "Write":
        proposed = tool_input.get("content")
    elif tool_name == "Edit":
        # We only have the diff, not the full post-edit file. Read the current
        # file and apply the edit in-memory.
        new_string = tool_input.get("new_string") or ""
        old_string = tool_input.get("old_string") or ""
        try:
            current = Path(file_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            current = ""
        if old_string and old_string in current:
            proposed = current.replace(old_string, new_string, 1)
        else:
            # File doesn't exist yet (Edit-as-create) — use the new_string alone.
            proposed = new_string
    elif tool_name == "MultiEdit":
        edits = tool_input.get("edits") or []
        try:
            current = Path(file_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            current = ""
        proposed = current
        for e in edits:
            old_s = e.get("old_string") or ""
            new_s = e.get("new_string") or ""
            replace_all = bool(e.get("replace_all"))
            if not old_s or old_s not in proposed:
                continue
            if replace_all:
                proposed = proposed.replace(old_s, new_s)
            else:
                proposed = proposed.replace(old_s, new_s, 1)

    if proposed is None or not proposed.strip():
        _silent_pass()

    # Heuristic: don't lint very large files (probably auto-generated, vendored, etc.)
    if len(proposed) > 250_000:
        _silent_pass()

    # Wire up the import paths and DB connection. The hook is invoked by Claude
    # Code, so we can't assume our venv's sys.path. Discover via DATA_ROOT.
    data_root_env = os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
    if data_root_env:
        data_root = Path(data_root_env)
    elif sys.platform == "darwin":
        data_root = Path.home() / "Library" / "Application Support" / "claude-dejavu"
    elif sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home() / "AppData/Local")
        data_root = Path(base) / "claude-dejavu"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        data_root = Path(xdg) / "claude-dejavu" if xdg else Path.home() / ".local/share/claude-dejavu"

    plugin_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(plugin_root / "code"))

    # Read PG creds from config.env
    cfg = {}
    cfg_file = data_root / "config.env"
    if cfg_file.exists():
        for line in cfg_file.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"): continue
            if "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    os_user = os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"
    pg_db = cfg.get("PG_DB") or f"claude_dejavu_{os_user}"

    started = time.perf_counter()
    try:
        import psycopg2  # type: ignore
        conn = psycopg2.connect(
            host=cfg.get("PG_HOST", "localhost"),
            port=cfg.get("PG_PORT", "5432"),
            user=cfg.get("PG_USER", "postgres"),
            password=cfg.get("PG_PASS", "postgres"),
            dbname=pg_db,
            connect_timeout=2,
        )
        conn.autocommit = True
        from lint import lint_proposed_edit  # type: ignore
        # Project slug = cwd basename (Claude Code passes cwd in payload)
        cwd = payload.get("cwd") or os.getcwd()
        project_slug = os.path.basename(os.path.realpath(cwd))

        # Graceful skip: if the project has zero indexed symbols, the lint
        # would flag every reference as "fabricated" — wrong and noisy.
        # This is the normal state during the first SessionStart of a
        # fresh project, while session_start.py's auto-reindex is still
        # running in the background. Silently pass; the lint will start
        # working as soon as the index has data.
        with conn.cursor() as _cur:
            _cur.execute(
                "SELECT count(*) FROM symbols WHERE project_slug = %s",
                (project_slug,),
            )
            symbol_count = _cur.fetchone()[0]
        if symbol_count == 0:
            sys.stderr.write(
                f"claude-dejavu lint hook: project '{project_slug}' not yet "
                f"indexed (0 symbols). Skipping — index will be built in the "
                f"background.\n"
            )
            _silent_pass()

        result = lint_proposed_edit(proposed, file_path, project_slug=project_slug, conn=conn)
    except Exception as e:
        # Best-effort: never break the user's workflow because of a hook failure.
        sys.stderr.write(f"claude-dejavu lint hook: skipped ({type(e).__name__}: {e})\n")
        _silent_pass()

    elapsed = time.perf_counter() - started
    if elapsed > timeout_s:
        sys.stderr.write(f"claude-dejavu lint hook: exceeded timeout ({elapsed:.1f}s > {timeout_s}s)\n")

    summary = result["summary"]
    verdict = summary["verdict"]
    if verdict == "ok":
        _silent_pass()

    # Replacements table (suggest / autofix modes use this prominently).
    # Only includes high-confidence rename targets — never the unknown set
    # (those have no defensible auto-fix).
    replacements: list[tuple[str, str]] = []
    for lc in result["low_confidence"]:
        c = lc.get("top_candidate") or {}
        if c.get("name"):
            replacements.append((lc["name"], c["name"]))

    # Build a compact systemMessage. Keep it short — Claude reads everything.
    msg_lines = [f"[claude-dejavu lint] verdict={verdict.upper()} mode={mode} file={file_path}"]
    if result["unknown"]:
        msg_lines.append(f"  Unknown ({len(result['unknown'])}) — likely fabricated:")
        for u in result["unknown"][:6]:
            c = u.get("top_candidate")
            via = f" [receiver: {u['via_receiver_type']}]" if u.get("via_receiver_type") else ""
            note = f"  note: {u['note']}" if u.get("note") else ""
            if c:
                msg_lines.append(f"    ? {u['name']}  closest: {c['name']} sim={c['trigram_sim']} ed={c['edit_distance']}{via}{note}")
            else:
                msg_lines.append(f"    ? {u['name']}  (no candidates){via}{note}")
    if result["low_confidence"]:
        msg_lines.append(f"  Low-confidence ({len(result['low_confidence'])}) — likely typo:")
        for lc in result["low_confidence"][:6]:
            c = lc["top_candidate"]
            msg_lines.append(f"    ~ {lc['name']} -> {c['name']} sim={c['trigram_sim']} ed={c['edit_distance']}")

    # Mode-specific tail
    if mode in ("suggest", "autofix") and replacements:
        msg_lines.append("")
        msg_lines.append("APPLY THESE CORRECTIONS before re-issuing the write:")
        for old, new in replacements:
            msg_lines.append(f"  - rename '{old}' → '{new}'")
        if mode == "autofix":
            msg_lines.append("")
            msg_lines.append("autofix mode: the write was BLOCKED. Re-issue with the renames above.")
            if result["unknown"]:
                msg_lines.append("Unknown names listed above must be resolved first — call dejavu_resolve_symbol or remove the references.")
    else:
        msg_lines.append("  Tip: call dejavu_resolve_symbol to verify or correct names before re-attempting.")

    msg = "\n".join(msg_lines)

    response: dict = {"systemMessage": msg}

    # Decision logic
    #   warn / suggest / off  → never block (decision omitted = approve silently)
    #   block                  → block on verdict=block
    #   autofix                → block on verdict in {warn, block} so any
    #                            non-ok lint forces a re-issue with renames.
    if mode == "block" and verdict == "block":
        response["decision"] = "block"
        response["reason"] = msg
    elif mode == "autofix" and verdict in ("warn", "block"):
        response["decision"] = "block"
        response["reason"] = msg

    print(json.dumps(response))
    sys.exit(0)


if __name__ == "__main__":
    main()
