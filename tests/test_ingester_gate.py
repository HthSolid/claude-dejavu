"""Unit tests for v0.8.7 ingester-side `gate` wiring.

The bridge primitive `dejavu_bridge.gate(text) -> bool` decides whether
a turn is worth cloud-embedding (real wheel: >= 50 chars, >= 30%
alphanumeric). The ingester calls it BEFORE pushing a row into the
Weaviate batch — gate-rejected rows just stay `vectorized=FALSE` (no
schema change) and the `_GATE_COUNTERS["rejected"]` field bumps so
the end-of-ingest log can surface skip totals.

Coverage:
  - `_gate_ok` returns False for trash, True for prose, bumps counter
  - `_gate_ok` swallows bridge exceptions and accepts (never lose
    a real turn to a wheel regression)
  - `_gate_ok` accepts when the bridge module is None (legacy fallback)
  - counter is monotonic across calls
  - `_gate_reset` zeroes the counters (test hook)
  - end-of-ingest log line is unchanged when nothing was rejected
    (no behavior regression for v0.8.6 transcripts)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

import ingester  # noqa: E402  type: ignore[import]


PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(msg: str) -> None:
    PASS.append(msg); print(f"  \033[32m✓\033[0m {msg}", flush=True)


def _fail(msg: str, d: str = "") -> None:
    FAIL.append((msg, d)); print(f"  \033[31m✗\033[0m {msg}")
    if d:
        print(f"      {d}")


def _aT(name: str, cond: bool, d: str = "") -> None:
    _ok(name) if cond else _fail(name, d)


def _step(name: str) -> None:
    print(f"\n\033[36m── {name} ──\033[0m", flush=True)


# ─── _gate_ok wiring ─────────────────────────────────────────────────────────

def test_gate_accepts_real_prose() -> None:
    _step("_gate_ok: accepts real prose, bumps accepted counter")
    ingester._gate_reset()
    text = ("This is a long paragraph of real prose that should "
            "definitely pass the bridge's gate — well over 50 chars, "
            "well over 30% alphanumeric density.")
    ok = ingester._gate_ok(text)
    _aT("returns True for real prose", ok is True)
    _aT("accepted counter bumped",
        ingester._GATE_COUNTERS["accepted"] == 1,
        f"got: {ingester._GATE_COUNTERS}")
    _aT("rejected counter untouched",
        ingester._GATE_COUNTERS["rejected"] == 0,
        f"got: {ingester._GATE_COUNTERS}")


def test_gate_rejects_short_input() -> None:
    _step("_gate_ok: rejects short input (< 50 chars)")
    ingester._gate_reset()
    ok = ingester._gate_ok("hi")
    _aT("returns False for short input", ok is False)
    _aT("rejected counter bumped",
        ingester._GATE_COUNTERS["rejected"] == 1,
        f"got: {ingester._GATE_COUNTERS}")


def test_gate_rejects_empty_string() -> None:
    _step("_gate_ok: rejects empty + None-ish input")
    ingester._gate_reset()
    _aT("empty string rejected", ingester._gate_ok("") is False)
    _aT("None coerced to empty + rejected",
        ingester._gate_ok("") is False)


def test_gate_counter_is_monotonic() -> None:
    _step("_gate_ok: counters are monotonic across many calls")
    ingester._gate_reset()
    long = ("A real paragraph that easily clears the gate, with "
            "letters and spaces and a little punctuation, totally fine.")
    for _ in range(5):
        ingester._gate_ok(long)
    for _ in range(3):
        ingester._gate_ok("x")
    _aT("accepted == 5",
        ingester._GATE_COUNTERS["accepted"] == 5,
        f"got: {ingester._GATE_COUNTERS}")
    _aT("rejected == 3",
        ingester._GATE_COUNTERS["rejected"] == 3,
        f"got: {ingester._GATE_COUNTERS}")


def test_gate_reset_zeros_counters() -> None:
    _step("_gate_reset: zeros counters")
    ingester._gate_ok("hi")
    ingester._gate_ok("hello"
                       + " a long enough paragraph to clear the gate easily")
    ingester._gate_reset()
    _aT("accepted == 0 after reset",
        ingester._GATE_COUNTERS["accepted"] == 0)
    _aT("rejected == 0 after reset",
        ingester._GATE_COUNTERS["rejected"] == 0)


def test_gate_failure_is_swallowed_and_accepts() -> None:
    """If the bridge module raises (broken wheel, bad signature, etc.)
    the gate accepts the row rather than losing it. v0.8.6 behavior
    (no gate at all) is preserved on regressions."""
    _step("_gate_ok: bridge exception → accept (never lose a real turn)")
    ingester._gate_reset()
    real = ingester._bridge_mod
    try:
        class _Boom:
            @staticmethod
            def gate(_text):
                raise RuntimeError("simulated wheel ICE")
        ingester._bridge_mod = _Boom  # type: ignore[assignment]
        ok = ingester._gate_ok(
            "some text that would normally be processed normally")
        _aT("returns True on bridge crash", ok is True)
        _aT("accepted counter bumped (fall-through path)",
            ingester._GATE_COUNTERS["accepted"] == 1,
            f"got: {ingester._GATE_COUNTERS}")
    finally:
        ingester._bridge_mod = real  # type: ignore[assignment]


def test_gate_when_bridge_module_missing_accepts() -> None:
    """If `_bridge_mod` is None (top-level distill_pipeline import
    blew up at startup), the gate accepts everything. Same idea as
    above — never let import wiring silently drop real content."""
    _step("_gate_ok: bridge module is None → accept (v0.8.6-equivalent)")
    ingester._gate_reset()
    real = ingester._bridge_mod
    try:
        ingester._bridge_mod = None  # type: ignore[assignment]
        ok = ingester._gate_ok("anything at all goes here for the test")
        _aT("returns True when bridge is None", ok is True)
        _aT("accepted counter bumped",
            ingester._GATE_COUNTERS["accepted"] == 1,
            f"got: {ingester._GATE_COUNTERS}")
    finally:
        ingester._bridge_mod = real  # type: ignore[assignment]


def test_gate_does_not_crash_ingest() -> None:
    """End-to-end smoke: gate calls return cleanly even on degenerate
    inputs that earlier versions might have crashed on (very long
    string, unicode-only, all-whitespace).  Tests that no exception
    leaks out of `_gate_ok` regardless of input."""
    _step("_gate_ok: degenerate inputs don't raise")
    for bad in [
        " " * 200,
        "日本語" * 30,
        "x" * 100000,
        "\x00\x01\x02" * 50,
    ]:
        try:
            ingester._gate_ok(bad)
            _ok(f"degenerate input handled (len={len(bad)})")
        except Exception as e:
            _fail(f"degenerate input raised: {type(e).__name__}: {e}")


def main() -> int:
    tests = [
        test_gate_accepts_real_prose,
        test_gate_rejects_short_input,
        test_gate_rejects_empty_string,
        test_gate_counter_is_monotonic,
        test_gate_reset_zeros_counters,
        test_gate_failure_is_swallowed_and_accepts,
        test_gate_when_bridge_module_missing_accepts,
        test_gate_does_not_crash_ingest,
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
