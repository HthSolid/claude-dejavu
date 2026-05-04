"""
Module-import grounding (v0.3.16).

Catches `import { fooBar } from 'lodash'` where `lodash` doesn't
export `fooBar`.
"""
from __future__ import annotations

import re

from . import LintContext, LintStrategy, Issue, IssueSeverity, register


# `import { Foo, Bar as Baz } from 'pkg'` and friends.
_NAMED_IMPORT_RE = re.compile(
    r"\bimport\s*(?:type\s+)?\{\s*(?P<names>[^}]+)\s*\}\s*from\s*"
    r"['\"](?P<pkg>[^'\"]+)['\"]"
)
_DEFAULT_IMPORT_RE = re.compile(
    r"\bimport\s+(?P<name>[A-Za-z_$][\w$]*)\s+from\s*"
    r"['\"](?P<pkg>[^'\"]+)['\"]"
)


def _is_npm_package(pkg: str) -> bool:
    """A bare specifier (no `./` / `../` / absolute) is an npm pkg."""
    return not (pkg.startswith("./") or pkg.startswith("../")
                  or pkg.startswith("/"))


class ImportGroundingStrategy:
    name = "import_grounding"
    languages = {"typescript", "tsx", "javascript", "jsx"}
    enabled_by_default = True

    def applies_to(self, ctx: LintContext) -> bool:
        return ctx.language in self.languages

    def check(self, ctx: LintContext) -> list[Issue]:
        from module_indexer import resolve_module_export

        # Bail early if no modules indexed.
        with ctx.conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM module_exports WHERE project_slug = %s",
                (ctx.project_slug,),
            )
            if (cur.fetchone()[0] or 0) == 0:
                return []

        issues: list[Issue] = []
        seen: set[tuple[str, str]] = set()

        # Named imports.
        for m in _NAMED_IMPORT_RE.finditer(ctx.content):
            pkg = m.group("pkg")
            if not _is_npm_package(pkg):
                continue
            line_no = ctx.content[:m.start()].count("\n") + 1
            for spec in m.group("names").split(","):
                spec = spec.strip()
                if not spec:
                    continue
                # `Foo` or `Foo as Bar`
                name = spec.split(" as ")[0].strip()
                if name in ("type",):
                    continue
                if (pkg, name) in seen:
                    continue
                seen.add((pkg, name))
                hits = resolve_module_export(pkg, name, ctx.project_slug,
                                               ctx.conn, fuzzy=True, limit=3)
                # If we have NO data at all for this package (probably
                # because we couldn't read its .d.ts), skip silently —
                # we don't want to flag every named import in that case.
                with ctx.conn.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM module_exports WHERE project_slug = %s AND package_name = %s",
                        (ctx.project_slug, pkg),
                    )
                    count_for_pkg = cur.fetchone()[0] or 0
                if count_for_pkg <= 1:  # 0 or just "default"
                    continue
                if not hits:
                    issues.append(Issue(
                        strategy=self.name,
                        severity=IssueSeverity.UNKNOWN,
                        name=f"import:{pkg}::{name}",
                        candidate=None,
                        confidence=0.85,
                        metadata={"pkg": pkg, "import": name, "line": line_no},
                        reason=(f"`{pkg}` does not export `{name}` "
                                f"(checked against the package's .d.ts)"),
                        fix=(f"verify the import name; run `dejavu_module_exports "
                             f"{pkg}` to browse what the package exposes"),
                    ))
                    continue
                top = hits[0]
                if top["match_kind"] == "fuzzy":
                    issues.append(Issue(
                        strategy=self.name,
                        severity=IssueSeverity.LOW_CONFIDENCE,
                        name=f"import:{pkg}::{name}",
                        candidate=top["export_name"],
                        confidence=float(top.get("trigram_sim", 0)),
                        metadata={"pkg": pkg, "import": name, "line": line_no,
                                  "candidate": top["export_name"]},
                        reason=(f"close match: `{pkg}` exports "
                                f"`{top['export_name']}` "
                                f"(sim={top.get('trigram_sim', 0):.2f})"),
                        fix=f"change `{name}` to `{top['export_name']}`",
                    ))
        return issues


register(ImportGroundingStrategy())
