---
description: Detect + recover corrupt or truncated Claude Code session JSONLs from dejavu's DB
argument-hint: [<session-uuid>]  or  --list  or  --all
---

The user is asking you to investigate and/or recover a corrupted or
truncated Claude Code session.

Use the bash command:

  claude-dejavu repair $ARGUMENTS

If $ARGUMENTS is empty or "--list", show the table of corrupt/truncated
sessions. If a UUID is provided, run the reconstruction. A
`.pre-repair.<unix-ts>.jsonl` backup is created automatically next to
the live file before any modification.

After repair, suggest the user `--resume` the session from Claude Code.
