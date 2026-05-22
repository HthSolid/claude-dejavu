#!/usr/bin/env bash
# Local-CI runner — authoritative pre-push gate. See CONTRIBUTING.md.
#
# Why this exists: today's audit-class bugs (v0.8.7 disclose silent
# failure against the real wheel; v0.8.8 test pollution of the live PG
# embed_cache) both slipped past unit tests AND past push-time review.
# This script runs the full regression suite + a few infra-free smoke
# checks BEFORE git accepts the push. The bridge-parity suite is part
# of run_all.py and would have caught v0.8.7's signature drift at the
# pre-push gate.
#
# Usage:
#   ./scripts/local-ci.sh            # full sweep (default)
#   ./scripts/local-ci.sh fast       # default for pre-push hook
#   ./scripts/local-ci.sh all        # same as no-arg
#   HTE_SKIP_LOCAL_CI=1 git push     # emergency bypass (logged to stderr)
#
# Exit codes:
#   0  every job passed
#   1  at least one job failed (push refused)
#   2  invalid usage
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"

if [ -t 1 ]; then
  BOLD=$'\e[1m'; GREEN=$'\e[32m'; RED=$'\e[31m'; YELLOW=$'\e[33m'; RESET=$'\e[0m'
else
  BOLD=""; GREEN=""; RED=""; YELLOW=""; RESET=""
fi
step() { printf '%s==>%s %s%s%s\n' "$GREEN" "$RESET" "$BOLD" "$1" "$RESET"; }
warn() { printf '%s==>%s %s%s%s\n' "$YELLOW" "$RESET" "$BOLD" "$1" "$RESET"; }
fail() { printf '%sFAIL:%s %s\n' "$RED" "$RESET" "$1" >&2; exit 1; }

mode="${1:-all}"
case "$mode" in
  fast|all) ;;
  *) echo "Usage: $0 [fast|all]" >&2; exit 2 ;;
esac

# ─── Locate the dejavu venv python ─────────────────────────────────────────
# Prefer the installed venv (production env where tests run); fall back to
# system python3 if the venv hasn't been set up yet (e.g. fresh clone).
VENV_PY="$HOME/.local/share/claude-dejavu/venv/bin/python3"
if [ ! -x "$VENV_PY" ]; then
  if command -v python3 >/dev/null 2>&1; then
    warn "dejavu venv missing at $VENV_PY — falling back to system python3"
    VENV_PY="$(command -v python3)"
  else
    fail "no python3 found (need dejavu venv or system python3)"
  fi
fi

# ─── Job 1: tests/run_all.py (always) ──────────────────────────────────────
step "tests/run_all.py — full infra-free regression suite"
if ! "$VENV_PY" tests/run_all.py; then
  fail "tests/run_all.py reported failures or crashed suites"
fi

# ─── Job 2: compileall — every .py parses cleanly (always) ────────────────
step "python -m compileall code/ hooks/ scripts/ tests/"
if ! "$VENV_PY" -m compileall -q code/ hooks/ scripts/ tests/ >/dev/null; then
  fail "compileall reported syntax errors"
fi

# ─── Job 3 (full mode only): bridge-parity stand-alone probe ──────────────
# Already inside run_all.py but worth re-running so a failure here surfaces
# louder. Cheap (<1s). Skipped in fast mode for speed.
if [ "$mode" = "all" ]; then
  step "bridge parity probe (real wheel signature drift sentry)"
  if ! "$VENV_PY" tests/test_bridge_signature_parity.py >/dev/null; then
    fail "bridge signature parity regression"
  fi
fi

printf '\n%sLocal CI:%s all jobs passed.\n' "$BOLD$GREEN" "$RESET"
