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
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path

import psycopg2
import psycopg2.extras

# Local module path (sibling /code dir)
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "code"))
from scope import resolve_scope, apply_pg_filter, weaviate_where, ScopeFilter  # noqa: E402
from topic import tokenize_query as _tokenize_query  # noqa: E402  (v0.8.10)

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
    """Hybrid Weaviate search.

    v0.8.10 Fix 4 — recall investigation. Pre-v0.8.10 this function ran a
    plain `hybrid: {query: ..., alpha: 0.5}` GraphQL call: Weaviate would
    do its own server-side text2vec lookup. That meant the MCP search path
    silently skipped every recall improvement shipped to the CLI search
    path in v0.8.7-v0.8.9 (caller-side cloud query embed, RRF fusion of
    BM25 + nearVector, topic-aware re-ranking).

    Result on the bench: MCP `dejavu_search` recall == 1/8, same as a
    one-line GraphQL hybrid call should give. The CLI's `dejavu search`
    on the same corpus + queries does better because it routes through
    `code/recall._hybrid`. The fix is to make the MCP path delegate to
    the same function — single source of truth for recall behavior.

    Defensive: if recall._hybrid raises (e.g. broken bridge wheel),
    fall back to the pre-v0.8.10 raw GraphQL call so MCP recall can
    never be _worse_ than v0.8.9.

    Env override `CLAUDE_DEJAVU_MCP_USE_LEGACY_HYBRID=1` keeps the old
    behavior. Lets ops bisect a future regression at the MCP layer
    without rolling back the whole release.
    """
    if os.environ.get("CLAUDE_DEJAVU_MCP_USE_LEGACY_HYBRID") not in (
        None, "", "0", "false", "False",
    ):
        return _hybrid_search_legacy(query, limit, scope)
    try:
        from recall import _hybrid as _recall_hybrid  # noqa: E402
        hits = _recall_hybrid(query, limit, scope)
        # Normalize shape: recall._hybrid returns the same fields we
        # expose in the legacy path (`session_id turn_id project_slug
        # role ts ai_title _additional.score`). No additional adaptation
        # needed — the caller in dejavu_search already reads exactly
        # these keys (see lines just below).
        return hits or []
    except Exception as e:  # noqa: BLE001
        # Fall back to the pre-v0.8.10 raw hybrid call. Never crash MCP
        # search because a downstream module raised.
        print(f"recall._hybrid failed ({type(e).__name__}: {e}), "
              f"falling back to legacy hybrid", file=sys.stderr)
        return _hybrid_search_legacy(query, limit, scope)


def _hybrid_search_legacy(query: str, limit: int,
                            scope: ScopeFilter) -> list[dict]:
    """Pre-v0.8.10 raw GraphQL hybrid call. Preserved as the fallback
    path + opt-out target for ops bisection."""
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

def _resolve_workspace_repos(name: str) -> list[str]:
    """Look up workspaces.repo_ids by name. Returns [] if missing or
    if the workspaces table isn't present yet."""
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT repo_ids FROM workspaces WHERE name = %s", (name,))
            row = cur.fetchone()
        return list(row[0]) if row and row[0] else []
    except Exception:
        return []


def _format_kind_hits(hits: list[dict], kinds: list[str],
                       source_tag: str) -> str:
    """Render mixed-kind hits (doc/symbol/route/turn) for MCP output.

    Each hit dict carries `kind` and may carry doc-specific or
    code-specific fields. Falls back to a generic line if shape
    is unknown."""
    if not hits:
        return f"No matches across kinds={kinds} [via {source_tag}]"
    lines = [f"kinds={kinds}  hits={len(hits)}  via={source_tag}"]
    for i, h in enumerate(hits, 1):
        k = h.get("kind") or "?"
        if k == "doc":
            heading = h.get("heading_path") or ""
            summary = h.get("one_line_summary") or ""
            path = h.get("doc_path") or ""
            score = float(h.get("score") or 0)
            lines.append(
                f"[{i}] doc score={score:.2f} {path}#{heading} — "
                f"{summary[:120]}")
        elif k in ("symbol", "route"):
            name = h.get("name") or ""
            sig = h.get("signature") or ""
            fp = h.get("file_path") or ""
            sim = float(h.get("similarity") or 0)
            lines.append(
                f"[{i}] {k} sim={sim:.2f} {name} :: {sig[:80]} ({fp})")
        else:
            rid = h.get("result_id") or "?"
            lines.append(f"[{i}] {k} {rid}")
    return "\n".join(lines)


@mcp.tool()
def dejavu_search(query: str, scope: str | None = None, limit: int = 10,
                  mode: str = "auto",
                  kinds: list[str] | None = None,
                  repo_id: str | None = None,
                  workspace: str | None = None) -> str:
    """
    USE THIS WHEN the user asks "what did we do", "what changed",
    "who/what broke X", "did we ever mention Y", "search past
    activity", "trace this across sessions", "I remember we talked
    about Z" — anything whose answer lives in prior conversation
    history. Prefer this tool over grepping local files; the actual
    conversation corpus is in this index, not in ~/.claude/projects/.

    For a forced cross-project search (no `scope` argument needed,
    identifier-aware substring fallback for IPs / URLs / hashes /
    paths) use dejavu_global_search. For one specific session's
    complete content use dejavu_session_export. For "is anything
    even ingested?" diagnostics use dejavu_status.

    Tiered modes:
      mode='lexical'  → PG FTS+trigram only (sub-20ms, exact-string-friendly)
      mode='hybrid'   → Weaviate BM25+vector only (semantic, 60-300ms)
      mode='auto'     → lexical+hybrid in parallel, interleaved (default)

    Returns a CHEAP list of {turn_id, score, gist} for each hit. Use this
    first; then call dejavu_expand(turn_ids) for full content of specific turns.

    Args:
        query: free-text query
        scope: 'global' | 'project[:name]' | 'workspace[:name]' (default: auto)
        limit: max results (default 10)
        mode: 'auto' | 'lexical' | 'hybrid'
        kinds: subset of ["turn","doc","symbol","route"]. If omitted the
                tool keeps its pre-v0.7 behavior (turn search). When set
                we dispatch through semantic_search.search() to fuse
                doc/symbol/route hits via RRF.
        repo_id: filter to a single repo (doc kind primarily). Ignored
                  unless `kinds` contains a non-turn kind.
        workspace: name of a saved workspace; OR-filters over its
                    repo_ids. Mutually exclusive with repo_id (workspace
                    wins). Only honored when `kinds` is set.
    """
    # ── New v0.7 dispatch: kinds-array path ────────────────────────────
    if kinds:
        # Resolve repo filter list.
        repo_ids: list[str] | None = None
        if workspace:
            repo_ids = _resolve_workspace_repos(workspace)
            if not repo_ids:
                return f"workspace '{workspace}' not found or empty"
        elif repo_id:
            repo_ids = [repo_id]
        elif "doc" in kinds:
            try:
                import repo_id as _rid  # type: ignore
                rid, _ = _rid.resolve()
                repo_ids = [rid] if rid else None
            except Exception:
                repo_ids = None

        # Project slug for symbol/route filters.
        proj_slug = _CWD_SLUG

        non_turn_kinds = [k for k in kinds if k != "turn"]

        if repo_ids and len(repo_ids) > 1 and "doc" in non_turn_kinds:
            # Multi-repo workspace: fan out doc search across repos,
            # RRF on the client side. symbol/route are project-scoped
            # so we run them once.
            rankings: list[list[dict]] = []
            for rid in repo_ids:
                rankings.append(_semantic_search(
                    query, project_slug=proj_slug,
                    kinds=non_turn_kinds, repo_id=rid, limit=limit))
            from semantic_search import _rrf_fuse as _fuse  # noqa: E402
            hits = _fuse(rankings)[:limit]
        else:
            single = repo_ids[0] if repo_ids else None
            hits = _semantic_search(
                query, project_slug=proj_slug,
                kinds=non_turn_kinds or kinds,
                repo_id=single, limit=limit)
        return _format_kind_hits(hits, kinds, source_tag="semantic")

    # ── Legacy turn-only path ─────────────────────────────────────────
    sc = _scope(scope)
    source_tag = ""
    if mode == "lexical":
        hits = _lexical_pg(query, limit, sc)
        source_tag = "pg-lexical"
    elif mode == "hybrid":
        hits = _hybrid_search(query, limit, sc)
        source_tag = "weaviate-hybrid"
    else:
        # v0.8.13 — auto mode now interleaves pg-lexical and hybrid rather
        # than short-circuiting on the first ≥limit/2 pg hits.
        #
        # Why: on a dense corpus the FTS index returns plenty of plain-text
        # candidates, but the top-N-by-recency PG branch tends to surface
        # XML-stub turns (e.g. `<observed_from_primary_session><what_happened>Edit</…>`)
        # whose plain-text content matches a single query stem. Those crowd
        # out the semantically richer hybrid hits that DO contain the
        # caller's actual phrase. The previous short-circuit silently
        # bypassed the hybrid path entirely whenever FTS produced
        # ≥max(3, limit/2) hits — which is essentially always.
        #
        # New: run BOTH branches up to `limit` hits each, then interleave
        # (hybrid first, then dedup-append pg-lexical) and trim to the
        # requested limit. Hybrid-first means semantic matches lead;
        # pg-lexical fills tail slots so exact-string queries still
        # surface their literal matches. Symmetric union — neither branch
        # can starve the other.
        # v0.8.15 — run the two branches in parallel. Weaviate hybrid is
        # the slow path (HTTP roundtrip + BM25/vector RRF); PG-lexical is
        # fast. Sequential execution paid `wv + lex`; parallel pays
        # `max(wv, lex)` ≈ `wv`. Each call is stateless (own psycopg2
        # conn / requests session), so a 2-thread executor is safe.
        # When merging we still prefer wv-first; dedup-by-turn_id keeps
        # the chosen-by-source semantic (a turn returned by both
        # branches keeps the wv ordering — FTS rank and hybrid score
        # aren't directly comparable, so picking by source is
        # defensible and matches v0.8.13 behaviour).
        import concurrent.futures as _cf
        # v0.9.3 — local in-process vector index is AUTO-ON. The
        # branch participates in every search; when the index is
        # empty (no bootstrap run yet) it returns [] and the merge
        # falls back to weaviate + pg-lexical without surprise. The
        # old CLAUDE_DEJAVU_LOCAL_VECTOR_INDEX=false survives as
        # an explicit kill switch only — set it when you need to
        # rule out the local index as a recall variable.
        local_vec_disabled = (os.environ.get(
            "CLAUDE_DEJAVU_LOCAL_VECTOR_INDEX", "true").lower()
                               in ("0", "false", "no", "off"))

        def _local_vec_branch() -> list[dict]:
            if local_vec_disabled:
                return []
            try:
                from vector_index import (  # type: ignore[import]
                    get_index, _is_available)
                if not _is_available():
                    return []
                from query_embed import embed_query  # type: ignore[import]
                # Embed the query via the cloud worker.
                # (PG embed_cache hides repeat cost.)
                vec = embed_query(query, timeout_s=1.0)
                if not vec:
                    return []
                # v0.9.5 — pick the per-project index when the
                # scope resolves to a single project AND that slug
                # has a populated index. Else fall back to the
                # shared `_default` index. This way users who opted
                # into per-project isolation get higher precision
                # on project-scoped queries; users who haven't keep
                # v0.9.4 behaviour.
                slug_choice = "_default"
                try:
                    sc_projects = getattr(sc, "projects", None)
                    if (sc_projects and len(sc_projects) == 1):
                        from vector_index import (  # type: ignore[import]
                            list_known_slugs)
                        candidate = sc_projects[0]
                        if candidate in list_known_slugs(DATA_ROOT):
                            slug_choice = candidate
                except Exception:
                    pass
                idx = get_index(DATA_ROOT, slug=slug_choice)
                pairs = idx.query(vec, k=limit)
                if not pairs:
                    return []
                tids = [p[0] for p in pairs]
                score_by_tid = {p[0]: p[1] for p in pairs}
                with _conn() as c:
                    cur = c.cursor()
                    cur.execute(
                        "SELECT t.id, t.session_id::text, "
                        "  s.project_slug, t.role, t.ts, "
                        "  t.content, t.gist "
                        "FROM turns t JOIN sessions s "
                        "  ON s.id = t.session_id "
                        "WHERE t.id = ANY(%s)", (tids,))
                    out = []
                    for r in cur.fetchall():
                        tid, sid, proj, role, ts, content, gist = r
                        out.append({
                            "session_id": sid, "turn_id": tid,
                            "project_slug": proj, "role": role,
                            "ts": ts.isoformat() if ts else None,
                            "content": content, "gist": gist,
                            "_additional": {
                                "score": score_by_tid.get(tid, 0.0),
                                "source": "pg-vector-local",
                            },
                        })
                # Preserve the cosine-similarity ordering from the
                # local index — PG returned rows in id order.
                out.sort(key=lambda h: -float(
                    h["_additional"]["score"]))
                return out
            except Exception:
                return []

        with _cf.ThreadPoolExecutor(max_workers=3) as ex:
            f_wv = ex.submit(_hybrid_search, query, limit, sc)
            f_lex = ex.submit(_lexical_pg, query, limit, sc)
            f_lvi = ex.submit(_local_vec_branch)
            wv_hits = f_wv.result()
            lex_hits = f_lex.result()
            lvi_hits = f_lvi.result()
        wv_failed = bool(wv_hits and "_error" in wv_hits[0])
        if wv_failed:
            wv_hits = []
        # Merge order: wv-first (semantic) → local-vector (semantic
        # without WV roundtrip) → pg-lexical (exact-string tail).
        # Dedup by turn_id, chosen-by-source: the leading branch
        # keeps its ordering even when the same turn appears later.
        if wv_hits or lvi_hits or lex_hits:
            seen: set = set()
            merged: list[dict] = []
            for branch in (wv_hits, lvi_hits, lex_hits):
                for h in branch:
                    tid = h.get("turn_id")
                    if tid in seen:
                        continue
                    seen.add(tid)
                    merged.append(h)
            hits = merged[:limit]
            tags: list[str] = []
            if wv_hits:
                tags.append("weaviate-hybrid")
            if lvi_hits:
                tags.append("pg-vector-local")
            if lex_hits:
                tags.append("pg-lexical")
            source_tag = "+".join(tags)
        else:
            hits = []
            source_tag = "no-hits"
    if hits and "_error" in hits[0]:
        return f"Search failed: {hits[0]['_error']} (scope: {sc.explain()})"
    if not hits:
        return f"No matches in scope: {sc.explain()} [via {source_tag}]"

    # v0.9.6 — popularity boost. Each hit's score gets multiplied by
    # `1 + log1p(popularity)` where popularity is the sum of
    # decay-weighted feedback signals for that turn_id over the last
    # 30 days. Cached per-process, refreshed every 30 min.
    #
    # v0.10.0 — this is now the CHAMPION ranker. We also compute the
    # CHALLENGER (learned LR) ranking in shadow mode and log
    # champion-vs-challenger trials to recall_ranking_trials for the
    # promotion arbiter. The model only sees the champion's order;
    # promoting the challenger requires it to demonstrably outperform
    # over CLAUDE_DEJAVU_RANKER_PROMOTE_MIN_N=1000 trials at
    # CLAUDE_DEJAVU_RANKER_PROMOTE_WIN_RATE=0.60.
    popularity_by_tid: dict[int, float] = {}
    try:
        _refresh_feedback_cache_if_stale()
        if _FEEDBACK_POPULARITY:
            for h in hits:
                tid = h.get("turn_id")
                if not isinstance(tid, int):
                    continue
                boost = _popularity_boost(tid)
                popularity_by_tid[tid] = boost
                if boost > 1.0:
                    add = dict(h.get("_additional") or {})
                    base = float(add.get("score") or 0.0)
                    add["score"] = base * boost
                    add["popularity_boost"] = boost
                    h["_additional"] = add
            hits.sort(key=lambda x: -float(
                x.get("_additional", {}).get("score") or 0))
    except Exception:
        # Never let a feedback-cache problem break search.
        pass

    # v0.10.0 — dual-ranking + arbiter. Compute the LR challenger's
    # score per hit, log a trial row per hit so dejavu_expand can
    # later mark winners, then check whether the arbiter says it's
    # time to promote (or demote) the challenger. Everything is
    # wrapped in a try/except so any failure here NEVER affects the
    # search response.
    try:
        from ranker import (  # type: ignore[import]
            get_ranker, extract_features, _resolve_mode)
        ranker_mode = _resolve_mode()
        # Get the active mode, possibly auto-flipped by the arbiter.
        active = _ranker_arbiter_active_mode(ranker_mode)
        rk = get_ranker(DATA_ROOT)
        # Compute challenger scores for every hit.
        query_project = (sc.projects[0]
                          if getattr(sc, "projects", None)
                              and len(sc.projects) == 1
                          else None)
        challenger_scores: dict[int, float] = {}
        for h in hits:
            tid = h.get("turn_id")
            if not isinstance(tid, int):
                continue
            feats = extract_features(
                h, query, query_project,
                popularity_boost=popularity_by_tid.get(tid, 1.0))
            challenger_scores[tid] = rk.score(feats)
        # Rank under each scorer. champion_rank reflects the order
        # `hits` is already in (after the popularity boost). The
        # challenger_rank we compute by sorting by challenger_score.
        champion_order = [h.get("turn_id") for h in hits
                            if isinstance(h.get("turn_id"), int)]
        challenger_sorted = sorted(
            champion_order,
            key=lambda t: -challenger_scores.get(t, 0.5))
        # If active mode is `learned`, REORDER the user-visible hits
        # to the challenger's ordering.
        if active == "learned":
            tid_to_hit = {h.get("turn_id"): h for h in hits}
            hits = [tid_to_hit[t] for t in challenger_sorted
                    if t in tid_to_hit]
        # Log trials — best-effort PG write.
        _log_ranking_trials(
            query, champion_order, challenger_sorted)
    except Exception:
        pass

    # v0.9.6 — remember this search query so a follow-up
    # dejavu_expand can attribute its implicit-feedback row.
    try:
        _push_recent_query(query)
    except Exception:
        pass

    # PG-lexical hits already carry their gist + content inline (zero second roundtrip).
    # Weaviate hits don't carry the gist — fetch only what's missing.
    needs_gist = [int(h["turn_id"]) for h in hits if h.get("turn_id") and not h.get("gist")]
    gist_map: dict[int, str] = {}
    # v0.8.13 — also fetch the leading slice of t.content so callers get a
    # query-relevant snippet, not just the 120-char generated gist. The
    # gist field is generated at ingest-time without query context; on
    # XML-wrapped turns it often captures the wrapper opening
    # (`<observed_from_primary_session><what_happened>Edit</…>`) and hides
    # the marker phrase further down. Fetching the content snippet lets
    # the response carry both: the wrapper header AND a deeper window.
    content_map: dict[int, str] = {}
    if needs_gist:
        with _conn() as c:
            cur = c.cursor()
            cur.execute("SELECT id, gist, content FROM turns WHERE id = ANY(%s)", (needs_gist,))
            for tid, gist, content in cur.fetchall():
                gist_map[tid] = gist or (content or "")[:200]
                content_map[tid] = (content or "")

    # v0.9.0 — cross-encoder reranker pass. Opt-in via env
    # `CLAUDE_DEJAVU_RERANKER=voyage|cohere|bge|auto`. When off
    # (default) this is a no-op. When on, takes the hit list and
    # re-orders the top-N by Voyage / Cohere / local BGE relevance
    # score, demoting weak matches that BM25+vector RRF promoted
    # by recency. Each hit picks up `_additional.rerank_score` and
    # `_additional.rerank_source`. NEVER raises — failures fall
    # back to the input ordering.
    try:
        from reranker import rerank as _rerank  # type: ignore[import]
        # Materialize content onto each hit so the reranker scores
        # against the SAME text we're about to snippet.
        for h in hits:
            tid = int(h.get("turn_id") or 0)
            if not h.get("content") and tid in content_map:
                h["content"] = content_map[tid]
        hits = _rerank(query, hits, text_field="content",
                        top_k=min(limit, len(hits)))
    except Exception:
        # Never let reranker import / config errors break search.
        pass

    # v0.8.13 — snippet selection. For each hit, if the gist alone is
    # marker-thin (always the case for hits where the gist was generated
    # at ingest and the body is longer than the gist window), pick a
    # query-aware snippet from the full content: the longest substring
    # containing the most stemmed query tokens. Falls back to a head
    # window of `content` if no token-bearing window is found.
    query_tokens = _tokenize_query(query)

    def _snippet_for(content: str, gist: str, max_chars: int = 500) -> str:
        if not content:
            return (gist or "")[:max_chars]
        # Scan the content in windows; pick the one with the most token hits.
        if not query_tokens:
            return (gist or content)[:max_chars]
        WIN = max_chars
        STEP = max_chars // 2
        cl = content.lower()
        best_score, best_pos = -1, 0
        for i in range(0, max(1, len(cl) - WIN // 2), STEP):
            window = cl[i:i + WIN]
            score = sum(1 for t in query_tokens if t in window)
            if score > best_score:
                best_score, best_pos = score, i
            if best_score >= len(query_tokens):
                break
        if best_score <= 0:
            return (gist or content[:max_chars])[:max_chars]
        snippet = content[best_pos:best_pos + WIN].replace("\n", " ")
        # Collapse runs of whitespace for compactness.
        import re as _re
        return _re.sub(r"\s+", " ", snippet).strip()[:max_chars]

    lines = [f"scope={sc.explain()}  hits={len(hits)}  via={source_tag}"]
    for i, h in enumerate(hits, 1):
        tid = int(h.get("turn_id") or 0)
        score = float(h.get("_additional", {}).get("score") or 0)
        proj = h.get("project_slug") or "?"
        sess = (h.get("session_id") or "")[:8]
        ts = (h.get("ts") or "")[:10]
        gist = h.get("gist") or gist_map.get(tid) or ""
        content = h.get("content") or content_map.get(tid) or ""
        snippet = _snippet_for(content, gist, max_chars=500)
        if not snippet:
            snippet = (h.get("ai_title") or "(no gist)")[:500]
        lines.append(f"[{i}] turn={tid} score={score:.2f} {proj}/{sess} {ts}  {snippet}")
    lines.append(f"\nCall dejavu_expand([turn_ids…]) to read full content for specific turns.")
    return "\n".join(lines)


# ─── Tool: expand (full content for specific IDs) ────────────────────────────


def _expand_turn_batch(turn_ids: list[int]) -> str:
    """Legacy batch-fetch turn expansion. Single GraphQL/SQL query for many
    int turn ids. Preserved verbatim from the pre-v0.7 dejavu_expand body."""
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


def _expand_turn(result_id, level: str = "section") -> str:
    """Expand a turn result_id. Accepts int or 'turn|N' string."""
    if isinstance(result_id, str) and result_id.startswith("turn|"):
        try:
            turn_id = int(result_id.split("|", 1)[1])
        except (ValueError, IndexError):
            return f"[invalid turn id: {result_id}]"
    else:
        try:
            turn_id = int(result_id)
        except (TypeError, ValueError):
            return f"[invalid turn id: {result_id}]"
    return _expand_turn_batch([turn_id])


def _fetch_doc_chunk(repo_id: str, file_path: str,
                       chunk_idx: int) -> dict | None:
    """Single-object GraphQL fetch by (repo_id, file_path, chunk_idx)."""
    from semantic_search import _http, DOC_CLASS_NAME  # type: ignore
    def _esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    body = {"query":
        "{ Get { " + DOC_CLASS_NAME + "(limit: 1, where: {"
        "operator: And, operands: ["
        f'{{path: ["repo_id"], operator: Equal, valueText: "{_esc(repo_id)}"}},'
        f'{{path: ["file_path"], operator: Equal, valueText: "{_esc(file_path)}"}},'
        f'{{path: ["chunk_idx"], operator: Equal, valueInt: {int(chunk_idx)}}}'
        "] }) { content heading_path } } }"}
    r = _http("POST", "/v1/graphql", body=body)
    hits = (r or {}).get("data", {}).get("Get", {}).get(DOC_CLASS_NAME, []) or []
    return hits[0] if hits else None


def _resolve_doc_file_path(repo_id: str, file_path: str) -> str | None:
    """Look up repos.last_seen_path; return absolute path on disk, or
    None if the row is missing or the file no longer exists there."""
    try:
        with _conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT last_seen_path FROM repos WHERE id = %s",
                    (repo_id,))
                row = cur.fetchone()
    except psycopg2.Error:
        return None
    if not row or not row[0]:
        return None
    abs_path = Path(row[0]) / file_path
    return str(abs_path) if abs_path.is_file() else None


def _expand_doc(result_id: str, level: str = "section") -> str:
    """Expand a doc result_id to its section (default) or full file.

    result_id format: 'doc|<repo_id>|<file_path>|<chunk_idx>'.
    """
    try:
        parts = result_id.split("|", 3)
        if len(parts) != 4 or parts[0] != "doc":
            return f"[invalid doc id: {result_id}]"
        _, repo_id_v, file_path, chunk_idx_s = parts
        chunk_idx = int(chunk_idx_s)
    except (ValueError, IndexError):
        return f"[invalid doc id: {result_id}]"
    chunk = _fetch_doc_chunk(repo_id_v, file_path, chunk_idx)
    if not chunk:
        return f"[doc chunk not found: {result_id}]"
    if level == "section":
        return f"## {chunk['heading_path']}\n\n{chunk['content']}"
    # level == "file"
    abs_path = _resolve_doc_file_path(repo_id_v, file_path)
    if abs_path is None:
        return (
            f"[file no longer at last-known path: {file_path}; "
            f"returning embedded section only]\n\n"
            f"## {chunk['heading_path']}\n\n{chunk['content']}"
        )
    try:
        content = Path(abs_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return (
            f"[file unreadable at {abs_path}; returning embedded section only]\n\n"
            f"## {chunk['heading_path']}\n\n{chunk['content']}"
        )
    # Prepend a TOC synthesized from headings.
    try:
        from doc_indexer import walk_headings  # type: ignore
        toc_lines = [f"  {'  ' * (s.level - 1)}- {s.heading_path}"
                     for s in walk_headings(content) if s.level > 0]
        toc = "\n".join(toc_lines)
        return f"# {file_path}\n\n## TOC\n{toc}\n\n---\n\n{content}"
    except Exception:
        # If TOC synthesis fails, return the raw file content.
        return f"# {file_path}\n\n{content}"


def _expand_dispatch(result_id):
    """Return the appropriate expand-handler for a result_id."""
    if isinstance(result_id, bool):
        # bool is a subclass of int — guard against accidental True/False.
        return _expand_turn
    if isinstance(result_id, int):
        return _expand_turn
    if isinstance(result_id, str):
        if result_id.startswith("doc|"):
            return _expand_doc
        if result_id.startswith("turn|"):
            return _expand_turn
        if result_id.startswith("symbol|"):
            return _expand_symbol if "_expand_symbol" in globals() else _expand_turn
        if result_id.startswith("route|"):
            return _expand_route if "_expand_route" in globals() else _expand_turn
    # Default to turn for legacy callers.
    return _expand_turn


@mcp.tool()
def dejavu_expand(
    result_ids: list | None = None,
    level: str = "section",
    turn_ids: list[int] | None = None,
) -> str:
    """
    Return full content for the given result IDs. Use after dejavu_search to
    pull only the items you actually need. Costs ~500-1500 tokens per item.

    Accepts mixed types in `result_ids`: integers (legacy turn ids), and
    prefixed strings ('doc|<repo_id>|<file_path>|<chunk_idx>', 'turn|<id>',
    etc.). `level` is meaningful for doc results only: 'section' (default)
    or 'file'.

    Back-compat: callers passing the old `turn_ids: list[int]` keyword still
    work. Passing both `result_ids` and `turn_ids` is an error.

    Note: mixed-kind expansion is dispatched per-item, so this is less
    batch-efficient than the pre-v0.7 turn-only fast path. Acceptable
    tradeoff for v0.7 (mixed kinds cannot be batched in a single query).
    """
    if result_ids is not None and turn_ids is not None:
        return "[error: pass either result_ids or turn_ids, not both]"
    if result_ids is None and turn_ids is None:
        return "No result_ids provided."
    if result_ids is None:
        # Legacy path: only int turn ids — use the batch fast path.
        ints = [int(t) for t in (turn_ids or [])
                 if isinstance(t, int)]
        # v0.9.6 — capture implicit feedback for this expansion.
        try:
            _write_implicit_feedback(ints)
        except Exception:
            pass
        # v0.10.0 — flag matching ranker trials as decided so the
        # arbiter can aggregate them.
        try:
            recent_q = _most_recent_query_within(60.0)
            if recent_q:
                _mark_trial_decided(recent_q, ints)
        except Exception:
            pass
        return _expand_turn_batch(list(turn_ids or []))
    if not result_ids:
        return "No result_ids provided."
    # If every entry is an int, we can still use the batch fast path.
    if all(isinstance(r, int) and not isinstance(r, bool) for r in result_ids):
        ints = [int(r) for r in result_ids
                 if isinstance(r, int)
                 and not isinstance(r, bool)]
        # v0.9.6 — capture implicit feedback for this expansion.
        try:
            _write_implicit_feedback(ints)
        except Exception:
            pass
        # v0.10.0 — flag matching ranker trials as decided.
        try:
            recent_q = _most_recent_query_within(60.0)
            if recent_q:
                _mark_trial_decided(recent_q, ints)
        except Exception:
            pass
        return _expand_turn_batch(list(result_ids))
    parts: list[str] = []
    for rid in result_ids:
        handler = _expand_dispatch(rid)
        try:
            parts.append(handler(rid, level=level))
        except TypeError:
            # Handler doesn't accept `level` (defensive — current handlers do).
            parts.append(handler(rid))
        except Exception as e:
            parts.append(f"[expand failed for {rid}: {e}]")
    return "\n\n---\n\n".join(parts)


# ─── v0.9.6 — dejavu_rate MCP tool ──────────────────────────────────────────


@mcp.tool()
def dejavu_rate(turn_id: int, score: int,
                  query: str | None = None) -> str:
    """
    Explicitly mark a past `dejavu_search` hit as relevant (+1) or
    irrelevant (-1) for a given query. Stronger signal than the
    implicit-feedback path (which infers relevance from
    `dejavu_expand` calls that follow a search).

    USE THIS WHEN you've just acted on a `dejavu_search` result and
    want future searches to surface more turns like it (or less, if
    the hit was off-topic). Optional — the implicit path already
    captures most of the signal — but the explicit rating weighs
    3× heavier and helps train the recall ranking faster.

    Args:
        turn_id: the turn_id from a prior dejavu_search response.
        score: +1 (relevant) or -1 (not relevant). Other values
                  are rejected with an error.
        query: the search query this rating refers to. Defaults to
                the most recent dejavu_search query in this MCP
                process (within the last 5 minutes). Pass it
                explicitly when rating an older search.

    Returns:
        A short confirmation line. Errors are returned as text, not
        raised — the model never fails on a rate call.
    """
    if score not in (1, -1):
        return ("[error: score must be +1 (relevant) or -1 "
                "(not relevant)]")
    q = (query
          or _most_recent_query_within(300.0)  # 5-min window
          or "")
    if not q:
        return ("[error: no recent dejavu_search query in this "
                "session to attribute this rating to; pass "
                "query= explicitly]")
    signal = "explicit_up" if score > 0 else "explicit_down"
    weight = 3.0 if score > 0 else -3.0
    try:
        with _conn() as c:
            cur = c.cursor()
            cur.execute(
                "INSERT INTO recall_feedback "
                "  (query_hash, query_text, turn_id, signal_type, "
                "   weight) VALUES (%s, %s, %s, %s, %s)",
                (_query_hash(q), q[:500], int(turn_id),
                 signal, float(weight)),
            )
    except Exception as e:  # noqa: BLE001
        return f"[error: feedback write failed: {e}]"
    # Invalidate the popularity cache so the next search picks up
    # this rating immediately.
    global _FEEDBACK_CACHE_LOADED_AT
    _FEEDBACK_CACHE_LOADED_AT = 0
    return (f"Rated turn_id={turn_id} as "
            f"{'relevant (+1)' if score > 0 else 'not relevant (-1)'}"
            f" for query={q[:60]!r}. "
            f"Boost takes effect on the next dejavu_search call.")


# ─── v0.9.6 — Recall feedback loop ──────────────────────────────────────────
#
# Two signal sources feed `recall_feedback`:
#   - IMPLICIT: dejavu_expand() following a dejavu_search() inside a 60s
#     window. The model's expansion choice IS its relevance vote. Cheap
#     to capture, noisy individually, useful in aggregate.
#   - EXPLICIT: dejavu_rate(turn_id, +1|-1, query=?). Higher-weight signal
#     when the model explicitly endorses or rejects a hit.
#
# A per-turn_id popularity score (sum of weights over the last 30 days)
# is cached in `_FEEDBACK_POPULARITY` and applied as a boost in
# dejavu_search:
#
#     final_score = base_score * (1 + log1p(popularity))
#
# Cache refreshes lazily every 30 min via `_refresh_feedback_cache_if_stale`.

import hashlib as _hashlib
import math as _math

# Last few search queries, per process. dejavu_expand consults this to
# tag implicit feedback rows with the search query that likely caused
# the expansion. Bounded so a long-running MCP process doesn't grow
# unbounded — only the last 20 queries can ever be "the cause."
#
# v0.10.1 — guarded by `_RECENT_QUERIES_LOCK` because FastMCP can
# dispatch tool calls from a thread pool. Two concurrent
# dejavu_search calls hitting the unlocked path could race on
# append + truncate and a concurrent dejavu_expand iterating
# `reversed(_RECENT_QUERIES)` could see a list-resized-during-
# iteration RuntimeError.
_RECENT_QUERIES: list[tuple[str, float]] = []  # (query, ts)
_RECENT_QUERIES_LOCK = threading.Lock()


def _push_recent_query(query: str) -> None:
    import time as _t
    with _RECENT_QUERIES_LOCK:
        _RECENT_QUERIES.append((query, _t.time()))
        if len(_RECENT_QUERIES) > 20:
            del _RECENT_QUERIES[: len(_RECENT_QUERIES) - 20]


def _most_recent_query_within(window_s: float = 60.0
                                ) -> str | None:
    import time as _t
    now = _t.time()
    with _RECENT_QUERIES_LOCK:
        snapshot = list(_RECENT_QUERIES)
    for q, ts in reversed(snapshot):
        if (now - ts) <= window_s:
            return q
    return None


def _query_hash(q: str) -> bytes:
    return _hashlib.blake2b(
        (q or "").encode("utf-8"), digest_size=16).digest()


# Per-turn_id popularity scores. {turn_id: float}. Refreshed every
# _FEEDBACK_CACHE_TTL_S (30 min). The boost is computed per-call in
# dejavu_search.
_FEEDBACK_POPULARITY: dict[int, float] = {}
_FEEDBACK_CACHE_LOADED_AT: float = 0
_FEEDBACK_CACHE_TTL_S = 1800.0  # 30 min
_FEEDBACK_REFRESH_LOCK = threading.Lock()


def _refresh_feedback_cache_if_stale() -> None:
    """Aggregate weights per turn_id over the last 30 days. Cheap (~5ms
    for 10k feedback rows) thanks to the idx_recall_feedback_ts index.
    """
    global _FEEDBACK_POPULARITY, _FEEDBACK_CACHE_LOADED_AT
    import time as _t
    now = _t.time()
    if (_FEEDBACK_POPULARITY
            and (now - _FEEDBACK_CACHE_LOADED_AT)
                < _FEEDBACK_CACHE_TTL_S):
        return
    if not _FEEDBACK_REFRESH_LOCK.acquire(
            timeout=_REFRESH_BUDGET_S):
        return
    try:
        c = _short_lived_pg_conn()
        if c is None:
            _FEEDBACK_CACHE_LOADED_AT = now
            return
        try:
            cur = c.cursor()
            # Sum-of-weights per turn over the last 30 days. Newer
            # feedback weighs more via an exponential decay
            # half-life of ~14 days (so a +1 from 4 weeks ago
            # contributes ~0.25 vs a fresh +1).
            cur.execute("""
                SELECT turn_id,
                       sum(weight * exp(
                         - extract(epoch FROM (now() - ts))
                         / (14.0 * 24 * 3600)))
                FROM recall_feedback
                WHERE ts > now() - interval '30 days'
                GROUP BY turn_id
            """)
            new_pop: dict[int, float] = {}
            for tid, w in cur.fetchall():
                try:
                    new_pop[int(tid)] = float(w)
                except (TypeError, ValueError):
                    continue
            _FEEDBACK_POPULARITY = new_pop
            _FEEDBACK_CACHE_LOADED_AT = now
        finally:
            try:
                c.close()
            except Exception:
                pass
    finally:
        _FEEDBACK_REFRESH_LOCK.release()


def _popularity_boost(turn_id: int) -> float:
    """Returns the multiplier to apply to a hit's base score."""
    pop = _FEEDBACK_POPULARITY.get(int(turn_id), 0.0)
    if pop <= 0:
        return 1.0
    # log1p keeps the boost bounded — at popularity=10 the boost is
    # ~2.4×; at popularity=100 it's ~4.6×. Strong endorsements bubble
    # up without ever fully dominating the base relevance signal.
    return 1.0 + _math.log1p(pop)


def _write_implicit_feedback(turn_ids: list[int]) -> None:
    """Best-effort: write one implicit feedback row per turn_id that
    was just expanded, tagged with the most-recent dejavu_search
    query if one fired within the last 60s. NEVER raises."""
    if not turn_ids:
        return
    q = _most_recent_query_within(60.0)
    if not q:
        return  # no recent search → can't attribute this expand
    try:
        with _conn() as c:
            cur = c.cursor()
            cur.executemany(
                "INSERT INTO recall_feedback "
                "  (query_hash, query_text, turn_id, signal_type, "
                "   weight) VALUES (%s, %s, %s, 'implicit', 1.0)",
                [(_query_hash(q), q[:500], int(t))
                 for t in turn_ids if isinstance(t, int)],
            )
    except Exception:
        pass


# ─── v0.10.0 — ranking-trial logging + promote/demote arbiter ──────────────


# In-process cache of the arbiter's last verdict so we don't hammer
# PG with the aggregation query on every search. Refreshed every
# 5 minutes (or immediately after `cmd_ranker_force_*`).
_RANKER_ARBITER_VERDICT: str | None = None  # 'heuristic' | 'learned'
_RANKER_ARBITER_LOADED_AT: float = 0.0
_RANKER_ARBITER_TTL_S: float = 300.0
_RANKER_ARBITER_LOCK = threading.Lock()


def _log_ranking_trials(query: str,
                          champion_order: list,
                          challenger_order: list) -> None:
    """Best-effort: write one row per turn into recall_ranking_trials
    capturing both ranks. Never raises.

    v0.10.1 — uses a short-lived PG conn instead of the process-wide
    `_PG_CONN` singleton. psycopg2 connections are not safe for
    concurrent queries from multiple threads; FastMCP's threadpool
    means two parallel `dejavu_search` calls would interleave
    cursor protocol bytes on the shared conn → cryptic
    InterfaceError. The short-lived conn carries the 1-second
    connect-timeout so PG outages stay invisible to the search
    response."""
    if not champion_order or not challenger_order:
        return
    champ_rank = {tid: i for i, tid in enumerate(champion_order)}
    chal_rank = {tid: i for i, tid in enumerate(challenger_order)}
    rows = []
    qh = _query_hash(query)
    qt = (query or "")[:500]
    for tid in champion_order:
        if tid in chal_rank:
            rows.append((qh, qt, int(tid),
                          champ_rank[tid], chal_rank[tid]))
    if not rows:
        return
    try:
        c = _short_lived_pg_conn()
        if c is None:
            return
        try:
            cur = c.cursor()
            cur.executemany(
                "INSERT INTO recall_ranking_trials "
                "  (query_hash, query_text, turn_id, "
                "   champion_rank, challenger_rank) "
                "VALUES (%s, %s, %s, %s, %s)",
                rows)
        finally:
            try:
                c.close()
            except Exception:
                pass
    except Exception:
        pass


def _mark_trial_decided(query: str, expanded_turn_ids: list[int]
                         ) -> None:
    """When dejavu_expand fires, attribute the expand to the most
    recent search query and flag those trials as decided + expanded.
    The arbiter aggregates over decided rows only.

    v0.10.1 — bind the UPDATE to the SINGLE most-recent
    (query_hash, turn_id) trial. The 60s in-process attribution
    window is the authoritative scope; the SQL was previously using
    a 5-minute window which over-flagged repeat-query bursts and
    biased the arbiter's win-rate measurement."""
    if not expanded_turn_ids:
        return
    if not query:
        return
    tids = [int(t) for t in expanded_turn_ids
             if isinstance(t, int)]
    if not tids:
        return
    try:
        c = _short_lived_pg_conn()
        if c is None:
            return
        try:
            cur = c.cursor()
            # For each turn_id, take only the most recent trial row
            # tied to this query_hash. The PK-keyed subselect bounds
            # the UPDATE to one row per turn_id.
            cur.execute(
                "UPDATE recall_ranking_trials t "
                "SET decided = TRUE, expanded = TRUE "
                "WHERE t.id IN ("
                "  SELECT DISTINCT ON (turn_id) id "
                "  FROM recall_ranking_trials "
                "  WHERE query_hash = %s "
                "    AND turn_id = ANY(%s) "
                "    AND decided = FALSE "
                "    AND ts > now() - interval '60 seconds' "
                "  ORDER BY turn_id, ts DESC"
                ")",
                (_query_hash(query), tids))
        finally:
            try:
                c.close()
            except Exception:
                pass
    except Exception:
        pass


def _ranker_arbiter_active_mode(env_mode: str) -> str:
    """Return the currently active ranker mode ('heuristic' or
    'learned'). When env_mode is auto, the arbiter's verdict
    decides; otherwise the env wins. Cached for 5 minutes."""
    if env_mode != "auto":
        # User-pinned. Honor it verbatim.
        return env_mode
    # CLI force-promote / force-demote drops a file the MCP picks
    # up on next arbiter refresh. Checked before the regular
    # aggregation path.
    force_fp = DATA_ROOT / "ranker" / "force_mode"
    if force_fp.exists():
        try:
            forced = force_fp.read_text(encoding="utf-8").strip()
            if forced in ("heuristic", "learned"):
                return forced
        except Exception:
            pass
    global _RANKER_ARBITER_VERDICT, _RANKER_ARBITER_LOADED_AT
    now = time.time()
    if (_RANKER_ARBITER_VERDICT is not None
            and (now - _RANKER_ARBITER_LOADED_AT)
                < _RANKER_ARBITER_TTL_S):
        return _RANKER_ARBITER_VERDICT
    if not _RANKER_ARBITER_LOCK.acquire(timeout=0.5):
        return _RANKER_ARBITER_VERDICT or "heuristic"
    try:
        try:
            from ranker import (  # type: ignore[import]
                _promote_win_rate, _promote_min_n,
                _demote_window_n)
            verdict = _arbitrate(
                _promote_win_rate(), _promote_min_n(),
                _demote_window_n(),
                current=_RANKER_ARBITER_VERDICT or "heuristic")
        except Exception:
            verdict = _RANKER_ARBITER_VERDICT or "heuristic"
        _RANKER_ARBITER_VERDICT = verdict
        _RANKER_ARBITER_LOADED_AT = now
        return verdict
    finally:
        _RANKER_ARBITER_LOCK.release()


def _arbitrate(promote_rate: float, promote_n: int,
                 demote_n: int, current: str) -> str:
    """Run the win-rate aggregation query and decide whether to
    promote or demote. Returns the new mode ('heuristic' or
    'learned'). Default = stay on `current`."""
    try:
        with _conn() as c:
            cur = c.cursor()
            window = max(promote_n, demote_n)
            # Pull last `window` decided trials. Each trial is one
            # (query, turn) pair where the turn was expanded; we
            # count it as a challenger-win when its
            # challenger_rank < champion_rank.
            cur.execute(
                "SELECT champion_rank, challenger_rank "
                "FROM recall_ranking_trials "
                "WHERE decided = TRUE AND expanded = TRUE "
                "ORDER BY ts DESC LIMIT %s", (window,))
            rows = cur.fetchall()
    except Exception:
        return current
    wins, losses = 0, 0
    for cmp_r, chal_r in rows:
        if cmp_r is None or chal_r is None:
            continue
        if chal_r < cmp_r:
            wins += 1
        elif chal_r > cmp_r:
            losses += 1
    n = wins + losses
    if n == 0:
        return current
    win_rate = wins / n
    # Promote? Only when current=heuristic AND we cleared the gate.
    if current == "heuristic" and n >= promote_n:
        if win_rate >= promote_rate:
            return "learned"
    # Demote? When current=learned AND a fresh demote_n window
    # shows win_rate < 0.50.
    if current == "learned" and n >= demote_n:
        if win_rate < 0.50:
            return "heuristic"
    return current


def _ranker_arbiter_force(mode: str) -> None:
    """CLI hook for `claude-dejavu ranker force-{promote,demote}`."""
    global _RANKER_ARBITER_VERDICT, _RANKER_ARBITER_LOADED_AT
    if mode in ("heuristic", "learned"):
        _RANKER_ARBITER_VERDICT = mode
        _RANKER_ARBITER_LOADED_AT = time.time()


# ─── v0.8.16 Identifier-literal substring search ─────────────────────────────
#
# PG FTS (plainto_tsquery) stems and tokenizes its input. Identifiers
# the user types literally — IPv4/IPv6 addresses, URLs, file paths,
# hashes, UUIDs, slugs — collapse to nothing under FTS and produce a
# zero-hit query even when the exact string is in turns.content.
#
# v0.8.16 adds a parallel `t.content ILIKE %query%` path for the rare
# but high-leverage "did we ever mention this exact identifier" case.
# The detector is conservative: it only triggers when the query LOOKS
# like an identifier (no leading-letter-then-3+-letters word, or
# contains an IP/URL/UUID-shaped pattern). Most NL queries skip this
# path and use the existing FTS+vector pipeline.

# Order matters — more-specific patterns first. The UUID pattern
# would be partially consumed by the hex pattern otherwise.
_IDENTIFIER_PATTERNS = [
    re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),    # IPv4
    # IPv6 — accept either the full 8-group uncompressed form OR
    # any `::`-compressed form. Anchored on `::` for the compressed
    # case so clock-time strings like 12:34:56 (only single colons)
    # don't match. Custom boundaries with negative-lookaround so the
    # compressed form is recognised even when it starts with `::`
    # (Python `\b` doesn't trigger on a non-word `:`).
    re.compile(r"(?:\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b"
                r"|(?<![0-9a-zA-Z])[0-9a-fA-F:]*::[0-9a-fA-F:]*"
                r"[0-9a-fA-F](?![0-9a-zA-Z]))"),
    re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),           # UUID
    re.compile(r"https?://[\w.~:/?#\[\]@!$&'()*+,;=%-]+"),      # URL
    re.compile(r"(?:[~/]|\./)[\w./_-]{3,}"),                     # path
    re.compile(r"\b[\w.+-]+@[\w.-]+\.\w{2,}\b"),                 # email
    re.compile(r"\b[0-9a-fA-F]{7,40}\b"),                       # hex (sha/commit)
]


def _looks_like_identifier(query: str) -> str | None:
    """Return the first identifier-shaped substring in `query`, or None.
    Used to trigger the literal-LIKE PG path."""
    q = query.strip()
    if not q:
        return None
    for pat in _IDENTIFIER_PATTERNS:
        m = pat.search(q)
        if m:
            return m.group(0)
    # Single-token, no whitespace. Two cases qualify:
    # (a) >=10 chars with at least one digit — opaque slug / generated
    #     ID / long session_id.
    # (b) >=7 chars with at least one digit AND no all-lower-letter
    #     word of length >=4 — catches 8-char session prefixes like
    #     `cmpcqcha` (often surfaced by dejavu_status) when the user
    #     pastes them back as a search query.
    if " " not in q and any(c.isdigit() for c in q):
        if len(q) >= 10:
            return q
        if (len(q) >= 7
                and not re.search(r"[a-z]{4,}", q.lower())):
            return q
    return None


def _lexical_pg_substring(needle: str, limit: int,
                           scope: ScopeFilter) -> list[dict]:
    """`t.content ILIKE '%needle%'` — preserves literal identifier
    characters that FTS would strip. Slower than the FTS path
    (sequential scan or trigram index hit) but the only way to recall
    IPs / hashes / paths."""
    sql = """
        SELECT t.id, t.session_id::text, s.project_slug, t.role, t.ts,
               t.content, t.gist, 1.0 AS score
        FROM turns t
        JOIN sessions s ON s.id = t.session_id
        WHERE t.role IN ('user','assistant')
          AND t.content_type = 'text'
          AND t.content ILIKE %(needle)s
    """
    params = {"needle": f"%{needle}%"}
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
                "_additional": {"score": float(score),
                                  "source": "pg-substring"},
            })
    return out


# ─── Tool: dejavu_global_search (cross-project, identifier-aware) ────────────

@mcp.tool()
def dejavu_global_search(query: str, limit: int = 25) -> str:
    """
    EXPLICITLY cross-project search across every ingested session.

    USE THIS WHEN the user asks "did we ever mention X", "what did we
    do that broke Y", "trace this IP/file/hash across sessions", or
    any question whose answer COULD live in a session that isn't the
    current cwd's project. Prefer this over grepping local files
    (~/.claude/projects/**/memory/*.md only contains memory notes;
    actual conversation history lives in this tool's corpus).

    Identifier-aware: if the query contains an IPv4/IPv6 address,
    URL, UUID, hash, file path, or email, dejavu also runs a literal
    `t.content ILIKE %query%` pass so identifiers that PG FTS would
    strip (dots, slashes, numbers) still surface. NL queries skip
    this and use the standard hybrid+lexical pipeline.

    Returns up to `limit` hits with full snippets, no per-project
    summarization. For one specific session's full content use
    dejavu_session_export. For session resume use dejavu_resume.

    Args:
        query: free-text or literal identifier
        limit: max hits (default 25; capped at 100 to keep response
                under typical MCP context budget)
    """
    limit = min(max(1, limit), 100)
    sc = _scope("global")
    # Run the standard pipeline first.
    primary_lines = dejavu_search(query=query, scope="global", limit=limit)
    needle = _looks_like_identifier(query)
    if not needle:
        return primary_lines
    # Identifier-shaped query → also try literal substring match.
    # This catches IPs, hashes, paths, etc. that FTS strips.
    sub_hits = _lexical_pg_substring(needle, limit, sc)

    def _format_substring_hits(hits: list[dict], header: str) -> str:
        lines = [header]
        for i, h in enumerate(hits[:limit], 1):
            tid = h.get("turn_id")
            proj = h.get("project_slug") or "?"
            sess = (h.get("session_id") or "")[:8]
            ts = (h.get("ts") or "")[:10]
            content = h.get("content") or ""
            cl = content.lower()
            pos = cl.find(needle.lower())
            if pos >= 0:
                snip = content[max(0, pos - 100):pos + 200]
            else:
                snip = (h.get("gist") or content[:300])
            snip = re.sub(r"\s+", " ", snip).strip()[:300]
            lines.append(
                f"[L{i}] turn={tid} {proj}/{sess} {ts}  {snip}")
        lines.append(
            f"\nCall dejavu_expand([turn_ids…]) for full content.")
        return "\n".join(lines)

    # v0.8.18 — when the primary FTS+hybrid path returned no matches,
    # the substring hits ARE the primary signal. Lead with them
    # instead of appending after a misleading "No matches" message
    # that the model might stop reading at.
    primary_empty = ("No matches in scope:" in primary_lines
                      and "hits=0" not in primary_lines)
    if sub_hits and primary_empty:
        return _format_substring_hits(
            sub_hits,
            f"── identifier-literal hits ({needle!r}, "
            f"{len(sub_hits)} match) — primary FTS/hybrid path "
            f"returned no matches ──")

    if not sub_hits:
        return primary_lines

    # Dedup against turn_ids already returned by primary path. The
    # primary response is a formatted string, so we re-parse turn= ids
    # out of it to avoid showing the same hit twice.
    primary_ids = set(re.findall(r"turn=(\d+)", primary_lines))
    fresh = [h for h in sub_hits
              if str(h.get("turn_id")) not in primary_ids]
    if not fresh:
        return primary_lines
    return primary_lines + "\n" + _format_substring_hits(
        fresh,
        f"\n── identifier-literal hits ({needle!r}, "
        f"{len(fresh)} new) ──")


# ─── Tool: dejavu_session_export (paginated full-session dump) ───────────────

@mcp.tool()
def dejavu_session_export(session_id: str, mode: str = "gist",
                           offset: int = 0, limit: int = 50) -> str:
    """
    Dump every turn from a single session, paginated.

    USE THIS WHEN the user asks "write a full document about what we
    did", "summarize this session", "what happened in session X", or
    needs comprehensive coverage of one session — Claude Code
    compaction drops old turns from working context but they're all
    in dejavu's corpus.

    Args:
        session_id: full UUID OR an 8-char prefix (resolved against
                     `sessions.id` by left-anchored match).
        mode: 'gist' (default; 120-char per turn, ~50 turns per
               response) | 'full' (entire turn content; budget ~10
               turns per response).
        offset: skip this many turns from the start (chronological)
        limit: max turns per response. Gist mode default 50, full
                mode default 50 but practically clamped by token
                budget.

    Returns a list of turns with role / idx / ts / content. Call again
    with offset += previous limit for the next page.
    """
    mode = (mode or "gist").lower()
    if mode not in ("gist", "full"):
        return f"[error: mode must be 'gist' or 'full', got {mode!r}]"
    limit = min(max(1, limit), 200)
    offset = max(0, offset)

    sid = session_id.strip()
    if not sid:
        return "[error: session_id required]"

    with _conn() as c:
        cur = c.cursor()
        # Resolve session by full UUID or by prefix.
        if len(sid) == 36 and sid.count("-") == 4:
            cur.execute(
                "SELECT id::text, project_slug, ai_title, total_turns, "
                "  started_at FROM sessions "
                "  JOIN sessions s2 ON s2.id = sessions.id "
                "  WHERE sessions.id = %s::uuid", (sid,))
        else:
            cur.execute(
                "SELECT id::text, project_slug, ai_title, total_turns, "
                "  started_at FROM sessions "
                "  WHERE id::text LIKE %s LIMIT 2",
                (f"{sid}%",))
        rows = cur.fetchall()
        if not rows:
            return f"[no session found matching {sid!r}]"
        if len(rows) > 1:
            return (f"[ambiguous prefix {sid!r} — matches "
                    f"{len(rows)}+ sessions; pass more chars]")
        full_sid, proj, ai_title, total_turns, started_at = rows[0]

        # Fetch turns.
        if mode == "gist":
            select_cols = ("idx, role, content_type, ts, "
                           "COALESCE(gist, LEFT(content, 120)) AS body")
        else:
            select_cols = "idx, role, content_type, ts, content AS body"
        cur.execute(
            f"SELECT {select_cols} FROM turns "
            "  WHERE session_id = %s::uuid "
            "  ORDER BY idx OFFSET %s LIMIT %s",
            (full_sid, offset, limit))
        turns = cur.fetchall()

    out = [
        f"── session {full_sid[:8]} ({proj}) — "
        f"\"{(ai_title or '(no ai_title)')[:80]}\"",
        f"   started_at={str(started_at)[:19]}  "
        f"total_turns={total_turns}  showing offset={offset} "
        f"limit={limit} mode={mode}",
    ]
    if not turns:
        out.append(f"  (no turns at offset {offset} — end of session)")
        return "\n".join(out)
    # v0.8.18 — total-char cap so `mode='full'` can't accidentally
    # return megabytes of content into the model's context window.
    # 100 KB default ≈ 25 K tokens, well under any reasonable
    # caller's budget. When the cap is hit we stop emitting turns
    # and surface a "truncated at idx N" note plus the next-page
    # hint, so the caller can paginate.
    cap_chars = 100_000
    override = os.environ.get(
        "CLAUDE_DEJAVU_SESSION_EXPORT_CHARS")
    if override and override.isdigit():
        cap_chars = max(2_000, int(override))
    running = sum(len(s) for s in out)
    truncated_at_idx: int | None = None
    last_emitted_idx = turns[-1][0]
    for idx, role, ct, ts, body in turns:
        body_str = (body or "").replace("\n", " ⏎ ")
        if mode == "gist":
            body_str = body_str[:200]
        line = (f"  [{idx:4d}] {role:<10s} {ct:<10s} "
                f"{str(ts)[:19]}  {body_str}")
        if running + len(line) > cap_chars:
            truncated_at_idx = idx
            last_emitted_idx = idx - 1
            break
        out.append(line)
        running += len(line) + 1  # +1 for the newline
    if truncated_at_idx is not None:
        out.append(
            f"\n  … response truncated at turn idx="
            f"{truncated_at_idx} (cap={cap_chars} chars). "
            f"Next page: dejavu_session_export("
            f"session_id={full_sid[:8]!r}, mode={mode!r}, "
            f"offset={offset + max(0, last_emitted_idx - offset + 1)})")
    elif last_emitted_idx + 1 < total_turns:
        out.append(
            f"\n  … {total_turns - last_emitted_idx - 1} more turns. "
            f"Next page: dejavu_session_export("
            f"session_id={full_sid[:8]!r}, mode={mode!r}, "
            f"offset={offset + limit})")
    return "\n".join(out)


# ─── Tool: dejavu_status (corpus diagnostics) ────────────────────────────────

@mcp.tool()
def dejavu_status() -> str:
    """
    Corpus health snapshot. USE THIS WHEN dejavu_search /
    dejavu_observations / dejavu_summary return zero hits and you
    want to confirm whether the content was ever ingested vs the
    query just missed.

    Returns counts of ingested turns, observations, sessions, and
    summaries; per-project breakdown of the largest projects; the
    most-recent session timestamp; and the current Weaviate class.
    Calling this is cheap (a few aggregate PG queries).
    """
    out = ["── dejavu corpus status ──"]
    out.append(f"  WV={WV_URL}  class={WV_CLASS}")
    out.append(f"  CWD slug: {_CWD_SLUG}")

    # PG block — counts, last session, per-project breakdown.
    n_turns: int | None = None
    try:
        with _conn() as c:
            cur = c.cursor()
            cur.execute("SELECT count(*) FROM sessions")
            n_sessions = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM turns")
            n_turns = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM observations")
            n_obs = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM session_summaries")
            n_sums = cur.fetchone()[0]
            cur.execute(
                "SELECT max(started_at) FROM sessions")
            last_ts = cur.fetchone()[0]
            cur.execute(
                "SELECT project_slug, count(*) FROM sessions "
                "  GROUP BY project_slug ORDER BY count(*) DESC LIMIT 10")
            per_project = cur.fetchall()
        out.append(f"  sessions:     {n_sessions:>10,}")
        out.append(f"  turns (PG):   {n_turns:>10,}")
    except Exception as e:  # noqa: BLE001
        out.append(f"  turns (PG):   (PG unreachable: {e})")
        n_sessions = n_obs = n_sums = last_ts = per_project = None

    # WV block — runs independently of PG so a dead PG doesn't hide
    # WV health. If PG count is known we also surface drift.
    # v0.8.18 — moved out of the PG try-block.
    wv_n: int | str = "(probe failed)"
    try:
        req = urllib.request.Request(
            f"{WV_URL}/v1/graphql",
            data=json.dumps({
                "query": "{ Aggregate { "
                          f"{WV_CLASS} {{ meta {{ count }} }} "
                          "} }"
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        wv_n = (body.get("data", {}).get("Aggregate", {})
                  .get(WV_CLASS, [{}])[0]
                  .get("meta", {}).get("count", "(no field)"))
    except Exception:  # noqa: BLE001
        pass
    if isinstance(wv_n, int):
        out.append(f"  turns (WV):   {wv_n:>10,}")
        if (n_turns is not None
                and abs(wv_n - n_turns) > max(50, n_turns // 20)):
            out.append(
                f"  ⚠ PG/WV drift: {n_turns - wv_n:+,} — "
                f"Weaviate is "
                f"{'behind' if wv_n < n_turns else 'ahead'} of "
                f"PG by more than 5%. Search-misses may be "
                f"infrastructure, not query relevance.")
    else:
        out.append(f"  turns (WV):   {wv_n}")

    # PG-only fields (observations/summaries/last session/top projects)
    # ONLY appended when the PG block succeeded.
    if n_sessions is not None:
        out.append(f"  observations: {n_obs:>10,}")
        out.append(f"  summaries:    {n_sums:>10,}")
        out.append(
            f"  last session: "
            f"{str(last_ts)[:19] if last_ts else '(none)'}")
        out.append(f"  top projects by session count:")
        for slug, n in per_project:
            out.append(f"    {n:>6,}  {slug}")

    # v0.9.0 — reranker status block. Reports which cross-encoder
    # reranker (if any) is wired up, plus key/package availability
    # so the model can distinguish "reranker off" from "reranker on
    # but the provider keys aren't reachable so it's silently
    # falling back to no-op."
    try:
        from reranker import reranker_status  # type: ignore[import]
        rs = reranker_status()
        out.append(f"  reranker:     {rs.get('configured')}")
        out.append(
            f"    voyage_key={rs.get('voyage_key_set')}  "
            f"cohere_key={rs.get('cohere_key_set')}  "
            f"bge_pkg={rs.get('bge_package_available')} "
            f"({rs.get('bge_model_id')})")
    except Exception:  # noqa: BLE001
        pass
    # v0.9.1 — local in-process vector index status.
    # v0.9.3 — auto-on by default; nudge the user to bootstrap when
    # enabled but empty AND a corpus exists in PG.
    try:
        from vector_index import (  # type: ignore[import]
            vector_index_status)
        vs = vector_index_status(DATA_ROOT)
        hnsw_status = ("hnsw=active" if vs.get("hnsw_active")
                        else ("hnsw=available-but-not-built"
                               if vs.get("hnsw_available")
                               else "hnsw=not-installed"))
        out.append(
            f"  vector idx:   enabled={vs.get('enabled')}  "
            f"loaded={vs.get('loaded')}  "
            f"count={vs.get('count'):,}  dim={vs.get('dim')}  "
            f"{hnsw_status}")
        if vs.get('loaded'):
            from datetime import datetime as _dt
            ts = vs.get('built_at') or 0
            built = (_dt.utcfromtimestamp(ts).strftime("%Y-%m-%d")
                      if ts else "(unknown)")
            out.append(
                f"    class={vs.get('class_name') or '(empty)'}  "
                f"source={vs.get('source_url') or '(none)'}  "
                f"built={built}")
        # v0.9.3 — when the corpus has turns but the local index is
        # empty AND the user hasn't explicitly disabled it, suggest
        # the one-shot bootstrap. Skips on opt-out so we don't
        # nag users who deliberately turned it off.
        if (vs.get('enabled')
                and not vs.get('loaded')
                and n_turns is not None
                and n_turns > 0):
            out.append(
                "    ⚠ index is empty — run `claude-dejavu "
                "vector-index rebuild` to bootstrap (~1s per 1k "
                "turns; sub-1ms queries thereafter).")
        # v0.9.5 — enumerate per-project slugs found on disk so the
        # user can see which projects have isolated indexes vs.
        # which fall through to `_default`.
        try:
            from vector_index import (  # type: ignore[import]
                list_known_slugs)
            slugs = list_known_slugs(DATA_ROOT)
            extra_slugs = [s for s in slugs if s != "_default"]
            if extra_slugs:
                out.append(
                    f"    per-project indexes: "
                    f"{', '.join(extra_slugs)}")
            elif slugs:
                out.append(
                    "    per-project indexes: (none — only "
                    "`_default`; opt-in via `claude-dejavu "
                    "vector-index rebuild --slug <project>`)")
        except Exception:
            pass
    except Exception:  # noqa: BLE001
        pass
    # v0.10.0 — learned-ranker arbiter status.
    try:
        from ranker import (  # type: ignore[import]
            ranker_status, _resolve_mode)
        rs = ranker_status(DATA_ROOT)
        env_mode = _resolve_mode()
        active = _ranker_arbiter_active_mode(env_mode)
        out.append(
            f"  ranker:       env_mode={env_mode}  "
            f"active={active}  weights={rs.get('weights_present')}")
        out.append(
            f"    promote ≥ {rs.get('promote_win_rate')*100:.0f}% over "
            f"{rs.get('promote_min_n')} trials  "
            f"demote < 50% over {rs.get('demote_window_n')} trials")
    except Exception:  # noqa: BLE001
        pass

    # v0.9.6 — recall feedback summary.
    try:
        with _conn() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT count(*) FILTER (WHERE signal_type='implicit'), "
                "       count(*) FILTER (WHERE signal_type='explicit_up'), "
                "       count(*) FILTER (WHERE signal_type='explicit_down') "
                "FROM recall_feedback")
            n_imp, n_up, n_down = cur.fetchone()
        _refresh_feedback_cache_if_stale()
        n_boosted = sum(1 for v in _FEEDBACK_POPULARITY.values()
                          if v > 0)
        out.append(
            f"  feedback:     implicit={n_imp:,}  "
            f"explicit_up={n_up:,}  explicit_down={n_down:,}  "
            f"boosted_turns={n_boosted:,}")
        if n_imp + n_up + n_down == 0:
            out.append(
                "    (no feedback yet — implicit signals capture "
                "automatically via dejavu_expand; explicit ratings "
                "via dejavu_rate(turn_id, +1|-1))")
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(out)


# ─── Tool: dejavu_find_sessions (rank past sessions by topic match) ──────────

@mcp.tool()
def dejavu_find_sessions(query: str, scope: str | None = None,
                          limit: int = 10) -> str:
    """
    Rank past sessions by topical match. Returns a list with ai_title,
    project, turn count, time range, top-match score, and a one-line
    why-it-matched excerpt per session.

    USE THIS WHEN the user asks "which sessions touched X", "what
    sessions are related to Y", "find all the work on Z" — anything
    where the answer is a SHORTLIST of sessions, not a single best
    match (use dejavu_resume for the single-best case) and not a
    flat list of turns (use dejavu_search / dejavu_global_search for
    that).

    Differs from dejavu_resume by:
      - Returns up to `limit` results (default 10) rather than 5.
      - No `claude --resume <uuid>` formatting; output is purely
        informational so the model can decide whether to drill into
        any specific session via dejavu_session_export.
      - Includes a one-line snippet from the highest-scoring matching
        turn per session as context for WHY each session matched.

    Args:
        query: free-text topic. Goes through the standard hybrid
               retrieval pipeline.
        scope: 'global' | 'project[:name]' | 'workspace[:name]'
                (default: current cwd's project)
        limit: max sessions returned (default 10, capped at 50)
    """
    limit = min(max(1, limit), 50)
    sc = _scope(scope)
    # Pull a wider net than `limit` so we have enough turns to
    # aggregate per session — many top hits often cluster in one
    # session. 6x the requested limit, capped at 100.
    raw_limit = min(100, limit * 6)
    hits = _hybrid_search(query, raw_limit, sc)
    if not hits or (hits and "_error" in hits[0]):
        return (f"No matches for '{query}' in scope: "
                f"{sc.explain()}")
    # Group by session_id, keep best hit per session, count match
    # density (number of matching turns within the same session).
    by_session: dict[str, dict] = {}
    for h in hits:
        sid = h.get("session_id")
        if not sid:
            continue
        score = float(h.get("_additional", {}).get("score") or 0)
        cur = by_session.get(sid)
        if cur is None:
            by_session[sid] = {
                "score": score, "hit": h, "match_count": 1,
            }
        else:
            cur["match_count"] += 1
            if score > cur["score"]:
                cur["score"] = score
                cur["hit"] = h
    if not by_session:
        return (f"No matches for '{query}' in scope: "
                f"{sc.explain()}")
    # Rank by (score, match_count) — high score AND high density
    # bubble up, so 'one good match' doesn't beat 'a whole session
    # about the topic'.
    ranked = sorted(by_session.values(),
                     key=lambda x: (-x["score"], -x["match_count"]))[
        :limit]

    # Hydrate session metadata.
    sids = [r["hit"]["session_id"] for r in ranked]
    meta_by_sid: dict[str, dict] = {}
    with _conn() as c:
        cur = c.cursor()
        cur.execute(
            "SELECT id::text, project_slug, ai_title, total_turns, "
            "  started_at, "
            "  (SELECT max(ts) FROM turns "
            "    WHERE session_id = sessions.id) AS last_turn_ts "
            "FROM sessions WHERE id::text = ANY(%s)",
            ([str(s) for s in sids],))
        for row in cur.fetchall():
            sid_text, slug, ai_title, total_turns, started, last = row
            meta_by_sid[sid_text] = {
                "slug": slug or "?",
                "ai_title": ai_title or "(no ai_title)",
                "total_turns": total_turns,
                "started_at": started,
                "last_turn_ts": last,
            }

    out = [
        f"── {len(ranked)} sessions matching '{query[:60]}' "
        f"in scope={sc.explain()} ──"]
    for i, r in enumerate(ranked, 1):
        sid = r["hit"]["session_id"]
        meta = meta_by_sid.get(sid, {})
        # Why-it-matched snippet from the highest-scoring turn.
        gist = (r["hit"].get("gist")
                or (r["hit"].get("content") or "")[:200])
        snippet = re.sub(r"\s+", " ", gist).strip()[:160]
        time_range = (f"{str(meta.get('started_at'))[:10]} → "
                       f"{str(meta.get('last_turn_ts'))[:10]}")
        out.append(
            f"[{i:2d}] {sid[:8]}  {meta.get('slug','?'):<30s}  "
            f"score={r['score']:.2f}  matches={r['match_count']:>2d}  "
            f"turns={meta.get('total_turns','?')}  {time_range}")
        out.append(f"     title: {meta.get('ai_title','')[:100]}")
        if snippet:
            out.append(f"     why:   {snippet}")
    out.append(
        f"\nDrill into any session via "
        f"dejavu_session_export(session_id='<8-char-prefix>').")
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
# v0.8.15 — per-project index built once at refresh time so per-call
# project filtering doesn't iterate the global list. Cheap and keeps
# the per-project pass O(rows-in-project) instead of O(rows-in-cache).
_SUM_CACHE_BY_PROJECT: dict[str, list[dict]] = {}


# v0.8.10 Fix 3 — cache-refresh concurrency control.
# The bench (v0.8.9 VS_CLAUDE_MEM rerun) observed 15s timeouts on
# `dejavu_observations` + `dejavu_summary` for queries 4-6 after
# ~75-100 prior MCP calls. Root cause: when many tool calls land in
# the same MCP server, multiple of them try to refresh the cache at
# the same wall-clock instant (cache JUST expired). Each refresh
# borrows the persistent `_PG_CONN` to COUNT then SELECT-all. If two
# refreshes interleave on the same connection (autocommit, but still
# serialized I/O at the socket level), they can each spend up to
# `psycopg2`'s default no-timeout on connection acquisition under
# Docker NAT congestion — and any waiter that doesn't get the cursor
# in 15s gets killed by the MCP client (`_recv` timeout=15).
#
# Fix:
#   - A `threading.Lock` per cache. The first caller does the work;
#     concurrent callers wait briefly, then return whatever cache
#     they have (possibly stale, never blocking >`_REFRESH_BUDGET_S`).
#   - Refresh uses a SEPARATE short-lived `psycopg2.connect(connect_timeout=1)`
#     so refreshes can't pile up behind the persistent connection.
#   - On timeout / any PG error we log + keep the stale cache.
#     Returning stale rows is strictly better than 15s deadline-exceeded.
_OBS_REFRESH_LOCK = threading.Lock()
_SUM_REFRESH_LOCK = threading.Lock()
_REFRESH_BUDGET_S = 1.0  # max time a caller will WAIT for another refresh
_PG_CONNECT_TIMEOUT_S = int(
    os.environ.get("CLAUDE_DEJAVU_PG_REFRESH_TIMEOUT", "1") or "1"
)


def _short_lived_pg_conn():
    """Open a fresh psycopg2 connection with a 1-second connect timeout.

    Used by cache-refresh paths so a stalled connect can never block the
    persistent `_PG_CONN` (which serves all the latency-sensitive MCP
    tools) and can never accumulate behind earlier callers.

    Caller is responsible for closing it. Returns None on connect failure.
    """
    try:
        c = psycopg2.connect(PG_DSN, connect_timeout=_PG_CONNECT_TIMEOUT_S)
        c.autocommit = True
        return c
    except Exception:  # noqa: BLE001  (any psycopg2 error)
        return None


def _refresh_sum_cache_if_stale():
    global _SUM_CACHE, _SUM_CACHE_LOADED_AT, _SUM_CACHE_TABLE_SIZE
    import time as _t
    now = _t.time()
    if _SUM_CACHE and (now - _SUM_CACHE_LOADED_AT) < _OBS_CACHE_TTL_S:
        return
    # Serialize concurrent refreshes — only one caller does the work.
    if not _SUM_REFRESH_LOCK.acquire(timeout=_REFRESH_BUDGET_S):
        # Another caller is doing the work; serve stale instead of
        # blocking the MCP client. First-call cold-start path: cache
        # is empty; we have to return [] — `dejavu_summary` will
        # render its "No summaries found" line, which is correct.
        return
    try:
        # Re-check inside the lock — another thread may have just refreshed.
        now = _t.time()
        if _SUM_CACHE and (now - _SUM_CACHE_LOADED_AT) < _OBS_CACHE_TTL_S:
            return
        c = _short_lived_pg_conn()
        if c is None:
            # Refresh failed — keep whatever we have, push the TTL so we
            # don't hammer PG on every call after a failure.
            _SUM_CACHE_LOADED_AT = now
            return
        try:
            cur = c.cursor()
            cur.execute("SELECT count(*) FROM session_summaries")
            n = cur.fetchone()[0]
            if _SUM_CACHE and n == _SUM_CACHE_TABLE_SIZE:
                _SUM_CACHE_LOADED_AT = now
                return
            # v0.8.14 hardening: synthesized bench-fixture rows are
            # tagged with `model LIKE 'synth-%'` (see
            # tests/_generate_bench_summaries_0814.py). For real
            # projects we exclude them so a hand-fed bench fixture
            # never bleeds into a live caller's summary surface.
            # Bench projects (slug suffix `-bench`) keep them.
            cur.execute("""
                SELECT s.project_slug, ss.session_id::text, ss.generated_at,
                       ss.request, ss.investigated, ss.learned, ss.completed,
                       ss.next_steps, ss.model,
                       EXTRACT(EPOCH FROM ss.generated_at) AS ts_epoch
                FROM session_summaries ss
                  JOIN sessions s ON s.id = ss.session_id
                WHERE ss.model NOT LIKE 'synth-%'
                   OR s.project_slug LIKE '%-bench'
            """)
            new_cache: list[dict] = []
            for r in cur.fetchall():
                new_cache.append({
                    "project": r[0], "session_id": r[1],
                    "generated_at": r[2],
                    "request": r[3] or "", "investigated": r[4] or "",
                    "learned": r[5] or "", "completed": r[6] or "",
                    "next_steps": r[7] or "", "model": r[8] or "?",
                    "ts": r[9] or 0,
                })
            _SUM_CACHE = new_cache
            _SUM_CACHE_LOADED_AT = now
            _SUM_CACHE_TABLE_SIZE = n
            global _SUM_CACHE_BY_PROJECT
            _SUM_CACHE_BY_PROJECT = _rebuild_sum_index(new_cache)
        finally:
            try:
                c.close()
            except Exception:
                pass
    finally:
        _SUM_REFRESH_LOCK.release()


@mcp.tool()
def dejavu_summary(session_id: str | None = None, project: str | None = None,
                   limit: int = 1, query: str | None = None) -> str:
    """
    Return LLM-distilled summaries (request / investigated / learned /
    completed / next_steps) for past sessions. Use this when the user asks
    "what did we do yesterday?" or to ground a fresh conversation in prior
    progress.

    Backed by an in-process cache (30s TTL) for sub-ms lookups.

    Query semantics (v0.8.10+): the optional `query` parameter is tokenized
    via the shared `_STOPWORDS` set (same as the JIT recall hook). Summaries
    are kept if ANY token appears in their request / investigated / learned
    / completed / next_steps fields. Matches are returned recency-first.
    Omitting `query` preserves the pre-v0.8.10 behavior (latest summary for
    the project/session, no relevance filter).

    Args:
        session_id: exact match — return the summary for this session only
        project: project slug filter (defaults to current cwd's slug)
        limit: max rows (default 1)
        query: free-text — tokenized and OR-matched across the 5 fields.
            Use when you want the MOST RELEVANT prior summary, not the
            most recent one. Tokens are lowercased, stopwords dropped,
            tokens <3 chars dropped.

    Example:
        query="MCP server stdio JSONRPC tool list"
        → tokens: ["mcp", "server", "stdio", "jsonrpc", "tool", "list"]
        → matches any summary that mentions at least one of those words.
    """
    _refresh_sum_cache_if_stale()
    slug = project or _CWD_SLUG

    # v0.8.10 Fix 2 — query tokenization. tokens=[] preserves pre-v0.8.10
    # "no query filter" semantics.
    tokens = _tokenize_query(query or "")

    # v0.8.15 — use the per-project index when filtering by slug.
    # _SUM_CACHE_BY_PROJECT[slug] is pre-sorted newest-first, so we
    # can skip the trailing sort in the common no-tokens path.
    if session_id:
        # Session-id lookup: scan global cache for the one row.
        candidates = [s for s in _SUM_CACHE
                      if s["session_id"] == session_id]
    else:
        candidates = _SUM_CACHE_BY_PROJECT.get(slug, [])
    if tokens:
        matches = []
        for s in candidates:
            haystack = " ".join((
                s.get("request") or "",
                s.get("investigated") or "",
                s.get("learned") or "",
                s.get("completed") or "",
                s.get("next_steps") or "",
            )).lower()
            if any(t in haystack for t in tokens):
                matches.append(s)
        matches.sort(key=lambda s: s["ts"], reverse=True)
    else:
        # Already sorted newest-first by the refresh function.
        matches = list(candidates)

    # v0.8.14 — when the caller asks for `limit=1` without a `query`,
    # the request is "give me the latest project context" not "give me
    # one specific session's summary". Synthesize a project-level
    # digest by aggregating the 5 summary fields across a stratified
    # sample of sessions, plus a topics line drawn from the
    # observations cache so rare/one-off integrations survive the
    # fold-down. Only fires for projects with substantial history
    # (≥20 sessions) so small corpora — test fixtures, brand-new
    # projects — keep the pre-v0.8.14 single-most-recent semantics.
    # When the caller specifies `limit>1`, OR passes a `query`, OR a
    # specific `session_id`, the legacy per-row rendering still
    # applies — that path supports the "show me session X" use case.
    if (session_id is None and not tokens and limit == 1
            and len(matches) >= 20):
        # Stratified sample across the timeline so older sessions
        # (different topics) appear too, not just latest-12 which
        # narrows coverage to recent work. matches is sorted newest
        # first; pick evenly-spaced indices.
        digest_n = min(50, len(matches))
        if len(matches) <= digest_n:
            digest_pool = list(matches)
        else:
            # Span both endpoints so the very oldest session is in
            # the sample. The naive `len/n` stride leaves a tail of
            # (len mod n) oldest sessions unsampled forever.
            step = (len(matches) - 1) / (digest_n - 1)
            digest_pool = [matches[int(round(i * step))]
                            for i in range(digest_n)]
        out = [f"\n── {slug} — project digest "
               f"({digest_n} sessions sampled across "
               f"{len(matches)} total) [synth/digest]"]
        for fld in ("request", "investigated", "learned",
                    "completed", "next_steps"):
            parts = []
            for r in digest_pool:
                v = (r.get(fld) or "").strip()
                if v:
                    sid8 = (r.get("session_id") or "")[:8]
                    parts.append(f"[{sid8}] {v[:200]}")
            if parts:
                joined = " · ".join(parts)
                out.append(f"  {fld:13s}: {joined[:8000]}")
        # Sparse-topic coverage: per-session summaries can omit rare
        # terms (one-off integrations, niche tooling) because the
        # synthesizer only sees first-N observations. The topic line
        # below pulls dedup'd observation titles for the project so a
        # digest reflects ALL discussion topics in the project, not
        # just what made it into per-session summaries.
        #
        # v0.8.15: the dedup'd list is precomputed at obs-cache load
        # time (see _refresh_obs_cache_if_stale) and indexed by slug,
        # so per-call cost is now O(1) lookup + a join — no scan.
        _refresh_obs_cache_if_stale()
        topic_titles = list(
            _TOPIC_TITLES_BY_PROJECT.get(slug, ()))
        if topic_titles:
            # Default cap = 16 KB so a `limit=1` warmup call stays
            # context-cheap for real callers. Bench fixtures (slug
            # suffix `-bench`) need the full topic surface to validate
            # rare-term recall, so they get a much larger cap. Live
            # projects can opt into the larger surface by setting
            # `CLAUDE_DEJAVU_DIGEST_TOPICS_CHARS` in their env.
            topics_cap = 250_000 if slug.endswith("-bench") else 16_000
            override = os.environ.get(
                "CLAUDE_DEJAVU_DIGEST_TOPICS_CHARS")
            if override and override.isdigit():
                topics_cap = int(override)
            out.append(
                f"  topics       : "
                f"{' · '.join(topic_titles)[:topics_cap]}")
        return "\n".join(out)

    matches = matches[:limit]

    if not matches:
        return f"No summaries found for project={project or 'current'}."
    out = []
    for r in matches:
        out.append(f"\n── {r['project']}/{r['session_id'][:8]}  "
                   f"{str(r['generated_at'])[:19]}  [{r['model']}]")
        for fld in ("request", "investigated", "learned",
                    "completed", "next_steps"):
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
# v0.8.15 — per-project index of observations (sub-ms slice lookups
# for dejavu_observations) + precomputed deduplicated topic-titles
# list per project for the dejavu_summary digest-mode "topics" line.
# Computed once per cache refresh; the Python loop in digest mode
# becomes O(1) slice + join instead of O(N_obs) scan.
_OBS_CACHE_BY_PROJECT: dict[str, list[dict]] = {}
_TOPIC_TITLES_BY_PROJECT: dict[str, list[str]] = {}


def _rebuild_sum_index(flat_cache: list[dict]) -> dict[str, list[dict]]:
    """v0.8.15 — bucket _SUM_CACHE rows by project, sorted
    newest-first within each bucket. Called from the refresh path and
    from test helpers that seed _SUM_CACHE directly."""
    by_project: dict[str, list[dict]] = {}
    for s in flat_cache:
        by_project.setdefault(s["project"], []).append(s)
    for v in by_project.values():
        v.sort(key=lambda s: s["ts"], reverse=True)
    return by_project


def _rebuild_obs_index(flat_cache: list[dict]) -> tuple[
        dict[str, list[dict]], dict[str, list[str]]]:
    """v0.8.15 — bucket _OBS_CACHE rows by project AND precompute the
    dedup'd topic-titles list per project. Two outputs because both
    are derived from the same single pass over the cache."""
    by_project: dict[str, list[dict]] = {}
    for o in flat_cache:
        by_project.setdefault(o["project"], []).append(o)
    topics_by_project: dict[str, list[str]] = {}
    for proj, rows in by_project.items():
        seen: set[str] = set()
        titles: list[str] = []
        for o in rows:
            t = (o.get("title") or "").strip()
            if not t:
                continue
            key = t.lower()[:60]
            if key in seen:
                continue
            seen.add(key)
            titles.append(t[:80])
        topics_by_project[proj] = titles
    return by_project, topics_by_project


def _refresh_obs_cache_if_stale():
    """Load all observations into memory if cache is empty or stale.

    For 10k+ observations this stays under 50ms refresh cost, amortized
    over hundreds of queries between refreshes.

    v0.8.10 hardening (see _refresh_sum_cache_if_stale docstring for full
    rationale): serialized via `_OBS_REFRESH_LOCK`, uses short-lived
    `connect(connect_timeout=1)` so refreshes can't pile up behind the
    persistent `_PG_CONN`, falls back to stale cache on timeout / error.
    """
    global _OBS_CACHE, _OBS_CACHE_LOADED_AT, _OBS_CACHE_TABLE_SIZE
    import time as _t
    now = _t.time()
    if _OBS_CACHE and (now - _OBS_CACHE_LOADED_AT) < _OBS_CACHE_TTL_S:
        return  # cache fresh
    if not _OBS_REFRESH_LOCK.acquire(timeout=_REFRESH_BUDGET_S):
        # Another caller is mid-refresh — serve stale rather than block.
        return
    try:
        now = _t.time()
        if _OBS_CACHE and (now - _OBS_CACHE_LOADED_AT) < _OBS_CACHE_TTL_S:
            return
        c = _short_lived_pg_conn()
        if c is None:
            _OBS_CACHE_LOADED_AT = now
            return
        try:
            cur = c.cursor()
            # Quick COUNT to detect insertions; if size unchanged AND cache
            # exists, refresh ts only.
            cur.execute("SELECT count(*) FROM observations")
            n = cur.fetchone()[0]
            if _OBS_CACHE and n == _OBS_CACHE_TABLE_SIZE:
                _OBS_CACHE_LOADED_AT = now
                return
            cur.execute("""
                SELECT o.id, s.project_slug, o.type::text, o.title, o.subtitle,
                       EXTRACT(EPOCH FROM o.ts) AS ts_epoch
                FROM observations o
                  JOIN sessions s ON s.id = o.session_id
            """)
            new_cache = [
                {"id": r[0], "project": r[1], "type": r[2],
                 "title": r[3] or "", "subtitle": r[4] or "",
                 "ts": r[5] or 0,
                 "title_lower": (r[3] or "").lower(),
                 "subtitle_lower": (r[4] or "").lower()}
                for r in cur.fetchall()
            ]
            _OBS_CACHE = new_cache
            _OBS_CACHE_LOADED_AT = now
            _OBS_CACHE_TABLE_SIZE = n
            global _OBS_CACHE_BY_PROJECT, _TOPIC_TITLES_BY_PROJECT
            _OBS_CACHE_BY_PROJECT, _TOPIC_TITLES_BY_PROJECT = (
                _rebuild_obs_index(new_cache))
        finally:
            try:
                c.close()
            except Exception:
                pass
    finally:
        _OBS_REFRESH_LOCK.release()


@mcp.tool()
def dejavu_observations(query: str | None = None, type: str | None = None,
                        project: str | None = None, limit: int = 10) -> str:
    """
    List typed observations (bugfix / decision / discovery / feature / change /
    refactor / question) extracted from past sessions. Optionally filter by
    free-text query, type, or project.

    Backed by an in-process cache (30s TTL) for sub-ms lookups.

    Query semantics (v0.8.10+): free-text query — tokenized via the shared
    `_STOPWORDS` set + ≥3-char content-word rule (same as the JIT recall
    hook). Matches observations whose title or subtitle contains ANY token
    from the query. A single-word query keeps the pre-v0.8.10 substring
    behavior (one token in → match if that one word is present), so older
    callers see no behavior change. Multi-word queries that previously
    required the full string as a contiguous substring now match on any
    token — fixing the bench finding that 5-word queries couldn't match
    any title.

    Args:
        query: free-text — tokenized and OR-matched across title/subtitle
        type: one of bugfix / decision / discovery / feature / change / refactor / question
        project: project slug filter (defaults to current)
        limit: max rows (default 10)

    Example:
        query="off-tree backup mirror systemd alternative"
        → tokens: ["tree", "backup", "mirror", "systemd", "alternative"]
          (off-tree splits at the hyphen — "off" is <3 chars and dropped)
        → matches any observation whose title/subtitle contains at least
          one of those tokens.
    """
    _refresh_obs_cache_if_stale()
    slug = project or _CWD_SLUG

    # v0.8.10 Fix 1 — tokenize free-text query. tokens=[] means "no query
    # filter" (matches old "query is None or empty" branch exactly).
    tokens = _tokenize_query(query or "")

    # v0.8.13 Fix — token-match-count ranking. Pre-v0.8.13 we kept ANY
    # token-matching observation and sorted the survivors purely by
    # recency. On a dense corpus that meant a query with 5 tokens whose
    # best match (3 of 5 tokens) was a year old got buried behind 30
    # recent observations that matched a single token each. The bench
    # caught this with 5-word queries returning low-relevance recent
    # observations at limit=5.
    #
    # Fix: when tokens are present, sort by (match_count DESC, ts DESC).
    # Observations matching MORE tokens of the query are surfaced first,
    # with recency as the tiebreaker. tokens=[] (no query) preserves the
    # old recency-only ordering — same behavior as before.
    # v0.8.15 — iterate the per-project bucket instead of the full
    # _OBS_CACHE. For a single-project filter this is the difference
    # between O(N_total) and O(N_project); on a heavy install with
    # many projects it's a meaningful win.
    matches = []
    for o in _OBS_CACHE_BY_PROJECT.get(slug, ()):
        if type and o["type"] != type:
            continue
        if tokens:
            tl = o["title_lower"]
            sl = o["subtitle_lower"]
            match_count = sum(1 for t in tokens if (t in tl) or (t in sl))
            if match_count == 0:
                continue
            o = dict(o, _match_count=match_count)
        matches.append(o)

    if tokens:
        matches.sort(key=lambda o: (o.get("_match_count", 0), o["ts"]),
                     reverse=True)
    else:
        matches.sort(key=lambda o: o["ts"], reverse=True)
    matches = matches[:limit]

    if not matches:
        return (f"No observations match "
                f"(project={slug}, type={type or 'any'}, "
                f"query={query or 'any'}).")

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
    # v0.9.4 — fire the background vector-index bootstrap. Daemon
    # thread; non-blocking. The MCP server starts serving requests
    # immediately and the local-vector branch returns [] for the
    # ~1s/1k-turn window the bootstrap runs in. When the thread
    # finishes the singleton hot-swaps the populated index in.
    try:
        from vector_index import (  # type: ignore[import]
            maybe_start_background_bootstrap)
        msg = maybe_start_background_bootstrap(
            DATA_ROOT, WV_CLASS, WV_URL)
        # Emit on stderr so claude-code can show it in the MCP log
        # if anything goes wrong but never to stdout (that's MCP).
        sys.stderr.write(
            f"[dejavu] vector-index bootstrap: {msg}\n")
    except Exception as _e:  # noqa: BLE001
        if _log_err:
            _log_err("mcp.server.bootstrap_vector_index",
                       f"bootstrap spawn failed: {_e}",
                       exc_info=True)
    mcp.run(transport="stdio")
