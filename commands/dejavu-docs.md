---
description: Search project documentation embeddings via dejavu (docs-only)
argument-hint: <query>  [workspace=NAME]
---

The user is asking you to search the project's documentation embeddings.

Use the MCP tool `dejavu_search` with `kinds=["doc"]`. Pass through the
user's query verbatim. Default scope is the current repo; if the user
mentions a workspace name, pass `workspace=...`.

Query: $ARGUMENTS

After the search returns hits, present them as:

  [score] HEADING_PATH
      file_path → one_line_summary

If the user expresses interest in a specific hit, call `dejavu_expand`
with the `result_id` and `level="section"`. Use `level="file"` only if
the user asks for the whole document.

If `dejavu_search` returns no hits, suggest the user verify documents
have been embedded with: `claude-dejavu reindex --docs`.
