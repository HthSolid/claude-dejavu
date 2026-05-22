---
description: Print the last N CHANGELOG release headlines for a repo — passive context priming (v0.8.4)
argument-hint: [--repo PATH] [--last N] [--json]
---

The user is asking what's shipped recently in a repo. Reads
`<repo>/CHANGELOG.md` plus the most recent `chore(release): vX.Y.Z` git
commits and prints a compact summary of the last N releases.

Use the bash command:

  claude-dejavu what-shipped $ARGUMENTS

Default output is one paragraph per version (max 6 wrapped lines):

```
v0.8.6 — 2026-05-18  (5f3d65a)
  Added (Context Economy phase 3 — distill + budget + trash gate):
  code/distill_pipeline.py — claude-dejavu's fidelity wrapper over
  the closed-source dejavu_bridge.distill primitive. Pre-extracts
  load-bearing tokens (paths, URLs, port numbers, version strings,
  …) and splices them back as a leading [anchors: …] block.
```

Flags:

- `--repo PATH` — repo root containing `CHANGELOG.md` (default: cwd).
- `--last N` — how many releases to show (default: 10).
- `--json` — emit a JSON array of `{version, date, commit, summary}`
  instead of human text.

Works without git on `PATH` — `commit` becomes `null` in that mode.

Use cases:

- Drop into a SessionStart preamble to remind the user (and Claude
  Code) what they shipped this week.
- Diff your mental model against ground truth before a planning
  session.
- Power external tooling via `--json` (sparkline dashboards,
  Slack release-digest bots, etc.).

Related:

- `claude-dejavu memory record-version` — distill one CHANGELOG
  section into the dejavu-owned `DATA_ROOT/versions/` so the
  SessionStart digest hook can surface it across projects.
- `hooks/session_start_digest.py` — the cross-project release
  digest hook that consumes `DATA_ROOT/versions/`.

Exit codes: 0 ok, 1 missing `CHANGELOG.md`, 2 argparse error.
