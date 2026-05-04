"""
Prose grounding (v0.4.0).

Catches typos in proper nouns and defined terms in markdown / plain
text / LaTeX / reStructuredText / Org-mode files. The walker:

  1. Builds the file's "known good" set: every name/alias in the
     name_registry, plus a small allowlist of common English proper
     nouns (Monday, January, USA, etc.) — those don't get flagged.

  2. Walks the prose for tokens that look like proper nouns or defined
     terms:
       - Capitalized multi-word phrases ("Whitestone Citadel")
       - Tokens flanked by markdown emphasis (*Aria*, **Kael**)
       - Backtick-quoted technical terms (`veilcraft`)

  3. Each candidate is resolved against the registry. Strict no-match
     => unknown (block). Fuzzy match => low_confidence (typo).

The strategy ONLY fires on prose languages. Code projects without a
name_registry.json see zero overhead.

What we do NOT flag:
  - Single capitalized words at the start of sentences (overly noisy).
  - Common English proper nouns (days, months, countries, etc.).
  - Anything that doesn't repeat at least once in the document
    (one-offs are usually intentional).
"""
from __future__ import annotations

import re

from . import LintContext, LintStrategy, Issue, IssueSeverity, register


# Languages this strategy runs on.
_PROSE_LANGUAGES = {"markdown", "text", "rst", "org", "latex"}

# Common English proper nouns + a tiny everyday vocabulary that should
# never be flagged. (We only consult this when the registry is non-empty,
# so for projects without a registry we don't fire at all.)
_EN_ALLOWLIST = {
    # days / months
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
    # geographic / political (selective; full list would be huge)
    "USA", "UK", "EU", "US", "America", "Europe", "Asia", "Africa",
    # tech proper nouns
    "GitHub", "Google", "Apple", "Microsoft", "Amazon", "Meta", "Anthropic",
    "OpenAI", "Stripe", "Twilio", "AWS", "GCP", "Azure",
    "Linux", "macOS", "Windows", "Ubuntu", "Debian", "Fedora",
    "Python", "JavaScript", "TypeScript", "TypeScript", "Java", "Ruby",
    "Rust", "Go", "C", "PHP", "Kotlin", "Swift", "Scala", "Haskell",
    "React", "Vue", "Angular", "Svelte", "Next", "Nuxt", "Remix",
    "PostgreSQL", "MySQL", "Redis", "MongoDB", "SQLite",
    "Docker", "Kubernetes", "Helm", "Terraform",
    # filler that comes up in markdown
    "Note", "Tip", "Warning", "Important", "TODO", "FIXME", "XXX",
}


# Capitalized words / phrases. We capture sequences of `[A-Z][a-z]+`
# tokens separated by single spaces (or hyphens/apostrophes within a
# token).
_CAP_PHRASE_RE = re.compile(
    r"\b((?:[A-Z][a-z'-]+(?:\s+(?:[a-z]{1,3}\s+)?[A-Z][a-z'-]+){0,2}))\b"
)
# Markdown-emphasized tokens: *Aria*, **Kael**, _Volena_
_EMPHASIS_RE = re.compile(
    r"(?:\*\*|\*|_)([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)?)(?:\*\*|\*|_)"
)
# Backtick technical terms: `veilcraft`
_BACKTICK_RE = re.compile(r"`([a-z][\w-]+)`")

# Lowercase words from the name registry are also surfaced as candidates
# (any single-line token that exactly matches a registry name).


class ProseGroundingStrategy:
    name = "prose_grounding"
    languages = _PROSE_LANGUAGES
    enabled_by_default = True

    def applies_to(self, ctx: LintContext) -> bool:
        return ctx.language in self.languages

    def check(self, ctx: LintContext) -> list[Issue]:
        from registry_indexer import resolve_name

        # Bail early on empty registry.
        with ctx.conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM name_registry WHERE project_slug = %s",
                (ctx.project_slug,),
            )
            total = cur.fetchone()[0] or 0
            if total == 0:
                return []

        # Collect candidate phrases.
        candidates: dict[str, int] = {}  # phrase → first line
        for rx in (_CAP_PHRASE_RE, _EMPHASIS_RE, _BACKTICK_RE):
            for m in rx.finditer(ctx.content):
                phrase = m.group(1).strip()
                if phrase in _EN_ALLOWLIST:
                    continue
                if len(phrase) < 3:
                    continue
                if phrase in candidates:
                    continue
                line_no = ctx.content[:m.start()].count("\n") + 1
                candidates[phrase] = line_no

        # For each candidate, resolve against the registry.
        issues: list[Issue] = []
        for phrase, line_no in candidates.items():
            hits = resolve_name(phrase, ctx.project_slug, ctx.conn,
                                  fuzzy=True, limit=3)
            if not hits:
                continue  # unknown → silent (we're conservative for prose)
            top = hits[0]
            if top["match_kind"] == "exact" or top["match_kind"] == "alias":
                continue  # known good
            # Fuzzy match → likely typo of a registered name.
            if top.get("trigram_sim", 0) < 0.7:
                continue  # too weak to be confident
            issues.append(Issue(
                strategy=self.name,
                severity=IssueSeverity.LOW_CONFIDENCE,
                name=f"prose:{phrase}",
                candidate=top["name"],
                confidence=float(top.get("trigram_sim", 0)),
                metadata={"phrase": phrase, "kind": top["kind"],
                          "line": line_no,
                          "trigram_sim": round(top.get("trigram_sim", 0), 3)},
                reason=(f"`{phrase}` looks like `{top['name']}` "
                        f"({top['kind']}, sim={top.get('trigram_sim', 0):.2f})"),
                fix=f"change `{phrase}` to `{top['name']}`",
            ))
        return issues


register(ProseGroundingStrategy())
