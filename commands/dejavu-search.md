---
description: Hybrid (BM25 + vector) search across past Claude Code sessions
argument-hint: <query>
---

Use the `dejavu_search` MCP tool from the claude-dejavu plugin to find past
session content matching the query below.

Query: $ARGUMENTS

Default scope is the current project. To override pass `scope='global' |
'project:NAME' | 'workspace:NAME'` to the tool.

Show the user a compact list of hits (gist + session id + role + ts) and
offer to call `dejavu_expand` on specific turn IDs if they want full content.
