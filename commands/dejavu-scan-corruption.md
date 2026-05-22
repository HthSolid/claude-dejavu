---
description: Scan for zeroed / partial-zero / shrunk files in the current project (or all known projects)
argument-hint: [--path <dir>]  or  --all-projects  or  --include "<glob>" --exclude "<glob>" [--verbose] [--json]
---

The user is asking you to scan a project tree for file corruption signals
(zeroed bytes, partial NUL runs, snapshot-vs-on-disk size collapse).

Use the bash command:

  claude-dejavu scan-corruption $ARGUMENTS

Default scope is the current working directory (recursive). Pass
`--all-projects` to walk every distinct `project_path` value in the dejavu
`sessions` table (skipping `_orphan` sessions). `--include "<glob>"` and
`--exclude "<glob>"` are repeatable; sensible default excludes apply
(`node_modules/`, `.git/`, `dist/`, `build/`, `target/`, `.next/`,
`__pycache__/`).

Exit codes:
- 0 — zero actionable findings (or only UNTRACKED_CHANGE in `--verbose`)
- 1 — at least one ZEROED / PARTIAL_ZERO / SHRUNK finding

For each actionable finding suggest:

  claude-dejavu recover-file <path> [--session <uuid>]

(See `/dejavu-recover-file` for the recovery workflow.)
