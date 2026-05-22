"""v0.9.4 unit tests — background vector-index bootstrap + HNSW
presets.

Two pieces:

  B. `maybe_start_background_bootstrap()` — fires a daemon thread to
     populate the local vector index on first MCP startup when the
     conditions hold (numpy available, index empty, not explicitly
     disabled, WV reachable). Subsequent callers see the in-progress
     flag and skip.

  bonus. `CLAUDE_DEJAVU_HNSW_PRESET=high-recall|balanced|low-memory`
     wires the three HNSW tunables in one env var. Individual env
     vars still WIN when set explicitly.

Infra-free. WV unreachable in the test → bootstrap path returns
without firing, exercising the failure-safe degradation we want.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

os.environ.setdefault("CLAUDE_DEJAVU_DATA_ROOT",
                       "/tmp/dejavu-v0904-test")
os.makedirs("/tmp/dejavu-v0904-test", exist_ok=True)

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


# ─── HNSW presets ─────────────────────────────────────────────────────────

def test_default_preset_is_balanced() -> None:
    _step("HNSW preset: default = 'balanced' (M=16, ef=100, efc=200)")
    for k in ("CLAUDE_DEJAVU_HNSW_PRESET",
               "CLAUDE_DEJAVU_HNSW_M",
               "CLAUDE_DEJAVU_HNSW_EF",
               "CLAUDE_DEJAVU_HNSW_EF_CONSTRUCTION"):
        os.environ.pop(k, None)
    _aT("M default = 16", vector_index._hnsw_M() == 16,
        repr(vector_index._hnsw_M()))
    _aT("ef default = 100", vector_index._hnsw_ef() == 100,
        repr(vector_index._hnsw_ef()))
    _aT("ef_construction default = 200",
        vector_index._hnsw_ef_construction() == 200,
        repr(vector_index._hnsw_ef_construction()))


def test_high_recall_preset() -> None:
    _step("HNSW preset: high-recall (M=32, ef=200, efc=400)")
    os.environ["CLAUDE_DEJAVU_HNSW_PRESET"] = "high-recall"
    try:
        _aT("M = 32", vector_index._hnsw_M() == 32)
        _aT("ef = 200", vector_index._hnsw_ef() == 200)
        _aT("ef_construction = 400",
            vector_index._hnsw_ef_construction() == 400)
    finally:
        os.environ.pop("CLAUDE_DEJAVU_HNSW_PRESET", None)


def test_low_memory_preset() -> None:
    _step("HNSW preset: low-memory (M=8, ef=50, efc=100)")
    os.environ["CLAUDE_DEJAVU_HNSW_PRESET"] = "low-memory"
    try:
        _aT("M = 8", vector_index._hnsw_M() == 8)
        _aT("ef = 50", vector_index._hnsw_ef() == 50)
        _aT("ef_construction = 100",
            vector_index._hnsw_ef_construction() == 100)
    finally:
        os.environ.pop("CLAUDE_DEJAVU_HNSW_PRESET", None)


def test_explicit_env_overrides_preset() -> None:
    _step("HNSW preset: individual env vars beat the preset")
    os.environ["CLAUDE_DEJAVU_HNSW_PRESET"] = "high-recall"
    os.environ["CLAUDE_DEJAVU_HNSW_M"] = "24"
    try:
        # M is overridden, ef/efc inherit from preset
        _aT("M from env (24)", vector_index._hnsw_M() == 24)
        _aT("ef inherits preset (200)",
            vector_index._hnsw_ef() == 200)
        _aT("ef_construction inherits preset (400)",
            vector_index._hnsw_ef_construction() == 400)
    finally:
        os.environ.pop("CLAUDE_DEJAVU_HNSW_PRESET", None)
        os.environ.pop("CLAUDE_DEJAVU_HNSW_M", None)


def test_unknown_preset_falls_back_to_balanced() -> None:
    _step("HNSW preset: unknown name → balanced (no crash)")
    os.environ["CLAUDE_DEJAVU_HNSW_PRESET"] = "made-up"
    try:
        _aT("M falls back to 16 (balanced)",
            vector_index._hnsw_M() == 16)
    finally:
        os.environ.pop("CLAUDE_DEJAVU_HNSW_PRESET", None)


# ─── Background bootstrap ────────────────────────────────────────────────

def test_maybe_bootstrap_is_callable() -> None:
    _step("bootstrap: maybe_start_background_bootstrap is defined")
    _aT("module exports maybe_start_background_bootstrap",
        callable(getattr(vector_index,
                          "maybe_start_background_bootstrap",
                          None)))


def test_bootstrap_skip_when_disabled() -> None:
    _step("bootstrap: skip cleanly when env=false")
    root = Path(tempfile.mkdtemp(prefix="vi-boot-disabled-"))
    os.environ["CLAUDE_DEJAVU_LOCAL_VECTOR_INDEX"] = "false"
    try:
        msg = vector_index.maybe_start_background_bootstrap(
            root, "TestClass", "http://127.0.0.1:1")
        _aT(f"returns 'skip: explicitly disabled' (got: {msg!r})",
            "skip" in msg and "disabled" in msg, msg)
    finally:
        os.environ.pop("CLAUDE_DEJAVU_LOCAL_VECTOR_INDEX", None)


def test_bootstrap_skip_when_already_loaded() -> None:
    _step("bootstrap: skip when an index is already loaded")
    if not vector_index._NUMPY_AVAILABLE:
        return
    root = Path(tempfile.mkdtemp(prefix="vi-boot-loaded-"))
    idx = vector_index.TurnVectorIndex(root, dim=4)
    idx.add_vectors([1, 2, 3],
                     [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]])
    idx.save()
    # Reset the singleton so it reloads
    vector_index._SINGLETON = None
    vector_index._SINGLETON_DATA_ROOT = None
    msg = vector_index.maybe_start_background_bootstrap(
        root, "TestClass", "http://127.0.0.1:1")
    _aT(f"returns 'skip: already loaded' (got: {msg!r})",
        "skip" in msg and "loaded" in msg, msg)


def test_bootstrap_spawn_but_wv_unreachable() -> None:
    _step("bootstrap: spawned thread aborts cleanly when WV is "
          "unreachable (no crash, no partial save)")
    if not vector_index._NUMPY_AVAILABLE:
        return
    root = Path(tempfile.mkdtemp(prefix="vi-boot-no-wv-"))
    # Fresh singleton so the helper sees an empty index
    vector_index._SINGLETON = None
    vector_index._SINGLETON_DATA_ROOT = None
    msg = vector_index.maybe_start_background_bootstrap(
        root, "TestClass", "http://127.0.0.1:1")
    _aT(f"returns 'spawned' (got: {msg!r})",
        "spawned" in msg, msg)
    # Wait briefly for the thread to give up on the unreachable WV
    time.sleep(1.5)
    # No turns.npy should have been written
    _aT("no turns.npy written (WV was unreachable)",
        not (root / "vector_index" / "turns.npy").exists(),
        "")


def test_bootstrap_in_progress_flag_prevents_duplicate_spawn() -> None:
    _step("bootstrap: second call returns 'already in progress' while "
          "the first thread is running")
    if not vector_index._NUMPY_AVAILABLE:
        return
    root = Path(tempfile.mkdtemp(prefix="vi-boot-dup-"))
    vector_index._SINGLETON = None
    vector_index._SINGLETON_DATA_ROOT = None
    # First spawn — points at an unreachable WV so it'll abort
    # quickly. We race a second call before it does.
    msg1 = vector_index.maybe_start_background_bootstrap(
        root, "TestClass", "http://127.0.0.1:1")
    # Immediately call again — the in-progress flag should be set
    msg2 = vector_index.maybe_start_background_bootstrap(
        root, "TestClass", "http://127.0.0.1:1")
    _aT(f"first call spawned (got: {msg1!r})",
        "spawned" in msg1, msg1)
    _aT(f"second call sees in-progress (got: {msg2!r})",
        ("already in progress" in msg2) or ("loaded" in msg2),
        msg2)
    # Cleanup: wait for the worker to finish
    time.sleep(1.5)


def test_bootstrap_skip_when_numpy_unavailable() -> None:
    _step("bootstrap: skip when numpy isn't installed")
    if vector_index._NUMPY_AVAILABLE:
        # Simulate the absent path by patching the module flag.
        vector_index._NUMPY_AVAILABLE = False
        try:
            msg = vector_index.maybe_start_background_bootstrap(
                Path(tempfile.mkdtemp()), "TestClass",
                "http://127.0.0.1:1")
            _aT(f"reports 'numpy not installed' (got: {msg!r})",
                "numpy" in msg.lower(), msg)
        finally:
            vector_index._NUMPY_AVAILABLE = True


def main() -> int:
    print("v0.9.4 background bootstrap + HNSW presets tests")
    tests = [
        test_default_preset_is_balanced,
        test_high_recall_preset,
        test_low_memory_preset,
        test_explicit_env_overrides_preset,
        test_unknown_preset_falls_back_to_balanced,
        test_maybe_bootstrap_is_callable,
        test_bootstrap_skip_when_disabled,
        test_bootstrap_skip_when_already_loaded,
        test_bootstrap_spawn_but_wv_unreachable,
        test_bootstrap_in_progress_flag_prevents_duplicate_spawn,
        test_bootstrap_skip_when_numpy_unavailable,
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
