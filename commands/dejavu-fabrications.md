---
description: List + triage names the rewriter has flagged as RESIDUAL/REPLACE — declare, blacklist, or leave open
argument-hint: [--status=open|declared|blacklisted] [--mark NAME DOMAIN STATUS]
---

Use the claude-dejavu plugin's per-project fabrication memory to
review names the rewriter cascade has flagged across past
generations.

Three triage states:
  - **open**         — not yet triaged (default for newly recorded names)
  - **declared**     — user has added it to `.env.example` / schema /
                       flags.json, etc., so the rewriter will find it
                       via the manifest going forward
  - **blacklisted**  — intentional reject; the rewriter
                       short-circuits the cascade entirely on next
                       encounter, never proposing the name again

Sample flow:

  /dejavu-fabrications                 # list open fabrications
  /dejavu-fabrications --min-count=3   # only show repeat offenders
  /dejavu-fabrications --mark LAUNCHDARKLY_SDK_KEY env declared
  /dejavu-fabrications --mark FAKE_KEY env blacklisted

After marking a name `declared`, run `/dejavu-env propose
LAUNCHDARKLY_SDK_KEY --apply` (if it's an env var) so the project's
canonical source actually contains it.

Args: $ARGUMENTS
