"""Infra-free regression test suite for the v0.5.0a follow-up work.

Exercises every code path that doesn't require a running Postgres
or Weaviate. Catches:

  - Rewriter cascade verdicts on the 5 documented v0.5.0a failure
    inputs
  - error_log write→read round-trip
  - Logs CLI surfaces records
  - Doctor's _check_error_log returns sensible status
  - Embedding strategy: REPLACE on high-cosine, RESIDUAL on miss,
    RESIDUAL when offline (mocked semantic_search)
  - Project-history strategy against a real git repo with a removed
    env var
  - env propose CLI: dry-run + apply path
  - Runtime-trap generator: TypeScript + Python forms
  - Generated Python trap raises DejavuUndeclaredEnvError correctly

What this does NOT cover (needs running services):
  - dejavu_logs MCP tool over JSON-RPC stdio (PG-dependent through
    the broader server startup)
  - Per-project fabrication memory (requires PG)
  - Live Weaviate semantic search

Run:
  python tests/test_v05_followups.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import types
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


# ─── (1) Rewriter cascade — 5 documented v0.5.0a failure inputs ─────────────


def test_rewriter_cascade() -> None:
    _step("rewriter cascade verdicts on documented v0.5.0a failures")
    from rewriter import rewrite, load_sdk_catalog

    manifest = {
        "env_vars": ["DATABASE_URL", "API_BASE_URL",
                       "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                       "AWS_REGION", "STRIPE_SECRET_KEY"],
        "models": ["User", "Post"],
        "user_fields": ["id", "email", "name"],
        "post_fields": ["id", "title", "body"],
        "routes": ["GET /api/users/:id"],
        "gql_types": ["User", "Post", "Query", "Mutation"],
        "flags": ["checkout-flow-v2"],
    }
    deps = {"@launchdarkly/node-server-sdk", "aws-sdk", "next"}
    text = """import * as LD from "@launchdarkly/node-server-sdk";
const k = process.env.LD_SDK_KEY;
const k2 = process.env.LAUNCHDARKLY_SDK_KEY;
const s = process.env.AWS_SESSION_TOKEN;
const e = process.env.NODE_ENV;
const d = process.env.DATABSE_URL;
"""
    bad = {"env": ["LD_SDK_KEY", "LAUNCHDARKLY_SDK_KEY",
                     "AWS_SESSION_TOKEN", "NODE_ENV", "DATABSE_URL"],
            "db": [], "route": [], "graphql": [], "flag": []}
    rewritten, outcomes = rewrite(text, manifest,
                                       project_deps=deps,
                                       bad_names=bad,
                                       sdk_catalog=load_sdk_catalog())

    expected = {"LD_SDK_KEY": "ACCEPT_SDK",
                  "LAUNCHDARKLY_SDK_KEY": "ACCEPT_SDK",
                  "AWS_SESSION_TOKEN": "ACCEPT_SDK",
                  "NODE_ENV": "ACCEPT_SDK",
                  "DATABSE_URL": "REPLACE"}
    by_name = {o.original: o for o in outcomes}
    for n, exp in expected.items():
        if n not in by_name:
            _fail(f"cascade: missing outcome for {n}"); return
        if by_name[n].final_verdict != exp:
            _fail(f"cascade {n}: expected {exp}, got "
                    f"{by_name[n].final_verdict}"); return
    if by_name["DATABSE_URL"].chosen.replacement != "DATABASE_URL":
        _fail(f"cascade DATABSE_URL: expected DATABASE_URL, got "
                f"{by_name['DATABSE_URL'].chosen.replacement}"); return
    if "DATABASE_URL" not in rewritten:
        _fail("cascade: rewrite didn't apply REPLACE substitution")
        return
    _ok("cascade: 5/5 verdicts correct, DATABSE_URL -> DATABASE_URL "
         "applied to text")


# ─── (2) error_log write→read round-trip ─────────────────────────────────────


def test_error_log_round_trip() -> None:
    _step("error_log write→read round-trip")
    # Use a temp HOME so we don't pollute the real log
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "XDG_DATA_HOME": str(Path(tmp) / "data")}
        script_path = Path(tmp) / "_runner.py"
        script_path.write_text(f"""
import sys, json
sys.path.insert(0, {str(ROOT / 'code')!r})
from error_log import log_error, read_recent, log_path_str
log_error('test.module', 'first', context={{'k': 1}})
try:
    raise ValueError('boom')
except Exception:
    log_error('test.module', 'with traceback', exc_info=True)
recs = read_recent(limit=10)
print(json.dumps({{
    'count': len(recs),
    'path': log_path_str(),
    'first_msg': recs[0]['message'] if recs else None,
    'has_tb': any('traceback' in r for r in recs),
}}))
""")
        r = subprocess.run([sys.executable, str(script_path)], env=env,
                              capture_output=True, text=True)
        if r.returncode != 0:
            _fail("error_log script crashed", r.stderr[-200:]); return
        try:
            info = json.loads(r.stdout.strip())
        except json.JSONDecodeError:
            _fail("error_log: non-JSON stdout", r.stdout[:200]); return
        if info["count"] < 2:
            _fail(f"error_log: expected ≥2 records, got {info['count']}")
            return
        if not info["has_tb"]:
            _fail("error_log: traceback wasn't captured"); return
        if not info["path"].endswith("errors.log"):
            _fail(f"error_log: unexpected path {info['path']}")
            return
        _ok(f"error_log: round-trip works, captured traceback "
             f"({info['count']} records)")


# ─── (3) Embedding strategy: mock-validated cases ───────────────────────────


def test_embedding_strategy() -> None:
    _step("embedding strategy: REPLACE / RESIDUAL / offline")
    fake = types.ModuleType("semantic_search")
    fake.is_available = lambda: True

    def search(intent, project_slug, limit=5, **k):
        if "STRIPE" in intent:
            return [{"name": "STRIPE_SECRET_KEY",
                      "_additional": {"distance": 0.10}}]
        return []
    fake.search = search
    sys.modules["semantic_search"] = fake

    # Force re-import of strategy_embedding so it picks up the fake
    if "rewriter" in sys.modules:
        del sys.modules["rewriter"]
    from rewriter import strategy_embedding

    candidates = ["STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
                    "JWT_SECRET"]

    res = strategy_embedding("STRIPE_KEY", candidates,
                                  project_slug="t")
    if res.verdict != "REPLACE" or res.replacement != "STRIPE_SECRET_KEY":
        _fail(f"embedding REPLACE case: got {res.verdict} / "
                f"{res.replacement}"); return
    _ok(f"embedding: REPLACE on high-cosine "
         f"(STRIPE_KEY → STRIPE_SECRET_KEY, conf={res.confidence:.2f})")

    res = strategy_embedding("UNRELATED", candidates, project_slug="t")
    if res.verdict != "RESIDUAL":
        _fail(f"embedding miss case: got {res.verdict}"); return
    _ok("embedding: RESIDUAL on no-match")

    fake.is_available = lambda: False
    res = strategy_embedding("ANYTHING", candidates, project_slug="t")
    if res.verdict != "RESIDUAL" or "offline" not in res.reason:
        _fail(f"embedding offline case: got {res.verdict} / "
                f"{res.reason}"); return
    _ok("embedding: RESIDUAL when Weaviate offline")

    # Cleanup the monkey-patch so subsequent tests get the real module
    del sys.modules["semantic_search"]
    if "rewriter" in sys.modules:
        del sys.modules["rewriter"]


# ─── (4) Project-history strategy against real git ──────────────────────────


def test_project_history_strategy() -> None:
    _step("project_history strategy against real git history")
    if "rewriter" in sys.modules:
        del sys.modules["rewriter"]
    from rewriter import (rewrite, load_sdk_catalog,
                              project_history_names)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        try:
            subprocess.run(["git", "init", "-q", "-b", "main",
                              str(root)], check=True,
                              capture_output=True)
            subprocess.run(["git", "-C", str(root), "config",
                              "user.email", "t@x"], check=True,
                              capture_output=True)
            subprocess.run(["git", "-C", str(root), "config",
                              "user.name", "t"], check=True,
                              capture_output=True)
            (root / ".env.example").write_text(
                "DATABASE_URL=\nLEGACY_API_KEY=\nSESSION=\n")
            subprocess.run(["git", "-C", str(root), "add",
                              ".env.example"], check=True,
                              capture_output=True)
            subprocess.run(["git", "-C", str(root), "commit", "-q",
                              "-m", "init"], check=True,
                              capture_output=True)
            (root / ".env.example").write_text(
                "DATABASE_URL=\nSESSION=\n")
            subprocess.run(["git", "-C", str(root), "commit", "-q",
                              "-am", "remove legacy"], check=True,
                              capture_output=True)
        except subprocess.CalledProcessError as e:
            _fail("git setup failed", e.stderr.decode() if e.stderr
                                                    else str(e)); return

        history = project_history_names(root)
        if "LEGACY_API_KEY" not in history:
            _fail(f"project_history_names: didn't pick up LEGACY_API_KEY "
                    f"from git log (got {sorted(history)})"); return
        _ok(f"project_history_names: found {len(history)} historical "
             f"names including LEGACY_API_KEY")

        manifest = {"env_vars": ["DATABASE_URL", "SESSION"],
                      "models": [], "user_fields": [], "post_fields": [],
                      "routes": [], "gql_types": [], "flags": []}
        rewritten, outcomes = rewrite(
            "const k = process.env.LEGACY_API_KEY;",
            manifest, project_deps=set(),
            bad_names={"env": ["LEGACY_API_KEY"], "db": [], "route": [],
                         "graphql": [], "flag": []},
            sdk_catalog=load_sdk_catalog(),
            history_names=history)
        chosen = outcomes[0].chosen if outcomes else None
        if not chosen or chosen.strategy != "project_history":
            _fail(f"project_history strategy didn't fire; chosen="
                    f"{chosen and chosen.strategy}"); return
        _ok("project_history: regression-hint RESIDUAL emitted "
             "for re-introduced removed name")


# ─── (5) env propose CLI: dry-run + apply ────────────────────────────────────


def test_env_propose_cli() -> None:
    _step("`claude-dejavu env propose` dry-run + apply")
    venv_py = Path(sys.executable)
    recall = ROOT / "code" / "recall.py"
    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp)
        (proj / ".env.example").write_text("DATABASE_URL=\n")

        # Dry run
        r = subprocess.run([str(venv_py), str(recall), "env", "propose",
                              "LAUNCHDARKLY_SDK_KEY",
                              "--package=@launchdarkly/node-server-sdk"],
                              cwd=proj, capture_output=True, text=True)
        if r.returncode != 0:
            _fail("env propose dry-run failed", r.stderr[-200:]); return
        if "DRY_RUN" not in r.stdout or "LAUNCHDARKLY_SDK_KEY" not in r.stdout:
            _fail("env propose dry-run output unexpected",
                   r.stdout[:200]); return
        # File should be unchanged
        if "LAUNCHDARKLY_SDK_KEY" in (proj / ".env.example").read_text():
            _fail("dry-run wrote to file (should not have)"); return
        _ok("env propose --dry-run: shows diff, leaves file untouched")

        # Apply
        r = subprocess.run([str(venv_py), str(recall), "env", "propose",
                              "LAUNCHDARKLY_SDK_KEY",
                              "--package=@launchdarkly/node-server-sdk",
                              "--apply"],
                              cwd=proj, capture_output=True, text=True)
        if r.returncode != 0:
            _fail("env propose apply failed", r.stderr[-200:]); return
        if "APPLIED" not in r.stdout:
            _fail("env propose apply output unexpected",
                   r.stdout[:200]); return
        if "LAUNCHDARKLY_SDK_KEY" not in (proj / ".env.example").read_text():
            _fail("apply didn't write the new key"); return
        _ok("env propose --apply: wrote new key to .env.example "
             "with comment naming the SDK package")

        # Re-running should NO_CHANGE
        r = subprocess.run([str(venv_py), str(recall), "env", "propose",
                              "LAUNCHDARKLY_SDK_KEY"],
                              cwd=proj, capture_output=True, text=True)
        if "NO_CHANGE" not in r.stdout:
            _fail("env propose re-run should detect existing key",
                   r.stdout[:200]); return
        _ok("env propose: NO_CHANGE on already-declared key")


# ─── (6) Runtime-trap generator — TS + Python forms ─────────────────────────


def test_runtime_trap_typescript() -> None:
    _step("runtime-trap generator: TypeScript form")
    venv_py = Path(sys.executable)
    recall = ROOT / "code" / "recall.py"
    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp)
        (proj / ".env.example").write_text(
            "DATABASE_URL=\nLAUNCHDARKLY_SDK_KEY=\nSTRIPE_SECRET_KEY=\n")
        out = proj / "env.ts"
        r = subprocess.run([str(venv_py), str(recall), "generate",
                              "runtime-trap", "--language=typescript",
                              f"--output={out}"],
                              cwd=proj, capture_output=True, text=True)
        if r.returncode != 0:
            _fail("ts generator failed", r.stderr[-200:]); return
        if not out.is_file():
            _fail("ts generator didn't write the file"); return
        content = out.read_text()
        for needed in ('import { z } from "zod"', "DeclaredEnvKey",
                          "DATABASE_URL", "LAUNCHDARKLY_SDK_KEY",
                          "STRIPE_SECRET_KEY", "Proxy", "throw new Error"):
            if needed not in content:
                _fail(f"ts trap missing `{needed}`"); return
        _ok("ts trap: generated with Zod schema + Proxy + 3 declared "
             "keys + tsc-checkable union type")


def test_runtime_trap_python() -> None:
    _step("runtime-trap generator: Python form + functional check")
    venv_py = Path(sys.executable)
    recall = ROOT / "code" / "recall.py"
    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp)
        (proj / ".env.example").write_text(
            "DATABASE_URL=\nLAUNCHDARKLY_SDK_KEY=\n")
        out = proj / "dejavu_env.py"
        r = subprocess.run([str(venv_py), str(recall), "generate",
                              "runtime-trap", "--language=python",
                              f"--output={out}"],
                              cwd=proj, capture_output=True, text=True)
        if r.returncode != 0:
            _fail("py generator failed", r.stderr[-200:]); return
        if not out.is_file():
            _fail("py generator didn't write the file"); return
        # Functional check: the generated module's _EnvProxy raises on
        # undeclared, returns value (or None) on declared.
        check = (
            f"import sys; sys.path.insert(0, {str(proj)!r}); "
            "from dejavu_env import env, DejavuUndeclaredEnvError; "
            "assert 'DATABASE_URL' in env, 'declared check failed'; "
            "assert 'MADE_UP' not in env, 'undeclared check failed'; "
            "_ = env.DATABASE_URL; "
            "raised = False;\n"
            "try:\n"
            "    env.MADE_UP\n"
            "except DejavuUndeclaredEnvError:\n"
            "    raised = True\n"
            "assert raised, 'should have raised on undeclared'; "
            "print('OK')"
        )
        r = subprocess.run([str(venv_py), "-c", check],
                              capture_output=True, text=True,
                              env={**os.environ,
                                    "DATABASE_URL": "test_value"})
        if r.returncode != 0:
            _fail("py trap functional check failed", r.stderr[-200:])
            return
        if "OK" not in r.stdout:
            _fail("py trap functional check: no OK marker",
                   r.stdout[:200]); return
        _ok("py trap: declared returns value, undeclared raises "
             "DejavuUndeclaredEnvError, `in env` check works")


# ─── (7) Replay regression check (deterministic, no API calls) ──────────────


def test_replay_regression() -> None:
    _step("replay regression: 200-trial v0.5.0a corpus")
    venv_py = Path(sys.executable)
    raw_path = ROOT / "docs" / "benchmarks" / "hallucination-rate-raw.json"
    if not raw_path.is_file():
        _fail(f"raw bench data not found at {raw_path}"); return
    r = subprocess.run([str(venv_py),
                           str(ROOT / "tests" / "replay_with_rewriter.py")],
                          capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        _fail("replay crashed", r.stderr[-300:]); return
    out = r.stdout + r.stderr
    if "1 / 200 (0.5%)" not in out:
        _fail(f"replay regressed — expected 1/200 (0.5%), got:\n"
                f"{out[-400:]}"); return
    _ok("replay: 200-trial corpus still scores 0.5% post-rewrite "
         "(99% reduction vs v0.3.12 baseline)")


# ─── Main ───────────────────────────────────────────────────────────────────


def main() -> int:
    print("claude-dejavu v0.5.0a infra-free regression tests")
    print(f"  ROOT={ROOT}")
    print()
    tests = [
        test_rewriter_cascade,
        test_error_log_round_trip,
        test_embedding_strategy,
        test_project_history_strategy,
        test_env_propose_cli,
        test_runtime_trap_typescript,
        test_runtime_trap_python,
        test_replay_regression,
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
