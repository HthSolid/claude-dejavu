"""Top-level test runner for the infra-free suite.

Runs every standalone test file and aggregates pass/fail counts.
Used as the canonical pre-publish regression check; doesn't need
Postgres or Weaviate. For full E2E coverage including the indexer
and MCP stdio paths, run `python tests/smoke.py` separately (needs
PG + Weaviate up)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

SUITES = [
    ("strategies", "test_strategies.py"),
    ("helpers + cascade", "test_helpers.py"),
    ("error_log", "test_error_log.py"),
    ("runtime_trap", "test_runtime_trap.py"),
    ("scraper", "test_scraper.py"),
    ("v05 followups (integration)", "test_v05_followups.py"),
    ("ingester robustness (NUL bytes / encoding / boundary)",
     "test_ingester_robustness.py"),
    ("install.py helpers (v0.5.0e — docker compose project name, "
       "INSTALL_STATUS, venv resolver, schema migrations)",
     "test_install.py"),
    ("health (storage integrity + auto-repair)",
     "test_health.py"),
]


def main() -> int:
    total_pass = 0
    total_fail = 0
    crashed: list[str] = []
    print("=" * 60)
    print("claude-dejavu — pre-publish regression suite")
    print("=" * 60)

    for label, fname in SUITES:
        print(f"\n┌─ {label}")
        print(f"│  ({fname})")
        print(f"└─ ", end="", flush=True)
        r = subprocess.run([sys.executable, str(ROOT / fname)],
                              capture_output=True, text=True)
        # Parse the "PASS: N     FAIL: M" footer
        out = r.stdout
        last_line = ""
        for line in reversed(out.splitlines()):
            if line.startswith("PASS:"):
                last_line = line
                break
        if not last_line:
            print(f"\033[31mCRASHED\033[0m (rc={r.returncode})")
            print(out[-500:])
            print(r.stderr[-500:])
            crashed.append(fname)
            continue
        # Extract numbers
        try:
            parts = last_line.replace(":", " ").split()
            p = int(parts[1]); f = int(parts[3])
        except (IndexError, ValueError):
            p = f = -1
        total_pass += max(0, p)
        total_fail += max(0, f)
        if r.returncode == 0:
            print(f"\033[32mOK\033[0m  ({p} pass)")
        else:
            print(f"\033[31mFAIL\033[0m  ({p} pass, {f} fail)")
            # Print the failure lines for context
            for ln in out.splitlines()[-10:]:
                print(f"   {ln}")

    print()
    print("=" * 60)
    print(f"TOTAL: {total_pass} pass, {total_fail} fail, "
            f"{len(crashed)} crashed suite(s)")
    if crashed:
        print(f"  crashed: {', '.join(crashed)}")
    print("=" * 60)
    return 0 if (total_fail == 0 and not crashed) else 1


if __name__ == "__main__":
    sys.exit(main())
