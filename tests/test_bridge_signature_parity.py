"""Regression test: dejavu_bridge stub and real wheel MUST expose the
same call shape for every primitive claude-dejavu uses.

Why this exists
===============
v0.8.7 audit (2026-05-19) found that v0.8.6 had silently been calling
``_bridge.disclose(hits, token_budget=N)`` — but the real wheel's
signature is ``disclose(candidates, budget_tokens)``. The stub used
the matching wrong names, so the unit suite passed both modes
identically. The real wheel raised TypeError on every JIT recall
invocation, which was caught by a ``try/except: pass`` and silently
fell back to the v0.8.5 codepath. Token-budget disclosure was
effectively never running in v0.8.6 or v0.8.7's pre-audit state.

This file locks the two surfaces together: it imports BOTH the real
wheel (skip if unavailable — degraded-mode environments) AND the in-
tree stub, and asserts every primitive can be called with the SAME
canonical call shape we use in production code. If either surface
diverges, this test fails before push.

Coverage
========
For each of the 6 primitives we exercise the EXACT call form the
production callers use. The test passes if both surfaces accept it.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

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


# ─── Load both surfaces (real may be absent in pure-stub envs) ──────────────

import dejavu_bridge_stub as stub  # type: ignore[import]

try:
    import dejavu_bridge as real  # type: ignore[import]
    _HAVE_REAL = True
except ImportError:
    real = None  # type: ignore[assignment]
    _HAVE_REAL = False


def _both(test_name: str, exercise):
    """Run `exercise(mod)` against both stub and real (if available).
    Each must not raise."""
    _step(test_name)
    _aT(f"stub: {test_name} accepts canonical call",
         _try(lambda: exercise(stub)),
         f"stub raised")
    if _HAVE_REAL:
        _aT(f"real: {test_name} accepts canonical call",
             _try(lambda: exercise(real)),
             f"real raised")
    else:
        _ok(f"real: skipped (dejavu_bridge wheel not installed)")


def _try(fn) -> bool:
    try:
        fn()
        return True
    except Exception as exc:
        print(f"      raised: {type(exc).__name__}: {exc}")
        return False


# ─── Canonical call-shape tests ─────────────────────────────────────────────

def test_distill_canonical():
    """Production caller (distill_pipeline) uses
    ``_bridge.distill(text, tier=tier)``."""
    _both("distill(text, tier=2)",
          lambda mod: mod.distill("lorem ipsum dolor sit amet et cetera"
                                  " yadda yadda yadda yadda"
                                  " more padding here for tier-2 cap",
                                  tier=2))


def test_gate_canonical():
    """Production caller (ingester) uses ``_bridge.gate(text)``."""
    _both("gate(text)",
          lambda mod: mod.gate("a sufficiently long sentence with content"
                               " for the gate filter to evaluate"))


def test_disclose_canonical():
    """Production caller (JIT hook) uses
    ``_bridge.disclose(candidates, budget_tokens=N)`` with
    ``{id, text, score}`` candidates. v0.8.7 audit regression."""
    _both("disclose(candidates, budget_tokens=100)",
          lambda mod: mod.disclose(
              [{"id": "1", "text": "first", "score": 9.0},
               {"id": "2", "text": "second", "score": 7.0}],
              budget_tokens=100,
          ))


def test_retrieve_rrf_canonical():
    """Production caller (recall, JIT) uses
    ``_bridge.retrieve_rrf([list1, list2])`` with string ids."""
    _both("retrieve_rrf([list1, list2]) string-id dicts",
          lambda mod: mod.retrieve_rrf(
              [[{"id": "a"}, {"id": "b"}],
               [{"id": "c"}, {"id": "a"}]]
          ))


def test_sem_cache_lookup_canonical():
    """Production caller (query_embed.lookup_by_vector) uses
    ``_bridge.sem_cache_lookup(query_vec, entries, high=h, low=l)``."""
    _both("sem_cache_lookup(query_vec, entries, high=0.97, low=0.92)",
          lambda mod: mod.sem_cache_lookup(
              [1.0, 0.0, 0.0, 0.0],
              [{"key_vec": [0.99, 0.01, 0.0, 0.0],
                "value_json": "{}"}],
              high=0.97, low=0.92,
          ))


def test_topic_reweight_canonical():
    """Forward-compat (deferred to v0.8.8). Real wheel signature:
    ``topic_reweight(hits, topic_id, hit_topics, same_topic_factor=0.7,
    cross_topic_factor=1.3)``."""
    _both("topic_reweight(hits, topic_id, hit_topics, ...)",
          lambda mod: mod.topic_reweight(
              [{"id": "1", "score": 5.0}],
              "topic-A",
              {"1": "topic-A"},
              same_topic_factor=0.7,
              cross_topic_factor=1.3,
          ))


# ─── Misuse detection (the actual bug v0.8.7 audit caught) ──────────────────

def test_topic_reweight_with_wrong_kwarg_fails_loudly():
    """v0.8.9 audit equivalent of the v0.8.7 disclose-token_budget bug.
    The real wheel's signature is
    ``topic_reweight(hits, topic_id, hit_topics, same_topic_factor=…,
    cross_topic_factor=…)``. Common misuse would be passing
    ``same_factor=…`` (matching our config-env-style key) — this
    MUST be rejected by both surfaces so we never silently fall
    through into the bare except path."""
    _step("topic_reweight(..., same_factor=…) is rejected by both surfaces")

    def _both_reject(name: str, mod):
        try:
            mod.topic_reweight(
                [{"id": "1", "score": 5.0}],
                "topic-A",
                {"1": "topic-A"},
                same_factor=0.7,  # wrong name; canonical is same_topic_factor
                cross_factor=1.3,
            )
            _fail(f"{name}: accepted wrong kwarg",
                  "should have raised TypeError")
        except TypeError:
            _ok(f"{name}: TypeError on same_factor= (correct)")
        except Exception as exc:
            _fail(f"{name}: wrong-but-not-TypeError",
                  f"{type(exc).__name__}: {exc}")

    _both_reject("stub", stub)
    if _HAVE_REAL:
        _both_reject("real", real)
    else:
        _ok("real: skipped (wheel not installed)")


def test_topic_reweight_unmapped_id_is_neutral():
    """Stub MUST match the real wheel on unmapped-id neutral behavior.
    Real wheel returns factor 1.0 for any id not in hit_topics; the
    stub historically returned cross_topic_factor (divergence!).
    Fixed in v0.8.9 to match real-wheel semantics."""
    _step("topic_reweight: unmapped id → factor 1.0 (stub matches real)")

    def _check(name: str, mod):
        r = mod.topic_reweight(
            [{"id": "1", "score": 10.0}, {"id": "2", "score": 10.0}],
            "topic-A",
            {"1": "topic-A"},  # '2' deliberately omitted
            same_topic_factor=0.7,
            cross_topic_factor=1.3,
        )
        by_id = {entry.get("id"): entry for entry in r if isinstance(entry, dict)}
        # mapped same-topic → factor 0.7 → score 7.0
        same_ok = abs(float(by_id["1"]["score"]) - 7.0) < 1e-6
        # unmapped → neutral factor 1.0 → score 10.0
        unmapped_ok = abs(float(by_id["2"]["score"]) - 10.0) < 1e-6
        _aT(f"{name}: same-topic id mapped to 0.7 factor", same_ok,
            f"got score: {by_id.get('1', {}).get('score')}")
        _aT(f"{name}: unmapped id is NEUTRAL (factor 1.0)", unmapped_ok,
            f"got score: {by_id.get('2', {}).get('score')}")

    _check("stub", stub)
    if _HAVE_REAL:
        _check("real", real)
    else:
        _ok("real: skipped (wheel not installed)")


def test_disclose_with_wrong_kwarg_fails_loudly():
    """The pre-audit bug: ``disclose(hits, token_budget=N)`` silently
    failed against the real wheel because that's not the kwarg name.
    Assert that BOTH surfaces still reject this — so we never re-
    introduce the divergence by adapting the stub to accept the wrong
    name."""
    _step("disclose(..., token_budget=N) is rejected by both surfaces")

    def _both_reject(name: str, mod):
        try:
            mod.disclose([{"id": "1", "text": "x", "score": 1.0}],
                          token_budget=10)
            _fail(f"{name}: accepted wrong kwarg",
                  "should have raised TypeError")
        except TypeError:
            _ok(f"{name}: TypeError on token_budget= (correct)")
        except Exception as exc:
            _fail(f"{name}: wrong-but-not-TypeError",
                  f"{type(exc).__name__}: {exc}")

    _both_reject("stub", stub)
    if _HAVE_REAL:
        _both_reject("real", real)
    else:
        _ok("real: skipped (wheel not installed)")


# ─── Runner ──────────────────────────────────────────────────────────────────

def main() -> int:
    tests = [
        test_distill_canonical,
        test_gate_canonical,
        test_disclose_canonical,
        test_retrieve_rrf_canonical,
        test_sem_cache_lookup_canonical,
        test_topic_reweight_canonical,
        test_topic_reweight_with_wrong_kwarg_fails_loudly,
        test_topic_reweight_unmapped_id_is_neutral,
        test_disclose_with_wrong_kwarg_fails_loudly,
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
