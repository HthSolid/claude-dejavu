"""
Semantic intent — embedding-based fallback when lexical/AST channels fail.

The naming-drift case: trigram says `processUpdate` is similar to
`process_user_data` (sim=0.43, but they're unrelated), AND lexical can't
find `getDate` for a query like `fetchDate` (no shared trigrams). Embedding
similarity catches both.

We don't run this on every reference (would be 9k embeddings per session).
Instead it's an explicit fallback path: when symbol_grounding produces
an UNKNOWN issue and the user's lint mode is suggest/autofix, semantic
is consulted to enrich the rejection with a "but you might mean X
based on intent" candidate.

For v0.3.2.1 ship, the strategy emits diagnostic-only rows (so we can
measure how often it would have helped) without affecting the verdict.
A future version can promote semantic candidates into low_confidence
once we trust the threshold.
"""
from __future__ import annotations

from . import LintContext, LintStrategy, Issue, IssueSeverity, register


class SemanticIntentStrategy:
    name = "semantic_intent"
    languages = {"python", "typescript", "tsx", "javascript",
                 "go", "rust", "java", "ruby", "bash"}
    enabled_by_default = True

    def applies_to(self, ctx: LintContext) -> bool:
        return ctx.language in self.languages

    def check(self, ctx: LintContext) -> list[Issue]:
        try:
            from semantic_search import is_available, search
        except Exception:
            return []
        if not is_available():
            return []

        issues: list[Issue] = []
        # For each referenced bare name not already resolved by the lexical
        # symbol-grounding pass, run a semantic lookup. Diagnostic-only.
        for name in sorted(ctx.referenced):
            # Skip very short names — embeddings are noisy below ~4 chars
            if len(name) < 4:
                continue
            hits = search(name, ctx.project_slug, kind="symbol", limit=2)
            if not hits:
                continue
            top = hits[0]
            sim = top.get("similarity", 0.0)
            if sim < 0.6 or top.get("name") == name:
                continue
            issues.append(Issue(
                strategy=self.name,
                severity=IssueSeverity.LOW_CONFIDENCE,
                name=name,
                candidate=top["name"],
                confidence=float(sim),
                metadata={
                    "channel": "semantic",
                    "cosine_similarity": round(sim, 3),
                    "file_path": top.get("file_path"),
                    "language": top.get("language"),
                },
                reason=(f"semantic match: '{top['name']}' (cosine={sim:.2f}). "
                        f"Diagnostic-only — verify before applying."),
                fix=f"if you meant '{top['name']}', rename '{name}' → '{top['name']}'",
            ))
        return issues


register(SemanticIntentStrategy())
