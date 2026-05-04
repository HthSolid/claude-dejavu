---
description: List or restore deleted Claude Code session .jsonl files from the off-tree backup
argument-hint: [--list | <session-uuid> | --all]
---

Use the `dejavu_restore` MCP tool from the claude-dejavu plugin.

Args: $ARGUMENTS

Behavior:
  - No args / `--list`: show every session in the backup that is missing
    from `~/.claude/projects/<slug>/<uuid>.jsonl` on the live system —
    these are the recovery candidates.
  - `<session-uuid>`: restore that specific session byte-perfect from
    the off-tree mirror. Verify SHA256 after restore.
  - `--all`: restore everything missing.

If a session looks like an active live one (size > 0 and recent mtime),
warn before overwriting.
