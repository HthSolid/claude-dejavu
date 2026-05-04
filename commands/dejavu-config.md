---
description: Read or change claude-dejavu settings (lint mode, auto-reindex, scope defaults, …)
argument-hint: list | get <KEY> | set <KEY> <VALUE> | reset <KEY>
---

Use the `dejavu_config` MCP tool to read/write claude-dejavu settings.

Args: $ARGUMENTS

Common operations:
  - `list`                          — show all known settings + current/default
  - `get LINT_MODE`                 — print the current value of a setting
  - `set LINT_MODE suggest`         — change a setting (validated against schema)
  - `reset LINT_MODE`               — revert a setting to its built-in default

Most-asked-about settings:

  LINT_MODE — PreToolUse lint behaviour on Edit/Write/MultiEdit:
    off       disabled
    warn      advisory only (default)
    suggest   advisory + actionable rename table
    block     refuse fabricated names
    autofix   refuse on any non-ok verdict, force re-issue with renames

  AUTO_REINDEX — automatically index a project's source files on first
                 SessionStart in that project (true/false)

  INJECT_SESSION_START — preamble injection on SessionStart (true/false)

  UPDATE_CHECK — check GitHub for newer plugin tags on SessionStart (true/false)

  CLAUDE_DEJAVU_DEFAULT_SCOPE — global / project / workspace

Settings take effect on the user's NEXT Claude Code session unless
otherwise noted. Connection settings (PG_HOST, WV_URL, ...) are
read-only via this tool — to change them, re-run scripts/install.py.

If the user asks to "turn off" or "enable" something, map to the
correct LINT_MODE/AUTO_REINDEX/etc. value before calling set.
