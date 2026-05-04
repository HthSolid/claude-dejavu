"""
Symbol grounding — the v0.3.0/0.3.1 functionality wrapped as a registry strategy.

Catches:
  - Function / method / class / type / interface / enum / const names
    that don't exist in the project's symbol index.
  - Typos against close-by symbol names (trigram + edit-distance + length re-rank).
  - obj.method() calls where the receiver type is locally inferrable but
    the method isn't defined on that type.

Per-language coverage matches symbol_indexer.py: python, typescript, tsx,
javascript, go, rust, java, ruby, bash.

This strategy is intentionally a thin wrapper around lint.py's existing
logic. Future strategies (route_grounding, semantic_intent, etc.) will
share the same LintContext but use independent indices.
"""
from __future__ import annotations

from . import LintContext, LintStrategy, Issue, IssueSeverity, register


class SymbolGroundingStrategy:
    name = "symbol_grounding"
    languages = {"python", "typescript", "tsx", "javascript",
                 "go", "rust", "java", "ruby", "bash"}
    enabled_by_default = True

    def applies_to(self, ctx: LintContext) -> bool:
        return ctx.language in self.languages

    def check(self, ctx: LintContext) -> list[Issue]:
        from symbol_indexer import resolve_symbol  # lazy import — avoids circular at module load
        from lint import _PY_BUILTINS, _TS_BUILTINS, _GO_BUILTINS, _RUST_BUILTINS, _JAVA_BUILTINS, _RUBY_BUILTINS, _BASH_BUILTINS  # type: ignore

        builtins_by_lang = {
            "python": _PY_BUILTINS,
            "typescript": _TS_BUILTINS, "tsx": _TS_BUILTINS, "javascript": _TS_BUILTINS,
            "go": _GO_BUILTINS, "rust": _RUST_BUILTINS, "java": _JAVA_BUILTINS,
            "ruby": _RUBY_BUILTINS, "bash": _BASH_BUILTINS,
        }
        builtins = builtins_by_lang.get(ctx.language, set())

        issues: list[Issue] = []

        # ─── Type-aware member calls (obj.method() with known obj type) ───
        # When we know obj's type AND the method isn't on that type, that's
        # a stronger signal than a bare-name lookup. We resolve before the
        # bare-name pass so we can dedupe.
        method_resolved: set[str] = set()
        method_unknown_with_type: dict[str, str] = {}  # method -> receiver_type
        for recv, method in ctx.member_calls:
            if method in builtins:
                continue
            recv_type = ctx.var_types.get(recv)
            if not recv_type:
                continue
            qn = f"{recv_type}.{method}"
            with ctx.conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM symbols WHERE project_slug = %s AND qualified_name = %s LIMIT 1",
                    (ctx.project_slug, qn),
                )
                if cur.fetchone():
                    method_resolved.add(method)
                    issues.append(Issue(
                        strategy=self.name,
                        severity=IssueSeverity.OK,
                        name=method,
                        candidate=qn,
                        confidence=1.0,
                        metadata={"via_receiver_type": recv_type, "qualified_name": qn},
                        reason=f"method '{method}' is defined on receiver type '{recv_type}'",
                    ))
                else:
                    cur.execute(
                        "SELECT 1 FROM symbols WHERE project_slug = %s AND name = %s LIMIT 1",
                        (ctx.project_slug, recv_type),
                    )
                    receiver_known = cur.fetchone() is not None
                    note = (f"method '{method}' is not defined on receiver type '{recv_type}'"
                            if receiver_known else
                            f"receiver type '{recv_type}' is also not in the index")
                    method_unknown_with_type[method] = recv_type
                    issues.append(Issue(
                        strategy=self.name,
                        severity=IssueSeverity.UNKNOWN,
                        name=method,
                        candidate=None,
                        confidence=0.9 if receiver_known else 0.5,
                        metadata={"via_receiver_type": recv_type,
                                  "receiver_type_exists_in_index": receiver_known},
                        reason=note,
                        fix=f"check that '{recv_type}' has a '{method}' method, "
                             f"or call dejavu_resolve_symbol to find what's actually defined",
                    ))

        # ─── Bare-name references ─────────────────────────────────────────
        for name in sorted(ctx.referenced):
            if name in builtins:
                continue
            if name.startswith("__") and name.endswith("__"):
                continue
            if name in method_resolved or name in method_unknown_with_type:
                continue

            exact = resolve_symbol(name, project_slug=ctx.project_slug, conn=ctx.conn,
                                   fuzzy=False, limit=1)
            if exact:
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.OK,
                    name=name,
                    candidate=exact[0]["name"],
                    confidence=1.0,
                    metadata={"file_path": exact[0]["file_path"], "kind": exact[0]["kind"]},
                    reason="exact match in symbol index",
                ))
                continue

            fuzzy = resolve_symbol(name, project_slug=ctx.project_slug, conn=ctx.conn,
                                   fuzzy=True, limit=3)
            if not fuzzy:
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.UNKNOWN,
                    name=name,
                    candidate=None,
                    confidence=0.95,
                    reason="no plausible match in symbol index",
                    fix=f"verify '{name}' exists, or use dejavu_resolve_symbol",
                ))
                continue

            top = fuzzy[0]
            high_conf = (top["trigram_sim"] >= 0.7 and top["edit_distance"] <= 2)
            if high_conf:
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.LOW_CONFIDENCE,
                    name=name,
                    candidate=top["name"],
                    confidence=float(top.get("rerank_score", top["trigram_sim"])),
                    metadata={
                        "trigram_sim": round(top["trigram_sim"], 3),
                        "edit_distance": top["edit_distance"],
                        "file_path": top["file_path"],
                        "kind": top["kind"],
                    },
                    reason=(f"close match '{top['name']}' (sim={top['trigram_sim']:.2f}, "
                            f"ed={top['edit_distance']}) — likely a typo"),
                    fix=f"rename '{name}' → '{top['name']}'",
                ))
            else:
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.UNKNOWN,
                    name=name,
                    candidate=top["name"],
                    confidence=float(top.get("rerank_score", top["trigram_sim"])),
                    metadata={
                        "trigram_sim": round(top["trigram_sim"], 3),
                        "edit_distance": top["edit_distance"],
                        "file_path": top["file_path"],
                    },
                    reason=(f"no high-confidence match — closest is '{top['name']}' "
                            f"(sim={top['trigram_sim']:.2f}, ed={top['edit_distance']})"),
                    fix=f"verify '{name}' or consider '{top['name']}'",
                ))
        return issues


register(SymbolGroundingStrategy())
