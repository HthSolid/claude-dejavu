"""
Route grounding — verifies client-side URL literals (in fetch / axios /
useQuery / useSWR / requests / httpx) against the project's api_routes
index.

Catches:
  - Fabricated URLs the project doesn't expose:
      `fetch('/api/v1/users/profile/update', POST)`
      when the real route is `PATCH /api/users/:id`.
  - Method/path mismatches (the route exists but with a different verb).
  - Typos in the path (`/api/usrs` → `/api/users`).

Out of scope for v0.3.2.1:
  - Interpolated URLs (template strings / f-strings) — we can't ground
    a partially-known URL. Flagged as `interpolated` in the call site
    and skipped silently.
  - Body / query parameter shape mismatches — the api_route_params
    table is populated but the strategy doesn't consume it yet.
"""
from __future__ import annotations

from . import LintContext, LintStrategy, Issue, IssueSeverity, register


class RouteGroundingStrategy:
    name = "route_grounding"
    languages = {"python", "typescript", "tsx", "javascript"}
    enabled_by_default = True

    def applies_to(self, ctx: LintContext) -> bool:
        return ctx.language in self.languages

    def check(self, ctx: LintContext) -> list[Issue]:
        from url_walker import extract_call_sites
        from route_indexer import resolve_route, normalize_path

        issues: list[Issue] = []
        sites = extract_call_sites(ctx.content, ctx.language)
        if not sites:
            return issues

        # Bail early if the project has no routes indexed yet — like the
        # symbol-grounding strategy, no point flagging during the bootstrap
        # window.
        with ctx.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM api_routes WHERE project_slug = %s",
                        (ctx.project_slug,))
            if (cur.fetchone()[0] or 0) == 0:
                return issues

        for site in sites:
            if site.interpolated:
                # Can't ground a templated URL — skip silently. Future v0.3.x
                # work could try to extract the literal prefix and check that.
                continue
            # Strip leading 'http(s)://host' so we match against path-only routes.
            url = site.url
            if url.startswith("http://") or url.startswith("https://"):
                from urllib.parse import urlparse
                p = urlparse(url)
                url = p.path or "/"
            # Strip any query string for the lookup
            url = url.split("?", 1)[0]
            if not url.startswith("/"):
                url = "/" + url

            normalized = normalize_path(url)

            # Lookup
            hits = resolve_route(site.method, normalized, ctx.project_slug, ctx.conn,
                                 fuzzy=True, limit=5)
            if not hits:
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.UNKNOWN,
                    name=f"{site.method} {url}",
                    candidate=None,
                    confidence=0.9,
                    metadata={"library": site.library, "line": site.start_line, "url": url},
                    reason=f"no route in this project handles `{site.method} {normalized}`",
                    fix=("verify the URL/method, or call dejavu_resolve_route to "
                         "find the closest declared route in the project"),
                ))
                continue

            # Exact OR pattern match? Resolved if method matches, else
            # path-exists-but-wrong-method.
            confirmed = [h for h in hits if h["match_kind"] in ("exact", "pattern")]
            if confirmed:
                # Prefer the hit whose method actually matches the call's method.
                exact_method_hit = next(
                    (h for h in confirmed if h["method"] == site.method
                     or h["method"] == "*" or site.method == "*"),
                    None,
                )
                if exact_method_hit:
                    top = exact_method_hit
                    issues.append(Issue(
                        strategy=self.name,
                        severity=IssueSeverity.OK,
                        name=f"{site.method} {url}",
                        candidate=f"{top['method']} {top['path_pattern']}",
                        confidence=1.0,
                        metadata={
                            "framework": top["framework"], "source": top["source"],
                            "source_file": top["source_file"], "library": site.library,
                            "line": site.start_line, "match_kind": top["match_kind"],
                        },
                        reason=(f"{top['match_kind']} match against {top['framework']} "
                                f"({top['source']}) at {top['source_file']}:{top['start_line']}"),
                    ))
                else:
                    # Path-only hit — wrong verb.
                    top = confirmed[0]
                    declared_methods = sorted({h["method"] for h in confirmed})
                    issues.append(Issue(
                        strategy=self.name,
                        severity=IssueSeverity.LOW_CONFIDENCE,
                        name=f"{site.method} {url}",
                        candidate=f"{top['method']} {top['path_pattern']}",
                        confidence=0.85,
                        metadata={"library": site.library, "line": site.start_line,
                                  "wrong_method": True,
                                  "declared_methods": declared_methods},
                        reason=(f"path exists but only handles `{', '.join(declared_methods)}`, "
                                f"not `{site.method}`"),
                        fix=(f"change method to one of {declared_methods} "
                             f"or declare a `{site.method} {top['path_pattern']}` route"),
                    ))
                continue

            # Fuzzy only — likely a typo.
            top = hits[0]
            issues.append(Issue(
                strategy=self.name,
                severity=IssueSeverity.LOW_CONFIDENCE,
                name=f"{site.method} {url}",
                candidate=f"{top['method']} {top['path_pattern']}",
                confidence=float(top.get("trigram_sim", 0)),
                metadata={
                    "framework": top["framework"], "source": top["source"],
                    "library": site.library, "line": site.start_line,
                    "trigram_sim": round(top.get("trigram_sim", 0), 3),
                },
                reason=(f"close match `{top['method']} {top['path_pattern']}` "
                        f"(sim={top.get('trigram_sim', 0):.2f}) — likely a typo or wrong verb"),
                fix=f"change to `{top['method']} {top['path_pattern']}` if that's what you meant",
            ))

        return issues


register(RouteGroundingStrategy())
