#!/usr/bin/env python3
"""
ingester.py — Parse Claude Code .jsonl transcripts into Postgres + Weaviate.

Idempotent: tracks last-ingested line per session via ingest_cursors.
Run modes:
  - Backfill all:   python ingester.py --backfill
  - Single session: python ingester.py --session <uuid> --jsonl <path>
  - Stop hook:      python ingester.py --stop-hook  (reads {session_id, transcript_path} from stdin)
"""
import argparse
import json
import os
import sys
import uuid as uuidlib
import hashlib
import re
import time
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

import psycopg2
import psycopg2.extras

# Read connection from config.env if present, else fallback.
# Resolution order:
#   1. CLAUDE_DEJAVU_DATA_ROOT (sandbox/tests/explicit override)
#   2. XDG_DATA_HOME/claude-dejavu (Linux default)
#   3. ~/Library/Application Support/claude-dejavu (macOS)
#   4. ~/.local/share/claude-dejavu (Linux fallback)
def _load_config():
    cfg = {}
    override = os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
    if override:
        home_data = Path(override)
    elif sys.platform == "darwin":
        home_data = Path.home() / "Library" / "Application Support" / "claude-dejavu"
    elif sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home() / "AppData/Local")
        home_data = Path(base) / "claude-dejavu"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        home_data = (Path(xdg) / "claude-dejavu") if xdg else Path.home() / ".local" / "share" / "claude-dejavu"
    f = home_data / "config.env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"): continue
            if "=" in line:
                k, v = line.split("=", 1); cfg[k.strip()] = v.strip()
    return cfg

_CFG = _load_config()
_OS_USER = os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"
_PG_DB  = _CFG.get("PG_DB") or f"claude_dejavu_{_OS_USER}"
PG_DSN = (f"host={_CFG.get('PG_HOST','localhost')} port={_CFG.get('PG_PORT','5450')} "
          f"user={_CFG.get('PG_USER','postgres')} password={_CFG.get('PG_PASS','postgres')} "
          f"dbname={_PG_DB}")
WEAVIATE_URL = _CFG.get("WV_URL", "http://localhost:8888")
# v0.8.12: honor CLAUDE_DEJAVU_WV_CLASS / WV_CLASS overrides so the bench
# corpus can ingest into a dedicated `ClaudeDejavuTurn_bench` class without
# polluting the live `ClaudeDejavuTurn` class. recall.py already honors
# the same env var (see code/recall.py:59). Default unchanged.
WEAVIATE_CLASS = (
    os.environ.get("CLAUDE_DEJAVU_WV_CLASS")
    or _CFG.get("WV_CLASS")
    or "ClaudeDejavuTurn"
)

# v0.8.15 — defense in depth for the bench-isolation contract: warn
# (loudly, once per slug) if a `-bench` project would route vectors
# into a non-bench Weaviate class. Catches the failure mode that
# v0.8.12 was built to prevent — bench fixture leaking into live.
_BENCH_SLUG_WARNED: set[str] = set()


def _warn_if_bench_misroute(slug: str) -> None:
    if not slug or not slug.endswith("-bench"):
        return
    if WEAVIATE_CLASS.endswith("_bench"):
        return
    if slug in _BENCH_SLUG_WARNED:
        return
    _BENCH_SLUG_WARNED.add(slug)
    print(
        f"\n!!! WARNING: project_slug='{slug}' (bench fixture) "
        f"is routing vectors into Weaviate class "
        f"'{WEAVIATE_CLASS}' (not _bench). Set "
        f"CLAUDE_DEJAVU_WV_CLASS=<something>_bench to isolate, "
        f"or your live ClaudeDejavuTurn class WILL be polluted.\n",
        file=sys.stderr, flush=True,
    )

# Cloud embed mode (DEJAVU_EMBED_MODE=cloud) routes vectorization through
# the dejavu-cloud managed embedder. When active, every batch upsert
# computes vectors client-side and sends them with the object payload (the
# Weaviate class must be recreated with vectorizer:none — see migrate_to_cloud.py).
_DEJAVU_EMBED_MODE = (os.environ.get("DEJAVU_EMBED_MODE")
                      or _CFG.get("DEJAVU_EMBED_MODE", "local")).strip().lower()
# embedding_provider only reads env vars, so propagate config.env values
# into the env at import time (env wins on conflict — explicit override).
for _k in ("DEJAVU_EMBED_MODE", "DEJAVU_CLOUD_URL", "DEJAVU_CLOUD_KEY",
           "DEJAVU_LOCAL_URL"):
    if _CFG.get(_k) and not os.environ.get(_k):
        os.environ[_k] = _CFG[_k]
TOOL_OUTPUT_TRUNC = 8000
CONTENT_VECTORIZE_MAX = 4000
CONTENT_VECTORIZE_MIN = 50
WEAVIATE_BATCH_SIZE = 32
WEAVIATE_TIMEOUT = 600

# Local imports
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    # v0.8.6 (Context Economy phase 3): route gist computation through
    # `distill_pipeline`, which wraps the closed-source `dejavu_bridge`
    # wheel (or the in-tree stub when the wheel is missing). The
    # pipeline preserves load-bearing tokens (paths, ports, env vars,
    # error class names, version strings, flags) and skips
    # uncompressible content (code blocks, tracebacks, JSON, diffs).
    # Returns (None, None) for skip-types so the caller stores
    # gist=NULL and recall falls back to content[:200].
    from distill_pipeline import make_gist as _distill_make_gist
    from distill_pipeline import _bridge as _bridge_mod  # noqa: F401

    def make_gist(text, role=None, content_type=None, prefer_llm=False):
        # `prefer_llm` retained for legacy callers; ignored — the
        # pipeline decides whether to invoke the bridge.
        return _distill_make_gist(text, role=role, content_type=content_type)
except Exception:
    # Belt-and-suspenders fallback: even the stub import shouldn't
    # fail, but if something deeper breaks (PYTHONPATH munged, etc.),
    # legacy first-80-chars behaviour keeps ingest moving.
    def make_gist(text, role=None, content_type=None, prefer_llm=False):
        return text[:80], "rule"

    _bridge_mod = None  # type: ignore[assignment]


# v0.8.7 (Context Economy phase 4): trash-input gate. Bridge primitive
# `gate(text) -> bool` returns True if a turn is worth embedding under
# the spec defaults (>= 50 chars, >= 30% alphanumeric). Skipping
# low-signal turns BEFORE the cloud-embed roundtrip saves quota +
# clutter; gate-rejected rows just stay `vectorized=FALSE` (no schema
# change). On bridge import failure we fall through to "accept all",
# matching v0.8.6 behaviour exactly so nobody regresses.

# Module-level counter for `_gate_emit_summary` to log at end of ingest.
_GATE_COUNTERS: dict[str, int] = {"accepted": 0, "rejected": 0}


def _gate_ok(text: str) -> bool:
    """Bridge-gated trash check. Returns True if the text passes the
    bridge's `gate` (i.e., is worth embedding). Bumps the appropriate
    counter so the caller's end-of-ingest log can surface skip totals.
    Any exception (broken wheel, malformed text) falls through to
    True — never let a wheel bug suppress real content.
    """
    try:
        if _bridge_mod is None:
            _GATE_COUNTERS["accepted"] += 1
            return True
        ok = bool(_bridge_mod.gate(text or ""))
    except Exception:
        # Defensive: bridge raised — accept the turn. We'd rather
        # waste an embed call than lose a real turn to a wheel
        # regression.
        _GATE_COUNTERS["accepted"] += 1
        return True
    if ok:
        _GATE_COUNTERS["accepted"] += 1
    else:
        _GATE_COUNTERS["rejected"] += 1
    return ok


def _gate_reset() -> None:
    """Test hook: zero the counters."""
    _GATE_COUNTERS["accepted"] = 0
    _GATE_COUNTERS["rejected"] = 0


def decode_project_path(encoded: str) -> str:
    """Best-effort decode. The Claude Code encoding (`/` → `-`) is lossy
    for project names that contain hyphens, so we just store the encoded form
    when we cannot reliably split. Used only for `project_path` text field."""
    if encoded == "_orphan" or not encoded:
        return ""
    return encoded  # store verbatim to avoid lossy splitting


def project_slug(encoded: str) -> str:
    """Heuristic slug derivation from Claude Code's encoded project path.

    Claude Code stores transcripts under ~/.claude/projects/<encoded>/<uuid>.jsonl
    where <encoded> is the absolute project path with '/' replaced by '-'.
    e.g.  /home/alice/projects/my-app  →  -home-alice-projects-my-app

    Resolution order:
      1. '-projects-<slug>'         (most common: ~/Documents/projects/<slug>, ~/projects/<slug>)
      2. '-home-<user>--<slug>'     (double-hyphen sentinel for hidden dirs like ~/.claude-…)
      3. '-home-<user>-<rest>'      (trailing component after the home prefix)
      4. fallback: encoded path stripped of leading hyphens
    """
    if encoded == "_orphan" or not encoded:
        return "_orphan"
    e = encoded
    # 1. Most common: …-projects-<slug>
    if "-projects-" in e:
        return e.split("-projects-", 1)[1] or e
    # 2. -home-<user>--<rest>  (double-hyphen marks where '/.<dir>' became '/-<dir>')
    m = re.match(r"^-home-[^-]+--(.+)$", e)
    if m:
        return m.group(1) or e
    # 3. -home-<user>-<rest>
    m = re.match(r"^-home-[^-]+-(.+)$", e)
    if m:
        return m.group(1) or e
    # 4. fallback
    return e.lstrip("-")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _strip_nul(obj):
    """Recursively strip NUL (0x00) bytes from strings in any nested
    structure. Postgres TEXT and JSONB both reject NUL bytes; Claude Code
    .jsonl files can contain them when tool outputs captured binary or
    truncated terminal data. Without this, a single NUL-containing turn
    aborts the whole ingest transaction and the cursor never advances —
    every subsequent SessionEnd hook re-fails at the same byte and the
    session goes silently un-indexed.
    """
    if isinstance(obj, str):
        return obj.replace("\x00", "") if "\x00" in obj else obj
    if isinstance(obj, dict):
        return {k: _strip_nul(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_nul(x) for x in obj]
    return obj


def extract_text_blocks(content) -> list[tuple[str, str]]:
    """Return list of (content_type, text) pairs from a message.content field.

    All returned strings are NUL-sanitized at the boundary so downstream
    PG inserts cannot crash on `\\x00`.
    """
    out: list[tuple[str, str]] = []
    if isinstance(content, str):
        out.append(("text", content))
    elif isinstance(content, list):
        for c in content:
            if not isinstance(c, dict):
                continue
            ct = c.get("type")
            if ct == "text":
                out.append(("text", c.get("text", "")))
            elif ct == "thinking":
                out.append(("thinking", c.get("thinking", "")))
            elif ct == "tool_use":
                out.append(("tool_use", json.dumps(
                    {"name": c.get("name"),
                     "input": _strip_nul(c.get("input")),
                     "id": c.get("id")})))
            elif ct == "tool_result":
                tr = c.get("content", "")
                if isinstance(tr, list):
                    tr = "\n".join(
                        x.get("text", "") for x in tr
                        if isinstance(x, dict) and x.get("type") == "text"
                    )
                out.append(("tool_result", tr if isinstance(tr, str) else json.dumps(tr)))
    # Sanitize every emitted string before it leaves this function.
    return [(ct, _strip_nul(t) if isinstance(t, str) else t) for ct, t in out]


def weaviate_id_for(session_id: str, turn_id: int) -> str:
    return str(uuidlib.uuid5(uuidlib.NAMESPACE_URL, f"claudeturn:{session_id}:{turn_id}"))


def weaviate_batch_upsert(items: list[tuple[int, dict]]) -> dict[int, str]:
    """Upsert a batch of items via /v1/batch/objects. Returns {turn_id: weaviate_id}.
    Uses deterministic UUIDs so re-runs overwrite the same object.

    Cloud mode (DEJAVU_EMBED_MODE=cloud) embeds via the dejavu-cloud Worker
    BEFORE upsert, and sends the vector along with the object. The class
    must have `vectorizer:none` — run code/migrate_to_cloud.py once.
    """
    if not items:
        return {}
    objects = []
    out = {}
    for turn_id, props in items:
        oid = weaviate_id_for(props["session_id"], turn_id)
        out[turn_id] = oid
        objects.append({"class": WEAVIATE_CLASS, "id": oid, "properties": props})

    # ---- CLOUD MODE: client-side vectorization ----
    if _DEJAVU_EMBED_MODE == "cloud":
        try:
            from embedding_provider import get_provider, EmbeddingError  # type: ignore
        except ImportError as e:
            print(f"  cloud mode: embedding_provider unavailable: {e}", file=sys.stderr)
        else:
            try:
                prov = get_provider()
                # We embed on `content` (truncated). Match the local
                # text2vec-transformers default which would have done the
                # same field selection per moduleConfig.
                texts = [(o["properties"].get("content") or "")[:CONTENT_VECTORIZE_MAX]
                         for o in objects]
                result = prov.embed(texts)
                if len(result.vectors) != len(objects):
                    print(f"  cloud embed length mismatch: "
                          f"got {len(result.vectors)} for {len(objects)} objects",
                          file=sys.stderr)
                else:
                    for o, v in zip(objects, result.vectors):
                        o["vector"] = v
                    print(f"  cloud embed: {len(objects)} texts, {result.tokens} tokens, "
                          f"{result.cache_hits} cache hits, {result.elapsed_ms}ms",
                          file=sys.stderr)
            except EmbeddingError as e:
                # The objects below will be sent WITHOUT a `vector` field,
                # which means Weaviate falls back to its CLASS-CONFIGURED
                # vectorizer. That works only if (a) the class still has
                # `text2vec-transformers` AND (b) the inference container
                # is reachable. Cloud-only users (transformers stopped,
                # class migrated to vectorizer:none) will see this error
                # propagate as `weaviate batch item N failed: …` below.
                print(f"  cloud embed failed: {e} — sending without vector "
                      f"(Weaviate will try its configured vectorizer; will "
                      f"fail if class=vectorizer:none or container is down)",
                      file=sys.stderr)

    body = json.dumps({"objects": objects}).encode()
    req = urllib.request.Request(f"{WEAVIATE_URL}/v1/batch/objects", data=body, method="POST", headers={"Content-Type": "application/json"})
    succeeded = {}
    try:
        with urllib.request.urlopen(req, timeout=WEAVIATE_TIMEOUT) as r:
            results = json.loads(r.read())
        for i, res in enumerate(results):
            turn_id = items[i][0]
            status = (res.get("result", {}) or {}).get("status")
            errors = (res.get("result", {}) or {}).get("errors")
            if status == "SUCCESS" or (status is None and not errors):
                succeeded[turn_id] = out[turn_id]
            else:
                err = errors.get("error") if errors else "unknown"
                print(f"  weaviate batch item {turn_id} failed: {err}", file=sys.stderr)
    except urllib.error.HTTPError as e:
        err_body = e.read()[:500].decode(errors='replace')
        print(f"  weaviate batch HTTP {e.code}: {err_body}", file=sys.stderr)
    except Exception as e:
        print(f"  weaviate batch error: {type(e).__name__}: {e}", file=sys.stderr)
    return succeeded


def _ingest_lock_path(session_uuid: str) -> Path:
    """Per-session ingest lock file. Prevents concurrent stop-hooks for the
    same session from racing on PG (multiple Claude Code windows ending
    around the same time used to deadlock here).

    Stale locks (older than INGEST_LOCK_TTL_S) are reaped — handles
    crashed-ingester scenario without manual cleanup."""
    cfg = _CFG
    data_root = cfg.get("DATA_ROOT")
    if not data_root:
        override = os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
        data_root = override or str(Path.home() / ".local" / "share" / "claude-dejavu")
    locks_dir = Path(data_root) / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    return locks_dir / f"ingest-{session_uuid}.lock"


INGEST_LOCK_TTL_S = 1800   # 30 min — long enough for a 50 MB session,
                            # short enough that a hung-and-killed
                            # ingester clears within 30 min.


def _acquire_ingest_lock(session_uuid: str) -> bool:
    """Best-effort file lock — write our PID into the lockfile.
    Returns True if we got it."""
    lock = _ingest_lock_path(session_uuid)
    try:
        if lock.exists():
            age = time.time() - lock.stat().st_mtime
            if age < INGEST_LOCK_TTL_S:
                # Another live ingester for this session.
                return False
            # Stale — reap.
            lock.unlink(missing_ok=True)
        # O_CREAT|O_EXCL is the canonical "atomic create new file" pattern.
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception:
        # If we can't lock for any other reason (perms, FS full), proceed
        # without lock — better to ingest than to silently skip.
        return True


def _release_ingest_lock(session_uuid: str) -> None:
    try:
        _ingest_lock_path(session_uuid).unlink(missing_ok=True)
    except Exception:
        pass


def _progress_emit(session_uuid: str, line_no: int, file_size: int,
                   bytes_read: int, new_turns: int, started: float) -> None:
    """Emit a one-line progress heartbeat to stderr. Picked up by stop.py's
    ingest.log redirect AND by `claude-dejavu reingest --verbose`."""
    elapsed = time.time() - started
    pct = (bytes_read / file_size * 100) if file_size > 0 else 0.0
    rate = (new_turns / elapsed) if elapsed > 0 else 0.0
    print(f"[ingest] {session_uuid[:8]}  line={line_no}  bytes={bytes_read}/{file_size} ({pct:.1f}%)  "
          f"+{new_turns}t  {rate:.0f}t/s  {elapsed:.0f}s",
          file=sys.stderr, flush=True)


def _should_skip_repaired(jsonl_path) -> bool:
    """Return True if ``jsonl_path`` has a fresh ``.dejavu-repair-fingerprint``
    sidecar (meaning we just reconstructed it from the DB and re-ingesting
    would create duplicate ``turns`` rows via line_no-based idx).

    Spec section 7.1 (idx-collision avoidance): the repair command writes
    the fingerprint at ``<live_stem>.dejavu-repair-fingerprint`` after a
    successful write. Claude Code's first session-resume bumps the file's
    mtime past the fingerprint (>5s slack for filesystem clock skew), at
    which point we clear the stale fingerprint and allow normal ingest.
    """
    p = Path(jsonl_path)
    fp = p.parent / f"{p.stem}.dejavu-repair-fingerprint"
    if not fp.is_file():
        return False
    try:
        meta = json.loads(fp.read_text(encoding="utf-8"))
        repaired_at = int(meta.get("repaired_at", 0))
    except Exception:
        return False
    try:
        mtime = int(p.stat().st_mtime)
    except OSError:
        return False
    if mtime > repaired_at + 5:  # 5s slack for filesystem clock skew
        try:
            fp.unlink()
        except OSError:
            pass
        return False
    return True


def ingest_jsonl(jsonl_path: str, session_uuid: str, project_encoded: str | None, conn):
    """Ingest a single jsonl file. Idempotent via ingest_cursors. Per-session
    locked to prevent concurrent stop-hooks from racing on the same session."""
    if not os.path.exists(jsonl_path):
        print(f"  jsonl not found: {jsonl_path}", file=sys.stderr)
        return 0

    # v0.7.1: skip if the file was just reconstructed by `claude-dejavu
    # repair` (spec section 7.1). The fingerprint sidecar tells us the
    # current on-disk content already round-trips the DB — re-ingesting
    # would land at new line-numbered idx values and duplicate every
    # reconstructed turn.
    if _should_skip_repaired(jsonl_path):
        print(f"[ingest] {Path(jsonl_path).name}: "
              f"skipped (fresh dejavu-repair-fingerprint)",
              file=sys.stderr)
        return 0

    if not _acquire_ingest_lock(session_uuid):
        # Another ingester for this exact session is already running.
        # Skip cleanly — its progress is what we'd be writing anyway.
        print(f"[ingest] {session_uuid[:8]} skipped (another ingester holds the lock)",
              file=sys.stderr)
        return None

    try:
        return _ingest_jsonl_locked(jsonl_path, session_uuid, project_encoded, conn)
    finally:
        # Always release the lock — even on uncaught exceptions, KeyboardInterrupt,
        # OOM, etc. The lock TTL is a fallback; this is the primary release.
        _release_ingest_lock(session_uuid)


def _ingest_jsonl_locked(jsonl_path: str, session_uuid: str,
                          project_encoded: str | None, conn):
    """Inner ingest body, called only with the per-session lock held.
    Split out so `ingest_jsonl` can wrap the body in try/finally for
    guaranteed lock release."""
    file_size = os.path.getsize(jsonl_path)
    file_sha = sha256_file(jsonl_path) if file_size < 200_000_000 else None

    if not project_encoded:
        # try to derive from path
        m = re.search(r'/projects/(-home-[^/]+)/', jsonl_path)
        project_encoded = m.group(1) if m else "_orphan"

    slug = project_slug(project_encoded)
    _warn_if_bench_misroute(slug)
    project_path_decoded = decode_project_path(project_encoded)

    cur = conn.cursor()

    # Upsert session (now stamped with os_user for multi-user isolation)
    cur.execute("""
        INSERT INTO sessions (id, project_path, project_slug, os_user, jsonl_path, jsonl_size, jsonl_sha256)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            jsonl_path = EXCLUDED.jsonl_path,
            jsonl_size = EXCLUDED.jsonl_size,
            jsonl_sha256 = COALESCE(EXCLUDED.jsonl_sha256, sessions.jsonl_sha256),
            os_user = COALESCE(sessions.os_user, EXCLUDED.os_user)
    """, (session_uuid, project_path_decoded, slug, _OS_USER, jsonl_path, file_size, file_sha))

    # Get last cursor
    cur.execute("SELECT last_line FROM ingest_cursors WHERE session_id = %s", (session_uuid,))
    row = cur.fetchone()
    last_line = row[0] if row else 0

    # Stream the jsonl from last_line
    new_turns = 0
    new_vectors = 0
    ai_title = None
    started_at = None
    last_ts = None
    line_no = 0
    pending_vec: list[tuple[int, dict]] = []  # (turn_id, props)

    def flush_vectors():
        nonlocal new_vectors
        if not pending_vec:
            return
        ok = weaviate_batch_upsert(pending_vec)
        if ok:
            psycopg2.extras.execute_values(
                cur,
                "UPDATE turns AS t SET vectorized=TRUE, weaviate_id=v.wid::uuid FROM (VALUES %s) AS v(tid, wid) WHERE t.id = v.tid",
                [(tid, wid) for tid, wid in ok.items()],
            )
            new_vectors += len(ok)
            conn.commit()
        pending_vec.clear()

    try:
        # Explicit utf-8 + errors='replace' so a stray byte from a
        # Windows-encoded paste or a truncated terminal capture doesn't
        # crash the read loop with UnicodeDecodeError.
        fh = open(jsonl_path, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        # Source jsonl was deleted or moved (common when bootstrapping
        # against an aged ingest_cursors row pointing at a removed
        # project). Skip cleanly — the cursor stays untouched so a
        # later run can pick it up if the file returns.
        # Lock is released by the outer ingest_jsonl wrapper's finally.
        print(f"[ingest] source missing, skipping: {jsonl_path}",
              file=sys.stderr)
        return None

    # Periodic-commit cadence: how many successful turns between durable
    # cursor saves. Trades a little throughput for crash-resilience —
    # if a later turn explodes, we still keep ~N turns of forward
    # progress instead of redoing everything from `last_line`.
    COMMIT_EVERY_N_TURNS = 500
    PROGRESS_EVERY_N_TURNS = 250
    turns_since_commit = 0
    turns_since_progress = 0
    started = time.time()
    bytes_read = 0

    def _save_cursor_and_commit(at_line: int, at_byte: int) -> None:
        """Persist progress up to `at_line` (and the actual byte offset
        within the jsonl file at that line) and commit. Used for
        periodic durable progress + final-bookkeeping commit at end."""
        cur.execute("""
            INSERT INTO ingest_cursors (session_id, last_line, last_byte, last_run_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (session_id) DO UPDATE
              SET last_line  = EXCLUDED.last_line,
                  last_byte  = EXCLUDED.last_byte,
                  last_run_at = now()
        """, (session_uuid, at_line, at_byte))
        conn.commit()

    skipped_lines = 0
    failed_lines = 0
    with fh:
        for line in fh:
            line_no += 1
            # Track actual byte offset by accumulating per-line length.
            # We can't use `fh.tell()` here because Python's text-mode
            # file iterator uses an internal readahead buffer that
            # disables tell() (raises "telling position disabled by
            # next() call"). Encoding each line back to bytes is
            # ~free (microseconds) and gives us an accurate offset.
            bytes_read += len(line.encode("utf-8", errors="replace"))
            if line_no <= last_line:
                continue
            # ── per-line try/except ──────────────────────────────────
            # Wrap each line so one bad turn cannot abort the whole
            # session ingest. Without this, a single PG-rejected row
            # leaves the transaction in an aborted state and every
            # subsequent statement fails with InFailedSqlTransaction
            # — which is exactly what made dejavu lose entire
            # sessions before this fix landed.
            try:
                try:
                    obj = json.loads(line)
                except Exception:
                    skipped_lines += 1
                    continue

                # Capture aiTitle
                if obj.get("type") == "ai-title":
                    ai_title = _strip_nul(obj.get("aiTitle"))
                    continue

                # Skip non-message types we don't track as turns
                if obj.get("type") not in ("user", "assistant"):
                    continue

                ts = parse_ts(obj.get("timestamp"))
                if ts and not started_at:
                    started_at = ts
                if ts:
                    last_ts = ts

                msg = obj.get("message", {}) or {}
                role = obj.get("type") if obj.get("type") in ("user", "assistant") else msg.get("role")
                model = msg.get("model") if obj.get("type") == "assistant" else None
                usage = msg.get("usage") or {}
                tokens_in = usage.get("input_tokens")
                tokens_out = usage.get("output_tokens")

                content = msg.get("content")
                blocks = extract_text_blocks(content)

                for content_type, text in blocks:
                    if not text:
                        continue
                    # Defense-in-depth: the source `extract_text_blocks`
                    # already strips NULs, but belt-and-suspenders here
                    # in case a custom call site bypassed it.
                    text = _strip_nul(text)
                    # Compute gist for user/asst text (cheapest case in the hot path)
                    # v0.8.6: make_gist may return (None, None) for
                    # uncompressible content (code blocks, tracebacks,
                    # diffs, JSON) — the caller stores gist=NULL and
                    # recall falls back to content[:200].
                    gist_val, gist_method = (None, None)
                    if role in ("user", "assistant") and content_type == "text" and len(text) >= 30:
                        gist_val, gist_method = make_gist(text, role=role, content_type=content_type, prefer_llm=False)
                        if gist_val is not None:
                            gist_val = _strip_nul(gist_val)

                    cur.execute("""
                        INSERT INTO turns (session_id, idx, parent_uuid, message_uuid, role, ts, content, content_type, tokens_in, tokens_out, model, gist, gist_method)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (session_id, idx) DO NOTHING
                        RETURNING id
                    """, (session_uuid, line_no * 10 + len(text) % 9,
                          obj.get("parentUuid"), obj.get("uuid"),
                          role, ts, text, content_type, tokens_in, tokens_out, model,
                          gist_val, gist_method))
                    row = cur.fetchone()
                    if row is None:
                        continue
                    turn_id = row[0]
                    new_turns += 1
                    turns_since_commit += 1

                    # Tool calls + file touches
                    if content_type == "tool_use":
                        try:
                            d = json.loads(text)
                        except Exception:
                            d = {}
                        tool_name = _strip_nul(d.get("name", "")) or ""
                        tool_input = _strip_nul(d.get("input") or {})
                        cur.execute("""
                            INSERT INTO tool_calls (turn_id, session_id, tool_use_id, tool_name, tool_input, ts)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            RETURNING id
                        """, (turn_id, session_uuid, d.get("id"), tool_name, psycopg2.extras.Json(tool_input), ts))
                        tc_id = cur.fetchone()[0]
                        # File touches
                        if tool_name in ("Write", "Edit", "MultiEdit") and isinstance(tool_input, dict):
                            fp = _strip_nul(tool_input.get("file_path"))
                            if fp:
                                op = {"Write": "write", "Edit": "edit", "MultiEdit": "multi_edit"}[tool_name]
                                cur.execute("""
                                    INSERT INTO file_touches (tool_call_id, session_id, file_path, op, ts)
                                    VALUES (%s, %s, %s, %s, %s)
                                """, (tc_id, session_uuid, fp, op, ts))

                    # Vectorize ONLY user/assistant text (skip tool_use/tool_result/thinking)
                    # AND skip trivially short messages.
                    # v0.8.7: trash-input gate runs before pushing to the
                    # cloud-embed queue. Rejected rows stay vectorized=FALSE
                    # (no schema change) and just don't get embedded —
                    # the `_GATE_COUNTERS` summary surfaces how many we
                    # skipped at end of ingest.
                    if (role in ("user", "assistant")
                            and content_type == "text"
                            and len(text) >= CONTENT_VECTORIZE_MIN
                            and _gate_ok(text)):
                        snippet = text[:CONTENT_VECTORIZE_MAX]
                        pending_vec.append((turn_id, {
                            "session_id": session_uuid,
                            "turn_id": turn_id,
                            "project_slug": slug,
                            "role": role,
                            "content": snippet,
                            "ts": ts.isoformat() if ts else None,
                            "model": model or "",
                            "ai_title": ai_title or "",
                        }))
                        if len(pending_vec) >= WEAVIATE_BATCH_SIZE:
                            flush_vectors()

                # Periodic progress heartbeat — visible in `claude-dejavu
                # reingest` interactive runs and in stop.py's ingest.log.
                # Lets the user see "is it making progress" without
                # opening pg_stat_activity.
                turns_since_progress += 1
                if turns_since_progress >= PROGRESS_EVERY_N_TURNS:
                    _progress_emit(session_uuid, line_no, file_size,
                                   bytes_read, new_turns, started)
                    turns_since_progress = 0

                # Periodic durable progress: every COMMIT_EVERY_N_TURNS
                # successful turns, flush pending vectors, persist the
                # cursor up to the current line, and commit. If a later
                # line crashes the loop, we keep at least N-N_TURNS
                # turns of forward progress instead of having to redo
                # the entire session from scratch on next run.
                if turns_since_commit >= COMMIT_EVERY_N_TURNS:
                    flush_vectors()
                    _save_cursor_and_commit(line_no, bytes_read)
                    turns_since_commit = 0

            except Exception as e:
                # One bad line should not break the rest of the
                # session. Roll back the aborted transaction (psycopg2
                # leaves it in InFailedSqlTransaction state otherwise),
                # log a single line of context, and march on.
                conn.rollback()
                failed_lines += 1
                snippet = (line[:120] if isinstance(line, str) else str(line)[:120]).replace("\n", " ")
                print(f"[ingest] line {line_no} skipped ({type(e).__name__}: {e}) — {snippet!r}",
                      file=sys.stderr)
                # Move the cursor past this line so we don't retry it
                # next run. Save in its own tx since previous one was
                # rolled back.
                try:
                    _save_cursor_and_commit(line_no, bytes_read)
                except Exception:
                    conn.rollback()

    # Flush any remaining vectorization pending
    flush_vectors()
    if skipped_lines or failed_lines:
        print(f"[ingest] {jsonl_path}: skipped_json={skipped_lines}  failed_inserts={failed_lines}",
              file=sys.stderr)
    # Final completion heartbeat (always, regardless of N-turn cadence,
    # so even tiny sessions log a result line).
    elapsed = time.time() - started
    gate_rej = _GATE_COUNTERS.get("rejected", 0)
    gate_tail = (f"  gate_skipped={gate_rej}" if gate_rej else "")
    print(f"[ingest] {session_uuid[:8]} done  +{new_turns} turns  +{new_vectors} vectors  "
          f"line={line_no}  bytes={bytes_read}/{file_size}  in {elapsed:.1f}s"
          f"{gate_tail}",
          file=sys.stderr, flush=True)

    # Update session aggregates
    cur.execute("""
        UPDATE sessions SET
            ai_title = COALESCE(%s, ai_title),
            started_at = COALESCE(started_at, %s),
            ended_at = %s,
            total_turns = (SELECT count(*) FROM turns WHERE session_id = %s)
        WHERE id = %s
    """, (ai_title, started_at, last_ts, session_uuid, session_uuid))

    # Save cursor — at end-of-file, last_byte is the actual file size
    # because we read to EOF.
    cur.execute("""
        INSERT INTO ingest_cursors (session_id, last_line, last_byte, last_run_at)
        VALUES (%s, %s, %s, now())
        ON CONFLICT (session_id) DO UPDATE SET last_line = EXCLUDED.last_line, last_byte = EXCLUDED.last_byte, last_run_at = now()
    """, (session_uuid, line_no, file_size))

    conn.commit()
    # Lock release handled by outer ingest_jsonl finally.
    return new_turns, new_vectors, line_no


def cmd_bootstrap_from_live(conn):
    """One-shot: scan ~/.claude/projects/<encoded>/<uuid>.jsonl (Claude
    Code's live transcript directory), mirror each to chat-logs/, and
    ingest. Used by:
      - install.py at first-install time (so a freshly-installed plugin
        immediately knows about every past session — closes the
        'reinstall after 4 months gives me back nothing' gap).
      - SessionStart auto-trigger when the live tree has many .jsonl
        files but the DB has few.

    Idempotent — re-runs are no-ops because ingest_cursors tracks per-
    session position. Mirroring uses os.path.exists + size check to
    skip files that are already mirrored at the same size.
    """
    import shutil
    home = Path.home()
    live_root = home / ".claude" / "projects"
    if not live_root.is_dir():
        print("[bootstrap] no ~/.claude/projects directory — nothing to do.")
        return
    data_root = _CFG.get("DATA_ROOT") or str(home / ".local" / "share" / "claude-dejavu")
    backup_root = Path(data_root) / "chat-logs"
    backup_root.mkdir(parents=True, exist_ok=True)

    n_mirrored = n_sess = n_turn = n_vec = 0
    skipped_mirror = 0
    for proj_enc in sorted(os.listdir(live_root)):
        pdir_live = live_root / proj_enc
        if not pdir_live.is_dir():
            continue
        pdir_back = backup_root / proj_enc
        pdir_back.mkdir(parents=True, exist_ok=True)
        for f in sorted(os.listdir(pdir_live)):
            if not f.endswith(".jsonl"):
                continue
            src = pdir_live / f
            dst = pdir_back / f
            try:
                src_size = src.stat().st_size
            except OSError:
                continue
            if dst.exists() and dst.stat().st_size == src_size:
                skipped_mirror += 1
            else:
                try:
                    shutil.copy2(src, dst)
                    n_mirrored += 1
                except OSError as e:
                    print(f"  (mirror failed: {src} -> {dst}: {e})", file=sys.stderr)
                    continue
            session_uuid = f.replace(".jsonl", "")
            res = ingest_jsonl(str(dst), session_uuid, proj_enc, conn)
            if res:
                turns, vecs, _ = res
                if turns or vecs:
                    n_sess += 1
                    n_turn += turns
                    n_vec += vecs
    print(f"[bootstrap] mirrored {n_mirrored} new (.jsonl), skipped {skipped_mirror} unchanged")
    print(f"[bootstrap] ingested {n_sess} session(s), {n_turn} turns, {n_vec} vectors")


def cmd_backfill(conn):
    """Walk <DATA_ROOT>/chat-logs/<encoded-project>/<uuid>.jsonl"""
    data_root = _CFG.get("DATA_ROOT")
    if not data_root:
        # mirror the same resolution chain as _load_config
        override = os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
        data_root = override or str(Path.home() / ".local" / "share" / "claude-dejavu")
    backup_root = f"{data_root}/chat-logs"
    n_sess = n_turn = n_vec = 0
    for proj_enc in sorted(os.listdir(backup_root)):
        pdir = os.path.join(backup_root, proj_enc)
        if not os.path.isdir(pdir):
            continue
        for f in sorted(os.listdir(pdir)):
            if not f.endswith(".jsonl"):
                continue
            session_uuid = f.replace(".jsonl", "")
            jsonl = os.path.join(pdir, f)
            print(f"[ingest] {proj_enc}/{session_uuid}  size={os.path.getsize(jsonl)/1e6:.1f}MB")
            t0 = time.time()
            res = ingest_jsonl(jsonl, session_uuid, proj_enc, conn)
            if res:
                turns, vecs, lines = res
                dt = time.time() - t0
                print(f"  -> +{turns} turns, +{vecs} vectors, lines={lines}, took {dt:.1f}s")
                n_sess += 1; n_turn += turns; n_vec += vecs
    print(f"\nDone: {n_sess} sessions, {n_turn} turns, {n_vec} vectors")


def cmd_stop_hook(conn):
    """Read hook payload from stdin and ingest the named session incrementally."""
    payload = json.loads(sys.stdin.read())
    session_id = payload.get("session_id")
    transcript = payload.get("transcript_path")
    if not (session_id and transcript):
        return
    # Derive project from transcript path
    m = re.search(r'/projects/(-home-[^/]+)/', transcript)
    proj_enc = m.group(1) if m else "_orphan"
    res = ingest_jsonl(transcript, session_id, proj_enc, conn)
    if res:
        print(f"[stop-hook] +{res[0]} turns, +{res[1]} vectors")
    # v0.9.3 — incremental sync runs unconditionally; the local
    # vector index is AUTO-ON. Best-effort: when (a) numpy is
    # available, (b) the index has been bootstrapped at least once
    # (else there's nothing to sync against), and (c) Weaviate is
    # reachable, we append any new turns. Any failure is logged +
    # returns — the ingest itself already committed to PG/Weaviate
    # so a vector-index sync miss just means the next ingest tick
    # catches up. The old CLAUDE_DEJAVU_LOCAL_VECTOR_INDEX=false
    # env survives as an explicit kill switch.
    local_vec_disabled = (os.environ.get(
        "CLAUDE_DEJAVU_LOCAL_VECTOR_INDEX", "true").lower()
                           in ("0", "false", "no", "off"))
    if not local_vec_disabled:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from vector_index import (  # type: ignore[import]
                TurnVectorIndex, _is_available)
            if _is_available():
                # Resolve the data root the same way mcp/server.py
                # does (XDG_DATA_HOME → ~/.local/share/claude-dejavu).
                xdg = os.environ.get("XDG_DATA_HOME")
                home = Path.home()
                if sys.platform == "darwin":
                    data_root = (home / "Library"
                                  / "Application Support"
                                  / "claude-dejavu")
                elif sys.platform.startswith("win"):
                    base = (os.environ.get("LOCALAPPDATA")
                             or os.environ.get("APPDATA")
                             or str(home / "AppData" / "Local"))
                    data_root = Path(base) / "claude-dejavu"
                else:
                    data_root = (
                        Path(xdg) / "claude-dejavu" if xdg
                        else home / ".local" / "share"
                              / "claude-dejavu")
                override = os.environ.get(
                    "CLAUDE_DEJAVU_DATA_ROOT")
                if override:
                    data_root = Path(override)
                idx = TurnVectorIndex.load_or_empty(data_root)
                if idx.is_loaded:
                    n = idx.sync_new_from_weaviate(
                        WEAVIATE_CLASS, WEAVIATE_URL)
                    if n > 0:
                        idx.save()
                        print(f"[stop-hook] +{n} vectors to "
                              f"local index")
        except Exception as e:
            # Non-fatal — log and move on.
            print(f"[stop-hook] local-vector-index sync skipped: "
                  f"{type(e).__name__}: {e}")


def _ingest_one_with_own_conn(jsonl_path: str, session_uuid: str,
                                proj_enc: str) -> tuple[str, tuple | None, str | None]:
    """Worker fn for parallel reingest. Opens its own PG connection so
    multiple workers can run concurrently. Returns
    (session_uuid, ingest_result_or_None, error_str_or_None)."""
    try:
        worker_conn = psycopg2.connect(PG_DSN)
    except Exception as e:
        return (session_uuid, None, f"connect-failed: {type(e).__name__}: {e}")
    try:
        res = ingest_jsonl(jsonl_path, session_uuid, proj_enc, worker_conn)
        return (session_uuid, res, None)
    except Exception as e:
        try:
            worker_conn.rollback()
        except Exception:
            pass
        return (session_uuid, None, f"{type(e).__name__}: {e}")
    finally:
        try:
            worker_conn.close()
        except Exception:
            pass


def _find_missing_sessions(conn) -> list[tuple[str, str, str, int]]:
    """Walk ~/.claude/projects/ and return (sid, proj_enc, jsonl_path, size_bytes)
    for every session whose jsonl exists on disk but has zero indexed
    turns OR no ingest_cursors row. Used by --missing-only."""
    live_root = Path.home() / ".claude" / "projects"
    if not live_root.is_dir():
        return []
    cur = conn.cursor()
    out: list[tuple[str, str, str, int]] = []
    for proj in sorted(live_root.iterdir()):
        if not proj.is_dir():
            continue
        for f in sorted(proj.iterdir()):
            if not f.name.endswith(".jsonl"):
                continue
            sid = f.name[:-len(".jsonl")]
            cur.execute(
                "SELECT (SELECT count(*) FROM turns WHERE session_id = %s::uuid), "
                "       (SELECT count(*) FROM ingest_cursors WHERE session_id = %s::uuid)",
                (sid, sid))
            turn_count, cursor_count = cur.fetchone()
            if turn_count == 0 or cursor_count == 0:
                try:
                    size = f.stat().st_size
                except OSError:
                    size = 0
                out.append((sid, proj.name, str(f), size))
    return out


def cmd_reingest(conn, session_id=None, missing_only=False, workers: int = 1):
    """Force re-ingest of session(s) — recovery action for ingest gaps.

    Modes:
      - session_id: clear that one session's cursor and re-ingest it.
        Use when a single session is known-bad.
      - missing_only=True: re-ingest only sessions whose .jsonl exists
        on disk but is NOT represented in the `turns` table (the gap
        case — sessions whose ingest crashed before the cursor was
        ever written).
      - default: clear ALL cursors and re-run bootstrap (full re-scan).
        Use after fixing an ingest bug to backfill historical sessions
        that were partially or never indexed.

    workers > 1: process sessions in parallel using thread workers, each
    with its own PG connection. Useful for --missing-only / default
    paths where 10+ sessions are independent (each session has its
    own row lock and Weaviate UUID space, so concurrency is safe).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    cur = conn.cursor()

    if session_id:
        # Single-session targeted re-ingest.
        cur.execute("DELETE FROM ingest_cursors WHERE session_id = %s", (session_id,))
        conn.commit()
        live_root = Path.home() / ".claude" / "projects"
        for proj in live_root.iterdir() if live_root.is_dir() else []:
            jsonl = proj / f"{session_id}.jsonl"
            if jsonl.exists():
                proj_enc = proj.name
                print(f"[reingest] {proj_enc}/{session_id}  size={jsonl.stat().st_size/1e6:.1f}MB")
                res = ingest_jsonl(str(jsonl), session_id, proj_enc, conn)
                if res:
                    print(f"  -> +{res[0]} turns, +{res[1]} vectors, lines={res[2]}")
                return
        print(f"[reingest] session {session_id} not found in ~/.claude/projects/",
              file=sys.stderr)
        return

    if missing_only:
        sessions = _find_missing_sessions(conn)
        if not sessions:
            print("[reingest] no missing sessions — index is in sync with disk.")
            return

        total_size_mb = sum(s[3] for s in sessions) / 1e6
        print(f"[reingest] {len(sessions)} session(s) missing from index "
              f"({total_size_mb:.1f}MB total). workers={workers}")

        # Clear cursors for all in one shot (cheaper than per-worker DELETEs).
        for sid, _, _, _ in sessions:
            cur.execute("DELETE FROM ingest_cursors WHERE session_id = %s::uuid", (sid,))
        conn.commit()

        n_done = n_turn = 0
        errors: list[tuple[str, str]] = []

        if workers <= 1:
            # Sequential path — keep original simple flow.
            for sid, proj_enc, jsonl, size in sessions:
                print(f"[reingest] missing: {proj_enc}/{sid[:8]}  size={size/1e6:.1f}MB")
                _, res, err = _ingest_one_with_own_conn(jsonl, sid, proj_enc)
                if err:
                    errors.append((sid, err))
                if res:
                    n_done += 1
                    n_turn += res[0]
                    print(f"  -> +{res[0]} turns, +{res[1]} vectors")
        else:
            # Parallel path — N workers, each with own conn.
            with ThreadPoolExecutor(max_workers=workers) as ex:
                fut_to_sid = {
                    ex.submit(_ingest_one_with_own_conn, jsonl, sid, proj_enc): sid
                    for sid, proj_enc, jsonl, _ in sessions
                }
                for fut in as_completed(fut_to_sid):
                    sid, res, err = fut.result()
                    if err:
                        errors.append((sid, err))
                        print(f"[reingest] {sid[:8]} FAILED: {err}", file=sys.stderr)
                    if res:
                        n_done += 1
                        n_turn += res[0]
                        print(f"[reingest] {sid[:8]} done  +{res[0]} turns  +{res[1]} vectors")

        print(f"\n[reingest] missing-only complete: {n_done}/{len(sessions)} session(s), "
              f"{n_turn} turn(s)")
        if errors:
            print(f"[reingest] {len(errors)} session(s) failed:", file=sys.stderr)
            for sid, err in errors:
                print(f"  - {sid[:8]}: {err}", file=sys.stderr)
        return

    # Default: full re-scan. Clear all cursors, then bootstrap from
    # live tree. The bootstrap already mirrors + ingests every jsonl.
    cur.execute("DELETE FROM ingest_cursors")
    conn.commit()
    print("[reingest] cleared all cursors; running full bootstrap")
    cmd_bootstrap_from_live(conn)


def _retry_vectorize_worker(rows: list[tuple], batch_size: int,
                              worker_label: str) -> tuple[int, int]:
    """Process a shard of rows with its own PG connection.

    Each worker is self-contained: opens its own conn, batches into
    Weaviate via `weaviate_batch_upsert`, and on each successful batch
    UPDATEs the matching `turns.vectorized` / `weaviate_id` rows.
    Returns (succeeded, seen) so the caller can aggregate.

    Failure mode: if connect fails or any unrecoverable exception
    leaks, we log and return what we managed. We do NOT raise — the
    parent thread aggregator treats us as a partial-success worker
    so other shards still get to report their numbers.
    """
    try:
        worker_conn = psycopg2.connect(PG_DSN)
    except Exception as e:
        print(f"[retry-vectorize] {worker_label} connect-failed: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return 0, 0

    succeeded = 0
    seen = 0
    items: list[tuple[int, dict]] = []

    def flush() -> None:
        nonlocal succeeded
        if not items:
            return
        ok = weaviate_batch_upsert(items)
        if ok:
            with worker_conn.cursor() as cur2:
                psycopg2.extras.execute_values(
                    cur2,
                    "UPDATE turns AS t SET vectorized=TRUE, "
                    "weaviate_id=v.wid::uuid FROM (VALUES %s) AS v(tid, wid) "
                    "WHERE t.id = v.tid",
                    [(tid, wid) for tid, wid in ok.items()],
                )
            worker_conn.commit()
            succeeded += len(ok)
        items.clear()

    try:
        for tid, sid, idx, slug, role, content, ts, model, ai_title in rows:
            # v0.8.7: gate before queueing for embed. Reject low-signal
            # turns (numeric noise, ack-only "ok"/"yes", boilerplate)
            # without blowing cloud quota. `seen` still counts the row so
            # progress reports stay accurate; just don't add to `items`.
            seen += 1
            if not _gate_ok(content or ""):
                continue
            snippet = (content or "")[:CONTENT_VECTORIZE_MAX]
            items.append((tid, {
                "session_id": sid,
                "turn_id": idx,
                "project_slug": slug or "",
                "role": role,
                "content": snippet,
                "ts": ts.isoformat() if ts else None,
                "model": model or "",
                "ai_title": ai_title or "",
            }))
            if len(items) >= batch_size:
                flush()
        flush()
    except Exception as e:
        print(f"[retry-vectorize] {worker_label} error: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        try:
            worker_conn.rollback()
        except Exception:
            pass
    finally:
        try:
            worker_conn.close()
        except Exception:
            pass
    return succeeded, seen


def cmd_retry_vectorize(conn, *, limit: int | None = None,
                         batch_size: int = 64,
                         workers: int = 1) -> None:
    """Re-vectorize turns stuck with vectorized=FALSE.

    Bypasses the cursor-based ingest dedup and just processes all
    narrative-text turns whose Weaviate insert previously failed
    (transformers down, network blip, killed mid-batch, etc.).
    Honors DEJAVU_EMBED_MODE so cloud mode pumps them through the
    dejavu-cloud Worker rather than the local container.

    Filters mirror the live ingest path: only role in (user,assistant),
    only content_type=text, only content >= CONTENT_VECTORIZE_MIN chars.

    workers > 1: shards rows into `workers` chunks; each thread gets
    its own PG connection and runs the same flush loop in parallel.
    Mirrors the cmd_reingest parallel pattern. Safe because each row's
    UPDATE targets a distinct `turns.id` (no overlap between shards)
    and `weaviate_batch_upsert` is thread-safe (each call gets its own
    HTTP session). Default 1 keeps the single-threaded behavior.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    sql = (
        "SELECT t.id, t.session_id::text, t.idx, s.project_slug, "
        "       t.role, t.content, t.ts, t.model, s.ai_title "
        "FROM turns t JOIN sessions s ON s.id = t.session_id "
        "WHERE NOT t.vectorized "
        "  AND t.role IN ('user','assistant') "
        "  AND t.content_type = 'text' "
        "  AND length(t.content) >= %s "
        "ORDER BY t.id"
    )
    params: list = [CONTENT_VECTORIZE_MIN]
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    workers = max(1, workers)
    print(f"[retry-vectorize] {len(rows)} eligible turn(s); "
          f"mode={_DEJAVU_EMBED_MODE}, batch_size={batch_size}, "
          f"workers={workers}")
    if not rows:
        return

    started = time.time()

    if workers == 1:
        # Sequential path — uses the parent connection directly so
        # behavior is bit-for-bit identical to the pre-v0.8.4
        # single-threaded codepath (one PG conn, in-loop progress
        # ticker every 200 rows).
        succeeded_total = 0
        seen_total = 0
        last_progress = 0
        items: list[tuple[int, dict]] = []

        def flush() -> None:
            nonlocal succeeded_total
            if not items:
                return
            ok = weaviate_batch_upsert(items)
            if ok:
                with conn.cursor() as cur2:
                    psycopg2.extras.execute_values(
                        cur2,
                        "UPDATE turns AS t SET vectorized=TRUE, "
                        "weaviate_id=v.wid::uuid FROM (VALUES %s) AS v(tid, wid) "
                        "WHERE t.id = v.tid",
                        [(tid, wid) for tid, wid in ok.items()],
                    )
                conn.commit()
                succeeded_total += len(ok)
            items.clear()

        try:
            for tid, sid, idx, slug, role, content, ts, model, ai_title in rows:
                # v0.8.7: gate first; skip embed for trash content.
                seen_total += 1
                if not _gate_ok(content or ""):
                    if seen_total - last_progress >= 200:
                        elapsed = max(0.001, time.time() - started)
                        rate = succeeded_total / elapsed
                        print(f"[retry-vectorize] {succeeded_total}/{seen_total} ok "
                              f"({rate:.1f}/s, eta "
                              f"{(len(rows) - seen_total) / max(rate, 0.1):.0f}s)")
                        last_progress = seen_total
                    continue
                snippet = (content or "")[:CONTENT_VECTORIZE_MAX]
                items.append((tid, {
                    "session_id": sid,
                    "turn_id": idx,
                    "project_slug": slug or "",
                    "role": role,
                    "content": snippet,
                    "ts": ts.isoformat() if ts else None,
                    "model": model or "",
                    "ai_title": ai_title or "",
                }))
                if len(items) >= batch_size:
                    flush()
                if seen_total - last_progress >= 200:
                    elapsed = max(0.001, time.time() - started)
                    rate = succeeded_total / elapsed
                    print(f"[retry-vectorize] {succeeded_total}/{seen_total} ok "
                          f"({rate:.1f}/s, eta "
                          f"{(len(rows) - seen_total) / max(rate, 0.1):.0f}s)")
                    last_progress = seen_total
            flush()
        finally:
            elapsed = time.time() - started
            gate_rej = _GATE_COUNTERS.get("rejected", 0)
            gate_tail = f"  gate_skipped={gate_rej}" if gate_rej else ""
            print(f"[retry-vectorize] done in {elapsed:.1f}s — "
                  f"{succeeded_total}/{seen_total} succeeded "
                  f"(failed: {seen_total - succeeded_total - gate_rej})"
                  f"{gate_tail}")
        return

    # Parallel path — shard rows N ways. Each worker gets a contiguous
    # slice (preserving the per-shard `ORDER BY t.id` ordering inside
    # the slice) and its own PG connection. Shards are non-overlapping
    # by row index → no double-write race.
    n = len(rows)
    shard_size = (n + workers - 1) // workers  # ceil division
    shards = [rows[i:i + shard_size] for i in range(0, n, shard_size)]
    succeeded_total = 0
    seen_total = 0
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(_retry_vectorize_worker, shard, batch_size,
                            f"w{idx}"): idx
                for idx, shard in enumerate(shards)
            }
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    s_ok, s_seen = fut.result()
                except Exception as e:
                    print(f"[retry-vectorize] w{idx} crashed: "
                          f"{type(e).__name__}: {e}", file=sys.stderr)
                    continue
                succeeded_total += s_ok
                seen_total += s_seen
                print(f"[retry-vectorize] w{idx} done: "
                      f"{s_ok}/{s_seen} succeeded")
    finally:
        elapsed = time.time() - started
        gate_rej = _GATE_COUNTERS.get("rejected", 0)
        gate_tail = f"  gate_skipped={gate_rej}" if gate_rej else ""
        print(f"[retry-vectorize] done in {elapsed:.1f}s — "
              f"{succeeded_total}/{seen_total} succeeded "
              f"(failed: {seen_total - succeeded_total - gate_rej})"
              f"{gate_tail}")


def cmd_backfill_gists(conn, *, limit: int | None = None,
                        workers: int = 4,
                        dry_run: bool = False,
                        force: bool = False) -> None:
    """Backfill `turns.gist` + `turns.gist_method` via distill_pipeline.

    v0.8.6 (Context Economy phase 3). Walks every turn where
    `gist IS NULL AND content IS NOT NULL` (or every distillable turn
    when --force) and runs the load-bearing-anchor pipeline. Uncompressible
    rows (code blocks, tracebacks, diffs, JSON) stay NULL — that's a
    contract, not an error.

    Sharding: rows are id-ordered then split into `workers` non-overlapping
    chunks. Each thread gets its own PG connection. UPDATE targets distinct
    `turns.id` per chunk so no write contention.

    Idempotent by default: re-runs skip rows that already have a gist
    unless `force=True` (then the row is re-distilled — useful after
    bumping pipeline version).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Import the live pipeline (module-level make_gist may have been
    # wrapped — go straight to the source so we can also surface
    # bridge_version for the progress banner).
    try:
        import distill_pipeline as _dp
    except Exception as exc:  # noqa: BLE001
        print(f"[backfill-gists] distill_pipeline unavailable: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return

    sql = (
        "SELECT id, content, role, content_type "
        "FROM turns "
        "WHERE content IS NOT NULL "
        "  AND length(content) >= 30 "
        "  AND content_type = 'text' "
        "  AND role IN ('user', 'assistant') "
    )
    if not force:
        sql += "AND gist IS NULL "
    sql += "ORDER BY id"
    params: list = []
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    workers = max(1, workers)
    n = len(rows)
    bridge_tag = ("stub" if _dp.BRIDGE_IS_STUB
                  else f"wheel/{_dp.BRIDGE_VERSION}")
    print(f"[backfill-gists] {n} eligible turn(s); "
          f"bridge={bridge_tag}, workers={workers}, "
          f"dry_run={dry_run}, force={force}")
    if not rows:
        return

    started = time.time()

    def _process_shard(shard: list[tuple]) -> tuple[int, int, int]:
        """Returns (distilled, skipped_uncompressible, errors)."""
        if dry_run:
            local_conn = None
        else:
            local_conn = psycopg2.connect(PG_DSN)
        try:
            d = s = e = 0
            for tid, content, role, content_type in shard:
                try:
                    gist_val, gist_method = _dp.make_gist(
                        content, role=role, content_type=content_type)
                except Exception:  # noqa: BLE001
                    e += 1
                    continue
                if gist_val is None:
                    s += 1
                    continue
                gist_val = _strip_nul(gist_val)
                if not dry_run and local_conn is not None:
                    with local_conn.cursor() as cur2:
                        cur2.execute(
                            "UPDATE turns SET gist=%s, gist_method=%s "
                            "WHERE id=%s",
                            (gist_val, gist_method, tid))
                    local_conn.commit()
                d += 1
            return d, s, e
        finally:
            if local_conn is not None:
                try:
                    local_conn.close()
                except Exception:  # noqa: BLE001
                    pass

    if workers == 1:
        d, s, e = _process_shard(list(rows))
    else:
        shard_size = (n + workers - 1) // workers
        shards = [rows[i:i + shard_size] for i in range(0, n, shard_size)]
        d = s = e = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_process_shard, sh): i
                       for i, sh in enumerate(shards)}
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    sd, ss, se = fut.result()
                except Exception as exc:  # noqa: BLE001
                    print(f"[backfill-gists] w{idx} crashed: "
                          f"{type(exc).__name__}: {exc}", file=sys.stderr)
                    continue
                d += sd; s += ss; e += se
                print(f"[backfill-gists] w{idx} done: "
                      f"distilled={sd}, skipped={ss}, errors={se}")
    elapsed = time.time() - started
    print(f"[backfill-gists] done in {elapsed:.1f}s — "
          f"distilled={d}, skipped_uncompressible={s}, errors={e}, "
          f"total={n}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--backfill", action="store_true",
                   help="re-ingest from chat-logs/ mirror (post-Stop-hook directory)")
    p.add_argument("--bootstrap", action="store_true",
                   help="one-shot: scan ~/.claude/projects/ for live transcripts, "
                        "mirror + ingest each one. Used by install.py to import "
                        "pre-existing sessions on first install.")
    p.add_argument("--stop-hook", action="store_true")
    p.add_argument("--reingest", action="store_true",
                   help="Force re-scan of sessions. Default: clear ALL cursors "
                        "and re-bootstrap from live tree (recovery after ingest "
                        "bug). With --session: re-ingest one session. With "
                        "--missing-only: re-ingest only sessions whose .jsonl "
                        "exists but is not represented in the index (gap "
                        "recovery without disturbing complete sessions).")
    p.add_argument("--missing-only", action="store_true",
                   help="Used with --reingest: only re-ingest sessions whose "
                        ".jsonl is on disk but absent from the index.")
    p.add_argument("--workers", type=int, default=1,
                   help="Number of parallel workers (each gets its own PG "
                        "connection). Useful with --reingest --missing-only "
                        "for fast recovery across many sessions, OR with "
                        "--retry-vectorize to shard the backlog across "
                        "threads. Default 1 (single-threaded — bit-for-bit "
                        "identical to pre-v0.8.4 behavior).")
    p.add_argument("--session")
    p.add_argument("--jsonl")
    p.add_argument("--project")
    p.add_argument("--retry-vectorize", action="store_true",
                   help="re-process turns where vectorized=FALSE — uses "
                        "DEJAVU_EMBED_MODE to decide local vs cloud")
    p.add_argument("--limit", type=int, default=None,
                   help="cap items processed (with --retry-vectorize / "
                        "--backfill-gists)")
    # v0.8.6 Context Economy phase 3 — distill_pipeline backfill
    p.add_argument("--backfill-gists", action="store_true",
                   help="Walk turns with gist IS NULL and populate "
                        "turns.gist + turns.gist_method via the "
                        "distill_pipeline (load-bearing-anchor "
                        "preservation + skip-uncompressible).")
    p.add_argument("--dry-run", action="store_true",
                   help="(with --backfill-gists) compute gists but "
                        "do not write to the DB. Useful to preview "
                        "skip-vs-distill ratios on a sample.")
    p.add_argument("--force", action="store_true",
                   help="(with --backfill-gists) re-distill rows "
                        "that already have a gist. Default: skip "
                        "(idempotent).")
    args = p.parse_args()

    # Top-level try/except — if anything below leaks an uncaught
    # exception, log it to error_log so doctor can surface it. Without
    # this, stop-hook ingest crashes go to ingest.log only and never
    # reach the user. Best-effort — never block return on logging.
    conn = None
    try:
        conn = psycopg2.connect(PG_DSN)
        if args.retry_vectorize:
            cmd_retry_vectorize(conn, limit=args.limit,
                                 workers=max(1, args.workers))
        elif args.backfill_gists:
            # v0.8.6 — distill_pipeline backfill. Default workers=4
            # per spec; --workers overrides if explicitly set above.
            bg_workers = args.workers if args.workers != 1 else 4
            cmd_backfill_gists(conn,
                                limit=args.limit,
                                workers=max(1, bg_workers),
                                dry_run=args.dry_run,
                                force=args.force)
        elif args.reingest:
            cmd_reingest(conn,
                         session_id=args.session,
                         missing_only=args.missing_only,
                         workers=max(1, args.workers))
        elif args.backfill:
            cmd_backfill(conn)
        elif args.bootstrap:
            cmd_bootstrap_from_live(conn)
        elif args.stop_hook:
            cmd_stop_hook(conn)
        elif args.session and args.jsonl:
            res = ingest_jsonl(args.jsonl, args.session, args.project, conn)
            print(f"Ingested: {res}")
        else:
            p.print_help()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\n[ingester] interrupted by user", file=sys.stderr)
        raise
    except Exception as e:
        # Surface to error_log so dejavu doctor's error_log check
        # picks it up. Stop hook's silent-failure root cause closed.
        try:
            from error_log import log_error
            mode = (
                "reingest" if args.reingest else
                "backfill" if args.backfill else
                "bootstrap" if args.bootstrap else
                "stop-hook" if args.stop_hook else "single-session"
            )
            log_error(
                f"ingester.{mode}",
                f"top-level uncaught: {type(e).__name__}: {e}",
                exc_info=True,
                context={"session": args.session, "jsonl": args.jsonl},
            )
        except Exception:
            pass
        raise
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
