---
description: List recent Claude Code sessions in the current scope (project / workspace / global)
argument-hint: [--scope=global|project|workspace] [--limit=N]
---

Use the `dejavu_sessions` MCP tool from the claude-dejavu plugin to list
recent sessions ingested into the dejavu index.

Args: $ARGUMENTS

Default behavior:
  - Scope = current project (cwd basename).
  - Limit = 20 most recent.

Output a compact table with: started_at, ai_title (if any), project_slug,
total_turns, session_id (truncated). Group by project if scope=global.

If the user is hunting for a specific session to resume, suggest
`/dejavu-resume <hint>` instead — it does the LLM-driven matching for them.
