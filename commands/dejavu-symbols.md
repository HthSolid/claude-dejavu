---
description: Resolve a code symbol against the project's indexed symbol database (catches hallucinated names)
argument-hint: <symbol-name>
---

Use the `dejavu_resolve_symbol` MCP tool from the claude-dejavu plugin to
look up the following symbol in the current project's index. The tool does
fuzzy matching with edit-distance + trigram similarity, so misspelled or
case-drifted names will still surface the real symbol.

Symbol: $ARGUMENTS

If the tool returns:
  - One ed=0 hit → that's the real symbol; report file:line.
  - Multiple hits → list them with their kind, file path, and similarity.
  - No hits → the name doesn't exist in the index. The user is either
    referring to something not yet indexed (suggest `claude-dejavu reindex`)
    or about to fabricate a name. Flag explicitly.

If the user just wants to browse rather than resolve, suggest the
`dejavu_symbols` tool instead (paginated listing).
