"""Unit tests for scripts/scrape_sdk_envs.py — the SDK catalog
auto-scraper. Pure-function tests with mocked HTTP."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(msg): PASS.append(msg); print(f"  \033[32m✓\033[0m {msg}", flush=True)
def _fail(msg, d=""): FAIL.append((msg, d)); print(f"  \033[31m✗\033[0m {msg}"); d and print(f"      {d}")
def _step(name): print(f"\n\033[36m── {name} ──\033[0m", flush=True)
def _aT(name, cond, d=""): _ok(name) if cond else _fail(name, d)


def test_extract_env_names() -> None:
    _step("extract_env_names: regex covers all env-access patterns")
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "scrape_sdk_envs",
        str(ROOT / "scripts" / "scrape_sdk_envs.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    text = """
    // typescript
    const a = process.env.TS_VAR_ONE;
    const b = process.env.TS_VAR_TWO;

    # python
    x = os.environ['PY_BRACKET']
    y = os.environ["PY_DOUBLE"]
    z = os.getenv('PY_GETENV')
    w = os.getenv("PY_GETENV2")

    # noise that should NOT be captured
    const c = NOT_AN_ENV_REF;       // doesn't have process.env. prefix
    docs say NODE_ENV is universal  // NODE_ENV is in NOISE list
    """
    found = mod.extract_env_names(text)
    expected = {"TS_VAR_ONE", "TS_VAR_TWO", "PY_BRACKET",
                  "PY_DOUBLE", "PY_GETENV", "PY_GETENV2"}
    _aT("regex catches all 6 patterns",
         expected.issubset(found),
         f"got {sorted(found)}")
    _aT("noise NODE_ENV excluded (in NOISE_NAMES)",
         "NODE_ENV" not in found,
         f"got {sorted(found)}")


def test_diff_proposal() -> None:
    _step("diff_proposal: only adds new names, never removes")
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "scrape_sdk_envs",
        str(ROOT / "scripts" / "scrape_sdk_envs.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    catalog = {
        "env": {
            "stripe": ["STRIPE_SECRET_KEY", "STRIPE_PUBLISHABLE_KEY"],
            "redis": ["REDIS_URL"],
        }
    }
    scraped = {
        "stripe": {"STRIPE_SECRET_KEY", "STRIPE_NEW_KEY"},  # 1 new
        "redis": {"REDIS_URL"},                               # 0 new
        "newpkg": {"NEW_PKG_KEY"},                             # whole new pkg
    }
    proposal = mod.diff_proposal(catalog, scraped)
    _aT("stripe: only the new key appears",
         proposal.get("stripe") == ["STRIPE_NEW_KEY"],
         f"got {proposal.get('stripe')}")
    _aT("redis: no diff (no new names)",
         "redis" not in proposal,
         f"got keys {sorted(proposal.keys())}")
    _aT("newpkg: full pkg appears",
         proposal.get("newpkg") == ["NEW_PKG_KEY"],
         f"got {proposal.get('newpkg')}")


def test_github_repo_resolution() -> None:
    _step("_github_repo_for: parses various repo URL formats")
    import importlib.util, types
    spec = importlib.util.spec_from_file_location(
        "scrape_sdk_envs",
        str(ROOT / "scripts" / "scrape_sdk_envs.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Patch _registry_meta to avoid hitting the network.
    cases = [
        ("git+https://github.com/owner/repo.git", "owner/repo"),
        ("git://github.com/owner/repo.git",       "owner/repo"),
        ("https://github.com/owner/repo",         "owner/repo"),
        ("https://github.com/owner/repo#readme",  "owner/repo"),
        ("https://gitlab.com/owner/repo",         None),  # not GitHub
    ]
    for url, expected in cases:
        meta = {
            "dist-tags": {"latest": "1.0.0"},
            "versions": {"1.0.0": {"repository": {"url": url}}},
        }
        mod._registry_meta = lambda pkg, _meta=meta: _meta
        got = mod._github_repo_for("any")
        _aT(f"`{url[:40]}…` → {expected!r}", got == expected,
             f"got {got!r}")


def main() -> int:
    print("claude-dejavu scraper tests")
    print(f"  ROOT={ROOT}")
    tests = [
        test_extract_env_names,
        test_diff_proposal,
        test_github_repo_resolution,
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
