---
description: Find the best matching past Claude Code session and produce a ready-to-run resume command
argument-hint: <hint>
---

Use the `dejavu_resume` MCP tool from the claude-dejavu plugin to find the
most relevant prior session matching the hint and return a runnable
`claude --resume <uuid>` command.

Hint: $ARGUMENTS

Show the user:
  - The matching session's id, project, started_at, ai_title (if any),
    and a one-line gist of what it was about.
  - The exact `claude --resume <uuid>` command they can run to continue it.

If multiple sessions look relevant, surface the top 3 and let the user pick.
