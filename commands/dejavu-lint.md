---
description: Lint a proposed file edit against the symbol index — flag fabricated names + typos before write
argument-hint: <file-path> [proposed-content]
---

Lint a proposed edit using the `dejavu_lint_proposed_edit` MCP tool from
the claude-dejavu plugin. The tool parses the content with tree-sitter,
extracts external references (function calls, class instantiations, type
references), and checks each against the project's symbol index.

Args: $ARGUMENTS

If the user provided just a file path:
  1. Read the file with the Read tool.
  2. Call dejavu_lint_proposed_edit(content=<file-content>, file_path=<path>).

If the user provided content inline (e.g. a heredoc), pass it directly
as the `content` argument.

Verdict semantics from the tool:
  - 'ok'    → all references confirmed; safe.
  - 'warn'  → close-typo matches found; suggest the corrected name.
  - 'block' → at least one fabricated reference; surface the unknowns
              and the closest candidates if any.

Always show the user any low-confidence matches with the receiver type
context (`via_receiver_type`) when present — this is the type-aware
lookup that catches `obj.wrongMethod()` even if the receiver type exists.
