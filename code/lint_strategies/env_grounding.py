"""
Env-var grounding — verifies environment variable reads
(`process.env.X`, `os.getenv("X")`, `Deno.env.get("X")`,
`import.meta.env.X`, `System.getenv("X")`, `ENV["X"]`,
`std::env::var("X")`, `os.Getenv("X")`) against the project's
env_var_decls index.

Catches:
  - Fabricated names the project doesn't declare anywhere:
      `process.env.STRIPE_API_KEY` when the .env.example only declares
      STRIPE_SECRET_KEY.
  - Typos in the name:
      `process.env.DATABSE_URL` → DATABASE_URL.
  - Names read in source but never declared canonically (no
    .env.example / zod schema / docker-compose / GitHub Actions entry).
    Surfaced as LOW_CONFIDENCE so Claude knows to add a declaration.

Out of scope (future work):
  - Computed names (`process.env[\`PREFIX_${suffix}\`]`) — flagged as
    `interpolated` and skipped.
  - Per-environment overrides (.env.production-only vars).
"""
from __future__ import annotations

from . import LintContext, LintStrategy, Issue, IssueSeverity, register


_CANONICAL_SOURCES = {"env-example", "zod-schema", "envalid",
                       "docker-compose", "github-actions"}


class EnvGroundingStrategy:
    name = "env_grounding"
    languages = {"python", "typescript", "tsx", "javascript",
                  "go", "rust", "java", "ruby", "bash"}
    enabled_by_default = True

    def applies_to(self, ctx: LintContext) -> bool:
        return ctx.language in self.languages

    def check(self, ctx: LintContext) -> list[Issue]:
        from env_indexer import extract_env_reads_from_text, resolve_env_var

        # Bail early if the project has no env vars indexed — first-run
        # bootstrap window, same pattern as symbol_grounding.
        with ctx.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM env_var_decls WHERE project_slug = %s",
                        (ctx.project_slug,))
            if (cur.fetchone()[0] or 0) == 0:
                return []

        names = extract_env_reads_from_text(ctx.content, ctx.language)
        if not names:
            return []

        issues: list[Issue] = []
        for name in sorted(names):
            hits = resolve_env_var(name, ctx.project_slug, ctx.conn,
                                    fuzzy=True, limit=5)
            if not hits:
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.UNKNOWN,
                    name=f"process.env.{name}",
                    candidate=None,
                    confidence=0.92,
                    metadata={"var_name": name},
                    reason=(f"no project source declares env var `{name}` — "
                            f"not in .env.example, zod/envalid schema, "
                            f"docker-compose, or GitHub Actions"),
                    fix=(f"declare `{name}` in .env.example (and any zod env "
                         f"schema), or check the actual variable name in your "
                         f"deployment config"),
                ))
                continue

            top = hits[0]

            # Exact match in a CANONICAL source → resolved.
            if top["match_kind"] == "exact" and top["source"] in _CANONICAL_SOURCES:
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.OK,
                    name=f"process.env.{name}",
                    candidate=name,
                    confidence=1.0,
                    metadata={
                        "var_name": name,
                        "source": top["source"],
                        "source_file": top["source_file"],
                        "is_secret": top.get("is_secret", False),
                    },
                    reason=(f"declared in {top['source']} at "
                            f"{top['source_file']}:{top['start_line']}"),
                ))
                continue

            # Exact match but ONLY in source-read tier → declared nowhere.
            # Could be a real var that the project is using without
            # declaring (which is itself a bug Claude can fix), OR could
            # be a typo that's been replicated across files.
            if top["match_kind"] == "exact" and top["source"] == "source-read":
                # Re-query, this time excluding the source-read tier, to
                # see if any canonical source has a fuzzy match. If so,
                # the proposed name is likely a typo replicated across
                # files (the misspelling got cargo-culted into source).
                canonical_hits = resolve_env_var(
                    name, ctx.project_slug, ctx.conn, fuzzy=True, limit=5,
                    exclude_sources=("source-read", "dotenv-runtime"),
                )
                fuzzy_canonical = next(
                    (h for h in canonical_hits
                     if h["match_kind"] == "fuzzy"
                     and h.get("trigram_sim", 0) >= 0.6),
                    None,
                )
                if fuzzy_canonical:
                    issues.append(Issue(
                        strategy=self.name,
                        severity=IssueSeverity.LOW_CONFIDENCE,
                        name=f"process.env.{name}",
                        candidate=fuzzy_canonical["name"],
                        confidence=float(fuzzy_canonical.get("trigram_sim", 0)),
                        metadata={
                            "var_name": name,
                            "candidate": fuzzy_canonical["name"],
                            "candidate_source": fuzzy_canonical["source"],
                            "trigram_sim": round(
                                fuzzy_canonical.get("trigram_sim", 0), 3
                            ),
                            "diagnosis": "typo_replicated",
                        },
                        reason=(f"`{name}` is read in source but never declared "
                                f"canonically; close match `{fuzzy_canonical['name']}` "
                                f"exists in {fuzzy_canonical['source']} "
                                f"(sim={fuzzy_canonical.get('trigram_sim', 0):.2f}) "
                                f"— likely a typo replicated across files"),
                        fix=(f"replace `{name}` with `{fuzzy_canonical['name']}` "
                             f"throughout, or add `{name}` to .env.example if "
                             f"it's actually a different variable"),
                    ))
                else:
                    issues.append(Issue(
                        strategy=self.name,
                        severity=IssueSeverity.LOW_CONFIDENCE,
                        name=f"process.env.{name}",
                        candidate=name,
                        confidence=0.55,
                        metadata={
                            "var_name": name,
                            "diagnosis": "undeclared_but_used",
                        },
                        reason=(f"`{name}` is used in source code but is not "
                                f"declared in .env.example, zod schema, "
                                f"docker-compose, or GitHub Actions"),
                        fix=(f"add `{name}` to .env.example with a default "
                             f"or comment, and to any zod env schema"),
                    ))
                continue

            # Fuzzy-only match → typo of a known canonical var.
            if top["match_kind"] == "fuzzy":
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.LOW_CONFIDENCE,
                    name=f"process.env.{name}",
                    candidate=top["name"],
                    confidence=float(top.get("trigram_sim", 0)),
                    metadata={
                        "var_name": name,
                        "candidate": top["name"],
                        "candidate_source": top["source"],
                        "trigram_sim": round(top.get("trigram_sim", 0), 3),
                        "diagnosis": "typo",
                    },
                    reason=(f"close match `{top['name']}` in {top['source']} "
                            f"at {top['source_file']}:{top['start_line']} "
                            f"(sim={top.get('trigram_sim', 0):.2f})"),
                    fix=f"change `{name}` to `{top['name']}` if that's what you meant",
                ))

        return issues


register(EnvGroundingStrategy())
