#!/usr/bin/env python3
"""
claude-dejavu MCP server — exposes hybrid recall to Claude in-session.

Two-pass design (token discipline):
  - dejavu_search   : cheap. Returns gists + IDs. ~80 tok/hit.
  - dejavu_expand   : on-demand full content for specific turn IDs.
  - dejavu_resume   : best matching session + ready-to-run resume command.
  - dejavu_sessions : list recent sessions in current scope.
  - dejavu_errors   : tool failures with truncated context.
  - dejavu_files    : sessions that touched files matching a path substring.
  - dejavu_workspaces : list defined workspaces.

All scope-aware. Default scope = workspace (if cwd ∈ one) else current project.
Override per call with `scope='global' | 'project:name' | 'ws:name'`.
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

import psycopg2
import psycopg2.extras

# Local module path (sibling /code dir)
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "code"))
from scope import resolve_scope, apply_pg_filter, weaviate_where, ScopeFilter  # noqa: E402

from mcp.server.fastmcp import FastMCP

# ─── Config ──────────────────────────────────────────────────────────────────
def _resolve_data_root():
    override = os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "claude-dejavu"
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home() / "AppData/Local")
        return Path(base) / "claude-dejavu"
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg) / "claude-dejavu" if xdg else Path.home() / ".local" / "share" / "claude-dejavu"

DATA_ROOT = _resolve_data_root()
CONFIG = {}
config_file = DATA_ROOT / "config.env"
if config_file.exists():
    for line in config_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"): continue
        if "=" in line:
            k, v = line.split("=", 1)
            CONFIG[k.strip()] = v.strip()

OS_USER = os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"
PG_DB = CONFIG.get("PG_DB") or f"claude_dejavu_{OS_USER}"
PG_DSN = (
    f"host={CONFIG.get('PG_HOST','localhost')} "
    f"port={CONFIG.get('PG_PORT','5432')} "
    f"user={CONFIG.get('PG_USER','postgres')} "
    f"password={CONFIG.get('PG_PASS','postgres')} "
    f"dbname={PG_DB}"
)
WV_URL = CONFIG.get("WV_URL", "http://localhost:8080")
WV_CLASS = os.environ.get("CLAUDE_DEJAVU_WV_CLASS") or CONFIG.get("WV_CLASS") or "ClaudeDejavuTurn"
TENANT = CONFIG.get("WV_TENANT") or OS_USER

# Claude Code injects this in hook env for the running project.
CWD_FOR_SCOPE = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
# Pre-compute the slug once at startup — saves a realpath() syscall per call.
_CWD_SLUG = os.path.basename(os.path.realpath(CWD_FOR_SCOPE))

mcp = FastMCP("claude-dejavu")


_PG_CONN = None
def _conn():
    """Return a persistent psycopg2 connection, lazily created.
    Reuses across MCP tool calls — saves ~30ms per call vs reconnecting.
    Auto-reopens on death."""
    global _PG_CONN
    if _PG_CONN is None or _PG_CONN.closed != 0:
        _PG_CONN = psycopg2.connect(PG_DSN)
        _PG_CONN.autocommit = True  # avoid implicit transaction overhead per call
    return _PG_CONN


def _scope(scope_arg: str | None) -> ScopeFilter:
    try:
        with _conn() as c:
            return resolve_scope(scope_arg, cwd=CWD_FOR_SCOPE, pg_conn=c)
    except Exception:
        return resolve_scope(scope_arg, cwd=CWD_FOR_SCOPE, pg_conn=None)


def _lexical_pg(query: str, limit: int, scope: ScopeFilter) -> list[dict]:
    """Fast PG-only lexical retrieval via FTS5-style index.

    Strategy:
      1. plainto_tsquery (AND-of-stems) — selective, leverages the FTS index
         for sub-5ms candidate filtering.
      2. ORDER BY recency (id DESC) — for memory recall, recent matches are
         usually the right answer; ts_rank() would require heap reads (slow).
      3. Read full content + gist only for the small final set (LIMIT).

    For multi-word natural-language queries that AND won't match, the auto
    mode falls through to Weaviate hybrid (semantic).
    """
    sql = """
        SELECT t.id, t.session_id::text, s.project_slug, t.role, t.ts,
               t.content, t.gist, 1.0 AS score
        FROM turns t
        JOIN sessions s ON s.id = t.session_id
        WHERE t.role IN ('user','assistant')
          AND t.content_type = 'text'
          AND to_tsvector('english', coalesce(t.content,''))
              @@ plainto_tsquery('english', %(q)s)
    """
    params = {"q": query}
    if scope.projects is not None:
        sql += " AND s.project_slug = ANY(%(projects)s)"
        params["projects"] = scope.projects
    if scope.excludes:
        sql += " AND s.project_slug <> ALL(%(excludes)s)"
        params["excludes"] = scope.excludes
    sql += " ORDER BY t.id DESC LIMIT %(limit)s"
    params["limit"] = limit

    out = []
    with _conn() as c:
        cur = c.cursor()
        cur.execute(sql, params)
        for row in cur.fetchall():
            tid, sid, proj, role, ts, content, gist, score = row
            out.append({
                "session_id": sid, "turn_id": tid, "project_slug": proj,
                "role": role, "ts": ts.isoformat() if ts else None,
                "content": content, "gist": gist,
                "_additional": {"score": float(score), "source": "pg-fts"},
            })
    return out


def _hybrid_search(query: str, limit: int, scope: ScopeFilter) -> list[dict]:
    where = weaviate_where(scope)
    gql = f'''{{
      Get {{
        {WV_CLASS}(
          hybrid: {{ query: {json.dumps(query)}, alpha: 0.5 }}
          limit: {limit}
          {where}
        ) {{
          session_id turn_id project_slug role ts ai_title
          _additional {{ score }}
        }}
      }}
    }}'''
    req = urllib.request.Request(
        f"{WV_URL}/v1/graphql",
        data=json.dumps({"query": gql}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
        return d.get("data", {}).get("Get", {}).get(WV_CLASS) or []
    except Exception as e:
        return [{"_error": f"{type(e).__name__}: {e}"}]


# ─── Tool: search (cheap, gist-only) ─────────────────────────────────────────

@mcp.tool()
def dejavu_search(query: str, scope: str | None = None, limit: int = 10,
                  mode: str = "auto") -> str:
    """
    Tiered search across past turns:
      mode='lexical'  → PG FTS+trigram only (sub-20ms, exact-string-friendly)
      mode='hybrid'   → Weaviate BM25+vector only (semantic, 60-300ms)
      mode='auto'     → lexical first, fall through to hybrid if <limit/2 hits (default)

    Returns a CHEAP list of {turn_id, score, gist} for each hit. Use this
    first; then call dejavu_expand(turn_ids) for full content of specific turns.

    Args:
        query: free-text query
        scope: 'global' | 'project[:name]' | 'workspace[:name]' (default: auto)
        limit: max results (default 10)
        mode: 'auto' | 'lexical' | 'hybrid'
    """
    sc = _scope(scope)
    source_tag = ""
    if mode == "lexical":
        hits = _lexical_pg(query, limit, sc)
        source_tag = "pg-lexical"
    elif mode == "hybrid":
        hits = _hybrid_search(query, limit, sc)
        source_tag = "weaviate-hybrid"
    else:
        # auto: try fast path first, augment if too few results
        lex_hits = _lexical_pg(query, limit, sc)
        if len(lex_hits) >= max(3, limit // 2):
            hits, source_tag = lex_hits, "pg-lexical"
        else:
            wv_hits = _hybrid_search(query, limit - len(lex_hits), sc)
            seen = {h.get("turn_id") for h in lex_hits}
            merged = lex_hits + [h for h in wv_hits if h.get("turn_id") not in seen]
            hits = merged[:limit]
            source_tag = "pg+weaviate" if lex_hits else "weaviate-hybrid"
    if hits and "_error" in hits[0]:
        return f"Search failed: {hits[0]['_error']} (scope: {sc.explain()})"
    if not hits:
        return f"No matches in scope: {sc.explain()} [via {source_tag}]"

    # PG-lexical hits already carry their gist + content inline (zero second roundtrip).
    # Weaviate hits don't carry the gist — fetch only what's missing.
    needs_gist = [int(h["turn_id"]) for h in hits if h.get("turn_id") and not h.get("gist")]
    gist_map = {}
    if needs_gist:
        with _conn() as c:
            cur = c.cursor()
            cur.execute("SELECT id, gist, content FROM turns WHERE id = ANY(%s)", (needs_gist,))
            for tid, gist, content in cur.fetchall():
                gist_map[tid] = gist or (content or "")[:120]

    lines = [f"scope={sc.explain()}  hits={len(hits)}  via={source_tag}"]
    for i, h in enumerate(hits, 1):
        tid = int(h.get("turn_id") or 0)
        score = float(h.get("_additional", {}).get("score") or 0)
        proj = h.get("project_slug") or "?"
        sess = (h.get("session_id") or "")[:8]
        ts = (h.get("ts") or "")[:10]
        gist = h.get("gist") or gist_map.get(tid) or (h.get("content") or h.get("ai_title") or "(no gist)")[:120]
        lines.append(f"[{i}] turn={tid} score={score:.2f} {proj}/{sess} {ts}  {gist[:120]}")
    lines.append(f"\nCall dejavu_expand([turn_ids…]) to read full content for specific turns.")
    return "\n".join(lines)


# ─── Tool: expand (full content for specific IDs) ────────────────────────────

@mcp.tool()
def dejavu_expand(turn_ids: list[int]) -> str:
    """
    Return full content for the given turn IDs. Use after dejavu_search to
    pull only the turns you actually need. Costs ~500-1500 tokens per turn.

    Args:
        turn_ids: list of integer turn ids returned by dejavu_search
    """
    if not turn_ids:
        return "No turn_ids provided."
    with _conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT t.id, t.session_id, s.project_slug, s.ai_title,
                   t.role, t.content_type, t.ts, t.content
            FROM turns t JOIN sessions s ON s.id = t.session_id
            WHERE t.id = ANY(%s)
            ORDER BY t.ts NULLS LAST
        """, (turn_ids,))
        rows = cur.fetchall()
    if not rows:
        return f"No turns found for ids: {turn_ids}"
    out = []
    for r in rows:
        sess = str(r['session_id'])[:8]
        ts = str(r.get('ts') or '')[:19]
        out.append(f"\n── turn {r['id']} {r['project_slug']}/{sess} {r['role']}/{r['content_type']} {ts}")
        if r.get('ai_title'):
            out.append(f"   session-title: {r['ai_title']}")
        out.append(r['content'] or "(empty)")
    return "\n".join(out)


# ─── Tool: resume (find session + return resume command) ─────────────────────

@mcp.tool()
def dejavu_resume(hint: str, scope: str | None = None) -> str:
    """Find best-matching past session for a description; returns runnable
    `cd <dir> && claude --resume <uuid>` command.
    """
    sc = _scope(scope)
    hits = _hybrid_search(hint, 20, sc)
    if not hits or (hits and "_error" in hits[0]):
        return f"No matches for '{hint}' in {sc.explain()}"
    by_session: dict[str, dict] = {}
    for h in hits:
        sid = h.get("session_id")
        score = float(h.get("_additional", {}).get("score") or 0)
        cur = by_session.get(sid)
        if not cur or score > cur["score"]:
            by_session[sid] = {"score": score, "hit": h}
    ranked = sorted(by_session.values(), key=lambda x: -x["score"])[:5]

    out = [f"Top matches in scope={sc.explain()}:"]
    home = os.path.expanduser("~")
    with _conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        for i, r in enumerate(ranked, 1):
            sid = r["hit"]["session_id"]
            cur.execute("SELECT project_slug, ai_title, total_turns, started_at FROM sessions WHERE id=%s::uuid", (sid,))
            meta = cur.fetchone() or {}
            slug = meta.get("project_slug") or "?"
            cwd = None
            for base in (f"{home}/Documents/projects", f"{home}/projects", home):
                if os.path.isdir(os.path.join(base, slug)):
                    cwd = os.path.join(base, slug); break
            cwd = cwd or "<project-dir>"
            title = (meta.get("ai_title") or "(no title)")[:80]
            out.append(f"  [{i}] {sid}  score={r['score']:.2f}  {slug}  turns={meta.get('total_turns','?')}  {str(meta.get('started_at',''))[:10]}")
            out.append(f"      {title}")
            out.append(f"      $ cd {cwd} && claude --resume {sid}")
    return "\n".join(out)


# ─── Tool: sessions ──────────────────────────────────────────────────────────

@mcp.tool()
def dejavu_sessions(scope: str | None = None, limit: int = 20) -> str:
    """List recent sessions in the resolved scope."""
    sc = _scope(scope)
    sql = "SELECT id, project_slug, ai_title, total_turns, started_at FROM sessions s"
    params = []
    sql, params = apply_pg_filter(sql, params, sc, table_alias="s")
    sql += " ORDER BY started_at DESC NULLS LAST LIMIT %s"
    params.append(limit)
    with _conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        rows = cur.fetchall()
    if not rows:
        return f"No sessions in scope: {sc.explain()}"
    out = [f"scope={sc.explain()}"]
    for r in rows:
        out.append(
            f"  {str(r['id'])[:8]} {(r['project_slug'] or '?'):20s} "
            f"turns={r['total_turns'] or 0:>5} "
            f"{str(r['started_at'] or '')[:10]} "
            f"{(r['ai_title'] or '(no title)')[:60]}"
        )
    return "\n".join(out)


# ─── Tool: errors ────────────────────────────────────────────────────────────

@mcp.tool()
def dejavu_errors(tool: str | None = None, scope: str | None = None, limit: int = 20) -> str:
    """Recent tool failures with truncated input + output."""
    sc = _scope(scope)
    sql = ("SELECT tc.id AS tool_call_id, s.project_slug, tc.tool_name, tc.tool_input, "
           "       left(tc.tool_output, 600) AS error_preview, tc.ts "
           "FROM tool_calls tc JOIN sessions s ON s.id = tc.session_id WHERE tc.is_error")
    params = []
    if tool:
        sql += " AND tc.tool_name = %s"
        params.append(tool)
    sql, params = apply_pg_filter(sql, params, sc, table_alias="s")
    sql += " ORDER BY tc.ts DESC NULLS LAST LIMIT %s"
    params.append(limit)
    with _conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        rows = cur.fetchall()
    if not rows:
        return f"No errors in scope: {sc.explain()}"
    out = [f"scope={sc.explain()}  errors={len(rows)}"]
    for r in rows:
        out.append(f"\n[{str(r['ts'])[:19]}] {r['project_slug']} {r['tool_name']}")
        out.append(f"  input: {json.dumps(r['tool_input'] or {})[:160]}")
        out.append(f"  err:   {(r['error_preview'] or '')[:200]}")
    return "\n".join(out)


# ─── Tool: files ─────────────────────────────────────────────────────────────

@mcp.tool()
def dejavu_files(path: str, scope: str | None = None, limit: int = 30) -> str:
    """Find sessions that touched files matching a path substring."""
    sc = _scope(scope)
    sql = ("SELECT s.project_slug, s.id AS session_id, ft.file_path, ft.op, ft.ts "
           "FROM file_touches ft JOIN sessions s ON s.id = ft.session_id "
           "WHERE ft.file_path ILIKE %s")
    params = [f"%{path}%"]
    sql, params = apply_pg_filter(sql, params, sc, table_alias="s")
    sql += " ORDER BY ft.ts DESC NULLS LAST LIMIT %s"
    params.append(limit)
    with _conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        rows = cur.fetchall()
    if not rows:
        return f"No file touches matching '{path}' in scope: {sc.explain()}"
    out = [f"scope={sc.explain()}"]
    for r in rows:
        out.append(f"  {str(r['ts'] or '')[:19]} {r['project_slug']}/{str(r['session_id'])[:8]} {r['op']:10s} {r['file_path']}")
    return "\n".join(out)


# ─── Tool: workspaces ────────────────────────────────────────────────────────

@mcp.tool()
def dejavu_restore(session_uuids: list[str] | None = None,
                   list_only: bool = False,
                   restore_all: bool = False) -> str:
    """
    Recover Claude Code sessions that were deleted from ~/.claude/projects/
    but still exist in the off-tree backup mirror.

    THIS IS THE KEY VALUE PROPOSITION of claude-dejavu: when Claude Code loses
    a session (and it does, sometimes), we have it.

    Args:
        session_uuids: list of session UUIDs to restore. Empty list + list_only=False
                       + restore_all=False is interpreted as a list-only request.
        list_only: just show what's restorable; don't restore.
        restore_all: restore all missing/truncated sessions.

    The user can then run `claude --resume <uuid>` from the restored session's
    project directory.
    """
    import shutil, hashlib, datetime as dt
    backup_root = DATA_ROOT / "chat-logs"
    live_root = Path.home() / ".claude" / "projects"
    if not backup_root.is_dir():
        return f"Backup root not found: {backup_root}. Has the Stop hook been running?"

    candidates = []
    for proj_dir in backup_root.iterdir():
        if not proj_dir.is_dir() or proj_dir.name.startswith("_"):
            continue
        for jsonl in proj_dir.glob("*.jsonl"):
            uuid = jsonl.stem
            live_path = live_root / proj_dir.name / f"{uuid}.jsonl"
            backup_size = jsonl.stat().st_size
            backup_mtime = jsonl.stat().st_mtime
            if not live_path.exists():
                candidates.append({"proj": proj_dir.name, "uuid": uuid, "src": jsonl,
                                   "dst": live_path, "size": backup_size,
                                   "mtime": backup_mtime, "kind": "deleted"})
            else:
                live_size = live_path.stat().st_size
                if live_size < backup_size * 0.95:
                    candidates.append({"proj": proj_dir.name, "uuid": uuid, "src": jsonl,
                                       "dst": live_path, "size": backup_size,
                                       "live_size": live_size, "mtime": backup_mtime,
                                       "kind": "truncated"})

    if list_only or (not session_uuids and not restore_all):
        if not candidates:
            return "Nothing to restore — all backup sessions present in live."
        out = [f"Restorable sessions ({len(candidates)}):"]
        for c in candidates:
            mtime_str = dt.datetime.fromtimestamp(c["mtime"]).isoformat(sep=" ", timespec="minutes")
            extra = f" (live={c.get('live_size','?')})" if c["kind"] == "truncated" else ""
            sz_mb = c['size'] / 1e6
            out.append(f"  {c['kind']:10s} {c['uuid']}  {c['proj'][:40]}  {mtime_str}  {sz_mb:.1f}MB{extra}")
        out.append("\nCall dejavu_restore(session_uuids=[...]) or dejavu_restore(restore_all=True) to recover.")
        return "\n".join(out)

    targets = candidates if restore_all else [c for c in candidates if c["uuid"] in (session_uuids or [])]
    missing = set(session_uuids or []) - {c["uuid"] for c in candidates}
    if not targets:
        msg = "No sessions to restore."
        if missing:
            msg += f" Not found in backup: {', '.join(missing)}"
        return msg

    restored = []
    failures = []
    for c in targets:
        try:
            c["dst"].parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(c["src"], c["dst"])
            h_src = hashlib.sha256(c["src"].read_bytes()).hexdigest()
            h_dst = hashlib.sha256(c["dst"].read_bytes()).hexdigest()
            if h_src != h_dst:
                c["dst"].unlink(missing_ok=True)
                failures.append(f"{c['uuid']} (checksum mismatch)")
                continue
            slug = c["proj"].lstrip("-").rsplit("projects-", 1)[-1] or c["proj"]
            home = str(Path.home())
            cwd = next((f"{base}/{slug}" for base in (f"{home}/Documents/projects", f"{home}/projects", home)
                        if Path(f"{base}/{slug}").is_dir()), "<project-dir>")
            restored.append({"uuid": c["uuid"], "size_mb": c["size"] / 1e6, "resume": f"cd {cwd} && claude --resume {c['uuid']}"})
        except Exception as e:
            failures.append(f"{c['uuid']} ({e})")

    out = [f"Restored {len(restored)}/{len(targets)} session(s)."]
    for r in restored:
        out.append(f"  ✅ {r['uuid']}  ({r['size_mb']:.1f} MB)")
        out.append(f"     resume: {r['resume']}")
    if missing:
        out.append(f"\nNot found in backup: {', '.join(missing)}")
    if failures:
        out.append(f"\nFailures: {', '.join(failures)}")
    return "\n".join(out)


# Same caching pattern as observations — sub-ms summary lookups.
_SUM_CACHE: list[dict] = []
_SUM_CACHE_LOADED_AT: float = 0
_SUM_CACHE_TABLE_SIZE: int = -1


def _refresh_sum_cache_if_stale():
    global _SUM_CACHE, _SUM_CACHE_LOADED_AT, _SUM_CACHE_TABLE_SIZE
    import time as _t
    now = _t.time()
    if _SUM_CACHE and (now - _SUM_CACHE_LOADED_AT) < _OBS_CACHE_TTL_S:
        return
    cur = _conn().cursor()
    cur.execute("SELECT count(*) FROM session_summaries")
    n = cur.fetchone()[0]
    if _SUM_CACHE and n == _SUM_CACHE_TABLE_SIZE:
        _SUM_CACHE_LOADED_AT = now
        return
    cur.execute("""
        SELECT s.project_slug, ss.session_id::text, ss.generated_at,
               ss.request, ss.investigated, ss.learned, ss.completed,
               ss.next_steps, ss.model,
               EXTRACT(EPOCH FROM ss.generated_at) AS ts_epoch
        FROM session_summaries ss JOIN sessions s ON s.id = ss.session_id
    """)
    _SUM_CACHE = []
    for r in cur.fetchall():
        _SUM_CACHE.append({
            "project": r[0], "session_id": r[1], "generated_at": r[2],
            "request": r[3] or "", "investigated": r[4] or "",
            "learned": r[5] or "", "completed": r[6] or "",
            "next_steps": r[7] or "", "model": r[8] or "?",
            "ts": r[9] or 0,
        })
    _SUM_CACHE_LOADED_AT = now
    _SUM_CACHE_TABLE_SIZE = n


@mcp.tool()
def dejavu_summary(session_id: str | None = None, project: str | None = None, limit: int = 1) -> str:
    """
    Return LLM-distilled summaries (request / investigated / learned / completed
    / next_steps) for past sessions. Use this when the user asks "what did we
    do yesterday?" or to ground a fresh conversation in prior progress.

    Backed by an in-process cache (30s TTL) for sub-ms lookups.
    """
    _refresh_sum_cache_if_stale()
    slug = project or _CWD_SLUG

    matches = []
    for s in _SUM_CACHE:
        if session_id and s["session_id"] != session_id: continue
        if not session_id and s["project"] != slug: continue
        matches.append(s)
    matches.sort(key=lambda s: s["ts"], reverse=True)
    matches = matches[:limit]

    if not matches:
        return f"No summaries found for project={project or 'current'}."
    out = []
    for r in matches:
        out.append(f"\n── {r['project']}/{r['session_id'][:8]}  {str(r['generated_at'])[:19]}  [{r['model']}]")
        for fld in ("request", "investigated", "learned", "completed", "next_steps"):
            v = r.get(fld) or ""
            if v.strip():
                out.append(f"  {fld:13s}: {v[:600]}")
    return "\n".join(out)


# In-process cache of observations. Refreshed every CACHE_TTL_S, or when
# table size changes (cheap COUNT query). Sub-ms substring lookups.
_OBS_CACHE: list[dict] = []
_OBS_CACHE_LOADED_AT: float = 0
_OBS_CACHE_TABLE_SIZE: int = -1
_OBS_CACHE_TTL_S = 30.0


def _refresh_obs_cache_if_stale():
    """Load all observations into memory if cache is empty or stale.
    For 10k+ observations this stays under 50ms refresh cost, amortized
    over hundreds of queries between refreshes."""
    global _OBS_CACHE, _OBS_CACHE_LOADED_AT, _OBS_CACHE_TABLE_SIZE
    import time as _t
    now = _t.time()
    if _OBS_CACHE and (now - _OBS_CACHE_LOADED_AT) < _OBS_CACHE_TTL_S:
        return  # cache fresh
    cur = _conn().cursor()
    # Quick COUNT to detect insertions; if size unchanged AND cache exists, refresh ts only
    cur.execute("SELECT count(*) FROM observations")
    n = cur.fetchone()[0]
    if _OBS_CACHE and n == _OBS_CACHE_TABLE_SIZE:
        _OBS_CACHE_LOADED_AT = now
        return
    cur.execute("""
        SELECT o.id, s.project_slug, o.type::text, o.title, o.subtitle,
               EXTRACT(EPOCH FROM o.ts) AS ts_epoch
        FROM observations o JOIN sessions s ON s.id = o.session_id
    """)
    _OBS_CACHE = [
        {"id": r[0], "project": r[1], "type": r[2], "title": r[3] or "",
         "subtitle": r[4] or "", "ts": r[5] or 0,
         "title_lower": (r[3] or "").lower(),
         "subtitle_lower": (r[4] or "").lower()}
        for r in cur.fetchall()
    ]
    _OBS_CACHE_LOADED_AT = now
    _OBS_CACHE_TABLE_SIZE = n


@mcp.tool()
def dejavu_observations(query: str | None = None, type: str | None = None,
                        project: str | None = None, limit: int = 10) -> str:
    """
    List typed observations (bugfix / decision / discovery / feature / change /
    refactor / question) extracted from past sessions. Optionally filter by
    free-text query, type, or project.

    Backed by an in-process cache (30s TTL) for sub-ms substring lookups.

    Args:
        query: free-text title/subtitle substring
        type: one of bugfix / decision / discovery / feature / change / refactor / question
        project: project slug filter (defaults to current)
        limit: max rows (default 10)
    """
    _refresh_obs_cache_if_stale()
    slug = project or _CWD_SLUG
    q_lower = (query or "").lower().strip()

    matches = []
    for o in _OBS_CACHE:
        if o["project"] != slug: continue
        if type and o["type"] != type: continue
        if q_lower and q_lower not in o["title_lower"] and q_lower not in o["subtitle_lower"]:
            continue
        matches.append(o)

    matches.sort(key=lambda o: o["ts"], reverse=True)
    matches = matches[:limit]

    if not matches:
        return f"No observations match (project={slug}, type={type or 'any'}, query={query or 'any'})."

    out = []
    for o in matches:
        out.append(f"  [{o['type']:9s}] {o['title'][:140]}")
        if o["subtitle"]:
            out.append(f"             ↳ {o['subtitle'][:140]}")
    return "\n".join(out)


@mcp.tool()
def dejavu_handoff(project: str | None = None) -> str:
    """
    Build a ready-to-paste handoff prompt summarizing the last session's
    state for the given project. Useful for kicking off a new conversation
    with context.

    Args:
        project: project slug (defaults to current cwd's slug)
    """
    slug = project or _CWD_SLUG
    with _conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT ss.session_id, ss.request, ss.completed, ss.next_steps, ss.generated_at
            FROM session_summaries ss
            JOIN sessions s ON s.id = ss.session_id
            WHERE s.project_slug = %s
            ORDER BY ss.generated_at DESC
            LIMIT 1
        """, (slug,))
        r = cur.fetchone()
        cur.execute("""
            SELECT o.type::text, o.title FROM observations o
            JOIN sessions s ON s.id = o.session_id
            WHERE s.project_slug = %s
            ORDER BY o.ts DESC LIMIT 5
        """, (slug,))
        obs = cur.fetchall()
    if not r and not obs:
        return f"No prior context for project={slug}."
    lines = [f"Picking up where we left off on {slug}.", ""]
    if r:
        lines.append(f"Last session ({str(r['generated_at'])[:10]}, {str(r['session_id'])[:8]}):")
        if r.get('request'):    lines.append(f"  request:    {r['request']}")
        if r.get('completed'):  lines.append(f"  completed:  {r['completed']}")
        if r.get('next_steps'): lines.append(f"  next steps: {r['next_steps']}")
    if obs:
        lines.append("")
        lines.append("Recent observations:")
        for o in obs:
            lines.append(f"  [{o['type']}] {o['title']}")
    lines.append("")
    lines.append("Continue from there.")
    return "\n".join(lines)


from symbol_indexer import (  # noqa: E402
    resolve_symbol as _idx_resolve,
    list_symbols as _idx_list,
    log_outcome as _idx_log_outcome,
    list_outcomes as _idx_list_outcomes,
)
from lint import lint_proposed_edit as _lint_edit  # noqa: E402
from route_indexer import resolve_route as _resolve_route, list_routes as _list_routes, normalize_path as _normalize_path  # noqa: E402
from page_indexer import check_new_page as _check_new_page, list_pages as _list_pages, file_path_to_route as _file_path_to_route  # noqa: E402
from env_indexer import resolve_env_var as _resolve_env_var, list_env_vars as _list_env_vars  # noqa: E402
from db_indexer import (  # noqa: E402
    resolve_db_field as _resolve_db_field,
    resolve_db_model as _resolve_db_model,
    list_db_models as _list_db_models,
    list_db_fields as _list_db_fields,
)
from graphql_indexer import (  # noqa: E402
    resolve_gql_field as _resolve_gql_field,
    resolve_gql_type as _resolve_gql_type,
    list_gql_types as _list_gql_types,
    list_gql_fields as _list_gql_fields,
)
from flag_indexer import (  # noqa: E402
    resolve_flag as _resolve_flag,
    list_flags as _list_flags,
)
from semantic_search import search as _semantic_search, is_available as _semantic_available  # noqa: E402
from lsp_client import verify as _lsp_verify, list_available_lsps as _list_lsps  # noqa: E402


@mcp.tool()
def dejavu_check_name(
    name: str,
    kind: str | None = None,
    parent: str | None = None,
    project: str | None = None,
) -> str:
    """v0.5.0a: single-entry name verification.

    THE one tool the model should call BEFORE writing any non-trivial
    name. Faster + cheaper than dumping the project schema into every
    system prompt.

    Returns one of three outcomes per name:
      - "OK: <name> declared as <kind> in <source>"   → safe to use
      - "TYPO: did you mean <name>? (<source>)"        → rename
      - "UNKNOWN: <name> not declared in this project" → don't use

    Args:
      name:    the identifier you're about to use (env var, model.field,
               URL, GraphQL field, flag key, CSS class, i18n key,
               import name, etc.)
      kind:    one of:
                 'symbol' | 'env' | 'db-field' | 'db-model' | 'route' |
                 'page' | 'gql-field' | 'gql-type' | 'flag' |
                 'css-class' | 'i18n-key' | 'module-export' | 'name' |
                 'auto' (default — dispatch to all resolvers)
      parent:  required for kinds that need a parent context:
                 - db-field → model name (e.g. 'User')
                 - gql-field → type name (e.g. 'User')
                 - module-export → package name (e.g. 'lodash')
      project: project slug; defaults to current cwd basename
    """
    project_slug = project or _CWD_SLUG
    conn = _conn()
    k = (kind or "auto").lower()

    def _fmt_hits(hits: list[dict], domain: str, name_field: str = "name",
                    extra: str | None = None) -> str:
        if not hits:
            return ""
        top = hits[0]
        match = top.get("match_kind", "exact")
        sim = top.get("trigram_sim", 1.0)
        full = top.get(name_field, name)
        src = top.get("source") or top.get("source_file") or domain
        if match == "exact":
            return f"OK: `{name}` declared as {domain} ({extra or ''} in {src})"
        # fuzzy / cross-* / pattern
        if sim >= 0.75 or match in ("cross-model", "cross-type"):
            return (f"TYPO: did you mean `{full}`? "
                    f"({domain}, sim={sim:.2f}, in {src})")
        return f"UNCERTAIN: closest match `{full}` (sim={sim:.2f})"

    # Domain dispatch
    if k in ("env", "auto"):
        from env_indexer import resolve_env_var
        hits = resolve_env_var(name, project_slug, conn, fuzzy=True, limit=3)
        if hits:
            return _fmt_hits(hits, "env-var", name_field="name")
    if k in ("db-field", "auto"):
        from db_indexer import resolve_db_field
        hits = resolve_db_field(parent, name, project_slug, conn,
                                  fuzzy=True, limit=3)
        if hits:
            top = hits[0]
            mk = top.get("match_kind")
            if mk == "exact":
                return (f"OK: `{top['model_name']}.{top['field_name']}` "
                        f"declared in {top['source']}")
            if mk == "cross-model":
                return (f"TYPO: `{name}` is on `{top['model_name']}`, "
                        f"not `{parent}` ({top['source']})")
            return _fmt_hits(hits, "db-field", name_field="field_name")
    if k in ("db-model", "auto"):
        from db_indexer import resolve_db_model
        hits = resolve_db_model(name, project_slug, conn, fuzzy=True, limit=3)
        if hits:
            return _fmt_hits(hits, "db-model", name_field="model_name")
    if k in ("route", "auto"):
        from route_indexer import resolve_route
        hits = resolve_route("GET", name, project_slug, conn,
                                fuzzy=True, limit=3)
        if hits:
            return _fmt_hits(hits, "route", name_field="path_pattern")
    if k in ("gql-field", "auto"):
        from graphql_indexer import resolve_gql_field
        hits = resolve_gql_field(parent, name, project_slug, conn,
                                    fuzzy=True, limit=3)
        if hits:
            return _fmt_hits(hits, "graphql-field", name_field="field_name")
    if k in ("gql-type", "auto"):
        from graphql_indexer import resolve_gql_type
        hits = resolve_gql_type(name, project_slug, conn, fuzzy=True, limit=3)
        if hits:
            return _fmt_hits(hits, "graphql-type", name_field="type_name")
    if k in ("flag", "auto"):
        from flag_indexer import resolve_flag
        hits = resolve_flag(name, project_slug, conn, fuzzy=True, limit=3)
        if hits:
            return _fmt_hits(hits, "feature-flag", name_field="flag_key")
    if k in ("css-class", "auto"):
        from quickwin_indexer import resolve_css_class, is_tailwind_class
        if is_tailwind_class(name):
            return f"OK: `{name}` is a valid Tailwind utility class"
        hits = resolve_css_class(name, project_slug, conn, fuzzy=True, limit=3)
        if hits:
            return _fmt_hits(hits, "css-class", name_field="class_name")
    if k in ("i18n-key", "auto"):
        from quickwin_indexer import resolve_i18n_key
        hits = resolve_i18n_key(name, project_slug, conn, fuzzy=True, limit=3)
        if hits:
            return _fmt_hits(hits, "i18n-key", name_field="key")
    if k in ("module-export", "auto"):
        from module_indexer import resolve_module_export
        if parent:
            hits = resolve_module_export(parent, name, project_slug, conn,
                                            fuzzy=True, limit=3)
            if hits:
                return _fmt_hits(hits, "module-export",
                                   name_field="export_name",
                                   extra=f"from {parent}")
    if k in ("symbol", "auto"):
        from symbol_indexer import resolve_symbol
        hits = resolve_symbol(name, project_slug=project_slug, conn=conn,
                                fuzzy=True, limit=3)
        if hits:
            return _fmt_hits(hits, "symbol", name_field="name")
    if k in ("name", "auto"):
        from registry_indexer import resolve_name
        hits = resolve_name(name, project_slug, conn, fuzzy=True, limit=3)
        if hits:
            return _fmt_hits(hits, "name-registry", name_field="name")

    return (f"UNKNOWN: `{name}` is not declared in this project "
            f"(checked: {k if k != 'auto' else 'all 13 grounding domains'})")


@mcp.tool()
def dejavu_resolve_symbol(
    name: str,
    project: str | None = None,
    fuzzy: bool = True,
    languages: list[str] | None = None,
    limit: int = 10,
) -> str:
    """Resolve a code symbol (function, class, method, type, interface, enum,
    const, variable) to its actual definition in the indexed project.

    USE THIS BEFORE WRITING CODE that references functions/classes/types you
    haven't seen in this conversation — it catches hallucinated names.

    Args:
        name: the symbol name as you intend to write it (e.g. 'parseUserInput')
        project: project slug to scope the lookup. Defaults to the current
                 working-directory project.
        fuzzy: if True (default), trigram-similar candidates are returned.
               Set False for strict exact-match.
        languages: optional list to filter (e.g. ['python','typescript']).
        limit: max candidates to return.

    Returns a compact text listing of candidates with similarity score,
    edit distance, file path, and signature. If exactly one exact match
    exists, that's almost certainly the symbol you want; otherwise pick
    by signature/file context.
    """
    project_slug = project or _CWD_SLUG
    hits = _idx_resolve(name, project_slug=project_slug, conn=_conn(),
                        fuzzy=fuzzy, limit=limit, languages=languages)
    if not hits:
        # Log a "not found" outcome so the learning loop sees this case.
        _idx_log_outcome(_conn(), project_slug=project_slug,
                         language=(languages[0] if languages else None),
                         mode="lookup", proposed_name=name,
                         resolved_name=None, candidate_count=0,
                         outcome="unknown", outcome_signal="no_candidates")
        return f"No symbols matching '{name}' in project '{project_slug}'. Either it doesn't exist yet, the project isn't indexed, or this name is genuinely new."

    # Log the lookup outcome (top candidate). v0.3.1+ updates outcome later
    # based on whether Claude actually used the resolved name.
    top = hits[0]
    _idx_log_outcome(_conn(), project_slug=project_slug,
                     language=top.get("language"),
                     mode="lookup", proposed_name=name,
                     resolved_name=top["name"],
                     edit_distance=top.get("edit_distance"),
                     trigram_sim=top.get("trigram_sim"),
                     candidate_count=len(hits),
                     outcome="unknown",
                     outcome_signal="logged_at_lookup")

    out = [f"{len(hits)} candidate(s) for '{name}' in '{project_slug}':"]
    for h in hits:
        marker = "✓" if h["edit_distance"] == 0 else " "
        sig = h.get("signature") or h["name"]
        rel = h["file_path"]
        # Trim path to last 3 segments for readability
        parts = rel.split("/")
        rel = "…/" + "/".join(parts[-3:]) if len(parts) > 3 else rel
        out.append(
            f"  {marker} {h['kind']:9s} {h['name']:30s}  "
            f"sim={h['trigram_sim']:.2f} ed={h['edit_distance']}  "
            f"{rel}:{h['start_line']}"
        )
        if sig and sig != h["name"]:
            out.append(f"      {sig}")
    return "\n".join(out)


@mcp.tool()
def dejavu_symbols(
    project: str | None = None,
    file_path: str | None = None,
    kind: str | None = None,
    language: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> str:
    """Browse indexed symbols. Useful for surveying what exists in a file or
    when you want to know all classes/functions of a kind.

    Args:
        project: project slug (defaults to current cwd project).
        file_path: filter to a single file's symbols.
        kind: 'function'|'class'|'method'|'type'|'interface'|'enum'|'const'|'variable'.
        language: 'python'|'typescript'|'javascript'|...
        limit: max rows.
        offset: pagination offset.
    """
    project_slug = project or _CWD_SLUG
    rows = _idx_list(project_slug=project_slug, conn=_conn(),
                     file_path=file_path, kind=kind, language=language,
                     limit=limit, offset=offset)
    if not rows:
        return f"No symbols indexed for project '{project_slug}'. Run `claude-dejavu reindex` to build the index."
    out = [f"{len(rows)} symbol(s) in '{project_slug}':"]
    for r in rows:
        rel = r["file_path"].split("/")
        rel = "…/" + "/".join(rel[-3:]) if len(rel) > 3 else r["file_path"]
        out.append(f"  {r['kind']:9s} {r['name']:30s}  {rel}:{r['start_line']}")
    return "\n".join(out)


@mcp.tool()
def dejavu_doctor() -> str:
    """Diagnose claude-dejavu's install + runtime health and surface fixes.

    Use this when the user reports that anything's not working, or when the
    SessionStart preamble says 'install incomplete'. Returns a per-check
    breakdown (venv, Postgres reachable + schema applied, Weaviate
    responsive, symbol index populated, hooks wired) with overall status
    'ok' / 'warn' / 'fail'.

    Each failed/warn check carries a 'fix' field — a one-shot command or
    instruction the user can run to remediate. Show those to the user.
    """
    from doctor import run_doctor, report_to_text  # type: ignore
    rep = run_doctor()
    return report_to_text(rep)


@mcp.tool()
def dejavu_config(
    action: str,
    key: str | None = None,
    value: str | None = None,
) -> str:
    """Read / write claude-dejavu settings via the typed schema.

    Args:
        action: 'list' | 'get' | 'set' | 'reset'
        key:    setting name (required for get/set/reset)
        value:  new value (required for set)

    Settings catalog (full list via action='list'):
      LINT_MODE             off | warn | suggest | block | autofix
      INJECT_SESSION_START  bool
      AUTO_REINDEX          bool
      UPDATE_CHECK          bool
      LINT_TIMEOUT_S        int
      CLAUDE_DEJAVU_DEFAULT_SCOPE  global | project | workspace

    PG/Weaviate connection settings are read-only via this tool — change
    them by re-running scripts/install.py.

    Most settings take effect on the user's NEXT Claude Code session.
    """
    from config_service import (  # type: ignore
        get as cfg_get, set_value as cfg_set, reset as cfg_reset,
        list_settings, list_to_text, SETTINGS,
    )
    if action == "list":
        return list_to_text(list_settings())
    if action == "get":
        if not key: return "error: 'key' required for get"
        try: return f"{key} = {cfg_get(key)}"
        except KeyError as e: return f"error: {e}"
    if action == "set":
        if not key or value is None: return "error: 'key' and 'value' required for set"
        try:
            cfg_set(key, value)
        except (KeyError, ValueError, PermissionError) as e:
            return f"error: {e}"
        s = SETTINGS[key]
        suffix = ""
        if s.runtime == "next_session":
            suffix = " (takes effect on next Claude Code session)"
        return f"✓ {key} = {value}{suffix}"
    if action == "reset":
        if not key: return "error: 'key' required for reset"
        try: cfg_reset(key)
        except KeyError as e: return f"error: {e}"
        return f"✓ {key} reset to default ({SETTINGS[key].default})"
    return f"error: unknown action {action!r} (try list / get / set / reset)"


@mcp.tool()
def dejavu_lint_proposed_edit(
    content: str,
    file_path: str,
    project: str | None = None,
) -> str:
    """Lint a proposed file edit BEFORE writing — flags fabricated names and
    low-confidence typos against the project's symbol index.

    USE THIS BEFORE Edit/Write/MultiEdit for non-trivial code changes. Pass
    the COMPLETE proposed file content (post-edit) and the destination
    file_path. Returns a verdict + per-reference breakdown.

    Verdict semantics:
      - 'ok'    → all referenced names exist in the index. Safe to write.
      - 'warn'  → close-typo matches found (likely meant a real symbol with
                  a typo). Review the suggestions before writing.
      - 'block' → at least one referenced name has no plausible match.
                  Likely fabricated. Don't write without resolving these.

    Args:
        content: full proposed file content (TypeScript / JavaScript / Python).
        file_path: target file path (used to detect language by extension).
        project: project slug to scope the lookup; defaults to current cwd.

    Returns a compact text report. The structured form is also available via
    the `dejavu_lint_proposed_edit_json` shape for programmatic consumers.
    """
    project_slug = project or _CWD_SLUG
    r = _lint_edit(content, file_path, project_slug=project_slug, conn=_conn())

    s = r["summary"]
    head = (f"verdict: {s['verdict'].upper()}  "
            f"(refs={s['total_references']}  ok={s['resolved']}  "
            f"warn={s['low_confidence']}  unknown={s['unknown']})")
    if s.get("note"):
        head += f"  [{s['note']}]"

    lines = [head]
    if r["unknown"]:
        lines.append("\nUNKNOWN — likely fabricated, fix before writing:")
        for u in r["unknown"]:
            c = u.get("top_candidate")
            if c:
                lines.append(f"  ? {u['name']}  closest: {c['name']} (sim={c['trigram_sim']}, ed={c['edit_distance']}, {c['file_path']}:{c['start_line']})")
            else:
                lines.append(f"  ? {u['name']}  (no candidates in index)")
    if r["low_confidence"]:
        lines.append("\nLOW-CONFIDENCE — likely typo, verify before writing:")
        for lc in r["low_confidence"]:
            c = lc["top_candidate"]
            lines.append(f"  ~ {lc['name']}  -> {c['name']} (sim={c['trigram_sim']}, ed={c['edit_distance']}, {c['file_path']}:{c['start_line']})")
    if r["resolved"] and len(r["resolved"]) <= 8:
        lines.append("\nRESOLVED:")
        for x in r["resolved"]:
            lines.append(f"  ✓ {x['name']}  [{x['kind']}]  {x['file_path']}")
    elif r["resolved"]:
        lines.append(f"\nRESOLVED: {len(r['resolved'])} references confirmed in index.")
    if r["skipped_builtins"]:
        lines.append(f"\nSkipped builtins/imports: {', '.join(r['skipped_builtins'])}")
    return "\n".join(lines)


@mcp.tool()
def dejavu_logs(
    limit: int = 30,
    component: str | None = None,
    level: str | None = None,
    with_traceback: bool = False,
) -> str:
    """Show the most-recent dejavu error-log records.

    The error log lives at `$XDG_DATA_HOME/claude-dejavu/errors.log`
    (Linux) / `~/Library/Application Support/claude-dejavu/errors.log`
    (macOS) / `%APPDATA%\\claude-dejavu\\errors.log` (Windows). One
    JSON record per line, auto-rotated at 5 MB.

    Use this to:
      - Diagnose why a previous dejavu invocation behaved oddly
      - Forward a clean trace to the maintainer when filing an issue
      - Audit which subsystems have been hitting errors recently

    Args:
        limit: max records to return, newest-first.
        component: filter by component prefix (e.g.
            "indexer.env" matches every env-indexer error).
        level: "error" / "warning" / "info" — filter by severity.
        with_traceback: include the full Python traceback for
            records that captured one. Default False keeps output
            short.

    Returns a human-readable text listing. The header line tells
    you the log path so you can `cat` or attach the file directly.
    """
    import sys as _sys
    from pathlib import Path as _P
    _sys.path.insert(0, str(_P(__file__).resolve().parents[1] / "code"))
    from error_log import (read_recent, format_for_human,
                              log_path_str)
    records = read_recent(limit=max(1, min(limit, 500)),
                            component=component,
                            level=level)
    header = (f"# claude-dejavu error log ({len(records)} records)\n"
                f"# location: {log_path_str()}\n\n")
    return header + format_for_human(records,
                                          with_traceback=with_traceback)


@mcp.tool()
def dejavu_propose_env_addition(
    name: str,
    package: str | None = None,
    source_file: str = ".env.example",
    apply: bool = False,
) -> str:
    """Propose adding `name` to the project's .env.example (or
    similar source-of-truth file). Returns the proposed line +
    a unified diff. If `apply=True`, writes the change.

    This complements the rewriter's ACCEPT_SDK verdict: when dejavu
    sees a LIBRARY_DEFAULT name (e.g. LAUNCHDARKLY_SDK_KEY because
    the project depends on @launchdarkly/...), the model is right
    that the SDK needs the env, but the project's manifest is
    incomplete. This tool closes the gap by offering to declare
    the name in .env.example so future runs find it via the manifest
    instead of the SDK catalog.

    Args:
        name: env var to declare (e.g. "LAUNCHDARKLY_SDK_KEY")
        package: optional NPM/PyPI package whose convention this is.
            Used in the comment we attach to the new line.
        source_file: target file relative to project root. Defaults
            to ".env.example".
        apply: if True, write the change. Default False = dry run.

    Returns the proposed change + diff. Never overwrites an
    existing declaration of the same name.
    """
    from pathlib import Path as _P
    cwd = _P.cwd()
    target = cwd / source_file
    if not target.is_file():
        return (f"REFUSED: {source_file} doesn't exist at {target}. "
                  "Create the file first or pass a different "
                  "--source-file.")
    existing = target.read_text(encoding="utf-8", errors="ignore")
    # Already declared?
    import re as _re
    if _re.search(rf"^{_re.escape(name)}\s*=",
                    existing, _re.MULTILINE):
        return (f"NO_CHANGE: `{name}` is already declared in "
                  f"{source_file}.")
    comment = (f"# Standard env for `{package}` (declared by "
                 f"dejavu_propose_env_addition)\n"
                 if package else
                 "# Declared by dejavu_propose_env_addition\n")
    new_line = f"{name}=\n"
    new_content = (existing
                     + ("\n" if not existing.endswith("\n") else "")
                     + comment + new_line)
    diff = (f"--- a/{source_file}\n+++ b/{source_file}\n"
              f"@@ end-of-file @@\n"
              f"+{comment.rstrip()}\n"
              f"+{new_line.rstrip()}")
    if apply:
        target.write_text(new_content, encoding="utf-8")
        return (f"APPLIED: appended `{name}=` to {source_file}.\n\n"
                  f"{diff}\n\nFill in the value, then commit:\n"
                  f"  git add {source_file} && git commit -m "
                  f"'add {name}'")
    return (f"DRY_RUN: would append to {source_file}:\n\n{diff}\n\n"
              "Re-call with apply=True to write the change.")


@mcp.tool()
def dejavu_rewrite_proposed_edit(
    content: str,
    file_path: str,
    project: str | None = None,
) -> str:
    """Lint + auto-correct a proposed file edit BEFORE writing.

    Same input shape as `dejavu_lint_proposed_edit`, but additionally
    runs the deterministic 14-strategy rewriter cascade per flagged
    name. Returns the rewritten content (with TYPO fixes applied) plus
    a per-name verdict report.

    Verdicts mirror the rewriter:
      - REPLACE   → typo auto-fixed via word-boundary substitution
      - ACCEPT_SDK → name is a legitimate third-party SDK convention,
                     gated by package.json deps + import-source check
      - RESIDUAL  → couldn't resolve deterministically; caller should
                    show the closest candidates and ask the user

    The cascade is:
      exact → comment_context → import_gating → sdk_catalog →
      cross_domain → acronym → case_normalize → levenshtein_word →
      trigram → token_set → embedding → project_history →
      suffix_pattern

    Use this when you want correction, not just flagging. For pure
    flagging, use `dejavu_lint_proposed_edit`.

    Args:
        content: full proposed file content.
        file_path: target file path (used to detect language).
        project: project slug; defaults to current cwd.

    Returns the rewritten content followed by a per-name verdict
    report. If no rewrites apply, returns the original content
    unchanged.
    """
    import sys as _sys
    from pathlib import Path as _Path
    _CODE_DIR = str(_Path(__file__).resolve().parents[1] / "code")
    if _CODE_DIR not in _sys.path:
        _sys.path.insert(0, _CODE_DIR)
    try:
        from rewriter import (rewrite as _rewrite,
                                 load_sdk_catalog as _load_catalog,
                                 extend_manifest_from_sources,
                                 project_history_names)
    except ImportError as e:
        return f"REWRITER_UNAVAILABLE: {e}"

    project_slug = project or _CWD_SLUG
    # Reuse the existing lint to locate bad names.
    lint = _lint_edit(content, file_path, project_slug=project_slug,
                        conn=_conn())
    bad_names: dict[str, list[str]] = {"db": [], "env": [],
                                          "route": [], "graphql": [],
                                          "flag": []}
    # The lint result groups by domain implicitly via name kind.
    for u in lint.get("unknown", []) + lint.get("low_confidence", []):
        # Best-effort: route by kind heuristic
        nm = u.get("name", "")
        kind = (u.get("kind") or "").lower()
        if "env" in kind or nm.isupper():
            bad_names["env"].append(nm)
        elif "route" in kind or "/" in nm:
            bad_names["route"].append(nm)
        elif "flag" in kind or "-" in nm:
            bad_names["flag"].append(nm)
        elif "gql" in kind:
            bad_names["graphql"].append(nm)
        else:
            bad_names["db"].append(nm)

    # Build a manifest from this project's indexed sources.
    cwd = _Path.cwd()
    manifest = {"project_slug": project_slug,
                  "env_vars": [], "models": [], "user_fields": [],
                  "post_fields": [], "routes": [], "gql_types": [],
                  "flags": [], "package_deps": []}
    try:
        manifest = extend_manifest_from_sources(manifest, cwd)
    except Exception:
        pass

    deps_path = cwd / "package.json"
    project_deps: set[str] = set()
    if deps_path.is_file():
        try:
            import json as _json
            pkg = _json.loads(deps_path.read_text())
            project_deps = (set(pkg.get("dependencies", {}).keys())
                              | set(pkg.get("devDependencies", {}).keys()))
        except Exception:
            pass

    history = set()
    try:
        history = project_history_names(cwd)
    except Exception:
        pass

    rewritten, outcomes = _rewrite(
        content, manifest,
        project_deps=project_deps,
        bad_names=bad_names,
        sdk_catalog=_load_catalog(),
        history_names=history,
    )

    if rewritten == content and not outcomes:
        return content  # nothing to do

    report = ["", "", "─── dejavu rewriter report ───"]
    for o in outcomes:
        c = o.chosen
        report.append(
            f"  [{o.final_verdict:10s}] {o.domain:7s} "
            f"{o.original}"
            + (f"  →  {c.replacement}" if c and c.replacement
                  and c.replacement != o.original else "")
            + (f"  ({c.strategy}, conf={c.confidence:.2f})"
                if c else "")
        )
    return rewritten + "\n".join(report)


@mcp.tool()
def dejavu_resolve_route(
    method: str,
    path: str,
    project: str | None = None,
    fuzzy: bool = True,
    limit: int = 5,
) -> str:
    """Look up an HTTP route declared in the project. Use BEFORE writing
    any client-side call (fetch / axios / useQuery / requests / etc.) to
    verify the URL + method actually exist.

    Resolves in three layers:
      1. Exact normalized path match (handles /users/:id ≡ /users/{id} ≡ /users/[id])
      2. Pattern match — concrete URL '/api/users/123' against parameterized
         '/api/users/:id'
      3. Fuzzy on path_pattern via trigram (catches typos)

    Args:
        method: HTTP method ('GET' / 'POST' / ... / '*' to ignore method)
        path:   the URL or path you want to check ('/api/users/123', '/users/{id}', ...)
        project: project slug; defaults to current cwd basename
        fuzzy: include fuzzy matches when no exact/pattern match exists
        limit: max results

    Returns a compact list of matching routes with framework, declared
    method, normalized path, source file:line, and the match kind
    (exact / pattern / fuzzy). Use the source field to know whether the
    answer comes from OpenAPI ('openapi') or code-side detection ('code').
    """
    project_slug = project or _CWD_SLUG
    hits = _resolve_route(method, path, project_slug, _conn(),
                          fuzzy=fuzzy, limit=limit)
    if not hits:
        return (f"No route matching `{method.upper()} {_normalize_path(path)}` "
                f"in project '{project_slug}'. Either it doesn't exist yet, "
                f"the project hasn't been re-indexed since the route was added "
                f"(try `claude-dejavu reindex`), or the URL is fabricated.")
    lines = [f"{len(hits)} route(s) for `{method.upper()} {_normalize_path(path)}` in '{project_slug}':"]
    for h in hits:
        marker = "✓" if h["match_kind"] == "exact" else ("≈" if h["match_kind"] == "pattern" else "~")
        sim = f" sim={h['trigram_sim']:.2f}" if h["match_kind"] == "fuzzy" else ""
        rel = h["source_file"]
        parts = rel.split("/")
        rel = "…/" + "/".join(parts[-3:]) if len(parts) > 3 else rel
        lines.append(f"  {marker} {h['method']:6s} {h['path_pattern']:30s}  "
                     f"[{h['framework']}/{h['source']}]  {rel}:{h['start_line']}{sim}")
        if h.get("description"):
            lines.append(f"      {h['description']}")
    return "\n".join(lines)


@mcp.tool()
def dejavu_routes(
    project: str | None = None,
    framework: str | None = None,
    method: str | None = None,
    limit: int = 50,
) -> str:
    """Browse all routes declared in a project's index.

    Useful for surveying what the project's API surface actually is, or
    when you need to know "which routes exist on /api/users".
    """
    project_slug = project or _CWD_SLUG
    rows = _list_routes(project_slug, _conn(), framework=framework,
                        method=method, limit=limit)
    if not rows:
        return f"No routes indexed for '{project_slug}'. Run `claude-dejavu reindex` to build the index."
    lines = [f"{len(rows)} route(s) in '{project_slug}':"]
    for r in rows:
        rel = r["source_file"].split("/")
        rel = "…/" + "/".join(rel[-3:]) if len(rel) > 3 else r["source_file"]
        lines.append(f"  {r['method']:6s} {r['path_pattern']:35s}  "
                     f"[{r['framework']}/{r['source']}]  {rel}:{r['start_line']}")
    return "\n".join(lines)


@mcp.tool()
def dejavu_check_new_page(
    proposed_path: str,
    project: str | None = None,
    limit: int = 5,
) -> str:
    """Check whether a proposed frontend route already has an existing page
    in the project. Use BEFORE creating a new file under a routing-aware
    directory (Next.js pages/app, Remix routes, SvelteKit src/routes,
    Astro src/pages, or before adding a <Route> to React/Vue Router config).

    Three-layer overlap detection:
      1. Exact path match → definite duplicate (existing file already
         serves this URL).
      2. Pattern match — concrete proposed path against a parameterized
         existing one ('/blog/foo' overlaps with '/blog/:slug').
      3. Fuzzy match — near-name overlap ('/profile-edit' vs
         '/dashboard/profile/edit').

    Args:
        proposed_path: the URL the new page would serve (e.g. '/dashboard/profile')
        project: project slug; defaults to current cwd basename
        limit: max overlapping pages to return

    If the result is non-empty, the user should EDIT the existing file
    rather than creating a new one. The output includes the existing
    file path and line so Claude can read + amend instead of duplicating.
    """
    project_slug = project or _CWD_SLUG
    hits = _check_new_page(proposed_path, project_slug, _conn(), fuzzy=True, limit=limit)
    if not hits:
        return (f"No existing page covers `{proposed_path}` in '{project_slug}'. "
                f"Safe to create a new file at this route.")
    lines = [f"{len(hits)} existing page(s) overlap with `{proposed_path}` in '{project_slug}':"]
    for h in hits:
        marker = "✗" if h["match_kind"] == "exact" else ("≈" if h["match_kind"] == "pattern" else "~")
        sim = f" sim={h['trigram_sim']:.2f}" if h["match_kind"] == "fuzzy" else ""
        rel = h["source_file"].split("/")
        rel = "…/" + "/".join(rel[-3:]) if len(rel) > 3 else h["source_file"]
        lines.append(f"  {marker} {h['kind']:9s} {h['path_pattern']:35s}  [{h['framework']}]  {rel}:{h['start_line']}{sim}")
    lines.append("")
    if any(h["match_kind"] == "exact" for h in hits):
        lines.append("→ EDIT the existing file rather than creating a new one. "
                     "Two files serving the same URL is a duplicate-page bug.")
    elif any(h["match_kind"] == "pattern" for h in hits):
        lines.append("→ The path is already covered by a parameterized route. "
                     "Either use the existing route or pick a non-overlapping path.")
    else:
        lines.append("→ Near-name overlap. Verify whether the existing page covers "
                     "the same goal; if so, edit there instead of creating a new file.")
    return "\n".join(lines)


@mcp.tool()
def dejavu_pages(
    project: str | None = None,
    framework: str | None = None,
    kind: str | None = None,
    limit: int = 100,
) -> str:
    """Browse all frontend page routes declared in the project.

    Use to survey the existing routing surface before proposing a new
    page, or when you need to find which file handles a known URL.
    """
    project_slug = project or _CWD_SLUG
    rows = _list_pages(project_slug, _conn(), framework=framework, kind=kind, limit=limit)
    if not rows:
        return f"No pages indexed for '{project_slug}'. Run `claude-dejavu reindex` to build the index."
    lines = [f"{len(rows)} page(s) in '{project_slug}':"]
    for r in rows:
        rel = r["source_file"].split("/")
        rel = "…/" + "/".join(rel[-3:]) if len(rel) > 3 else r["source_file"]
        lines.append(f"  {r['kind']:9s} {r['path_pattern']:38s}  [{r['framework']}]  {rel}:{r['start_line']}")
    return "\n".join(lines)


@mcp.tool()
def dejavu_check_env_var(
    name: str,
    project: str | None = None,
    limit: int = 5,
) -> str:
    """Check whether an environment variable name resolves against the
    project's declared env vars (.env.example, zod/envalid schema,
    docker-compose, GitHub Actions). Use BEFORE writing code that reads
    `process.env.X` / `os.getenv("X")` / `Deno.env.get("X")` /
    `import.meta.env.X` to catch fabricated names + typos.

    Resolution semantics:
      - Exact match in a canonical source → declared. Safe to use.
      - Exact match only via source-read tier → name is read elsewhere
        in the project but never declared canonically. Either the project
        has an undocumented var (add to .env.example) or it's a typo
        replicated across files.
      - Fuzzy match → likely a typo. The MCP response includes the
        candidate name so you can correct.
      - No match → fabricated. Pick the right name OR declare the var.

    Args:
        name: env var name to resolve, e.g. 'DATABASE_URL'
        project: project slug; defaults to current cwd basename
        limit: max candidates to return
    """
    project_slug = project or _CWD_SLUG
    hits = _resolve_env_var(name, project_slug, _conn(), fuzzy=True, limit=limit)
    if not hits:
        return (f"No declaration found for `{name}` in '{project_slug}'. "
                f"This name would be flagged as fabricated. Either pick the "
                f"correct name (run dejavu_env_vars to browse) or declare "
                f"`{name}` in .env.example and any zod env schema.")
    canonical = ("env-example", "zod-schema", "envalid", "docker-compose",
                 "github-actions")
    top = hits[0]
    lines = [f"{len(hits)} match(es) for `{name}` in '{project_slug}':"]
    for h in hits:
        marker = "✓" if h["match_kind"] == "exact" else "~"
        sim = f" sim={h['trigram_sim']:.2f}" if h["match_kind"] == "fuzzy" else ""
        secret = " 🔒" if h["is_secret"] else ""
        rel = h["source_file"].split("/")
        rel = "…/" + "/".join(rel[-3:]) if len(rel) > 3 else h["source_file"]
        lines.append(f"  {marker} {h['name']:30s}  [{h['source']}]{secret}  "
                      f"{rel}:{h['start_line']}{sim}")
    lines.append("")
    if top["match_kind"] == "exact" and top["source"] in canonical:
        lines.append(f"→ `{name}` is declared in {top['source']}. Safe to use.")
    elif top["match_kind"] == "exact" and top["source"] == "source-read":
        lines.append(f"→ `{name}` is read elsewhere in the project but NOT declared "
                     f"in any canonical source. Add it to .env.example, OR check "
                     f"whether it's a typo of a similarly-named var.")
    else:
        lines.append(f"→ Closest match: `{top['name']}` "
                     f"(sim={top.get('trigram_sim', 0):.2f}). Likely a typo — "
                     f"consider replacing `{name}` with `{top['name']}`.")
    return "\n".join(lines)


@mcp.tool()
def dejavu_env_vars(
    project: str | None = None,
    source: str | None = None,
    secrets_only: bool = False,
    limit: int = 200,
) -> str:
    """Browse all environment variable declarations indexed in the
    project. Use to survey what env vars are available before writing
    code that reads `process.env.X` or to find what name to declare.

    Args:
        project: project slug; defaults to cwd basename
        source: filter by tier — env-example | zod-schema | envalid |
                docker-compose | github-actions | source-read
        secrets_only: only show vars matching SECRET/TOKEN/KEY/PASSWORD heuristic
        limit: max vars to return
    """
    project_slug = project or _CWD_SLUG
    rows = _list_env_vars(project_slug, _conn(), source=source,
                           secrets_only=secrets_only, limit=limit)
    if not rows:
        return (f"No env vars indexed for '{project_slug}'. "
                f"Run `claude-dejavu reindex` to build the index.")
    lines = [f"{len(rows)} env var(s) in '{project_slug}':"]
    for r in rows:
        marker = "🔒" if r["is_secret"] else "  "
        sources = ",".join(r["sources"])
        desc = f"  # {r['description']}" if r.get("description") else ""
        rel = ""
        if r.get("canonical_file"):
            parts = r["canonical_file"].split("/")
            rel = ("  …/" + "/".join(parts[-3:]) if len(parts) > 3
                   else "  " + r["canonical_file"])
            rel += f":{r['canonical_line']}"
        lines.append(f"  {marker} {r['name']:32s}  [{sources}]{rel}{desc}")
    return "\n".join(lines)


@mcp.tool()
def dejavu_check_db_field(
    target: str,
    project: str | None = None,
    limit: int = 5,
) -> str:
    """Resolve a database field reference against the project's
    Prisma / Drizzle / TypeORM / SQL schema. Use BEFORE writing
    `prisma.<model>.<op>({ where: { … } })`,
    `db.select().from(<schema>)`, or raw SQL to catch typos and
    fabricated columns.

    Args:
        target: 'MODEL.FIELD' (preferred) or just 'FIELD' (cross-model search)
        project: project slug; defaults to cwd basename
        limit: max candidates to return

    Match kinds in the result:
      ✓ exact      — same model + field exists
      ≈ cross-model — exact field name but on a DIFFERENT model
      ~ fuzzy      — close match on the named model (typo)
      ~ cross-fuzzy — close match on a different model
    """
    project_slug = project or _CWD_SLUG
    if "." in target:
        model_name, field_name = target.split(".", 1)
    else:
        model_name, field_name = None, target
    hits = _resolve_db_field(model_name, field_name, project_slug,
                              _conn(), fuzzy=True, limit=limit)
    if not hits:
        return (f"No field matching `{target}` in '{project_slug}'. This "
                f"would be flagged as fabricated. Either pick the correct "
                f"name (run dejavu_db_models / dejavu_db_fields to browse) "
                f"or declare it in the schema.")
    lines = [f"{len(hits)} match(es) for `{target}` in '{project_slug}':"]
    for h in hits:
        marker = ("✓" if h["match_kind"] == "exact" else
                  ("≈" if h["match_kind"] == "cross-model" else "~"))
        sim = (f" sim={h['trigram_sim']:.2f}"
                if "fuzzy" in h["match_kind"] else "")
        rel = h["source_file"].split("/")
        rel = "…/" + "/".join(rel[-3:]) if len(rel) > 3 else h["source_file"]
        lines.append(f"  {marker} {h['model_name']:20s}.{h['field_name']:25s} "
                      f"[{h['source']}]  {rel}:{h['start_line']}{sim}")
    lines.append("")
    top = hits[0]
    if top["match_kind"] == "exact":
        lines.append(f"→ `{target}` is declared. Safe to use.")
    elif top["match_kind"] == "cross-model":
        lines.append(f"→ `{field_name}` exists on `{top['model_name']}`, "
                     f"NOT on `{model_name}`. Either query `{top['model_name']}` "
                     f"or add the field to `{model_name}`.")
    else:
        lines.append(f"→ Closest match: `{top['model_name']}.{top['field_name']}` "
                     f"(sim={top.get('trigram_sim', 0):.2f}). Likely a typo.")
    return "\n".join(lines)


@mcp.tool()
def dejavu_db_models(
    project: str | None = None,
    source: str | None = None,
    limit: int = 100,
) -> str:
    """Browse all models / tables indexed in the project schema.
    Use to survey what models exist before writing ORM calls.

    Args:
        project: project slug; defaults to cwd basename
        source: filter — prisma | drizzle | typeorm | sql-migration
        limit: max models to return
    """
    project_slug = project or _CWD_SLUG
    rows = _list_db_models(project_slug, _conn(), source=source, limit=limit)
    if not rows:
        return (f"No DB models indexed for '{project_slug}'. "
                f"Run `claude-dejavu reindex` to build the index.")
    lines = [f"{len(rows)} model(s) in '{project_slug}':"]
    for r in rows:
        tn = f" → {r['table_name']}" if r.get("table_name") else ""
        rel = r["source_file"].split("/")
        rel = "…/" + "/".join(rel[-3:]) if len(rel) > 3 else r["source_file"]
        lines.append(f"  {r['model_name']:25s} [{r['source']}]  "
                      f"{r['field_count']} fields{tn}  {rel}:{r['start_line']}")
    return "\n".join(lines)


@mcp.tool()
def dejavu_db_fields(
    model: str,
    project: str | None = None,
    limit: int = 200,
) -> str:
    """List every field declared on a given model. Use BEFORE composing
    a Prisma `where` / `select` / `data` block to know exactly what
    keys the model accepts.

    Args:
        model: model name (e.g. 'User')
        project: project slug; defaults to cwd basename
        limit: max fields to return
    """
    project_slug = project or _CWD_SLUG
    rows = _list_db_fields(project_slug, _conn(), model_name=model, limit=limit)
    if not rows:
        return (f"No fields indexed for model `{model}` in '{project_slug}'. "
                f"Run dejavu_db_models to confirm the model name, or "
                f"`claude-dejavu reindex` to rebuild.")
    lines = [f"{len(rows)} field(s) on `{model}`:"]
    for r in rows:
        badges = []
        if r["is_id"]: badges.append("id")
        if r["is_unique"]: badges.append("unique")
        if r["is_relation"]: badges.append(f"→{r['related_model']}")
        badge = (" [" + ",".join(badges) + "]") if badges else ""
        lines.append(f"  {r['field_name']:25s} {r['field_type'] or '?':12s}{badge}")
    return "\n".join(lines)


@mcp.tool()
def dejavu_check_gql_field(
    target: str,
    project: str | None = None,
    limit: int = 5,
) -> str:
    """Resolve a GraphQL field reference against the project's
    indexed schema (`*.graphql` SDL files + inline `gql\`type X { … }\``
    template literals). Use BEFORE composing `gql\`query|mutation { … }\``
    operations to catch typos and fabricated fields.

    Args:
        target: 'TYPE.FIELD' (preferred) or just 'FIELD' (cross-type search)
        project: project slug; defaults to cwd basename
        limit: max candidates to return
    """
    project_slug = project or _CWD_SLUG
    if "." in target:
        type_name, field_name = target.split(".", 1)
    else:
        type_name, field_name = None, target
    hits = _resolve_gql_field(type_name, field_name, project_slug,
                                _conn(), fuzzy=True, limit=limit)
    if not hits:
        return (f"No GraphQL field matching `{target}` in '{project_slug}'. "
                f"Run dejavu_gql_types or dejavu_gql_fields to browse.")
    lines = [f"{len(hits)} match(es) for `{target}` in '{project_slug}':"]
    for h in hits:
        marker = ("✓" if h["match_kind"] == "exact" else
                  ("≈" if h["match_kind"] == "cross-type" else "~"))
        sim = (f" sim={h['trigram_sim']:.2f}"
                if "fuzzy" in h["match_kind"] else "")
        rel = h["source_file"].split("/")
        rel = "…/" + "/".join(rel[-3:]) if len(rel) > 3 else h["source_file"]
        lines.append(f"  {marker} {h['type_name']:20s}.{h['field_name']:25s} "
                      f"[{h['source']}]  {rel}:{h['start_line']}{sim}")
    lines.append("")
    top = hits[0]
    if top["match_kind"] == "exact":
        lines.append(f"→ `{target}` is declared. Safe to use.")
    elif top["match_kind"] == "cross-type":
        lines.append(f"→ `{field_name}` exists on `{top['type_name']}`, "
                     f"NOT on `{type_name}`.")
    else:
        lines.append(f"→ Closest match: `{top['type_name']}.{top['field_name']}` "
                     f"(sim={top.get('trigram_sim', 0):.2f}). Likely a typo.")
    return "\n".join(lines)


@mcp.tool()
def dejavu_gql_types(
    project: str | None = None,
    kind: str | None = None,
    source: str | None = None,
    limit: int = 100,
) -> str:
    """Browse all GraphQL types indexed in the project.

    Args:
        project: project slug; defaults to cwd basename
        kind: filter — object | input | enum | interface | union | scalar | schema
        source: filter — sdl-file | inline-gql | codegen
        limit: max types to return
    """
    project_slug = project or _CWD_SLUG
    rows = _list_gql_types(project_slug, _conn(), kind=kind, source=source,
                            limit=limit)
    if not rows:
        return (f"No GraphQL types indexed for '{project_slug}'. "
                f"Run `claude-dejavu reindex` to build the index.")
    lines = [f"{len(rows)} type(s) in '{project_slug}':"]
    for r in rows:
        rel = r["source_file"].split("/")
        rel = "…/" + "/".join(rel[-3:]) if len(rel) > 3 else r["source_file"]
        lines.append(f"  {r['kind']:10s} {r['type_name']:25s} [{r['source']}]  "
                      f"{r['field_count']} fields  {rel}:{r['start_line']}")
    return "\n".join(lines)


@mcp.tool()
def dejavu_gql_fields(
    type_name: str,
    project: str | None = None,
    limit: int = 200,
) -> str:
    """List every field declared on a GraphQL type. Use BEFORE composing
    a selection set to know what fields the type accepts.

    Args:
        type_name: GraphQL type name (e.g. 'User')
        project: project slug; defaults to cwd basename
        limit: max fields to return
    """
    project_slug = project or _CWD_SLUG
    rows = _list_gql_fields(project_slug, _conn(), type_name=type_name,
                              limit=limit)
    if not rows:
        return (f"No fields indexed for type `{type_name}` in '{project_slug}'.")
    lines = [f"{len(rows)} field(s) on `{type_name}`:"]
    for r in rows:
        badges = []
        if not r["is_nullable"]: badges.append("required")
        if r["is_list"]: badges.append("list")
        badge = (" [" + ",".join(badges) + "]") if badges else ""
        lines.append(f"  {r['field_name']:25s} {r['field_type'] or '?':15s}{badge}")
    return "\n".join(lines)


@mcp.tool()
def dejavu_check_flag(
    flag_key: str,
    project: str | None = None,
    limit: int = 5,
) -> str:
    """Resolve a feature-flag key against the project's indexed flag
    declarations (flags.json, featureFlags.ts, LaunchDarkly, Unleash,
    GrowthBook). Use BEFORE writing
    `client.variation('key', user)` / `isEnabled('key')` /
    `Statsig.checkGate('key')` to catch typos and fabricated keys.

    Args:
        flag_key: the canonical key string used at the call site
        project: project slug; defaults to cwd basename
        limit: max candidates to return
    """
    project_slug = project or _CWD_SLUG
    hits = _resolve_flag(flag_key, project_slug, _conn(), fuzzy=True,
                          limit=limit)
    if not hits:
        return (f"No flag matching `{flag_key}` in '{project_slug}'. "
                f"This would be flagged as fabricated. Run "
                f"`dejavu_feature_flags` to browse declared keys.")
    lines = [f"{len(hits)} match(es) for `{flag_key}` in '{project_slug}':"]
    for h in hits:
        marker = "✓" if h["match_kind"] == "exact" else "~"
        sim = (f" sim={h['trigram_sim']:.2f}"
                if h["match_kind"] == "fuzzy" else "")
        kill = " 🛑" if h["is_kill_switch"] else ""
        rel = h["source_file"].split("/")
        rel = "…/" + "/".join(rel[-3:]) if len(rel) > 3 else h["source_file"]
        lines.append(f"  {marker} {h['flag_key']:30s}  [{h['source']}]{kill}  "
                      f"{rel}:{h['start_line']}{sim}")
    return "\n".join(lines)


@mcp.tool()
def dejavu_feature_flags(
    project: str | None = None,
    source: str | None = None,
    kill_switches_only: bool = False,
    limit: int = 200,
) -> str:
    """Browse all feature flags indexed in the project.

    Args:
        project: project slug; defaults to cwd basename
        source: filter — flags-json | feature-flags-ts | launchdarkly | unleash | growthbook | source-read
        kill_switches_only: only show flags whose key matches kill/emergency/disable/panic
        limit: max flags to return
    """
    project_slug = project or _CWD_SLUG
    rows = _list_flags(project_slug, _conn(), source=source,
                        kill_switches_only=kill_switches_only, limit=limit)
    if not rows:
        return (f"No feature flags indexed for '{project_slug}'. "
                f"Run `claude-dejavu reindex` to build the index.")
    lines = [f"{len(rows)} flag(s) in '{project_slug}':"]
    for r in rows:
        marker = "🛑" if r["is_kill_switch"] else "  "
        sources = ",".join(r["sources"])
        rel = ""
        if r.get("canonical_file"):
            parts = r["canonical_file"].split("/")
            rel = ("  …/" + "/".join(parts[-3:]) if len(parts) > 3
                   else "  " + r["canonical_file"])
            rel += f":{r['canonical_line']}"
        lines.append(f"  {marker} {r['flag_key']:35s}  [{sources}]{rel}")
    return "\n".join(lines)


@mcp.tool()
def dejavu_about(
    project: str | None = None,
) -> str:
    """Who built claude-dejavu, what it does, where to find more.

    No-arg, no-side-effect — never captures email or any other
    identifier. Just displays information for users who want to know
    who's behind the plugin.

    Args:
        project: optional — if given, also surface the project's
                 indexed-domain stats (helpful as a one-shot
                 "what does dejavu know about this project" view).
    """
    lines = [
        "claude-dejavu — grounding for LLM-generated code",
        "",
        "Eight grounding domains catch fabricated names BEFORE they hit",
        "disk: symbols (9 languages), HTTP routes, frontend pages,",
        "env vars, DB schema (Prisma/Drizzle/TypeORM/SQL), GraphQL",
        "(SDL + inline gql), feature flags, plus an LSP wire-protocol",
        "passthrough for type-check ground truth.",
        "",
        "Built by HTE Switzerland — a Swiss digital solutions partner.",
        "  · Custom software development",
        "  · Cloud development & deployment",
        "  · Web & mobile app development",
        "  · UX / UI design",
        "  · Hosting + infrastructure",
        "  · Outsourcing & digital coaching",
        "",
        "Tagline: \"Building Digital Business with IT Solutions\"",
        "",
        "If you liked how this is built, that's the kind of work HTE",
        "delivers. https://hendrikthurau.enterprises",
        "",
        "Public benchmark — how much does claude-dejavu actually reduce",
        "Claude's hallucination rate?",
        "  https://hendrikthurau.enterprises/claude-dejavu/benchmark",
        "",
        "Source code, issues, contributions:",
        "  https://github.com/HthSolid/claude-dejavu",
        "",
        "claude-dejavu is fair-source under FSL-1.1-ALv2. Free for",
        "individual use, internal corporate use, OSS, research, and",
        "professional services. Each version converts to Apache 2.0",
        "two years after release. The plugin is fully functional",
        "forever. Commercial license available for bundling, hosting,",
        "or competing services — dejavu@hendrikthurau.enterprises.",
        "A future paid Pro tier (editor extensions, CI integration,",
        "hallucination dashboard, custom grounding domains) helps",
        "fund continued development.",
    ]
    if project:
        try:
            project_slug = project
            with _conn() as c, c.cursor() as cur:
                cur.execute("SELECT count(*) FROM symbols WHERE project_slug = %s",
                            (project_slug,))
                sym_count = cur.fetchone()[0] or 0
                cur.execute("SELECT count(*) FROM api_routes WHERE project_slug = %s",
                            (project_slug,))
                route_count = cur.fetchone()[0] or 0
                cur.execute("SELECT count(*) FROM page_routes WHERE project_slug = %s",
                            (project_slug,))
                page_count = cur.fetchone()[0] or 0
                cur.execute("SELECT count(*) FROM env_var_decls WHERE project_slug = %s",
                            (project_slug,))
                env_count = cur.fetchone()[0] or 0
                cur.execute("SELECT count(*) FROM db_models WHERE project_slug = %s",
                            (project_slug,))
                model_count = cur.fetchone()[0] or 0
                cur.execute("SELECT count(*) FROM graphql_types WHERE project_slug = %s",
                            (project_slug,))
                gql_count = cur.fetchone()[0] or 0
                cur.execute("SELECT count(*) FROM feature_flags WHERE project_slug = %s",
                            (project_slug,))
                flag_count = cur.fetchone()[0] or 0
            lines += [
                "",
                f"━━ what dejavu knows about '{project_slug}' ━━",
                f"  symbols       : {sym_count:>6}",
                f"  api routes    : {route_count:>6}",
                f"  pages         : {page_count:>6}",
                f"  env vars      : {env_count:>6}",
                f"  db models     : {model_count:>6}",
                f"  graphql types : {gql_count:>6}",
                f"  feature flags : {flag_count:>6}",
            ]
        except Exception as e:  # noqa: BLE001
            lines += [
                "",
                f"(could not load project stats: {type(e).__name__})",
            ]
    return "\n".join(lines)


@mcp.tool()
def dejavu_resolve_semantic(
    intent: str,
    kind: str | None = None,
    project: str | None = None,
    limit: int = 5,
) -> str:
    """Semantic search across code symbols + routes by intent rather than
    by exact name.

    Use when the user describes what they want by intent ("function that
    parses ISO dates", "endpoint to create a user") and you want to find
    the closest existing match. Complements dejavu_resolve_symbol /
    dejavu_resolve_route which work on exact / fuzzy name lookup.

    Args:
        intent: natural-language description of what to find
        kind: 'symbol' | 'route' | None (any)
        project: project slug; defaults to cwd basename
        limit: max results

    Returns ranked candidates with cosine similarity. Treat similarity
    < 0.6 as weak.
    """
    if not _semantic_available():
        return ("Weaviate is not reachable — semantic search is unavailable. "
                "Run `claude-dejavu doctor` to diagnose, then "
                "`docker compose -f docker/docker-compose.minimal.yml up -d` "
                "to start the stack.")
    project_slug = project or _CWD_SLUG
    hits = _semantic_search(intent, project_slug, kind=kind, limit=limit)
    if not hits:
        return f"No semantic matches for '{intent}' in '{project_slug}'."
    lines = [f"{len(hits)} semantic match(es) for '{intent}' in '{project_slug}':"]
    for h in hits:
        sim = h.get("similarity", 0)
        marker = "✓" if sim >= 0.85 else ("~" if sim >= 0.65 else "·")
        kind_l = (h.get("kind") or "?")
        lines.append(f"  {marker} cos={sim:.2f}  [{kind_l:6s}]  {h['name']:30s}  {h.get('file_path','')}")
        sig = h.get("signature") or h.get("docstring")
        if sig and sig != h["name"]:
            lines.append(f"      {sig[:120]}")
    return "\n".join(lines)


@mcp.tool()
def dejavu_lsp_verify(
    file_path: str,
    proposed_content: str,
    language: str | None = None,
) -> str:
    """Verify a proposed file edit via the project's language server
    (tsserver / pyright / rust-analyzer / gopls).

    Returns LSP diagnostics for the proposed-state file. If the LSP says
    a name is unresolvable / wrong type / missing import, that's ground
    truth. The answer trumps the lexical lint.

    For v0.3.2.1 this is scaffolding — returns availability info but
    doesn't yet collect actual diagnostics over the JSON-RPC wire.
    Full integration lands in v0.3.2.1.1.

    Args:
        file_path: target file path (used for language detection if `language` is None)
        proposed_content: the full proposed file content
        language: override language detection ('typescript', 'python', ...)
    """
    if language is None:
        ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
        language = {
            "ts": "typescript", "tsx": "tsx", "js": "javascript",
            "py": "python", "rs": "rust", "go": "go",
        }.get(ext, "")
    if not language:
        return f"Could not infer language from {file_path}; pass `language` explicitly."
    result = _lsp_verify(file_path, proposed_content, language)
    if not result.get("available"):
        avail = _list_lsps()
        installed = {k: v for k, v in avail.items() if v}
        return (f"No LSP binary on PATH for language='{language}'. "
                f"{result.get('skipped_reason', '')}\n"
                f"Available LSPs: {installed or 'none'}")
    diags = result.get("diagnostics", [])
    if not diags:
        note = result.get("skipped_reason", "")
        return (f"LSP {result.get('binary')} OK on {file_path}: no diagnostics."
                + (f"\n  ({note})" if note else ""))
    lines = [f"LSP {result.get('binary')} on {file_path}: {len(diags)} diagnostic(s)"]
    for d in diags[:8]:
        lines.append(f"  [{d.get('severity', '?'):5s}] L{d.get('line', '?')}:{d.get('column', '?')}  {d.get('message', '')}")
    return "\n".join(lines)


@mcp.tool()
def dejavu_workspaces() -> str:
    """List defined workspaces (named groups of projects for shared recall)."""
    with _conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT name, description, projects, created_at FROM workspaces ORDER BY name")
        rows = cur.fetchall()
    if not rows:
        return "No workspaces defined. Create one with `claude-dejavu workspace create <name> --projects=a,b,c`."
    out = []
    for r in rows:
        projs = ", ".join(r['projects'] or [])
        out.append(f"  {r['name']:20s} ({len(r['projects'] or [])} projects)  {r.get('description') or ''}")
        out.append(f"    projects: {projs}")
    return "\n".join(out)


def _warm_caches_at_startup():
    """Pre-populate the in-memory caches at server boot so the very first
    tool call is fast. Failures are silent (server still works without
    caches; they self-load on demand)."""
    try:
        _refresh_obs_cache_if_stale()
        _refresh_sum_cache_if_stale()
    except Exception:
        pass


if __name__ == "__main__":
    # Install the uncaught-exception hook BEFORE anything else so
    # crashes during startup are also captured to the error log.
    try:
        import sys as _sys
        from pathlib import Path as _P
        _sys.path.insert(0, str(_P(__file__).resolve().parents[1]
                                    / "code"))
        from error_log import install_excepthook as _install_hook
        from error_log import log_error as _log_err
        _install_hook(component="mcp.server")
    except Exception:  # noqa: BLE001
        _log_err = None  # type: ignore[assignment]
    try:
        _warm_caches_at_startup()
    except Exception as _e:  # noqa: BLE001
        if _log_err:
            _log_err("mcp.server.warm_caches",
                       f"warm-caches failed: {_e}",
                       exc_info=True)
    mcp.run(transport="stdio")
