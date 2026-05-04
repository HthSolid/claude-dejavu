"""
Page duplicate prevention — flags Claude proposing a NEW page when one
already exists for the same URL or near-identical URL.

The lint fires when:
  - A `Write` proposes creating a file under a routing-aware directory
    (Next.js `pages/`, `app/`, Remix `app/routes/`, SvelteKit
    `src/routes/`, Astro `src/pages/`).
  - The derived route path is checked against the project's `page_routes`
    index.

Severity:
  - Exact path overlap (different framework or different file extension)
    → UNKNOWN (block) — definite duplicate.
  - Pattern-match (proposed concrete vs existing parameterized OR vice
    versa) → LOW_CONFIDENCE — likely overlap, surface to Claude.
  - Fuzzy near-name match → LOW_CONFIDENCE — possible duplicate
    (e.g., `/profile-edit` vs `/profile/edit`).

When the strategy runs:
  - On lint of the proposed_content with file_path under a routing dir.
  - The strategy needs `project_root` to resolve relative routes — we
    use the same heuristic as auto-reindex: walk up from file_path to
    find the project marker (a directory containing `pages/`, `app/`,
    `src/routes/`, `src/pages/`, or a `package.json`).
"""
from __future__ import annotations

from pathlib import Path

from . import LintContext, LintStrategy, Issue, IssueSeverity, register


def _find_project_root(file_path: str) -> str | None:
    """Walk up from file_path looking for a routing root marker. Returns
    the topmost ancestor that contains one of the known routing dirs OR
    a package.json (whichever comes first when ascending)."""
    p = Path(file_path).resolve()
    if p.is_file():
        p = p.parent
    markers = ("package.json",)
    routing_dirs = ("pages", "app", "src/routes", "src/pages")
    for ancestor in [p, *p.parents]:
        for m in markers:
            if (ancestor / m).is_file():
                return str(ancestor)
        for r in routing_dirs:
            if (ancestor / r).is_dir():
                return str(ancestor)
    return None


class PageDuplicateStrategy:
    name = "page_duplicate"
    languages = {"typescript", "tsx", "javascript", "python"}  # filter cheap
    enabled_by_default = True

    def applies_to(self, ctx: LintContext) -> bool:
        # Only meaningful when the file_path looks like a frontend file.
        # The route-derivation function itself is the real filter — if it
        # returns None, the strategy emits nothing.
        return ctx.language in self.languages or ctx.file_path.endswith((
            ".astro", ".mdx", ".svelte"))

    def check(self, ctx: LintContext) -> list[Issue]:
        from page_indexer import file_path_to_route, check_new_page

        # Bail early if the project has no pages indexed yet.
        with ctx.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM page_routes WHERE project_slug = %s",
                        (ctx.project_slug,))
            if (cur.fetchone()[0] or 0) == 0:
                return []

        project_root = _find_project_root(ctx.file_path)
        if not project_root:
            return []
        derived = file_path_to_route(ctx.file_path, project_root)
        if not derived:
            return []  # not a routing-aware file
        framework, route = derived

        # The proposed file IS the new page. Look up existing pages that
        # would conflict.
        hits = check_new_page(route, ctx.project_slug, ctx.conn,
                              fuzzy=True, limit=5)
        if not hits:
            return []

        # If any hit comes from this same file, the page already exists at
        # this exact location — Claude is editing it, not duplicating.
        same_file = any(h["source_file"] == ctx.file_path for h in hits)
        if same_file:
            return []  # editing in place

        issues: list[Issue] = []
        for h in hits:
            existing_path = h["path_pattern"]
            mk = h["match_kind"]
            if mk == "exact":
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.UNKNOWN,  # block
                    name=f"NEW page at {route}",
                    candidate=f"{h['framework']} {existing_path}",
                    confidence=0.98,
                    metadata={
                        "proposed_framework": framework,
                        "existing_framework": h["framework"],
                        "existing_file": h["source_file"],
                        "existing_kind": h["kind"],
                        "match_kind": "exact",
                    },
                    reason=(f"a {h['kind']} for path '{existing_path}' "
                            f"already exists at {h['source_file']}:{h['start_line']} "
                            f"({h['framework']}) — creating a new file at "
                            f"{ctx.file_path} would shadow or duplicate it"),
                    fix=(f"edit the existing file '{h['source_file']}' instead, "
                         f"or pick a different route path"),
                ))
            elif mk == "pattern":
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.LOW_CONFIDENCE,
                    name=f"NEW page at {route}",
                    candidate=f"{h['framework']} {existing_path}",
                    confidence=0.85,
                    metadata={
                        "proposed_framework": framework,
                        "existing_framework": h["framework"],
                        "existing_file": h["source_file"],
                        "match_kind": "pattern",
                    },
                    reason=(f"path '{route}' is already handled by parameterized "
                            f"page '{existing_path}' at {h['source_file']}:{h['start_line']} "
                            f"({h['framework']})"),
                    fix=("use the existing parameterized route, or rename "
                         "the proposed page to a non-overlapping path"),
                ))
            elif mk == "fuzzy":
                # Only flag fuzzy hits with reasonable similarity.
                if h.get("trigram_sim", 0) < 0.6:
                    continue
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.LOW_CONFIDENCE,
                    name=f"NEW page at {route}",
                    candidate=f"{h['framework']} {existing_path}",
                    confidence=float(h.get("trigram_sim", 0)),
                    metadata={
                        "proposed_framework": framework,
                        "existing_framework": h["framework"],
                        "existing_file": h["source_file"],
                        "match_kind": "fuzzy",
                        "trigram_sim": round(h.get("trigram_sim", 0), 3),
                    },
                    reason=(f"near-name overlap: existing '{existing_path}' "
                            f"(sim={h.get('trigram_sim', 0):.2f}) at "
                            f"{h['source_file']}:{h['start_line']}"),
                    fix=("verify whether '{existing_path}' is the same goal as your "
                         "proposed route — if so, edit there instead"),
                ))
        return issues


register(PageDuplicateStrategy())
