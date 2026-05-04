---
description: Semantic search across symbols + routes by intent (cosine similarity via Weaviate embeddings)
argument-hint: <intent>  [--kind=symbol|route]
---

Use the `dejavu_resolve_semantic` MCP tool from the claude-dejavu
plugin to find code by *intent* rather than exact name. Backed by
Weaviate + the bundled text2vec-transformers model.

Args: $ARGUMENTS

Use cases:
  - "function that parses ISO dates" → finds `parseDate`,
    `from_isoformat`, etc. even when their names differ from the query.
  - "endpoint to create a user" → finds `POST /api/users`,
    `POST /admin/users` ranked by intent similarity.
  - "feature flag for dark mode" → finds `darkModeEnabled`,
    `enableDarkMode`, `theme.dark`, etc.

This is the SEMANTIC channel — complementary to `dejavu_resolve_symbol`
(lexical / fuzzy on names) and `dejavu_resolve_route` (path matching).
Use it when the user describes what they want by behavior rather than
by name. Treat `cosine < 0.6` as weak.

Requires Weaviate + the transformers container running. If not
available, the tool returns an explanatory message rather than failing.
