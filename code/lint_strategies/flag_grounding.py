"""
Feature-flag grounding — verifies flag-key references against the
project's feature_flags index.

Catches:
  client.variation('checkout-flow-v2', user)           ← key declared somewhere
  client.variation('checkout-flow-v3', user)           ← typo of v2 (low_conf)
  client.variation('totaly-fabriated-key', user)       ← unknown (block)
  isEnabled('beta-rollout')                            ← key only seen in source-read
                                                          (low_conf — declare it)

The strategy is essentially the env-var grounding pattern, applied to
flag keys instead of `process.env.X` reads.
"""
from __future__ import annotations

from . import LintContext, LintStrategy, Issue, IssueSeverity, register


_CANONICAL_SOURCES = {"flags-json", "feature-flags-ts", "launchdarkly",
                       "unleash", "growthbook"}


class FlagGroundingStrategy:
    name = "flag_grounding"
    languages = {"typescript", "tsx", "javascript", "python"}
    enabled_by_default = True

    def applies_to(self, ctx: LintContext) -> bool:
        return ctx.language in self.languages

    def check(self, ctx: LintContext) -> list[Issue]:
        from flag_indexer import (extract_flag_reads_from_text, resolve_flag)

        # Bail early on empty index.
        with ctx.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM feature_flags WHERE project_slug = %s",
                        (ctx.project_slug,))
            if (cur.fetchone()[0] or 0) == 0:
                return []

        keys = extract_flag_reads_from_text(ctx.content)
        if not keys:
            return []

        issues: list[Issue] = []
        seen: set[str] = set()
        for flag_key, line_no in sorted(keys):
            if flag_key in seen:
                continue
            seen.add(flag_key)
            hits = resolve_flag(flag_key, ctx.project_slug, ctx.conn,
                                 fuzzy=True, limit=5)
            if not hits:
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.UNKNOWN,
                    name=f"flag:{flag_key}",
                    candidate=None,
                    confidence=0.92,
                    metadata={"flag_key": flag_key, "line": line_no},
                    reason=(f"feature flag `{flag_key}` is not declared in "
                            f"flags.json, feature-flags.ts, LaunchDarkly, "
                            f"Unleash, or GrowthBook config — and is not "
                            f"used elsewhere in source"),
                    fix=(f"declare `{flag_key}` in your flags config, "
                         f"or check the actual key name in your provider"),
                ))
                continue
            top = hits[0]
            if top["match_kind"] == "exact" and top["source"] in _CANONICAL_SOURCES:
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.OK,
                    name=f"flag:{flag_key}",
                    candidate=top["flag_key"],
                    confidence=1.0,
                    metadata={"flag_key": flag_key, "source": top["source"],
                              "is_kill_switch": top.get("is_kill_switch"),
                              "line": line_no},
                    reason=(f"declared in {top['source']} at "
                            f"{top['source_file']}:{top['start_line']}"),
                ))
                continue
            if top["match_kind"] == "exact" and top["source"] == "source-read":
                # Used elsewhere but never declared canonically. Look for
                # a fuzzy canonical neighbor — if one exists, this is
                # likely a typo replicated across files.
                canonical = resolve_flag(
                    flag_key, ctx.project_slug, ctx.conn, fuzzy=True,
                    limit=5, exclude_sources=("source-read",),
                )
                fuzzy_canonical = next(
                    (h for h in canonical
                     if h["match_kind"] == "fuzzy"
                     and h.get("trigram_sim", 0) >= 0.6),
                    None,
                )
                if fuzzy_canonical:
                    issues.append(Issue(
                        strategy=self.name,
                        severity=IssueSeverity.LOW_CONFIDENCE,
                        name=f"flag:{flag_key}",
                        candidate=fuzzy_canonical["flag_key"],
                        confidence=float(fuzzy_canonical.get("trigram_sim", 0)),
                        metadata={"flag_key": flag_key,
                                  "candidate": fuzzy_canonical["flag_key"],
                                  "diagnosis": "typo_replicated",
                                  "line": line_no},
                        reason=(f"`{flag_key}` is read in source but never "
                                f"declared canonically; close match "
                                f"`{fuzzy_canonical['flag_key']}` exists in "
                                f"{fuzzy_canonical['source']}"),
                        fix=(f"replace `{flag_key}` with "
                             f"`{fuzzy_canonical['flag_key']}` throughout"),
                    ))
                else:
                    issues.append(Issue(
                        strategy=self.name,
                        severity=IssueSeverity.LOW_CONFIDENCE,
                        name=f"flag:{flag_key}",
                        candidate=flag_key,
                        confidence=0.55,
                        metadata={"flag_key": flag_key,
                                  "diagnosis": "undeclared_but_used",
                                  "line": line_no},
                        reason=(f"`{flag_key}` is referenced in source but "
                                f"not declared in flags.json / "
                                f"feature-flags.ts / LD / Unleash / GrowthBook"),
                        fix=(f"add `{flag_key}` to your canonical flags "
                             f"config so removals show up in lint"),
                    ))
                continue
            if top["match_kind"] == "fuzzy":
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.LOW_CONFIDENCE,
                    name=f"flag:{flag_key}",
                    candidate=top["flag_key"],
                    confidence=float(top.get("trigram_sim", 0)),
                    metadata={"flag_key": flag_key,
                              "candidate": top["flag_key"],
                              "candidate_source": top["source"],
                              "trigram_sim": round(top.get("trigram_sim", 0), 3),
                              "diagnosis": "typo",
                              "line": line_no},
                    reason=(f"close match `{top['flag_key']}` in "
                            f"{top['source']} (sim={top.get('trigram_sim', 0):.2f})"),
                    fix=f"change `{flag_key}` to `{top['flag_key']}`",
                ))
        return issues


register(FlagGroundingStrategy())
