"""query_embed.py — caller-side query embedding for v0.8.7+ hybrid recall.

Why this file exists
====================
ClaudeDejavuTurn ships with `vectorizer:none` — the cloud-embed
worker computes the 1024-dim e5-large-instruct embedding at ingest
time and stores it as the row's `_additional.vector`. To do
semantic search at recall time the caller has to embed the QUERY
the same way (POST to /v1/embed) and pass it as `nearVector` to
Weaviate. v0.8.5 + v0.8.6 only used BM25 keyword search because we
hadn't wired the caller-side query embed yet. v0.8.7 fixes that;
v0.8.8 adds cross-session persistence so the cloud call is paid at
most once per unique query string across every CLI/hook invocation
on the machine.

API
===
``embed_query(query: str, *, timeout_s: float = 1.0) -> list[float] | None``
  Returns the 1024-dim cloud embedding, or None on any failure
  (timeout, network, quota, bad auth, malformed response, etc.).
  ``timeout_s`` is enforced via ``urllib.urlopen(timeout=…)``; the
  JIT recall hook overrides it to fit inside its 2s budget.

Three-tier cache
================
Lookup order on every call (v0.8.8):

  Tier 1: in-process exact-match string cache. Bounded LRU of 128
          entries. O(1) lookup. Catches "user searched same thing
          twice" inside one CLI/hook invocation.

  Tier 2: PG-backed exact-match string cache (`embed_cache` table).
          Survives across processes / sessions / users (per database).
          Keyed by blake2b-16 hash of the query string. Bounded at
          10 000 rows by `last_used_at`; lazily trimmed once every
          _PG_EVICT_EVERY inserts. PG down / not installed → silently
          skipped (fall through to Tier 3).

  Tier 3: cloud /v1/embed POST. Cost path. On success the vector is
          written back to both Tier 2 (durable) and Tier 1 (fast).

  Bonus:  vector-similarity helper (``lookup_by_vector``). Exposed
          via dejavu_bridge.sem_cache_lookup for callers that already
          have a vector and want to check whether a semantically
          similar one was recently embedded. Not consulted by
          embed_query itself — to lookup by vector you need a vector,
          which means you'd have to embed first, which defeats the
          purpose of skipping the embed. Kept as a callable helper
          for future two-pass workflows.

Degraded mode
=============
- No DEJAVU_CLOUD_KEY in config.env: ``embed_query`` returns None
  immediately, no network call. Search code MUST treat None as
  "RRF unavailable, BM25-only path" — see ``recall._hybrid``.
- Network error / 5xx / timeout: returns None. Same code path.
- PG unreachable: Tier 2 silently skipped on every lookup + write.
  Tier 1 + Tier 3 still work. No log spam (PG-down is a common case
  for unconfigured installs and shouldn't sound alarms on the JIT
  hot path that fires hundreds of times per session).
- Bridge unavailable: Tier 1 + 2 still work; lookup_by_vector falls
  back to the stub's ``sem_cache_lookup`` (linear-scan cosine,
  deterministic).

Determinism
===========
The cloud worker is deterministic for the same text. All caches are
keyed by the EXACT query string (no normalization / lowercasing —
the caller controls case sensitivity). Tier 2 stores the blake2b-16
hash of the verbatim string; collisions are statistically impossible
within the 10k-row bound.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


_log = logging.getLogger(__name__)

# ─── Config resolution ───────────────────────────────────────────────────────
# Single source of truth: same data-root + config.env layout that every
# other claude-dejavu callsite uses (recall.py, semantic_search.py,
# cloud_cli.py). Keeping the resolver in lock-step with those means
# DEJAVU_CLOUD_URL / DEJAVU_CLOUD_KEY work out of the box for users
# who already ran `cloud signin`.
_DEFAULT_CLOUD_URL = "https://embed.hte.digital"


def _resolve_data_root() -> Path:
    override = os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support"
                / "claude-dejavu")
    if sys.platform.startswith("win"):
        base = (os.environ.get("LOCALAPPDATA")
                or os.environ.get("APPDATA")
                or str(Path.home() / "AppData" / "Local"))
        return Path(base) / "claude-dejavu"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) / "claude-dejavu" if xdg
            else Path.home() / ".local" / "share" / "claude-dejavu")


def _load_config_env() -> dict[str, str]:
    """Read config.env into a flat dict. Empty / missing file → {}."""
    cfg: dict[str, str] = {}
    fp = _resolve_data_root() / "config.env"
    if not fp.exists():
        return cfg
    try:
        for ln in fp.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            if "=" in ln:
                k, v = ln.split("=", 1)
                cfg[k.strip()] = v.strip()
    except (OSError, UnicodeDecodeError):
        pass
    return cfg


def _cloud_url() -> str:
    return (os.environ.get("DEJAVU_CLOUD_URL")
            or _load_config_env().get("DEJAVU_CLOUD_URL")
            or _DEFAULT_CLOUD_URL).rstrip("/")


def _cloud_key() -> str:
    """Resolve the DEJAVU_CLOUD_KEY. Env wins over config.env so
    one-off `DEJAVU_CLOUD_KEY=… claude-dejavu search …` invocations
    override the persisted setting."""
    return (os.environ.get("DEJAVU_CLOUD_KEY")
            or _load_config_env().get("DEJAVU_CLOUD_KEY", "")).strip()


# ─── Per-process cache ───────────────────────────────────────────────────────
# Two structures:
#   _STRING_CACHE: ordered dict mapping query string → vector.
#     Bounded at _CACHE_CAP entries; oldest evicted on overflow.
#     Lock protects against ThreadPoolExecutor concurrent hooks.
#   _VECTOR_CACHE: list of {key_vec, value_json} entries, mirroring
#     the format sem_cache_lookup expects. Same _CACHE_CAP bound.

_CACHE_CAP = 128
_STRING_CACHE: dict[str, list[float]] = {}
_VECTOR_CACHE: list[dict[str, Any]] = []
_CACHE_LOCK = threading.Lock()


def _cache_put(query: str, vec: list[float]) -> None:
    """Atomic put for both caches. Evicts the oldest entry if the
    string cache is at capacity. Same eviction policy for the vector
    cache (kept in 1-to-1 sync to simplify reasoning about both)."""
    with _CACHE_LOCK:
        # Evict oldest if at cap (or move-to-end if already present).
        if query in _STRING_CACHE:
            del _STRING_CACHE[query]
        elif len(_STRING_CACHE) >= _CACHE_CAP:
            # dict preserves insertion order in Python 3.7+.
            oldest = next(iter(_STRING_CACHE))
            del _STRING_CACHE[oldest]
            # Drop the matching vector entry too. We can't always
            # find it by key (oldest isn't tagged with the query
            # string in the vector cache) so we just pop the front.
            if _VECTOR_CACHE:
                _VECTOR_CACHE.pop(0)
        _STRING_CACHE[query] = vec
        _VECTOR_CACHE.append({
            "key_vec": vec,
            "value_json": json.dumps({"query": query}),
        })


def _cache_get(query: str) -> list[float] | None:
    """Exact-match lookup. Returns the cached vector or None."""
    with _CACHE_LOCK:
        return _STRING_CACHE.get(query)


def _cache_reset() -> None:
    """Test hook: clear both caches. NOT exposed externally."""
    with _CACHE_LOCK:
        _STRING_CACHE.clear()
        _VECTOR_CACHE.clear()


# ─── Tier 2: PG-backed cross-session cache ───────────────────────────────────
# Persisted in the `embed_cache` table (see code/schema.sql). Lazy psycopg2
# import so the module stays usable in environments where PG isn't installed
# (CI for tests with no postgres, etc.).
#
# Connection policy: open a fresh short-lived connection per call. The cache
# is consulted at most a handful of times per CLI invocation (once per
# unique query) so we don't need pooling and we don't want to hold a
# long-lived connection from a hook that might be killed mid-query.
#
# `_PG_AVAILABLE_CACHED` is a soft circuit breaker — if PG was unreachable
# in the last _PG_CIRCUIT_TTL_S seconds we skip the lookup entirely instead
# of paying the connect timeout on every embed_query. The breaker self-heals
# after the TTL.

_PG_EVICT_EVERY = 256      # check + trim every Nth write
_PG_CAP = 10_000           # max rows retained
_PG_CIRCUIT_TTL_S = 30.0   # how long to remember "PG was down"
_PG_INSERT_COUNTER = 0
_PG_CIRCUIT_LOCK = threading.Lock()
_PG_LAST_FAILURE_AT: float = 0.0


def _hash_query(query: str) -> bytes:
    """blake2b-16 of the query bytes. Stable across processes / machines /
    Python versions; collision-resistant within the bounded cache."""
    return hashlib.blake2b(query.encode("utf-8"), digest_size=16).digest()


def _pg_dsn() -> str | None:
    """Build a psycopg2 DSN from config.env + env-var overrides.

    Returns None when the user opted out (CLAUDE_DEJAVU_DISABLE_PG_CACHE=1)
    or when the circuit breaker says PG was unreachable recently. Returning
    None means embed_query skips Tier 2 entirely on this call.
    """
    if os.environ.get("CLAUDE_DEJAVU_DISABLE_PG_CACHE", "").strip() in (
            "1", "true", "yes"):
        return None
    with _PG_CIRCUIT_LOCK:
        global _PG_LAST_FAILURE_AT
        if (_PG_LAST_FAILURE_AT
                and time.monotonic() - _PG_LAST_FAILURE_AT < _PG_CIRCUIT_TTL_S):
            return None
    cfg = _load_config_env()
    host = os.environ.get("PG_HOST") or cfg.get("PG_HOST", "localhost")
    port = os.environ.get("PG_PORT") or cfg.get("PG_PORT", "5450")
    user = os.environ.get("PG_USER") or cfg.get("PG_USER", "postgres")
    pwd = os.environ.get("PG_PASS") or cfg.get("PG_PASS", "postgres")
    os_user = os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"
    db = (os.environ.get("PG_DB") or cfg.get("PG_DB")
          or f"claude_dejavu_{os_user}")
    return (f"host={host} port={port} user={user} password={pwd} "
            f"dbname={db}")


def _pg_connect():  # -> psycopg2.connection | None
    """Lazy psycopg2 import + connect. Returns None on any failure mode
    (psycopg2 missing, PG unreachable, bad creds, ...). Sets the circuit
    breaker on failure so subsequent calls skip the connect overhead."""
    dsn = _pg_dsn()
    if dsn is None:
        return None
    try:
        import psycopg2  # type: ignore[import]
    except ImportError:
        with _PG_CIRCUIT_LOCK:
            global _PG_LAST_FAILURE_AT
            _PG_LAST_FAILURE_AT = time.monotonic()
        return None
    try:
        # connect_timeout keeps the hot path bounded if PG is down or
        # being restarted. 1s is plenty on localhost; remote PG is rare
        # for this plugin so we don't bother with config knobs.
        conn = psycopg2.connect(dsn, connect_timeout=1)
        return conn
    except Exception as exc:  # noqa: BLE001 — anything psycopg2 raises
        with _PG_CIRCUIT_LOCK:
            _PG_LAST_FAILURE_AT = time.monotonic()
        _log.debug("pg_connect failed: %s: %s", type(exc).__name__, exc)
        return None


def _pg_lookup(query: str) -> list[float] | None:
    """Tier 2 lookup. Returns the cached vector + bumps last_used_at /
    use_count atomically. None on miss / PG down / any error."""
    conn = _pg_connect()
    if conn is None:
        return None
    h = _hash_query(query)
    try:
        with conn:  # autocommit-on-exit semantics
            with conn.cursor() as cur:
                # UPDATE … RETURNING does the bump + read in one roundtrip.
                cur.execute(
                    "UPDATE embed_cache "
                    "SET last_used_at = now(), use_count = use_count + 1 "
                    "WHERE query_hash = %s "
                    "RETURNING vector",
                    (psycopg2_bytes(h),))
                row = cur.fetchone()
                if not row:
                    return None
                vec_raw = row[0]
                # psycopg2 returns JSONB as already-parsed Python (when
                # the json adapter is registered) OR as a JSON string. Be
                # defensive — both shapes are valid library behavior.
                if isinstance(vec_raw, str):
                    try:
                        vec_raw = json.loads(vec_raw)
                    except (TypeError, ValueError):
                        return None
                if not isinstance(vec_raw, list):
                    return None
                try:
                    return [float(x) for x in vec_raw]
                except (TypeError, ValueError):
                    return None
    except Exception as exc:  # noqa: BLE001
        _log.debug("pg_lookup failed: %s: %s", type(exc).__name__, exc)
        return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _pg_store(query: str, vec: list[float]) -> None:
    """Tier 2 write. UPSERT keyed on query_hash so concurrent writers
    don't trip the PRIMARY KEY. Best-effort: any failure is swallowed
    (caller already has the cloud result; failing to cache it locally
    is not a user-visible problem)."""
    conn = _pg_connect()
    if conn is None:
        return
    h = _hash_query(query)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO embed_cache "
                    "  (query_hash, query_text, vector, "
                    "   created_at, last_used_at, use_count) "
                    "VALUES (%s, %s, %s::jsonb, now(), now(), 1) "
                    "ON CONFLICT (query_hash) DO UPDATE SET "
                    "  last_used_at = EXCLUDED.last_used_at, "
                    "  use_count = embed_cache.use_count + 1",
                    (psycopg2_bytes(h), query, json.dumps(vec)))
    except Exception as exc:  # noqa: BLE001
        _log.debug("pg_store failed: %s: %s", type(exc).__name__, exc)
        return
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    # Lazy eviction every Nth write. Counter is global per-process — a
    # one-off over-trim under concurrency is harmless (eviction is
    # idempotent — keeping 10k of 10k is a no-op).
    global _PG_INSERT_COUNTER
    _PG_INSERT_COUNTER += 1
    if _PG_INSERT_COUNTER % _PG_EVICT_EVERY == 0:
        _pg_evict()


def _pg_evict(cap: int | None = None) -> int:
    """Trim the cache down to `cap` rows, oldest-by-last_used_at first.
    Returns the number of rows deleted. Public-ish: scheduled tasks
    call this directly. Safe to call when PG is down (returns 0).
    """
    target = cap if cap is not None else _PG_CAP
    conn = _pg_connect()
    if conn is None:
        return 0
    try:
        with conn:
            with conn.cursor() as cur:
                # Single statement, transactional with the SELECT for the
                # threshold so we don't race a concurrent INSERT. Uses
                # ctid for cheap targeted deletes.
                cur.execute(
                    "DELETE FROM embed_cache WHERE ctid IN ("
                    "  SELECT ctid FROM embed_cache "
                    "  ORDER BY last_used_at DESC OFFSET %s"
                    ")",
                    (int(target),))
                return cur.rowcount or 0
    except Exception as exc:  # noqa: BLE001
        _log.debug("pg_evict failed: %s: %s", type(exc).__name__, exc)
        return 0
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def psycopg2_bytes(b: bytes):
    """Wrap raw bytes for psycopg2's BYTEA adapter. Imported lazily so
    the bare module load works without psycopg2 present."""
    try:
        import psycopg2  # type: ignore[import]
        return psycopg2.Binary(b)
    except ImportError:
        # Should never reach this path — callers gate on _pg_connect()
        # which already checks the import. Defensive return keeps the
        # function total.
        return b


def _pg_circuit_reset() -> None:
    """Test hook: clear the soft circuit breaker so the next call
    re-attempts the PG connect. Used by tests that monkeypatch psycopg2
    or by long-running daemons that want to re-probe after a restart."""
    with _PG_CIRCUIT_LOCK:
        global _PG_LAST_FAILURE_AT
        _PG_LAST_FAILURE_AT = 0.0


def lookup_by_vector(query_vec: list[float],
                     *,
                     high: float = 0.97,
                     low: float = 0.92,
                     ) -> dict[str, Any] | None:
    """Cosine-similarity probe via dejavu_bridge.sem_cache_lookup.

    Returns the best matching entry as ``{kind, value_json,
    similarity}`` if ``query_vec``'s similarity to any cached vector
    is ``>= low``. Returns None if the cache is empty, the bridge
    is unavailable, or no entry clears ``low``.

    Useful for callers that already have a vector and want to know
    if a semantically similar query was recently embedded. The
    ``embed_query`` path itself does NOT consult this — see the
    module docstring for the v0.8.7 design rationale.
    """
    with _CACHE_LOCK:
        if not _VECTOR_CACHE:
            return None
        snap = list(_VECTOR_CACHE)
    try:
        try:
            import dejavu_bridge as _bridge  # type: ignore[import]
        except ImportError:
            import dejavu_bridge_stub as _bridge  # type: ignore[import]
        return _bridge.sem_cache_lookup(query_vec, snap, high=high, low=low)
    except Exception as exc:  # noqa: BLE001
        _log.warning("lookup_by_vector failed: %s: %s",
                     type(exc).__name__, exc)
        return None


# ─── Cloud call ──────────────────────────────────────────────────────────────

def embed_query(query: str,
                *,
                timeout_s: float = 1.0,
                ) -> list[float] | None:
    """Cloud-embed a single query string for nearVector search.

    Returns the 1024-dim e5-large-instruct vector, or None on any
    failure mode. None is a contract — callers MUST treat it as
    "RRF unavailable, fall back to BM25" and never crash.

    Per-process exact-match cache is consulted first; cache hits
    skip the network entirely.

    `timeout_s` bounds the urllib call. The JIT recall hook overrides
    this with min(0.8, 0.4 * deadline) so the embed step can't blow
    the 2s budget. CLI search uses the default 1.0s.
    """
    if not query:
        return None

    # Tier 1: in-process exact-match cache.
    cached = _cache_get(query)
    if cached is not None:
        return cached

    # Tier 2: PG-backed cross-session cache. None on miss / PG-down /
    # disabled / circuit-broken. Hits warm Tier 1 so subsequent calls
    # in this process skip PG too.
    pg_hit = _pg_lookup(query)
    if pg_hit is not None:
        _cache_put(query, pg_hit)
        return pg_hit

    key = _cloud_key()
    if not key:
        # Degraded mode: caller must do BM25-only. Document by returning
        # None silently (no warning spam — the JIT recall hook fires
        # hundreds of times per session and a missing key is a deliberate
        # user choice, not an error).
        return None

    url = _cloud_url()
    body = json.dumps({"texts": [query]}).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/v1/embed", data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "X-Embed-Model": "e5-large-instruct",
        })
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=float(timeout_s)) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        _log.debug("embed_query HTTP %s: %s", e.code, e.reason)
        return None
    except Exception as e:  # noqa: BLE001 — network/json/timeout/anything
        _log.debug("embed_query failed: %s: %s", type(e).__name__, e)
        return None

    embeddings = data.get("embeddings") if isinstance(data, dict) else None
    if not isinstance(embeddings, list) or not embeddings:
        return None
    vec_raw = embeddings[0]
    if not isinstance(vec_raw, list):
        return None
    try:
        vec = [float(x) for x in vec_raw]
    except (TypeError, ValueError):
        return None

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    _log.debug("embed_query %d-dim in %dms", len(vec), elapsed_ms)
    _cache_put(query, vec)
    # Tier 2 write — best-effort, PG-down silently skipped.
    _pg_store(query, vec)
    return vec


__all__ = [
    "embed_query",
    "lookup_by_vector",
    "_cache_reset",          # test hook
    "_cache_get",            # test hook
    "_CACHE_CAP",
    "_pg_lookup",            # exposed for direct tests
    "_pg_store",
    "_pg_evict",
    "_pg_circuit_reset",     # test hook
    "_hash_query",
    "_PG_CAP",
    "_PG_EVICT_EVERY",
]
