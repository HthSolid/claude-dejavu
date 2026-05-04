"""Per-strategy unit tests. One test function per strategy, multiple
cases each. Validates every cascade strategy in isolation so we know
exactly which strategy regresses when the integration suite breaks."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(msg: str) -> None:
    PASS.append(msg)
    print(f"  \033[32m✓\033[0m {msg}", flush=True)


def _fail(msg: str, detail: str = "") -> None:
    FAIL.append((msg, detail))
    print(f"  \033[31m✗\033[0m {msg}")
    if detail:
        print(f"      {detail}")


def _step(name: str) -> None:
    print(f"\n\033[36m── {name} ──\033[0m", flush=True)


def _expect(name: str, actual, expected, detail: str = "") -> bool:
    if actual == expected:
        _ok(f"{name}: {actual}")
        return True
    _fail(f"{name}: expected {expected!r}, got {actual!r}",
            detail)
    return False


# ─── Strategy 1: exact ──────────────────────────────────────────────────────


def test_strategy_exact() -> None:
    _step("strategy_exact")
    from rewriter import strategy_exact
    r = strategy_exact("FOO", ["FOO", "BAR"])
    _expect("hit", (r.verdict, r.replacement), ("OK", "FOO"))
    r = strategy_exact("BAZ", ["FOO", "BAR"])
    _expect("miss", r.verdict, "RESIDUAL")
    r = strategy_exact("FOO", [])
    _expect("empty candidates", r.verdict, "RESIDUAL")


# ─── Strategy 2: sdk_catalog ────────────────────────────────────────────────


def test_strategy_sdk_catalog() -> None:
    _step("strategy_sdk_catalog")
    from rewriter import strategy_sdk_catalog, load_sdk_catalog
    cat = load_sdk_catalog()

    # Gated by deps: name in catalog + project depends on the package
    r = strategy_sdk_catalog("LAUNCHDARKLY_SDK_KEY",
                                  project_deps={"@launchdarkly/node-server-sdk"},
                                  sdk_catalog=cat)
    _expect("ld+dep", r.verdict, "ACCEPT_SDK")

    # Universal: NODE_ENV always accepted via _universal entry
    r = strategy_sdk_catalog("NODE_ENV", project_deps=set(),
                                  sdk_catalog=cat)
    _expect("universal NODE_ENV", r.verdict, "ACCEPT_SDK")

    # Name in catalog but project doesn't depend on the package
    r = strategy_sdk_catalog("LAUNCHDARKLY_SDK_KEY",
                                  project_deps=set(), sdk_catalog=cat)
    _expect("ld no-dep", r.verdict, "RESIDUAL")

    # Name not in catalog at all
    r = strategy_sdk_catalog("TOTALLY_RANDOM_KEY",
                                  project_deps={"stripe"},
                                  sdk_catalog=cat)
    _expect("not in catalog", r.verdict, "RESIDUAL")


# ─── Strategy 3: trigram ────────────────────────────────────────────────────


def test_strategy_trigram() -> None:
    _step("strategy_trigram")
    from rewriter import strategy_trigram
    # Use a closer typo (trailing extra char ~0.85 sim, well above 0.7)
    r = strategy_trigram("DATABASE_URLL",
                              ["DATABASE_URL", "REDIS_URL"],
                              threshold=0.7)
    _expect("near-typo above threshold",
              (r.verdict, r.replacement),
              ("REPLACE", "DATABASE_URL"))
    r = strategy_trigram("WILDLY_DIFFERENT", ["DATABASE_URL"],
                              threshold=0.7)
    _expect("no match below threshold", r.verdict, "RESIDUAL")
    r = strategy_trigram("FOO", [], threshold=0.7)
    _expect("empty candidates", r.verdict, "RESIDUAL")
    # Threshold tuning: same input, different threshold
    r_lo = strategy_trigram("STRIPE", ["STRIPE_KEY"], threshold=0.3)
    r_hi = strategy_trigram("STRIPE", ["STRIPE_KEY"], threshold=0.9)
    if r_lo.verdict != "REPLACE" or r_hi.verdict != "RESIDUAL":
        _fail(f"threshold sensitivity: lo={r_lo.verdict} "
                f"hi={r_hi.verdict}")
    else:
        _ok("threshold sensitivity: low passes, high blocks")


# ─── Strategy 4: levenshtein_word ───────────────────────────────────────────


def test_strategy_levenshtein_word() -> None:
    _step("strategy_levenshtein_word")
    from rewriter import strategy_levenshtein_word
    # One inserted token: STRIPE_KEY -> STRIPE_SECRET_KEY
    r = strategy_levenshtein_word("STRIPE_KEY",
                                       ["STRIPE_SECRET_KEY",
                                         "REDIS_URL"],
                                       threshold=1)
    _expect("one inserted token",
              (r.verdict, r.replacement),
              ("REPLACE", "STRIPE_SECRET_KEY"))
    # Two-token diff with threshold=1 should NOT match
    r = strategy_levenshtein_word("FOO_BAR_BAZ_QUX",
                                       ["TOTALLY_DIFFERENT"],
                                       threshold=1)
    _expect("too distant", r.verdict, "RESIDUAL")


# ─── Strategy 5: acronym ────────────────────────────────────────────────────


def test_strategy_acronym() -> None:
    _step("strategy_acronym")
    from rewriter import strategy_acronym
    abbrev = {"LD": "LAUNCHDARKLY", "DD": "DATADOG"}
    r = strategy_acronym("LD_SDK_KEY",
                              ["LAUNCHDARKLY_SDK_KEY", "OTHER"],
                              abbreviations=abbrev)
    _expect("LD expansion",
              (r.verdict, r.replacement),
              ("REPLACE", "LAUNCHDARKLY_SDK_KEY"))
    r = strategy_acronym("DD_API_KEY",
                              ["DATADOG_API_KEY"],
                              abbreviations=abbrev)
    _expect("DD expansion",
              (r.verdict, r.replacement),
              ("REPLACE", "DATADOG_API_KEY"))
    r = strategy_acronym("XX_RANDOM",
                              ["LAUNCHDARKLY_SDK_KEY"],
                              abbreviations=abbrev)
    _expect("unknown prefix", r.verdict, "RESIDUAL")


# ─── Strategy 6: case_normalize ─────────────────────────────────────────────


def test_strategy_case_normalize() -> None:
    _step("strategy_case_normalize")
    from rewriter import strategy_case_normalize
    r = strategy_case_normalize("databaseUrl", ["DATABASE_URL"])
    _expect("camel→snake",
              (r.verdict, r.replacement),
              ("REPLACE", "DATABASE_URL"))
    r = strategy_case_normalize("database-url", ["DATABASE_URL"])
    _expect("kebab→snake",
              (r.verdict, r.replacement),
              ("REPLACE", "DATABASE_URL"))
    r = strategy_case_normalize("DATABASE_URL", ["DATABASE_URL"])
    # Same name → no change → RESIDUAL (no useful rewrite)
    _expect("identity returns no-change",
              r.verdict, "RESIDUAL")
    r = strategy_case_normalize("foo", ["BAR"])
    _expect("no overlap", r.verdict, "RESIDUAL")


# ─── Strategy 7: token_set ──────────────────────────────────────────────────


def test_strategy_token_set() -> None:
    _step("strategy_token_set")
    from rewriter import strategy_token_set
    r = strategy_token_set("USER_API_TOKEN",
                                ["API_USER_TOKEN", "OTHER"])
    _expect("reordered tokens match",
              (r.verdict, r.replacement),
              ("REPLACE", "API_USER_TOKEN"))
    r = strategy_token_set("FOO_BAR", ["BAZ_QUX"])
    _expect("disjoint tokens", r.verdict, "RESIDUAL")
    r = strategy_token_set("", ["FOO"])
    _expect("empty input", r.verdict, "RESIDUAL")


# ─── Strategy 8: suffix_pattern ─────────────────────────────────────────────


def test_strategy_suffix_pattern() -> None:
    _step("strategy_suffix_pattern")
    from rewriter import strategy_suffix_pattern
    # SAME prefix + DIFFERENT suffix → flagged paired-purpose RESIDUAL
    # `STRIPE_KEY` vs declared `STRIPE_SECRET` — share prefix `STRIPE`,
    # differ on _KEY vs _SECRET.
    r = strategy_suffix_pattern("STRIPE_KEY", ["STRIPE_SECRET"])
    _expect("paired-key candidate", r.verdict, "RESIDUAL")
    if "paired-purpose" not in (r.reason or ""):
        _fail(f"paired-purpose reason missing: {r.reason}")
    else:
        _ok("paired-purpose hint present in reason")
    # Same prefix AND same suffix — different name pair, no fire
    r = strategy_suffix_pattern("STRIPE_PUBLIC_KEY",
                                     ["STRIPE_SECRET_KEY"])
    _expect("same suffix, different prefix doesn't fire",
              r.verdict, "RESIDUAL")
    # No suffix match at all
    r = strategy_suffix_pattern("FOO_BAR", ["BAZ"])
    _expect("no suffix overlap", r.verdict, "RESIDUAL")


# ─── Strategy 9: import_gating ──────────────────────────────────────────────


def test_strategy_import_gating() -> None:
    _step("strategy_import_gating")
    from rewriter import strategy_import_gating, load_sdk_catalog
    cat = load_sdk_catalog()

    # Source imports the SDK literal AND name is the SDK's env
    src = "import * as LD from '@launchdarkly/node-server-sdk';"
    r = strategy_import_gating("LD_SDK_KEY",
                                    source_text=src,
                                    project_deps=set(),
                                    sdk_catalog=cat)
    _expect("source-import gate", r.verdict, "ACCEPT_SDK")

    # No import literal, but package in deps → still accepts
    r = strategy_import_gating("AWS_SESSION_TOKEN",
                                    source_text="// no imports",
                                    project_deps={"aws-sdk"},
                                    sdk_catalog=cat)
    _expect("dep-only gate", r.verdict, "ACCEPT_SDK")

    # Neither import nor dep
    r = strategy_import_gating("LD_SDK_KEY",
                                    source_text="// no imports",
                                    project_deps=set(),
                                    sdk_catalog=cat)
    _expect("no gate", r.verdict, "RESIDUAL")


# ─── Strategy 10: cross_domain ──────────────────────────────────────────────


def test_strategy_cross_domain() -> None:
    _step("strategy_cross_domain")
    from rewriter import strategy_cross_domain
    manifest = {"env_vars": ["DATABASE_URL"],
                  "flags": ["checkout-flow-v2"],
                  "user_fields": ["email"]}
    # name typed as env but exists as flag
    r = strategy_cross_domain("checkout-flow-v2",
                                   manifest=manifest,
                                   current_domain="env")
    _expect("flag-as-env",
              (r.verdict, "feature flag" in (r.reason or "")),
              ("RESIDUAL", True))
    # name doesn't exist anywhere
    r = strategy_cross_domain("PHANTOM",
                                   manifest=manifest,
                                   current_domain="env")
    _expect("not anywhere", r.verdict, "RESIDUAL")
    # name in correct domain (would have been caught by exact, but
    # cross_domain shouldn't claim ownership)
    r = strategy_cross_domain("DATABASE_URL",
                                   manifest=manifest,
                                   current_domain="env")
    _expect("same-domain doesn't fire", r.verdict, "RESIDUAL")


# ─── Strategy 11: comment_context ───────────────────────────────────────────


def test_strategy_comment_context() -> None:
    _step("strategy_comment_context")
    from rewriter import strategy_comment_context
    src1 = ("// TODO: add LD_SDK_KEY to .env\n"
              "const k = process.env.LD_SDK_KEY;")
    r = strategy_comment_context("LD_SDK_KEY", source_text=src1)
    _expect("TODO comment with name above",
              r.verdict, "ACCEPT_SDK")

    # Name only inside comment line
    src2 = "// commented out: process.env.LD_SDK_KEY"
    r = strategy_comment_context("LD_SDK_KEY", source_text=src2)
    _expect("name inside comment", r.verdict, "ACCEPT_SDK")

    # Name in real code, no nearby comment
    src3 = "const k = process.env.LD_SDK_KEY;"
    r = strategy_comment_context("LD_SDK_KEY", source_text=src3)
    _expect("no comment context", r.verdict, "RESIDUAL")

    # Empty source
    r = strategy_comment_context("LD_SDK_KEY", source_text="")
    _expect("empty source", r.verdict, "RESIDUAL")


# ─── Strategy 12: project_history ───────────────────────────────────────────


def test_strategy_project_history() -> None:
    _step("strategy_project_history (unit — git tested elsewhere)")
    from rewriter import strategy_project_history
    history = {"LEGACY_API_KEY", "OLD_FLAG"}
    candidates = ["DATABASE_URL", "SESSION"]

    r = strategy_project_history("LEGACY_API_KEY", candidates,
                                       history_names=history)
    _expect("removed-name regression",
              (r.verdict, "regression" in (r.reason or "")
                  or "history" in (r.reason or "")),
              ("RESIDUAL", True))

    r = strategy_project_history("WHOLLY_NEW", candidates,
                                       history_names=history)
    _expect("not in history", r.verdict, "RESIDUAL")

    # Currently in manifest → doesn't claim
    r = strategy_project_history("DATABASE_URL", candidates,
                                       history_names=history)
    _expect("currently declared", r.verdict, "RESIDUAL")


# ─── Strategy 13: embedding ─────────────────────────────────────────────────


def test_strategy_embedding() -> None:
    _step("strategy_embedding (mocked semantic_search)")
    import types as _types
    fake = _types.ModuleType("semantic_search")
    fake.is_available = lambda: True

    def fake_search(intent, project_slug, limit=5, **kw):
        if "STRIPE" in intent:
            return [{"name": "STRIPE_SECRET_KEY",
                      "_additional": {"distance": 0.10}}]
        return []
    fake.search = fake_search
    sys.modules["semantic_search"] = fake
    if "rewriter" in sys.modules:
        del sys.modules["rewriter"]
    from rewriter import strategy_embedding

    r = strategy_embedding("STRIPE_KEY",
                                ["STRIPE_SECRET_KEY"],
                                project_slug="t",
                                embedding_threshold=0.85)
    _expect("REPLACE on high-cosine",
              (r.verdict, r.replacement),
              ("REPLACE", "STRIPE_SECRET_KEY"))

    r = strategy_embedding("UNRELATED",
                                ["STRIPE_SECRET_KEY"],
                                project_slug="t")
    _expect("no semantic neighbor", r.verdict, "RESIDUAL")

    fake.is_available = lambda: False
    r = strategy_embedding("ANY",
                                ["STRIPE_SECRET_KEY"],
                                project_slug="t")
    _expect("offline", r.verdict, "RESIDUAL")
    if "offline" not in (r.reason or ""):
        _fail(f"offline reason missing: {r.reason}")
    else:
        _ok("offline reason explicit")

    # Without project_slug, can't query
    fake.is_available = lambda: True
    r = strategy_embedding("STRIPE_KEY",
                                ["STRIPE_SECRET_KEY"],
                                project_slug=None)
    _expect("no project_slug", r.verdict, "RESIDUAL")

    del sys.modules["semantic_search"]
    if "rewriter" in sys.modules:
        del sys.modules["rewriter"]


# ─── Strategy 14: blacklist short-circuit (in cascade, via rewrite) ─────────


def test_blacklist_short_circuit() -> None:
    _step("blacklist short-circuit (via rewrite() with mock conn)")
    if "rewriter" in sys.modules:
        del sys.modules["rewriter"]
    from rewriter import rewrite, load_sdk_catalog

    class _FakeConn:
        """Minimal mock: any (project, name='BANNED', domain) is
        blacklisted."""
        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            self._last = (sql, params or ())

        def fetchone(self):
            sql, params = self._last
            if "fabrication_history" in sql and "SELECT" in sql:
                # lookup_fabrication query
                if params[1] == "BANNED":
                    return ("BANNED", "env", 5, "blacklisted",
                              None, None, "")
                return None
            if "INSERT INTO fabrication_history" in sql:
                return (1,)
            return None

        def rollback(self):
            pass

    manifest = {"project_slug": "p1",
                  "env_vars": ["DATABASE_URL"],
                  "models": [], "user_fields": [], "post_fields": [],
                  "routes": [], "gql_types": [], "flags": []}
    rewritten, outcomes = rewrite(
        "const x = process.env.BANNED;",
        manifest, project_deps=set(),
        bad_names={"env": ["BANNED"], "db": [], "route": [],
                     "graphql": [], "flag": []},
        sdk_catalog=load_sdk_catalog(),
        fabrication_conn=_FakeConn())
    if not outcomes:
        _fail("no outcomes from blacklisted name"); return
    o = outcomes[0]
    if o.final_verdict != "RESIDUAL" or (
            o.chosen and o.chosen.strategy != "blacklist"):
        _fail(f"blacklist short-circuit: verdict={o.final_verdict} "
                f"strategy={o.chosen and o.chosen.strategy}")
        return
    _ok("blacklisted name short-circuits cascade with strategy=blacklist")


def main() -> int:
    print("claude-dejavu strategy unit tests")
    print(f"  ROOT={ROOT}")
    tests = [
        test_strategy_exact,
        test_strategy_sdk_catalog,
        test_strategy_trigram,
        test_strategy_levenshtein_word,
        test_strategy_acronym,
        test_strategy_case_normalize,
        test_strategy_token_set,
        test_strategy_suffix_pattern,
        test_strategy_import_gating,
        test_strategy_cross_domain,
        test_strategy_comment_context,
        test_strategy_project_history,
        test_strategy_embedding,
        test_blacklist_short_circuit,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAIL.append((t.__name__, str(e)))
    print()
    print("=" * 60)
    print(f"PASS: {len(PASS)}     FAIL: {len(FAIL)}")
    for m, d in FAIL:
        print(f"  ✗ {m}  {d[:80]}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
