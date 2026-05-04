---
description: Diagnose claude-dejavu install + runtime health (Postgres, Weaviate, venv, hooks, symbol index)
argument-hint: [--fix]
---

Use the `dejavu_doctor` MCP tool from the claude-dejavu plugin to run a
full diagnostic. It checks every layer the plugin depends on:

  - INSTALL_STATUS marker (was the auto-installer interrupted?)
  - bundled venv at the right path with all deps importable
  - Postgres reachable + schema applied
  - Weaviate /v1/meta responsive + correct shape
  - symbol index populated for at least one project
  - hooks/hooks.json declares all 5 expected events

Args: $ARGUMENTS

Show the user the human-readable report. For each failed or warning
check, surface the 'fix' instruction prominently — it's a one-shot
command (run `claude-dejavu doctor --fix` for safe auto-recovery, or
the per-check `fix:` field for granular fixes).

If overall status is 'ok' just confirm everything is green. If 'fail',
prioritize the highest-impact failure first (usually install_status or
venv) and walk the user through the fix.
