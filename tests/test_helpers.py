"""Helper-function unit tests for code/rewriter.py.

Covers the primitives (_trigrams, _trigram_sim, _normalize_separators,
_token_set, _token_edit_distance) plus the public helpers
(manifest_candidates, extend_manifest_from_sources,
project_history_names, load_sdk_catalog) and the cascade orchestrator
itself."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
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


def _assert_true(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        _ok(name)
    else:
        _fail(name, detail)


# ─── Primitives ─────────────────────────────────────────────────────────────


def test_trigrams() -> None:
    _step("_trigrams + _trigram_sim")
    from rewriter import _trigrams, _trigram_sim

    tg = _trigrams("foo")
    _assert_true("trigrams of `foo` non-empty", len(tg) > 0,
                  f"got {tg}")

    sim_same = _trigram_sim("DATABASE_URL", "DATABASE_URL")
    _assert_true("identical strings -> 1.0", sim_same == 1.0,
                  f"got {sim_same}")

    sim_close = _trigram_sim("DATABASE_URL", "DATABASE_URLL")
    _assert_true("near-typo > 0.7", sim_close > 0.7,
                  f"got {sim_close:.2f}")

    sim_far = _trigram_sim("DATABASE_URL", "REDIS_HOST")
    _assert_true("disjoint < 0.3", sim_far < 0.3,
                  f"got {sim_far:.2f}")

    # Defensive: empty input
    sim_empty = _trigram_sim("", "FOO")
    _assert_true("empty string -> 0.0", sim_empty == 0.0,
                  f"got {sim_empty}")


def test_normalize_separators() -> None:
    _step("_normalize_separators")
    from rewriter import _normalize_separators
    cases = [
        ("databaseUrl",  "database url"),
        ("DATABASE_URL", "database url"),
        ("database-url", "database url"),
        ("API_BASE_URL", "api base url"),
        ("apiBaseUrl",   "api base url"),
    ]
    for inp, exp in cases:
        got = _normalize_separators(inp)
        if got == exp:
            _ok(f"`{inp}` → `{got}`")
        else:
            _fail(f"`{inp}` → `{got}`, expected `{exp}`")


def test_token_set() -> None:
    _step("_token_set")
    from rewriter import _token_set
    a = _token_set("USER_API_TOKEN")
    b = _token_set("API_USER_TOKEN")
    _assert_true("reordered tokens equal", a == b, f"{a} vs {b}")

    c = _token_set("databaseUrl")
    _assert_true("camelCase decomposes",
                  c == {"database", "url"}, f"{c}")


def test_token_edit_distance() -> None:
    _step("_token_edit_distance")
    from rewriter import _token_edit_distance
    cases = [
        (["A"], ["A"], 0),
        (["A"], ["A", "B"], 1),
        (["A", "B"], ["A"], 1),
        (["A", "B"], ["A", "C"], 1),
        (["A", "B", "C"], ["X", "Y", "Z"], 3),
        ([], [], 0),
        ([], ["A"], 1),
    ]
    for a, b, exp in cases:
        got = _token_edit_distance(a, b)
        if got == exp:
            _ok(f"{a} ↔ {b} = {got}")
        else:
            _fail(f"{a} ↔ {b}: expected {exp}, got {got}")


# ─── Manifest / candidate helpers ───────────────────────────────────────────


def test_manifest_candidates() -> None:
    _step("manifest_candidates")
    from rewriter import manifest_candidates
    manifest = {
        "env_vars": ["DATABASE_URL"],
        "models": ["User", "Post"],
        "user_fields": ["id", "email"],
        "post_fields": ["id", "title"],
        "routes": ["GET /api/u"],
        "gql_types": ["User"],
        "flags": ["fa"],
    }
    _assert_true("env",
                  manifest_candidates(manifest, "env") == ["DATABASE_URL"])
    _assert_true("db with parent=User",
                  manifest_candidates(manifest, "db",
                                          parent="User") == ["id", "email"])
    _assert_true("db without parent merges User+Post",
                  set(manifest_candidates(manifest, "db"))
                      == {"id", "email", "title"})
    _assert_true("route", manifest_candidates(manifest, "route")
                                    == ["GET /api/u"])
    _assert_true("flag", manifest_candidates(manifest, "flag")
                                  == ["fa"])
    _assert_true("unknown domain → []",
                  manifest_candidates(manifest, "x") == [])


def test_load_sdk_catalog() -> None:
    _step("load_sdk_catalog")
    from rewriter import load_sdk_catalog
    cat = load_sdk_catalog()
    _assert_true("has 'env' section", "env" in cat)
    _assert_true("has 'abbreviations'", "abbreviations" in cat)
    _assert_true("LD entries present",
                  "LAUNCHDARKLY_SDK_KEY" in
                      cat["env"].get("@launchdarkly/node-server-sdk", []))
    _assert_true("LD abbreviation",
                  cat["abbreviations"].get("LD") == "LAUNCHDARKLY")
    _assert_true("_universal entry present",
                  "_universal" in cat["env"])
    _assert_true("NODE_ENV in universal",
                  "NODE_ENV" in cat["env"]["_universal"])


# ─── extend_manifest_from_sources — exercise every source type ──────────────


def test_extend_manifest_from_sources() -> None:
    _step("extend_manifest_from_sources walks every source type")
    from rewriter import extend_manifest_from_sources
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # GitHub workflow
        (root / ".github" / "workflows").mkdir(parents=True)
        (root / ".github" / "workflows" / "ci.yml").write_text(
            "env:\n"
            "  WORKFLOW_ENV_VAR: 1\n"
            "steps:\n"
            "  - run: echo ${{ secrets.WORKFLOW_SECRET }}\n"
        )
        # docker-compose
        (root / "docker-compose.yml").write_text(
            "services:\n  db:\n    environment:\n"
            "      COMPOSE_DB_URL: postgres://...\n"
        )
        # Dockerfile
        (root / "Dockerfile").write_text(
            "FROM alpine\nENV DOCKERFILE_VAR=1\n"
            "ARG DOCKERFILE_ARG\n")
        # Next config
        (root / "next.config.js").write_text(
            "module.exports = { env: { NEXT_PUBLIC_VAR: process.env.NEXT_PUBLIC_FOO }};")
        # vercel.json
        (root / "vercel.json").write_text(
            '{"env": {"VERCEL_ENV_KEY": "@some-secret"}}'
        )
        # AST scan: Python
        (root / "app.py").write_text(
            "import os\n"
            "url = os.environ['PY_AST_KEY']\n"
            "k = os.getenv('PY_GETENV_KEY')\n"
        )
        # AST scan: TS
        (root / "app.ts").write_text(
            "const x = process.env.TS_AST_KEY;\n")

        manifest = {"env_vars": ["EXISTING"]}
        ext = extend_manifest_from_sources(manifest, root)

        for name in ("WORKFLOW_ENV_VAR",
                       "DOCKERFILE_VAR", "DOCKERFILE_ARG",
                       "NEXT_PUBLIC_FOO", "PY_AST_KEY",
                       "PY_GETENV_KEY", "TS_AST_KEY"):
            if name in ext["env_vars"]:
                _ok(f"{name}: extracted")
            else:
                _fail(f"{name}: NOT extracted",
                       f"got {sorted(ext['env_vars'])}")
        _assert_true("env_var_sources map populated",
                      bool(ext.get("env_var_sources")))
        _assert_true("preserves original env_vars",
                      "EXISTING" in ext["env_vars"])


# ─── project_history_names ──────────────────────────────────────────────────


def test_project_history_names() -> None:
    _step("project_history_names + git history walking")
    from rewriter import project_history_names
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        try:
            for cmd in (
                ["git", "init", "-q", "-b", "main", str(root)],
                ["git", "-C", str(root), "config", "user.email", "t@x"],
                ["git", "-C", str(root), "config", "user.name", "t"],
            ):
                subprocess.run(cmd, check=True, capture_output=True)
            (root / ".env.example").write_text(
                "DATABASE_URL=\nLEGACY_API_KEY=\nKEEP_VAR=\n")
            subprocess.run(["git", "-C", str(root), "add",
                              ".env.example"], check=True,
                              capture_output=True)
            subprocess.run(["git", "-C", str(root), "commit", "-q",
                              "-m", "init"], check=True,
                              capture_output=True)
            (root / ".env.example").write_text(
                "DATABASE_URL=\nKEEP_VAR=\n")
            subprocess.run(["git", "-C", str(root), "commit", "-q",
                              "-am", "remove legacy"], check=True,
                              capture_output=True)
        except subprocess.CalledProcessError as e:
            _fail("git setup failed", str(e)); return

        history = project_history_names(root)
        _assert_true("LEGACY_API_KEY in history",
                      "LEGACY_API_KEY" in history,
                      f"got {sorted(history)}")
        _assert_true("KEEP_VAR (still present) in history",
                      "KEEP_VAR" in history)
        _assert_true("DATABASE_URL in history",
                      "DATABASE_URL" in history)

    # Outside a git repo → empty set, no crash
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / ".env.example").write_text("X=\n")
        history = project_history_names(root)
        _assert_true("non-git project returns empty set",
                      history == set())


# ─── Cascade orchestrator ───────────────────────────────────────────────────


def test_cascade_verdict_priority() -> None:
    _step("cascade verdict-priority and tie-breaking")
    from rewriter import cascade, load_sdk_catalog

    # Case A: strategies vote OK (exact) AND ACCEPT_SDK (sdk_catalog)
    # → OK wins (priority 4 > 3)
    out = cascade("NODE_ENV", "env",
                     candidates=["NODE_ENV"],
                     project_deps=set(),
                     sdk_catalog=load_sdk_catalog(),
                     source_text="",
                     manifest={"env_vars": ["NODE_ENV"]})
    _assert_true("OK beats ACCEPT_SDK",
                  out.final_verdict == "OK",
                  f"got {out.final_verdict}, "
                  f"chosen={out.chosen and out.chosen.strategy}")

    # Case B: strategies vote ACCEPT_SDK (sdk_catalog) AND RESIDUAL
    # (everything else) → ACCEPT_SDK wins
    out = cascade("LD_SDK_KEY", "env",
                     candidates=["DATABASE_URL"],
                     project_deps={"@launchdarkly/node-server-sdk"},
                     sdk_catalog=load_sdk_catalog(),
                     source_text="",
                     manifest={"env_vars": ["DATABASE_URL"]})
    _assert_true("ACCEPT_SDK beats RESIDUAL",
                  out.final_verdict == "ACCEPT_SDK",
                  f"got {out.final_verdict}")

    # Case C: only RESIDUAL outcomes → RESIDUAL wins (highest
    # confidence among them)
    out = cascade("WHOLLY_NEW", "env",
                     candidates=["DATABASE_URL"],
                     project_deps=set(),
                     sdk_catalog=load_sdk_catalog(),
                     source_text="",
                     manifest={"env_vars": ["DATABASE_URL"]})
    _assert_true("all-RESIDUAL → RESIDUAL",
                  out.final_verdict == "RESIDUAL",
                  f"got {out.final_verdict}")


def test_cascade_per_domain_stacks() -> None:
    _step("cascade respects per-domain stack composition")
    from rewriter import STRATEGY_STACKS
    for dom in ("env", "db", "route", "graphql", "flag"):
        _assert_true(f"`{dom}` stack defined",
                      dom in STRATEGY_STACKS
                          and len(STRATEGY_STACKS[dom]) > 0)
    # env must include import_gating + sdk_catalog (the SDK signal
    # path that drives 13/15 of v0.5.0a fixes)
    env_names = {f.__name__ if hasattr(f, "__name__") else ""
                   for f in STRATEGY_STACKS["env"]}
    _assert_true("env stack has import_gating",
                  "strategy_import_gating" in env_names)
    _assert_true("env stack has sdk_catalog",
                  "strategy_sdk_catalog" in env_names)
    _assert_true("env stack has comment_context",
                  "strategy_comment_context" in env_names)


# ─── rewrite() integration: word-boundary substitution ─────────────────────


def test_rewrite_word_boundary() -> None:
    _step("rewrite() applies WORD-BOUNDARY substitution, "
            "not substring")
    from rewriter import rewrite, load_sdk_catalog

    # Replace `DATABSE_URL` (typo) with `DATABASE_URL`. Should not
    # affect a different identifier that happens to contain
    # `DATABSE_URL` as substring.
    text = "const a = DATABSE_URL;\nconst b = MY_DATABSE_URL_PREFIX;"
    manifest = {"env_vars": ["DATABASE_URL"], "models": [],
                  "user_fields": [], "post_fields": [], "routes": [],
                  "gql_types": [], "flags": []}
    rewritten, _ = rewrite(text, manifest, project_deps=set(),
                                bad_names={"env": ["DATABSE_URL"],
                                            "db": [], "route": [],
                                            "graphql": [], "flag": []},
                                sdk_catalog=load_sdk_catalog())
    _assert_true("typo got replaced",
                  "const a = DATABASE_URL;" in rewritten,
                  rewritten[:200])
    _assert_true("substring inside other identifier untouched",
                  "MY_DATABSE_URL_PREFIX" in rewritten,
                  rewritten[:200])


def test_rewrite_empty_inputs() -> None:
    _step("rewrite() handles empty inputs gracefully")
    from rewriter import rewrite, load_sdk_catalog
    text = "const x = 1;"
    rewritten, outcomes = rewrite(text,
                                       {"env_vars": [], "models": [],
                                         "user_fields": [], "post_fields": [],
                                         "routes": [], "gql_types": [],
                                         "flags": []},
                                       project_deps=set(),
                                       bad_names={"env": [], "db": [],
                                                   "route": [], "graphql": [],
                                                   "flag": []},
                                       sdk_catalog=load_sdk_catalog())
    _assert_true("empty bad_names → no outcomes",
                  outcomes == [])
    _assert_true("text unchanged", rewritten == text)


# ─── Main ───────────────────────────────────────────────────────────────────


def main() -> int:
    print("claude-dejavu helpers + cascade tests")
    print(f"  ROOT={ROOT}")
    tests = [
        test_trigrams,
        test_normalize_separators,
        test_token_set,
        test_token_edit_distance,
        test_manifest_candidates,
        test_load_sdk_catalog,
        test_extend_manifest_from_sources,
        test_project_history_names,
        test_cascade_verdict_priority,
        test_cascade_per_domain_stacks,
        test_rewrite_word_boundary,
        test_rewrite_empty_inputs,
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
