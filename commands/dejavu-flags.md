---
description: Check or browse feature flag declarations (flags.json, feature-flags.ts, LaunchDarkly, Unleash, GrowthBook) — catches typos and fabricated keys
argument-hint: <flag-key>  |  --list  [--source=NAME] [--kill-switches-only]
---

Use the claude-dejavu plugin to either:

1. **Check before referencing a flag key** — `dejavu_check_flag(flag_key)`.
   USE THIS BEFORE writing
   `client.variation('key', user)` / `isEnabled('key')` /
   `Statsig.checkGate('key')` / `posthog.isFeatureEnabled('key')` /
   `gb.feature('key')` etc.

   - ✓ exact in canonical config — declared, safe to use.
   - exact in source-read only — used elsewhere but never declared canonically.
     Add to `flags.json` / `featureFlags.ts` so removals show up in diffs.
   - ~ fuzzy — close match (typo); rename to candidate.
   - no match — fabricated; pick the right key OR declare it.

2. **Browse declared flags** — `dejavu_feature_flags(source?, kill_switches_only?)`.
   Survey `flags.json`, `featureFlags.ts`, LaunchDarkly exports,
   Unleash configs, GrowthBook exports. The `kill_switches_only`
   filter shows only flags whose key matches kill/emergency/disable
   /panic/circuit heuristic — useful when verifying critical-path
   gates exist before merging code that depends on them.

Args: $ARGUMENTS

This is the flags companion to `/dejavu-routes`, `/dejavu-pages`,
`/dejavu-env`, `/dejavu-db`, and `/dejavu-graphql`. Six grounding
domains; one consistent surface.
