---
description: (Re)index the current project's source files into the symbol grounding database
argument-hint: [--path=DIR] [--project=SLUG] [--force]
---

Run the `claude-dejavu reindex` CLI to (re)build the symbol index for
the current project. Indexing covers Python, TypeScript, TSX, JavaScript,
Go, Rust, Java, Ruby, and Bash via tree-sitter — this is the data the
PreToolUse lint hook and `dejavu_resolve_symbol` MCP tool use to ground
LLM-generated code against real names.

Args: $ARGUMENTS

Run it from the Bash tool:

```bash
claude-dejavu reindex $ARGUMENTS
```

Defaults (with no args):
  - --path = current working directory
  - --project = basename of cwd (so two projects with the same shape
    don't collide)
  - Incremental: only files whose mtime+size changed since the last
    scan are re-parsed.

Pass `--force` after large refactors or branch switches to invalidate
the per-file mtime cache and re-parse everything.

After reindex, suggest the user try `/dejavu-symbols <name>` to verify
a symbol they care about is now resolvable.
