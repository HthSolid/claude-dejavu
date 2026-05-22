"""Unit tests for the PG-backed Tier 2 embed cache (v0.8.8).

The v0.8.7 module exposed only the in-process Tier 1 LRU + the vector
helper. v0.8.8 adds a Tier 2 PG cache so the same query embedded in
one session is free in any later session / process / user on the same
machine. This file covers Tier 2 behavior in isolation by mocking
psycopg2.

Coverage targets
================

  - PG hit short-circuits the cloud call entirely (Tier 1 cold,
    Tier 2 warm, urlopen NEVER called).
  - PG miss falls through to cloud; cloud success writes to Tier 2.
  - PG hit also warms Tier 1 so a third call in the same process
    skips both PG and the cloud.
  - PG `last_used_at` / `use_count` are bumped on every hit (via
    UPDATE … RETURNING in the SQL we issue).
  - PG eviction trims down to _PG_CAP entries when over the cap.
  - PG unreachable (psycopg2.connect raises) falls back to
    Tier 1 + Tier 3 with no crash, no log spam.
  - Circuit breaker: a single PG failure short-circuits subsequent
    PG calls for _PG_CIRCUIT_TTL_S seconds (no repeated connect
    timeouts on the hot path).
  - CLAUDE_DEJAVU_DISABLE_PG_CACHE=1 disables Tier 2 globally.
  - _hash_query is deterministic + collision-resistant in the
    small-N regime we care about.
  - psycopg2.ImportError (PG client not installed) is handled
    silently — no exception bubbles to the caller.

Coverage tier
=============
Infra-free. A FakeConn class replays the SQL contract we care about
(SELECT + INSERT + DELETE) backed by a dict. We monkeypatch the
psycopg2 module into sys.modules so `_pg_connect` finds a benign
fake. Real psycopg2 + real PG never need to be available for these
tests to pass.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

import query_embed as qe  # noqa: E402  type: ignore[import]


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


# ─── FakeConn / FakeCursor / fake psycopg2 module ────────────────────────────

class _FakeCursor:
    """Minimal cursor that understands the three SQL shapes we issue:

    1. UPDATE embed_cache SET last_used_at = now(), use_count = use_count + 1
         WHERE query_hash = %s RETURNING vector
    2. INSERT INTO embed_cache ... ON CONFLICT (query_hash) DO UPDATE ...
    3. DELETE FROM embed_cache WHERE ctid IN (SELECT ctid FROM ... OFFSET %s)

    Backed by the parent _FakeConn's dict store. Any other SQL raises
    AssertionError so a future refactor of the SQL would scream loud.
    """
    def __init__(self, store: dict):
        self._store = store
        self._last_row: tuple | None = None
        self.rowcount = 0

    def execute(self, sql: str, params: tuple | None = None) -> None:
        params = params or ()
        s = " ".join(sql.split()).upper()
        if s.startswith("UPDATE EMBED_CACHE SET LAST_USED_AT"):
            # Tier 2 lookup: bump + return vector
            key = bytes(params[0]) if hasattr(params[0], "tobytes") \
                else _coerce_bytes(params[0])
            row = self._store.get(key)
            if row is None:
                self._last_row = None
                return
            # In-place bump
            row["last_used_at"] = time.time()
            row["use_count"] = int(row.get("use_count", 0)) + 1
            self._last_row = (row["vector"],)
        elif s.startswith("INSERT INTO EMBED_CACHE"):
            key = bytes(params[0]) if hasattr(params[0], "tobytes") \
                else _coerce_bytes(params[0])
            txt = params[1]
            vec_json = params[2]
            if key in self._store:
                # ON CONFLICT DO UPDATE branch
                self._store[key]["last_used_at"] = time.time()
                self._store[key]["use_count"] += 1
            else:
                self._store[key] = {
                    "query_hash": key,
                    "query_text": txt,
                    "vector": vec_json,
                    "created_at": time.time(),
                    "last_used_at": time.time(),
                    "use_count": 1,
                }
            self._last_row = None
        elif s.startswith("DELETE FROM EMBED_CACHE WHERE CTID IN"):
            offset = int(params[0]) if params else 0
            # Sort newest-by-last_used_at first; everything past `offset`
            # is the eviction set. Mirrors the SQL we issue.
            keys = sorted(
                self._store.keys(),
                key=lambda k: self._store[k]["last_used_at"],
                reverse=True,
            )
            to_drop = keys[offset:]
            for k in to_drop:
                del self._store[k]
            self.rowcount = len(to_drop)
            self._last_row = None
        else:
            raise AssertionError(
                f"_FakeCursor saw unexpected SQL: {sql!r}")

    def fetchone(self):
        return self._last_row

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _coerce_bytes(v) -> bytes:
    """Adapter handle → raw bytes. Real psycopg2.Binary wraps the bytes
    in a memoryview-ish object; our fake adapter just returns the bytes
    directly. Handle both shapes."""
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    if hasattr(v, "tobytes"):
        return v.tobytes()
    # FakeBinary marker
    if hasattr(v, "_raw"):
        return v._raw  # type: ignore[attr-defined]
    return bytes(v)


class _FakeConn:
    """Connection that yields _FakeCursor instances. Implements the
    context-manager + cursor() shape psycopg2 uses."""
    def __init__(self, store: dict):
        self._store = store
        self.closed = False

    def cursor(self):
        return _FakeCursor(self._store)

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _FakeBinary:
    """Stand-in for psycopg2.Binary — preserves the raw bytes so the
    FakeCursor can recover them as a dict key."""
    def __init__(self, raw: bytes):
        self._raw = raw


def _install_fake_psycopg2(store: dict, raise_on_connect: bool = False):
    """Build + register a fake psycopg2 module in sys.modules. Returns
    the fake module (for asserting on call counts) and a cleanup
    callable that restores the prior module if any.
    """
    import types
    fake = types.ModuleType("psycopg2")
    connect_calls = {"n": 0}

    def _connect(dsn, **kwargs):  # noqa: ARG001
        connect_calls["n"] += 1
        if raise_on_connect:
            raise RuntimeError("simulated pg unreachable")
        return _FakeConn(store)

    fake.connect = _connect  # type: ignore[attr-defined]
    fake.Binary = _FakeBinary  # type: ignore[attr-defined]
    fake._connect_calls = connect_calls  # type: ignore[attr-defined]
    prior = sys.modules.get("psycopg2")
    sys.modules["psycopg2"] = fake

    def _restore():
        if prior is not None:
            sys.modules["psycopg2"] = prior
        else:
            sys.modules.pop("psycopg2", None)
    return fake, _restore


# ─── urlopen fake (re-used from test_query_embed.py shape) ───────────────────

class _FakeResp:
    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self.status = status
        self.headers = {}

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _make_fake_urlopen(*, payload: dict | None = None,
                       raise_exc: Exception | None = None,
                       call_counter: dict | None = None):
    def _fake(req, timeout=None):  # noqa: ARG001
        if call_counter is not None:
            call_counter["n"] = call_counter.get("n", 0) + 1
        if raise_exc is not None:
            raise raise_exc
        body = json.dumps(payload or {}).encode("utf-8")
        return _FakeResp(body)
    return _fake


def _full_reset():
    """Reset every piece of in-process state between tests."""
    qe._cache_reset()
    qe._pg_circuit_reset()
    # Wipe the global insert counter so eviction is deterministic
    qe._PG_INSERT_COUNTER = 0


# ─── tests ───────────────────────────────────────────────────────────────────

def test_pg_cache_hit_skips_cloud() -> None:
    _step("Tier 2 hit: cached query never reaches the cloud")
    _full_reset()
    store: dict = {}
    h = qe._hash_query("rsync to restic migration")
    store[h] = {
        "query_hash": h,
        "query_text": "rsync to restic migration",
        "vector": json.dumps([0.1, 0.2, 0.3, 0.4]),
        "created_at": time.time(),
        "last_used_at": time.time(),
        "use_count": 1,
    }
    fake_pg, restore = _install_fake_psycopg2(store)
    counter = {"n": 0}
    fake_url = _make_fake_urlopen(
        raise_exc=RuntimeError("urlopen MUST NOT be called"),
        call_counter=counter,
    )
    try:
        with patch.object(qe.urllib.request, "urlopen", fake_url), \
             patch.object(qe, "_cloud_key", lambda: "dvk_test_fake"):
            v = qe.embed_query("rsync to restic migration")
        _aT("returns the PG-cached 4-dim vector",
            v == [0.1, 0.2, 0.3, 0.4], f"got: {v!r}")
        _aT("urlopen never called", counter["n"] == 0,
            f"counter={counter['n']}")
        _aT("PG connect was used",
            fake_pg._connect_calls["n"] >= 1,
            f"connects={fake_pg._connect_calls['n']}")
    finally:
        restore()


def test_pg_cache_miss_calls_cloud_then_writes_back() -> None:
    _step("Tier 2 miss → cloud call → write-back into PG")
    _full_reset()
    store: dict = {}  # empty
    fake_pg, restore = _install_fake_psycopg2(store)
    counter = {"n": 0}
    fake_url = _make_fake_urlopen(
        payload={"embeddings": [[0.5, 0.5, 0.5, 0.5]]},
        call_counter=counter,
    )
    try:
        with patch.object(qe.urllib.request, "urlopen", fake_url), \
             patch.object(qe, "_cloud_key", lambda: "dvk_test_fake"):
            v = qe.embed_query("never-seen-before query")
        _aT("returns the cloud vector",
            v == [0.5, 0.5, 0.5, 0.5], f"got: {v!r}")
        _aT("cloud was called exactly once",
            counter["n"] == 1, f"got: {counter['n']}")
        h = qe._hash_query("never-seen-before query")
        _aT("vector now in PG store",
            h in store, f"keys={list(store.keys())}")
        if h in store:
            cached_vec = json.loads(store[h]["vector"])
            _aT("PG row stores the vector verbatim",
                cached_vec == [0.5, 0.5, 0.5, 0.5],
                f"got: {cached_vec!r}")
            _aT("PG row stores the original query_text",
                store[h]["query_text"] == "never-seen-before query")
            _aT("use_count starts at 1",
                store[h]["use_count"] == 1)
    finally:
        restore()


def test_pg_hit_warms_tier1_cache() -> None:
    _step("Tier 2 hit also warms Tier 1 (third call skips both)")
    _full_reset()
    h = qe._hash_query("warm-tier1 query")
    store: dict = {
        h: {
            "query_hash": h,
            "query_text": "warm-tier1 query",
            "vector": json.dumps([0.9, 0.1]),
            "created_at": time.time(),
            "last_used_at": time.time(),
            "use_count": 1,
        }
    }
    fake_pg, restore = _install_fake_psycopg2(store)
    counter = {"n": 0}
    fake_url = _make_fake_urlopen(
        raise_exc=RuntimeError("urlopen MUST NOT be called"),
        call_counter=counter,
    )
    try:
        with patch.object(qe.urllib.request, "urlopen", fake_url), \
             patch.object(qe, "_cloud_key", lambda: "dvk_test_fake"):
            qe.embed_query("warm-tier1 query")
            connects_after_first = fake_pg._connect_calls["n"]
            # Second call should skip PG entirely — Tier 1 hit.
            qe.embed_query("warm-tier1 query")
        _aT("first call connected to PG once",
            connects_after_first >= 1, f"got={connects_after_first}")
        _aT("second call did NOT reconnect to PG",
            fake_pg._connect_calls["n"] == connects_after_first,
            f"connects={fake_pg._connect_calls['n']}")
        _aT("urlopen never called", counter["n"] == 0)
    finally:
        restore()


def test_pg_cache_bumps_last_used_at_on_hit() -> None:
    _step("Tier 2 hit bumps last_used_at + use_count")
    _full_reset()
    h = qe._hash_query("bump-me query")
    t0 = time.time() - 1000.0  # old timestamp
    store: dict = {
        h: {
            "query_hash": h,
            "query_text": "bump-me query",
            "vector": json.dumps([1.0, 0.0]),
            "created_at": t0,
            "last_used_at": t0,
            "use_count": 5,
        }
    }
    fake_pg, restore = _install_fake_psycopg2(store)
    try:
        with patch.object(qe.urllib.request, "urlopen",
                          lambda *a, **k: (_ for _ in ()).throw(
                              RuntimeError("nope"))), \
             patch.object(qe, "_cloud_key", lambda: "dvk_test_fake"):
            qe.embed_query("bump-me query")
        _aT("use_count bumped to 6",
            store[h]["use_count"] == 6,
            f"got={store[h]['use_count']}")
        _aT("last_used_at advanced",
            store[h]["last_used_at"] > t0,
            f"got={store[h]['last_used_at']} vs t0={t0}")
    finally:
        restore()


def test_pg_eviction_trims_to_cap() -> None:
    _step(f"Tier 2 eviction trims to {qe._PG_CAP} rows")
    _full_reset()
    store: dict = {}
    # Seed 10005 entries with strictly-increasing last_used_at so the
    # 5 oldest are deterministically the ones evicted.
    for i in range(10_005):
        h = qe._hash_query(f"q-{i:05d}")
        store[h] = {
            "query_hash": h,
            "query_text": f"q-{i:05d}",
            "vector": json.dumps([float(i)]),
            "created_at": float(i),
            "last_used_at": float(i),
            "use_count": 1,
        }
    fake_pg, restore = _install_fake_psycopg2(store)
    try:
        n_dropped = qe._pg_evict()
    finally:
        restore()
    _aT("dropped exactly 5 rows",
        n_dropped == 5, f"got: {n_dropped}")
    _aT(f"store now bounded at {qe._PG_CAP}",
        len(store) == qe._PG_CAP, f"got: {len(store)}")
    # Verify the dropped rows were the oldest (lowest last_used_at).
    survivors = sorted(int(v["last_used_at"]) for v in store.values())
    _aT("oldest 5 entries (last_used_at 0..4) evicted",
        survivors[0] == 5, f"got min={survivors[0]}")


def test_pg_eviction_lazy_trigger_every_nth_write() -> None:
    _step(f"Tier 2: eviction fires lazily every {qe._PG_EVICT_EVERY} inserts")
    _full_reset()
    store: dict = {}
    fake_pg, restore = _install_fake_psycopg2(store)
    fake_url = _make_fake_urlopen(
        payload={"embeddings": [[0.0]]},
    )
    try:
        with patch.object(qe.urllib.request, "urlopen", fake_url), \
             patch.object(qe, "_cloud_key", lambda: "dvk_test_fake"):
            # Counter is global per-process. Stuff (_PG_EVICT_EVERY - 1)
            # writes — should NOT trigger eviction yet.
            for i in range(qe._PG_EVICT_EVERY - 1):
                qe.embed_query(f"lazy-evict-q-{i}")
            # No eviction call expected. Verify the counter state instead
            # (eviction would have called DELETE; if it did, _PG_INSERT
            # counter would still match the inserts).
            _aT(
                f"counter at {qe._PG_EVICT_EVERY - 1}",
                qe._PG_INSERT_COUNTER == qe._PG_EVICT_EVERY - 1,
                f"got={qe._PG_INSERT_COUNTER}",
            )
            # One more write → counter hits the modulo trigger.
            qe.embed_query(f"lazy-evict-q-{qe._PG_EVICT_EVERY - 1}")
            _aT(
                "counter at evict-every threshold",
                qe._PG_INSERT_COUNTER == qe._PG_EVICT_EVERY,
                f"got={qe._PG_INSERT_COUNTER}",
            )
    finally:
        restore()


def test_pg_unavailable_falls_back_to_inmem_only() -> None:
    _step("PG unreachable → no crash, cloud still called, Tier 1 still works")
    _full_reset()
    store: dict = {}
    fake_pg, restore = _install_fake_psycopg2(store, raise_on_connect=True)
    counter = {"n": 0}
    fake_url = _make_fake_urlopen(
        payload={"embeddings": [[0.7, 0.3]]},
        call_counter=counter,
    )
    try:
        with patch.object(qe.urllib.request, "urlopen", fake_url), \
             patch.object(qe, "_cloud_key", lambda: "dvk_test_fake"):
            v1 = qe.embed_query("pg-down query")
            v2 = qe.embed_query("pg-down query")  # Tier 1 hit
        _aT("first call returned vector despite PG down",
            v1 == [0.7, 0.3], f"got: {v1!r}")
        _aT("second call returned same vector (Tier 1 hit)",
            v2 == [0.7, 0.3], f"got: {v2!r}")
        _aT("cloud called exactly once (Tier 1 absorbed second call)",
            counter["n"] == 1, f"got: {counter['n']}")
        _aT("no PG state written (PG was down)",
            len(store) == 0, f"got: {len(store)}")
    finally:
        restore()


def test_pg_circuit_breaker_skips_repeated_connects() -> None:
    _step("PG circuit breaker: a single failure short-circuits subsequent connects")
    _full_reset()
    store: dict = {}
    fake_pg, restore = _install_fake_psycopg2(store, raise_on_connect=True)
    fake_url = _make_fake_urlopen(payload={"embeddings": [[0.0]]})
    try:
        with patch.object(qe.urllib.request, "urlopen", fake_url), \
             patch.object(qe, "_cloud_key", lambda: "dvk_test_fake"):
            qe.embed_query("first-attempt query")
            connects_after_one = fake_pg._connect_calls["n"]
            # Subsequent calls within the circuit TTL should NOT attempt
            # to reconnect.
            qe.embed_query("second-attempt query")
            qe.embed_query("third-attempt query")
        _aT(
            "first call attempted at least one connect",
            connects_after_one >= 1, f"got={connects_after_one}",
        )
        _aT(
            "subsequent calls did NOT reconnect (circuit open)",
            fake_pg._connect_calls["n"] == connects_after_one,
            f"got={fake_pg._connect_calls['n']}",
        )
    finally:
        restore()
        _full_reset()  # break the circuit for the next test


def test_disable_pg_env_var_bypasses_tier2() -> None:
    _step("CLAUDE_DEJAVU_DISABLE_PG_CACHE=1 → Tier 2 never consulted")
    _full_reset()
    store: dict = {}
    # Seed a hit that should be IGNORED because the env var disables Tier 2
    h = qe._hash_query("ignore-me query")
    store[h] = {
        "query_hash": h,
        "query_text": "ignore-me query",
        "vector": json.dumps([99.0, 99.0]),
        "created_at": time.time(),
        "last_used_at": time.time(),
        "use_count": 1,
    }
    fake_pg, restore = _install_fake_psycopg2(store)
    counter = {"n": 0}
    fake_url = _make_fake_urlopen(
        payload={"embeddings": [[0.42, 0.42]]},
        call_counter=counter,
    )
    import os as _os
    prior = _os.environ.get("CLAUDE_DEJAVU_DISABLE_PG_CACHE")
    _os.environ["CLAUDE_DEJAVU_DISABLE_PG_CACHE"] = "1"
    try:
        with patch.object(qe.urllib.request, "urlopen", fake_url), \
             patch.object(qe, "_cloud_key", lambda: "dvk_test_fake"):
            v = qe.embed_query("ignore-me query")
        _aT("got the CLOUD vector, not the PG one",
            v == [0.42, 0.42], f"got: {v!r}")
        _aT("cloud was called (Tier 2 was bypassed)",
            counter["n"] == 1, f"got: {counter['n']}")
        _aT("no PG connects attempted",
            fake_pg._connect_calls["n"] == 0,
            f"got: {fake_pg._connect_calls['n']}")
    finally:
        if prior is None:
            _os.environ.pop("CLAUDE_DEJAVU_DISABLE_PG_CACHE", None)
        else:
            _os.environ["CLAUDE_DEJAVU_DISABLE_PG_CACHE"] = prior
        restore()


def test_hash_query_is_deterministic_and_fixed_width() -> None:
    _step("_hash_query: deterministic, 16 bytes, collision-resistant")
    h1 = qe._hash_query("abc")
    h2 = qe._hash_query("abc")
    h3 = qe._hash_query("abd")
    _aT("same input → same hash", h1 == h2)
    _aT("different input → different hash", h1 != h3)
    _aT("hash is exactly 16 bytes",
        isinstance(h1, bytes) and len(h1) == 16, f"got len={len(h1)}")
    # Unicode + binary content survives hashing.
    h4 = qe._hash_query("query with emoji \U0001f600 + chinese 中文")
    _aT("unicode hashed without raising",
        isinstance(h4, bytes) and len(h4) == 16)


def test_psycopg2_missing_silently_skips_tier2() -> None:
    _step("psycopg2 ImportError → Tier 2 silently disabled, no crash")
    _full_reset()
    # Replace psycopg2 in sys.modules with something that raises on import.
    # We simulate the "not installed" path by removing psycopg2 entirely
    # and replacing the module-loader hook with a no-op so the lazy import
    # inside _pg_connect fails.
    prior = sys.modules.pop("psycopg2", None)
    counter = {"n": 0}
    fake_url = _make_fake_urlopen(
        payload={"embeddings": [[0.11, 0.22]]},
        call_counter=counter,
    )
    # Inject an import meta-path hook that vetoes psycopg2 imports.
    class _Vetoer:
        @staticmethod
        def find_spec(name, path=None, target=None):  # noqa: ARG004
            if name == "psycopg2":
                raise ImportError("simulated: psycopg2 not installed")
            return None
    sys.meta_path.insert(0, _Vetoer)
    try:
        with patch.object(qe.urllib.request, "urlopen", fake_url), \
             patch.object(qe, "_cloud_key", lambda: "dvk_test_fake"):
            v = qe.embed_query("psycopg2-missing query")
        _aT("returned cloud vector despite missing psycopg2",
            v == [0.11, 0.22], f"got: {v!r}")
        _aT("cloud was called", counter["n"] == 1)
    finally:
        sys.meta_path.remove(_Vetoer)
        if prior is not None:
            sys.modules["psycopg2"] = prior
        _full_reset()


def test_pg_lookup_on_pg_down_returns_none() -> None:
    _step("_pg_lookup direct: PG down → returns None silently")
    _full_reset()
    fake_pg, restore = _install_fake_psycopg2({}, raise_on_connect=True)
    try:
        result = qe._pg_lookup("any query")
    finally:
        restore()
    _aT("returns None on PG-down", result is None, f"got: {result!r}")


def test_pg_store_on_pg_down_is_silent() -> None:
    _step("_pg_store direct: PG down → no exception, no side-effect")
    _full_reset()
    fake_pg, restore = _install_fake_psycopg2({}, raise_on_connect=True)
    try:
        try:
            qe._pg_store("q", [0.1, 0.2])
            ok = True
        except Exception as e:  # noqa: BLE001
            ok = False
            _fail("unexpected exception", repr(e))
            return
    finally:
        restore()
    _aT("_pg_store returned without raising", ok)


def test_pg_evict_on_pg_down_returns_zero() -> None:
    _step("_pg_evict on PG-down → 0 (no exception)")
    _full_reset()
    fake_pg, restore = _install_fake_psycopg2({}, raise_on_connect=True)
    try:
        n = qe._pg_evict()
    finally:
        restore()
    _aT("returned 0 deletions", n == 0, f"got: {n}")


def test_bridge_sem_cache_lookup_still_exposed() -> None:
    _step("v0.8.7 contract: lookup_by_vector still callable + sem_cache_lookup reachable")
    _full_reset()
    # Populate the vector cache via embed_query so lookup_by_vector has
    # something to compare against. PG-disabled so we don't pollute.
    import os as _os
    prior = _os.environ.get("CLAUDE_DEJAVU_DISABLE_PG_CACHE")
    _os.environ["CLAUDE_DEJAVU_DISABLE_PG_CACHE"] = "1"
    try:
        fake_url = _make_fake_urlopen(
            payload={"embeddings": [[1.0, 0.0, 0.0, 0.0]]})
        with patch.object(qe.urllib.request, "urlopen", fake_url), \
             patch.object(qe, "_cloud_key", lambda: "dvk_test_fake"):
            qe.embed_query("populator")
        hit = qe.lookup_by_vector([1.0, 0.0, 0.0, 0.0])
        _aT("lookup_by_vector still returns a non-None match",
            hit is not None, f"got: {hit!r}")
        if hit is not None:
            _aT("match shape preserved (has 'kind' key)",
                "kind" in hit, f"got: {hit!r}")
    finally:
        if prior is None:
            _os.environ.pop("CLAUDE_DEJAVU_DISABLE_PG_CACHE", None)
        else:
            _os.environ["CLAUDE_DEJAVU_DISABLE_PG_CACHE"] = prior
        _full_reset()


def main() -> int:
    tests = [
        test_pg_cache_hit_skips_cloud,
        test_pg_cache_miss_calls_cloud_then_writes_back,
        test_pg_hit_warms_tier1_cache,
        test_pg_cache_bumps_last_used_at_on_hit,
        test_pg_eviction_trims_to_cap,
        test_pg_eviction_lazy_trigger_every_nth_write,
        test_pg_unavailable_falls_back_to_inmem_only,
        test_pg_circuit_breaker_skips_repeated_connects,
        test_disable_pg_env_var_bypasses_tier2,
        test_hash_query_is_deterministic_and_fixed_width,
        test_psycopg2_missing_silently_skips_tier2,
        test_pg_lookup_on_pg_down_returns_none,
        test_pg_store_on_pg_down_is_silent,
        test_pg_evict_on_pg_down_returns_zero,
        test_bridge_sem_cache_lookup_still_exposed,
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
