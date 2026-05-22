"""v0.9.3 unit tests — local vector index auto-enable.

The v0.9.1/v0.9.2 gate was opt-in via
CLAUDE_DEJAVU_LOCAL_VECTOR_INDEX=true. That kept the feature dormant
for nearly every user. v0.9.3 flips the default: AUTO-ON. The same
env var survives as an explicit kill switch only.

Tests pin the new defaults so a future regression that reintroduces
the opt-in gate fails the pre-push suite loudly."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

os.environ.setdefault("CLAUDE_DEJAVU_DATA_ROOT",
                       "/tmp/dejavu-v0903-test")
os.makedirs("/tmp/dejavu-v0903-test", exist_ok=True)

import vector_index  # type: ignore[import]


PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(msg: str) -> None:
    PASS.append(msg)
    print(f"  \033[32m✓\033[0m {msg}", flush=True)


def _fail(msg: str, detail: str = "") -> None:
    FAIL.append((msg, detail))
    print(f"  \033[31m✗\033[0m {msg}")
    if detail:
        print(f"      {detail[:300]}")


def _aT(name: str, cond: bool, detail: str = "") -> None:
    _ok(name) if cond else _fail(name, detail)


def _step(name: str) -> None:
    print(f"\n\033[36m── {name} ──\033[0m", flush=True)


def test_default_enabled_when_env_unset() -> None:
    _step("v0.9.3: vector_index_status reports enabled=True when "
          "CLAUDE_DEJAVU_LOCAL_VECTOR_INDEX is unset")
    orig = os.environ.pop("CLAUDE_DEJAVU_LOCAL_VECTOR_INDEX", None)
    try:
        s = vector_index.vector_index_status(
            Path(tempfile.mkdtemp(prefix="vi-default-on-")))
        _aT("enabled is True by default", s.get("enabled") is True,
            repr(s.get("enabled")))
    finally:
        if orig is not None:
            os.environ["CLAUDE_DEJAVU_LOCAL_VECTOR_INDEX"] = orig


def test_explicit_false_disables() -> None:
    _step("v0.9.3: env=false acts as a kill switch")
    for raw in ("false", "0", "no", "off"):
        os.environ["CLAUDE_DEJAVU_LOCAL_VECTOR_INDEX"] = raw
        try:
            s = vector_index.vector_index_status(
                Path(tempfile.mkdtemp(prefix="vi-disable-")))
            _aT(f"env={raw!r} → enabled is False",
                s.get("enabled") is False,
                repr(s.get("enabled")))
        finally:
            os.environ.pop("CLAUDE_DEJAVU_LOCAL_VECTOR_INDEX", None)


def test_explicit_true_still_enables() -> None:
    _step("v0.9.3: env=true is still honored (no-op vs default)")
    os.environ["CLAUDE_DEJAVU_LOCAL_VECTOR_INDEX"] = "true"
    try:
        s = vector_index.vector_index_status(
            Path(tempfile.mkdtemp(prefix="vi-true-")))
        _aT("env=true → enabled is True", s.get("enabled") is True)
    finally:
        os.environ.pop("CLAUDE_DEJAVU_LOCAL_VECTOR_INDEX", None)


def test_search_branch_auto_participates() -> None:
    _step("v0.9.3: mcp/server.py uses negated env gate "
          "(local_vec_disabled), not the old positive one")
    src = (ROOT / "mcp" / "server.py").read_text(encoding="utf-8")
    _aT("server.py uses `local_vec_disabled` name",
        "local_vec_disabled" in src)
    _aT("server.py no longer references `local_vec_enabled`",
        "local_vec_enabled" not in src,
        "old positive-gate variable should be gone")


def test_ingester_sync_auto_participates() -> None:
    _step("v0.9.3: ingester.py Stop-hook sync uses negated env gate")
    src = (ROOT / "code" / "ingester.py").read_text(encoding="utf-8")
    _aT("ingester references `local_vec_disabled`",
        "local_vec_disabled" in src)


def main() -> int:
    print("v0.9.3 vector_index auto-enable tests")
    tests = [
        test_default_enabled_when_env_unset,
        test_explicit_false_disables,
        test_explicit_true_still_enables,
        test_search_branch_auto_participates,
        test_ingester_sync_auto_participates,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            _fail(f"{t.__name__} raised", repr(e))
    print()
    print("=" * 60)
    print(f"PASS: {len(PASS)}     FAIL: {len(FAIL)}")
    if FAIL:
        for n, d in FAIL:
            print(f"  ✗ {n}  {d[:120]}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
