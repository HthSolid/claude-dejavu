#!/usr/bin/env python3
"""
claude-dejavu — CLI for hybrid recall across Claude Code session history.

Mirrors the MCP server tools but for terminal use, plus workspace management.
"""
import argparse
import json
import os
import time
import subprocess
import sys
import textwrap
import urllib.request
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras

# Local imports
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scope import resolve_scope, apply_pg_filter, weaviate_where  # noqa: E402

# ─── Config (cross-platform, env-overridable) ─────────────────────────────────
def _resolve_data_root() -> Path:
    """Single source of truth for the per-OS data dir. Same logic as
    hooks/stop.py and scripts/install.py — keeping them in sync matters."""
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


def _load_config():
    home_data = _resolve_data_root()
    cfg = {"DATA_ROOT": str(home_data)}  # always present, even before install.py runs
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
_PG_DB = _CFG.get("PG_DB") or f"claude_dejavu_{_OS_USER}"
PG_DSN = (f"host={_CFG.get('PG_HOST','localhost')} port={_CFG.get('PG_PORT','5450')} "
          f"user={_CFG.get('PG_USER','postgres')} password={_CFG.get('PG_PASS','postgres')} "
          f"dbname={_PG_DB}")
WV_URL = _CFG.get("WV_URL", "http://localhost:8888")
WV_CLASS = os.environ.get("CLAUDE_DEJAVU_WV_CLASS") or _CFG.get("WV_CLASS") or "ClaudeDejavuTurn"

CWD_FOR_SCOPE = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def _conn():
    return psycopg2.connect(PG_DSN)


def _get_scope(args):
    try:
        with _conn() as c:
            return resolve_scope(getattr(args, "scope", None), cwd=CWD_FOR_SCOPE, pg_conn=c)
    except Exception:
        return resolve_scope(getattr(args, "scope", None), cwd=CWD_FOR_SCOPE)


def _bm25_query(query: str, limit: int, where: str,
                  timeout: float = 30.0) -> list[dict]:
    """BM25 keyword Weaviate query. v0.8.5 behavior, factored out so
    v0.8.7 RRF wiring can call it alongside the nearVector branch."""
    gql = f'''{{
      Get {{
        {WV_CLASS}(
          bm25: {{ query: {json.dumps(query)} }}
          limit: {limit}
          {where}
        ) {{
          session_id turn_id project_slug role ts ai_title
          _additional {{ score }}
        }}
      }}
    }}'''
    req = urllib.request.Request(f"{WV_URL}/v1/graphql",
        data=json.dumps({"query": gql}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())["data"]["Get"][WV_CLASS] or []
    except Exception as e:
        print(f"weaviate error (bm25): {e}", file=sys.stderr); return []


def _near_vector_query(vec: list[float], limit: int, where: str,
                         timeout: float = 30.0) -> list[dict]:
    """nearVector semantic Weaviate query. Returns hits with
    `_additional.distance` populated by Weaviate.
    Normalizes shape to match `_bm25_query` (adds `_additional.score
    = 1 - distance` so caller code can treat both lists uniformly)."""
    # GraphQL accepts an inline JSON array for the vector literal.
    vec_lit = "[" + ",".join(repr(float(x)) for x in vec) + "]"
    gql = f'''{{
      Get {{
        {WV_CLASS}(
          nearVector: {{ vector: {vec_lit} }}
          limit: {limit}
          {where}
        ) {{
          session_id turn_id project_slug role ts ai_title
          _additional {{ distance }}
        }}
      }}
    }}'''
    req = urllib.request.Request(f"{WV_URL}/v1/graphql",
        data=json.dumps({"query": gql}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = json.loads(r.read())["data"]["Get"][WV_CLASS] or []
    except Exception as e:
        print(f"weaviate error (nearVector): {e}", file=sys.stderr)
        return []
    # Normalize: synthesize a `score` from distance so RRF + display
    # code (which reads `_additional.score`) works without branching.
    for h in raw:
        d = (h.get("_additional") or {}).get("distance")
        try:
            score = 1.0 - float(d) if d is not None else 0.0
        except (ValueError, TypeError):
            score = 0.0
        h.setdefault("_additional", {})["score"] = score
    return raw


def _rrf_fuse(bm25_hits: list[dict],
              vec_hits: list[dict]) -> list[dict]:
    """Reciprocal-rank-fusion via dejavu_bridge.retrieve_rrf, keyed
    by `turn_id`. Both input lists are pre-sorted by their respective
    scores (BM25 score desc; vector similarity desc). The bridge
    returns a single fused list in descending fused-score order.

    Defensive: if the bridge call raises (broken wheel, malformed
    input), returns bm25_hits unchanged so the caller's BM25-only
    path is preserved. Never crashes recall.
    """
    if not vec_hits:
        return bm25_hits
    if not bm25_hits:
        return vec_hits
    try:
        # Lazy import so a corrupted bridge doesn't fail at module load.
        from distill_pipeline import _bridge as _bridge_mod
        # retrieve_rrf reads the `id` field and the real wheel returns
        # bare {id: str} dicts (only the id is preserved). Build a
        # payload map so we can re-attach the original turn metadata
        # in the fused list's order.
        payload: dict[str, dict] = {}
        def _to_id(lst: list[dict]) -> list[dict]:
            out = []
            for h in lst:
                tid = h.get("turn_id")
                if tid is None:
                    continue
                key = str(tid)
                payload.setdefault(key, h)
                out.append({"id": key})
            return out
        bm25_ids = _to_id(bm25_hits)
        vec_ids = _to_id(vec_hits)
        fused = _bridge_mod.retrieve_rrf([bm25_ids, vec_ids])
        # Re-attach the original payloads in the bridge's chosen order.
        out: list[dict] = []
        for entry in fused:
            if not isinstance(entry, dict):
                continue
            key = entry.get("id")
            if key is None:
                continue
            orig = payload.get(str(key))
            if orig is not None:
                out.append(orig)
        return out
    except Exception as e:
        print(f"rrf fusion failed: {type(e).__name__}: {e} — falling back to bm25",
              file=sys.stderr)
        return bm25_hits


# ─── v0.8.9 Context Economy phase 6 — topic_reweight wiring ──────────────────

def _cfg_bool(key: str, default: bool) -> bool:
    raw = _CFG.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _cfg_float(key: str, default: float) -> float:
    raw = _CFG.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default


def _topic_reweight(hits: list[dict], query: str) -> list[dict]:
    """v0.8.9 — wrap ``dejavu_bridge.topic_reweight`` for the CLI
    search path. Diversity boost: same-topic hits get demoted (factor
    0.7 by default), cross-topic hits get promoted (factor 1.3) so
    novel material surfaces alongside on-topic matches.

    The bridge follows a "smaller-is-better" score convention; our
    recall path uses higher-is-better (Weaviate `_additional.score`
    desc). Applied as-is, the default factors produce a DIVERSITY
    BOOST (push cross-topic up). Set TOPIC_REWEIGHT_SAME_FACTOR=1.3
    + TOPIC_REWEIGHT_CROSS_FACTOR=0.7 to invert into a relevance
    boost.

    Resilient: any failure (no ai_title, no topic extractable,
    bridge raises) returns the input list unchanged. NEVER crashes
    recall.
    """
    if not hits:
        return hits
    if not _cfg_bool("TOPIC_REWEIGHT_ENABLED", False):
        return hits
    same_f = _cfg_float("TOPIC_REWEIGHT_SAME_FACTOR", 0.7)
    cross_f = _cfg_float("TOPIC_REWEIGHT_CROSS_FACTOR", 1.3)
    try:
        from topic import extract_topic  # local import — keeps cold start cheap
        current_topic = extract_topic(query or "")
        if not current_topic:
            return hits  # query too vague — no anchor to reweight against
        # Build {turn_id_str: topic} from each hit's ai_title. ai_title
        # arrives directly from Weaviate (_bm25_query + _near_vector_query
        # both SELECT it), so no extra PG roundtrip.
        hit_topics: dict[str, str] = {}
        payload: dict[str, dict] = {}
        candidates: list[dict] = []
        for h in hits:
            tid = h.get("turn_id")
            if tid is None:
                continue
            key = str(tid)
            payload.setdefault(key, h)
            ai_title = h.get("ai_title") or ""
            topic = extract_topic(ai_title)
            if topic:
                hit_topics[key] = topic
            cur_score = float(((h.get("_additional") or {}).get("score")) or 0.0)
            candidates.append({"id": key, "score": cur_score})
        from distill_pipeline import _bridge as _bridge_mod
        reweighted = _bridge_mod.topic_reweight(
            candidates, current_topic, hit_topics,
            same_topic_factor=same_f,
            cross_topic_factor=cross_f,
        )
        # Re-hydrate original payloads + carry the new score back into
        # `_additional.score` so the rest of recall (display, sort,
        # threshold) sees the reweighted value.
        out: list[dict] = []
        for entry in reweighted:
            if not isinstance(entry, dict):
                continue
            key = entry.get("id")
            if key is None:
                continue
            orig = payload.get(str(key))
            if orig is None:
                continue
            try:
                new_score = float(entry.get("score", 0.0))
            except (TypeError, ValueError):
                new_score = 0.0
            merged = dict(orig)
            add = dict(merged.get("_additional") or {})
            add["score"] = new_score
            merged["_additional"] = add
            out.append(merged)
        # Re-sort by new score desc (higher = better in our pipeline).
        out.sort(key=lambda h: -float((h.get("_additional") or {}).get("score") or 0.0))
        return out
    except Exception as e:
        print(f"topic_reweight failed: {type(e).__name__}: {e} — preserving RRF order",
              file=sys.stderr)
        return hits


def _hybrid(query, limit, scope):
    """Hybrid recall: BM25 alone (v0.8.6 behavior) when query-embed is
    unavailable, BM25 + nearVector fused via RRF (v0.8.7) when the
    cloud embed succeeds.

    The function name stays `_hybrid` for back-compat with callers
    (cmd_search, cmd_expand-by-hint). The contract on the return
    list is unchanged: `[ {session_id, turn_id, project_slug, role,
    ts, ai_title, _additional: {score}} ]`. RRF rewrites the score
    field to the fused value (smaller k = higher rank).
    """
    where = weaviate_where(scope)
    # Run BM25 in every case — it's the safe floor and v0.8.6's whole
    # path. nearVector is opportunistic.
    bm25_hits = _bm25_query(query, limit, where)

    # Try the caller-side cloud embed. None means "no API key /
    # unreachable / timeout" — in any of those cases we MUST behave
    # exactly like v0.8.6 (BM25 only) so degraded mode is invisible
    # to the user (per the constraints in the v0.8.7 spec).
    try:
        from query_embed import embed_query
        vec = embed_query(query, timeout_s=1.0)
    except Exception:
        vec = None

    if vec is None:
        # v0.8.9 — topic_reweight is a no-op when flag is off, so the
        # BM25-only path stays bit-for-bit v0.8.6 by default. When
        # the user opts in, reweight here too so the BM25-degraded
        # path also benefits.
        return _topic_reweight(bm25_hits, query)[:limit]

    vec_hits = _near_vector_query(vec, limit, where)
    if not vec_hits:
        # Empty nearVector result (search returned nothing or
        # Weaviate balked) → silently fall back to BM25, preserving
        # v0.8.6 ordering.
        return _topic_reweight(bm25_hits, query)[:limit]

    fused = _rrf_fuse(bm25_hits, vec_hits)
    # v0.8.9 — opt-in topic_reweight (Context Economy phase 6). No-op
    # when TOPIC_REWEIGHT_ENABLED != true, so v0.8.8 ordering is
    # preserved bit-for-bit by default.
    fused = _topic_reweight(fused, query)
    return fused[:limit]


def w(s, n=80):
    s = s or ""; return s if len(s) <= n else s[:n - 1] + "…"


# ─── Commands ────────────────────────────────────────────────────────────────

def _parse_anchors(gist: str | None) -> tuple[str | None, list[str]]:
    """Split a v0.8.6-style `[anchors: a, b, c] body` gist into
    `(body_without_prefix, anchor_list)`. Returns the gist unchanged
    + [] for non-v0.8.6 gists (legacy `rule`/`llm` methods or
    plain text). Pure function — exposed for tests.
    """
    if not gist or not gist.startswith("[anchors:"):
        return gist, []
    # Find closing bracket. Anchors may contain almost any char
    # except `]` (we strip backticks at extraction time), so the
    # first `]` after `[anchors:` is the boundary.
    close = gist.find("]")
    if close < 0:
        return gist, []
    raw = gist[len("[anchors:"):close]
    body = gist[close + 1:].lstrip()
    anchors = [a.strip() for a in raw.split(",") if a.strip()]
    return body, anchors


def cmd_search(args):
    sc = _get_scope(args)
    hits = _hybrid(args.query, args.limit, sc)
    print(f"scope={sc.explain()}  hits={len(hits)}")
    if not hits:
        return
    turn_ids = [int(h.get("turn_id") or 0) for h in hits]
    # v0.8.6: fetch gist + first-200-chars-of-content fallback.
    # Default response shape: gist (or content[:200]), anchors[], score, ts.
    gist_map: dict[int, tuple[str | None, str | None]] = {}
    full_content_map: dict[int, str] = {}
    # why: PG might be down. Weaviate already gave us hit metadata
    # (turn_id, score, ai_title, ts) — we can still print useful output
    # without the gist/content fetch. Caught during 2026-05-18 hell test:
    # search crashed with a traceback when nicefoods-postgres was stopped.
    # Hooks already follow this defensive pattern; bringing the CLI in line.
    if turn_ids:
        try:
            with _conn() as c:
                cur = c.cursor()
                # Pulling first 200 chars at the SQL boundary keeps the
                # default path cheap — we never materialize 32MB rows for
                # a 10-hit search.
                if getattr(args, "full", False):
                    cur.execute(
                        "SELECT id, gist, content FROM turns "
                        "WHERE id = ANY(%s)", (turn_ids,))
                    for r in cur.fetchall():
                        gist_map[r[0]] = (r[1], (r[2] or "")[:200])
                        full_content_map[r[0]] = r[2] or ""
                else:
                    cur.execute(
                        "SELECT id, gist, substring(content, 1, 200) "
                        "FROM turns WHERE id = ANY(%s)", (turn_ids,))
                    for r in cur.fetchall():
                        gist_map[r[0]] = (r[1], r[2])
        except Exception as exc:  # noqa: BLE001 — PG unreachable / DSN bad / etc.
            print(f"postgres unavailable: {exc} — showing Weaviate metadata only",
                  file=sys.stderr)

    for i, h in enumerate(hits, 1):
        tid = int(h.get("turn_id") or 0)
        score = float(h.get("_additional", {}).get("score") or 0)
        proj = h.get("project_slug") or "?"
        sess = (h.get("session_id") or "")[:8]
        ts = (h.get("ts") or "")[:10]
        gist_raw, content_200 = gist_map.get(tid, (None, ""))
        # New default shape: gist (v0.8.6 strips the [anchors: …]
        # prefix for human display but keeps the anchors visible
        # below the line). If no gist, fall back to first 200 chars
        # of content.
        body, anchors = _parse_anchors(gist_raw)
        display = body or gist_raw or content_200 or h.get("ai_title") or "(no gist)"
        display = w(display.replace("\n", " "), 120)
        if args.mode == "full" or getattr(args, "full", False):
            content = full_content_map.get(tid, "")
            print(f"\n[{i}] turn={tid} score={score:.2f} {proj}/{sess} {ts}")
            if anchors:
                print(f"    anchors: {', '.join(anchors)}")
            print(textwrap.indent((content or "")[:1500], "    "))
        else:
            print(f"  [{i}] turn={tid} score={score:.2f} {proj}/{sess} "
                  f"{ts}  {display}")
            if anchors:
                # Single-line anchor footer; bounded so the search
                # output stays scannable.
                anchor_line = ", ".join(anchors[:8])
                if len(anchors) > 8:
                    anchor_line += f", …+{len(anchors) - 8}"
                print(f"        anchors: {anchor_line}")
    if args.mode != "full" and not getattr(args, "full", False):
        print(f"\nUse `claude-dejavu expand <turn_id>...` for full content "
              f"(or pass --full inline).")


def cmd_expand(args):
    if not args.turn_ids:
        print("Pass turn IDs as positional args. Get them from `claude-dejavu search`.")
        return
    with _conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT t.id, t.session_id, s.project_slug, s.ai_title, t.role, t.content_type, t.ts, t.content
            FROM turns t JOIN sessions s ON s.id = t.session_id
            WHERE t.id = ANY(%s) ORDER BY t.ts NULLS LAST
        """, (args.turn_ids,))
        for r in cur.fetchall():
            sess = str(r['session_id'])[:8]; ts = str(r.get('ts') or '')[:19]
            print(f"\n── turn {r['id']} {r['project_slug']}/{sess} {r['role']}/{r['content_type']} {ts}")
            if r.get('ai_title'): print(f"   session-title: {r['ai_title']}")
            print(r['content'] or "(empty)")


def cmd_sessions(args):
    sc = _get_scope(args)
    sql = "SELECT id, project_slug, ai_title, total_turns, started_at FROM sessions s"
    params = []
    sql, params = apply_pg_filter(sql, params, sc, table_alias="s")
    sql += " ORDER BY started_at DESC NULLS LAST LIMIT %s"
    params.append(args.limit)
    with _conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        rows = cur.fetchall()
    print(f"scope={sc.explain()}")
    print(f"{'session':12} {'project':22} {'turns':>5}  {'started':12}  title")
    print("-" * 110)
    for r in rows:
        sid = str(r["id"])[:10]
        title = w(r["ai_title"] or "(no title)", 50)
        print(f"{sid:12} {(r['project_slug'] or '?'):22} {(r['total_turns'] or 0):>5}  {str(r['started_at'] or '')[:10]:12}  {title}")


def cmd_resume(args):
    sc = _get_scope(args)
    hits = _hybrid(args.hint, 20, sc)
    if not hits:
        print(f"no matches in {sc.explain()}"); return
    by_session = {}
    for h in hits:
        sid = h.get("session_id"); score = float(h.get("_additional", {}).get("score") or 0)
        cur = by_session.get(sid)
        if not cur or score > cur["score"]: by_session[sid] = {"score": score, "hit": h}
    ranked = sorted(by_session.values(), key=lambda x: -x["score"])[:5]
    print(f"Top in scope={sc.explain()}:")
    home = os.path.expanduser("~")
    with _conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        for i, r in enumerate(ranked, 1):
            sid = r["hit"]["session_id"]
            cur.execute("SELECT project_slug, ai_title, total_turns, started_at FROM sessions WHERE id=%s::uuid", (sid,))
            meta = cur.fetchone() or {}
            slug = meta.get("project_slug") or "?"
            cwd = next((os.path.join(b, slug) for b in (f"{home}/Documents/projects", f"{home}/projects", home) if os.path.isdir(os.path.join(b, slug))), "<project-dir>")
            title = w(meta.get("ai_title") or "(no title)", 60)
            print(f"  [{i}] {sid}  score={r['score']:.2f}  {slug}  {title}")
            print(f"      $ cd {cwd} && claude --resume {sid}")


def _log_indexer_failure(name: str, exc: Exception) -> None:
    """Print to stderr (existing UX) AND persist to the structured
    error log so users can `dejavu logs` later. Never raises — the
    underlying log_error() falls back to stderr on disk failure."""
    print(f"  ({name} indexing failed: {exc})", file=sys.stderr)
    try:
        from error_log import log_error
        log_error(f"indexer.{name}",
                    f"{name} indexing failed: {exc}",
                    exc_info=True)
    except Exception:  # noqa: BLE001
        pass


def cmd_generate_runtime_trap(args):
    """Generate a runtime env-trap module for the project."""
    import re as _re
    from pathlib import Path
    from runtime_trap import generate

    project = args.project or _project_slug_for_cwd()
    cwd = Path.cwd()

    # Pull declared envs from .env.example (or the file the user
    # named) — this is the lowest-friction source.
    env_names: set[str] = set()
    src_path = cwd / args.from_file
    if src_path.is_file():
        for ln in src_path.read_text(errors="ignore").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            m = _re.match(r"^([A-Z_][A-Z0-9_]*)\s*=", ln)
            if m:
                env_names.add(m.group(1))

    # ALSO pull from the indexed env_vars table if the project is
    # indexed — this catches names declared in workflows / compose
    # / source AST scans that .env.example missed.
    try:
        with _conn() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT DISTINCT name FROM env_vars WHERE project_slug=%s",
                (project,))
            env_names |= {r[0] for r in cur.fetchall()}
    except Exception:  # noqa: BLE001
        pass  # Index may not exist yet — file source is enough

    if not env_names:
        print(f"No env vars found for project `{project}` "
                f"(checked {args.from_file} and the env_vars index). "
                f"Run `claude-dejavu reindex` first, or declare some "
                f"vars in {args.from_file}.", file=sys.stderr)
        sys.exit(1)

    output = Path(args.output) if args.output else None
    path, content = generate(sorted(env_names),
                                language=args.language,
                                output_path=output,
                                project_root=cwd)
    print(f"✓ generated {path.relative_to(cwd) if path.is_relative_to(cwd) else path}")
    print(f"  declared keys: {len(env_names)}")
    print(f"  language:      {args.language}")
    print(f"\nUsage in your code:")
    if args.language.lower() in ("typescript", "ts"):
        print(f"  import {{ env }} from \"./{path.stem}\";")
        print(f"  const url = env.DATABASE_URL;  // throws if not declared")
    else:
        print(f"  from {path.stem} import env")
        print(f"  url = env.DATABASE_URL  # raises DejavuUndeclaredEnvError if not declared")
    print(f"\nTo refresh after adding new env vars, re-run this command.")


def cmd_env_propose(args):
    """Append a new env var declaration to .env.example.

    Mirrors the dejavu_propose_env_addition MCP tool: writes the
    line + a comment naming the SDK convention, with a dry-run
    diff by default so the user can review before applying."""
    import re as _re
    from pathlib import Path
    target = Path.cwd() / args.file
    if not target.is_file():
        print(f"REFUSED: {args.file} doesn't exist at {target}. "
                "Create it first or pass --file pointing somewhere "
                "else.", file=sys.stderr)
        sys.exit(1)
    existing = target.read_text(encoding="utf-8", errors="ignore")
    if _re.search(rf"^{_re.escape(args.name)}\s*=",
                     existing, _re.MULTILINE):
        print(f"NO_CHANGE: `{args.name}` is already declared in "
                f"{args.file}.")
        return
    comment = (f"# Standard env for `{args.package}` (declared by "
                 f"`claude-dejavu env propose`)\n"
                 if args.package else
                 "# Declared by `claude-dejavu env propose`\n")
    new_line = f"{args.name}=\n"
    new_content = (existing
                     + ("\n" if not existing.endswith("\n") else "")
                     + comment + new_line)
    diff = (f"--- a/{args.file}\n+++ b/{args.file}\n"
              f"@@ end-of-file @@\n"
              f"+{comment.rstrip()}\n+{new_line.rstrip()}")
    if args.apply:
        target.write_text(new_content, encoding="utf-8")
        print(f"APPLIED: appended `{args.name}=` to {args.file}.")
        print(diff)
        print(f"\nFill in the value, then:")
        print(f"  git add {args.file} && git commit -m "
                f"'add {args.name}'")
    else:
        print(f"DRY_RUN: would append to {args.file}:")
        print(diff)
        print("\nRe-run with --apply to write the change.")


def cmd_fabrications(args):
    """List + triage per-project fabrication memory."""
    from fabrication_memory import (list_fabrications, set_status,
                                          project_summary)
    project = args.project or _project_slug_for_cwd()
    with _conn() as c:
        c.autocommit = True
        if args.mark:
            name, domain, status = args.mark
            ok_ = set_status(c, project, name, domain, status)
            if ok_:
                print(f"  ✓ {project}/{domain}: `{name}` → {status}")
            else:
                print(f"  ✗ no fabrication record for "
                        f"({project}, {name}, {domain}) — "
                        f"nothing to mark")
            return
        rows = list_fabrications(c, project,
                                      status=args.status,
                                      min_count=args.min_count,
                                      limit=args.limit)
        summary = project_summary(c, project)
    print(f"project: {project}")
    print(f"summary: open={summary['open']}  "
            f"declared={summary['declared']}  "
            f"blacklisted={summary['blacklisted']}  "
            f"total_occurrences={summary['total_occurrences']}")
    if not rows:
        print("\n(no records — either nothing has been fabricated yet "
                "or your filters excluded everything)")
        return
    print()
    print(f"  {'name':32s} {'domain':8s} {'status':12s} "
            f"{'count':>5s}  last_seen")
    print(f"  {'-'*32} {'-'*8} {'-'*12} {'-'*5}  {'-'*10}")
    for r in rows:
        print(f"  {r['name']:32s} {r['domain']:8s} "
                f"{r['status']:12s} {r['count']:5d}  "
                f"{str(r['last_seen_at'])[:19]}")
    print()
    print("  (mark with: claude-dejavu fabrications --project={} "
            "--mark NAME DOMAIN {{declared|blacklisted|open}})"
            .format(project))


def cmd_logs(args):
    """Show recent records from the dejavu plugin error log."""
    from error_log import (read_recent, format_for_human,
                              log_path_str)
    if getattr(args, "path", False):
        print(log_path_str())
        return
    records = read_recent(limit=max(1, min(args.limit, 500)),
                            component=args.component,
                            level=args.level)
    print(f"# claude-dejavu error log ({len(records)} records)")
    print(f"# location: {log_path_str()}")
    print()
    print(format_for_human(
        records, with_traceback=args.with_traceback))


def cmd_errors(args):
    sc = _get_scope(args)
    sql = ("SELECT tc.id AS tool_call_id, s.project_slug, tc.tool_name, tc.tool_input, "
           "       left(tc.tool_output, 600) AS error_preview, tc.ts "
           "FROM tool_calls tc JOIN sessions s ON s.id = tc.session_id WHERE tc.is_error")
    params = []
    if args.tool:
        sql += " AND tc.tool_name = %s"; params.append(args.tool)
    sql, params = apply_pg_filter(sql, params, sc, table_alias="s")
    sql += " ORDER BY tc.ts DESC LIMIT %s"
    params.append(args.limit)
    with _conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        rows = cur.fetchall()
    print(f"scope={sc.explain()}  errors={len(rows)}")
    for r in rows:
        print(f"\n[{str(r['ts'])[:19]}] {r['project_slug']} {r['tool_name']}")
        print(f"  input: {json.dumps(r['tool_input'] or {})[:160]}")
        print(f"  err:   {(r['error_preview'] or '')[:200]}")


def cmd_files(args):
    sc = _get_scope(args)
    sql = ("SELECT s.project_slug, s.id AS session_id, ft.file_path, ft.op, ft.ts "
           "FROM file_touches ft JOIN sessions s ON s.id = ft.session_id WHERE ft.file_path ILIKE %s")
    params = [f"%{args.path}%"]
    sql, params = apply_pg_filter(sql, params, sc, table_alias="s")
    sql += " ORDER BY ft.ts DESC LIMIT %s"
    params.append(args.limit)
    with _conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params); rows = cur.fetchall()
    print(f"scope={sc.explain()}")
    for r in rows:
        print(f"  {str(r['ts'] or '')[:19]} {r['project_slug']}/{str(r['session_id'])[:8]} {r['op']:10s} {r['file_path']}")


def cmd_restore(args):
    """Restore sessions that exist in backup but are missing from live ~/.claude/projects/.

    Usage:
        claude-dejavu restore --list                # show what's restorable
        claude-dejavu restore <session-uuid> [...]  # restore specific session(s)
        claude-dejavu restore --all                 # restore everything missing
        claude-dejavu restore --since=7d            # restore items missing within last 7 days
    """
    import shutil, hashlib, time, datetime as dt
    backup_root = Path(_CFG["DATA_ROOT"]) / "chat-logs"
    live_root = Path.home() / ".claude" / "projects"

    if not backup_root.is_dir():
        print(f"Backup root not found: {backup_root}", file=sys.stderr); return

    deleted = []  # list of dicts
    truncated = []
    for proj_dir in backup_root.iterdir():
        if not proj_dir.is_dir() or proj_dir.name.startswith("_"):
            continue
        for jsonl in proj_dir.glob("*.jsonl"):
            uuid = jsonl.stem
            live_path = live_root / proj_dir.name / f"{uuid}.jsonl"
            backup_size = jsonl.stat().st_size
            backup_mtime = jsonl.stat().st_mtime
            if not live_path.exists():
                deleted.append({"proj": proj_dir.name, "uuid": uuid, "src": jsonl, "dst": live_path,
                                "size": backup_size, "mtime": backup_mtime, "kind": "deleted"})
            else:
                live_size = live_path.stat().st_size
                if live_size < backup_size * 0.95:  # >5% smaller = likely truncated
                    truncated.append({"proj": proj_dir.name, "uuid": uuid, "src": jsonl, "dst": live_path,
                                      "size": backup_size, "live_size": live_size, "kind": "truncated"})

    candidates = deleted + truncated
    if args.since:
        cutoff = time.time() - _parse_duration(args.since)
        candidates = [c for c in candidates if c["mtime"] >= cutoff]

    if args.list or not (args.session_uuids or args.all):
        if not candidates:
            print("Nothing to restore — all backup sessions present in live (~/.claude/projects/).")
            return
        print(f"Restorable sessions ({len(candidates)}):\n")
        print(f"{'kind':10} {'uuid':38} {'project':30} {'mtime':19} {'backup_size':>12}")
        print("-" * 120)
        for c in candidates:
            mtime_str = dt.datetime.fromtimestamp(c["mtime"]).isoformat(sep=" ", timespec="seconds")
            extra = f" (live={c.get('live_size','?')})" if c["kind"] == "truncated" else ""
            print(f"{c['kind']:10} {c['uuid']:38} {c['proj'][:30]:30} {mtime_str:19} {c['size']:>12}{extra}")
        print(f"\nRestore with: claude-dejavu restore <uuid> [...]   or   claude-dejavu restore --all")
        return

    # Filter to what was requested
    if args.all:
        targets = candidates
    else:
        wanted = set(args.session_uuids)
        targets = [c for c in candidates if c["uuid"] in wanted]
        missing_in_backup = wanted - {c["uuid"] for c in candidates}
        for u in missing_in_backup:
            print(f"  ⚠ {u}: not in backup OR live already has equal-size copy", file=sys.stderr)

    if not targets:
        print("No sessions to restore."); return

    restored = 0
    for c in targets:
        c["dst"].parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(c["src"], c["dst"])
        # sha256 verify
        h_src = hashlib.sha256(c["src"].read_bytes()).hexdigest()
        h_dst = hashlib.sha256(c["dst"].read_bytes()).hexdigest()
        if h_src != h_dst:
            print(f"  ❌ {c['uuid']}: checksum mismatch — restore aborted", file=sys.stderr)
            c["dst"].unlink(missing_ok=True)
            continue
        restored += 1
        # Find a project dir for the resume hint
        slug = c["proj"].lstrip("-").rsplit("projects-", 1)[-1] or c["proj"]
        home = str(Path.home())
        cwd = next((f"{base}/{slug}" for base in (f"{home}/Documents/projects", f"{home}/projects", home)
                    if Path(f"{base}/{slug}").is_dir()), "<project-dir>")
        size_mb = c["size"] / 1e6
        print(f"  ✅ {c['uuid']}  ({size_mb:.1f} MB)  → {c['dst']}")
        print(f"     resume: cd {cwd} && claude --resume {c['uuid']}")

    print(f"\nRestored {restored}/{len(targets)} session(s).")


def cmd_repair(args):
    """`claude-dejavu repair {--list,--all,<uuids>...}` — detect + reconstruct
    corrupt/truncated session JSONLs from the dejavu DB.

    Exit codes: 0 success, 2 unknown session uuid(s) supplied, 3 reconstruction
    write failed for at least one target.
    """
    import repair as _rp
    live_root = Path.home() / ".claude" / "projects"

    # Pull sessions from DB.
    # - When specific UUIDs are passed, fetch only those (no row-count cap).
    # - For --list / --all, scan everything (no LIMIT) so older corrupt
    #   sessions are not silently invisible just because they fell off a
    #   recent-N window.
    with _conn() as c, c.cursor() as cur:
        if args.session_uuids:
            cur.execute(
                "SELECT id, project_path, project_slug, ai_title "
                "FROM sessions WHERE id = ANY(%s::uuid[])",
                (list(args.session_uuids),),
            )
        else:
            cur.execute(
                "SELECT id, project_path, project_slug, ai_title "
                "FROM sessions ORDER BY started_at DESC"
            )
        rows = cur.fetchall()

    diagnoses = []
    for sid, project_path, project_slug, ai_title in rows:
        if not project_path:
            continue
        encoded = project_path.replace("/", "-")
        if not encoded.startswith("-"):
            encoded = "-" + encoded
        live_path = live_root / encoded / f"{sid}.jsonl"
        if not live_path.is_file():
            continue  # `restore` subcommand handles the deleted-file case
        try:
            with _conn() as c:
                d = _rp.diagnose(sid, live_path, c)
        except Exception as e:
            print(f"  ⚠ {sid}: diagnose failed: {e}", file=sys.stderr)
            continue
        d.ai_title = ai_title
        d.project_slug = project_slug or ""
        d.project_path = project_path
        diagnoses.append(d)

    issues = [d for d in diagnoses if not d.healthy]

    if args.list or not (args.session_uuids or args.all):
        if not issues:
            print("No corruption detected.")
            return 0
        print(f"Found {len(issues)} session(s) with issues:\n")
        print(f"{'kind':22} {'uuid':38} {'project':30} {'live':>8} {'db_max':>8} {'gap':>6}  ai_title")
        print("-" * 140)
        for d in issues:
            if d.corrupted and d.truncated:
                kind = "CORRUPTED+TRUNCATED"
            elif d.corrupted:
                kind = "CORRUPTED"
            else:
                kind = "TRUNCATED"
            print(f"{kind:22} {d.session_id:38} {(d.project_slug or '')[:30]:30} "
                  f"{d.valid_lines:>8} {d.db_max_idx:>8} "
                  f"{(d.db_max_idx - d.valid_lines):>6}  {(d.ai_title or '')[:60]}")
        print(f"\nRepair with: claude-dejavu repair <uuid>   or   claude-dejavu repair --all")
        return 0

    # Pick targets
    if args.all:
        targets = issues
    else:
        wanted = set(args.session_uuids)
        targets = [d for d in diagnoses if d.session_id in wanted]
        missing = wanted - {d.session_id for d in diagnoses}
        for u in missing:
            print(f"  ⚠ {u}: not found in DB or live JSONL missing",
                  file=sys.stderr)
        if not targets:
            return 2

    repaired = 0
    failed = 0
    for d in targets:
        if d.healthy:
            print(f"  {d.session_id}: healthy — skipped")
            continue
        try:
            with _conn() as c:
                entries = _rp.reconstruct_jsonl(d, c, project_path=d.project_path)
            backup, n = _rp.write_repaired_jsonl(
                d, entries,
                no_pre_recovery_snapshot=getattr(
                    args, "no_pre_recovery_snapshot", False))
        except Exception as e:
            print(f"  ✗ {d.session_id}: repair failed: {e}", file=sys.stderr)
            failed += 1
            continue
        print(f"  ✓ {d.session_id}: {n} entries written  (backup: {backup})")
        repaired += 1
    print(f"\nRepaired: {repaired} / {len(targets)}")
    return 3 if failed else 0


def cmd_cleanup_stubs(args):
    """`claude-dejavu cleanup-stubs [--all-projects|--project <encoded>] [--dry-run]`

    Removes ai-title-only JSONL stubs from `~/.claude/projects/<encoded>/`.
    Claude Code writes one such stub per Bash tool call (containing only an
    ``ai-title`` line) which pollutes the session-resume picker. Stubs are
    backed up to a timestamped tarball before deletion.

    A stub is defined as:
      * `.jsonl` file under `~/.claude/projects/<encoded>/`
      * Size ≤ 500 bytes
      * Single line that parses as JSON with `type == "ai-title"` and
        the file's basename UUID matches the `sessionId` field.

    Exit codes: 0 success / nothing to clean, 2 backup-tar failed,
    3 deletion failed.
    """
    import tarfile, time
    projects_root = Path.home() / ".claude" / "projects"
    if not projects_root.is_dir():
        print(f"  {projects_root} does not exist; nothing to clean.")
        return 0

    # Target dirs: --project N flag wins; otherwise --all-projects sweeps; default = cwd-matched
    if args.project:
        targets = [projects_root / args.project]
        if not targets[0].is_dir():
            print(f"  ✗ project dir not found: {targets[0]}", file=sys.stderr)
            return 2
    elif args.all_projects:
        targets = [p for p in projects_root.iterdir() if p.is_dir()]
    else:
        # encoded form of cwd
        cwd = Path.cwd().resolve()
        encoded = "-" + str(cwd).lstrip("/").replace("/", "-")
        targets = [projects_root / encoded]
        if not targets[0].is_dir():
            print(f"  No project dir for cwd ({encoded}); pass --all-projects "
                  f"or --project <encoded> to target a different one.",
                  file=sys.stderr)
            return 2

    backup_root = Path(os.environ.get("DATA_ROOT") or
                       (Path.home() / ".local" / "share" / "claude-dejavu")) / "stubs-backup"
    backup_root.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    total_scanned = total_stubs = total_kept = total_deleted = 0
    archives: list[Path] = []

    for proj_dir in targets:
        candidates = list(proj_dir.glob("*.jsonl"))
        if not candidates:
            continue
        stubs: list[Path] = []
        for f in candidates:
            total_scanned += 1
            try:
                if f.stat().st_size > 500:
                    continue
                text = f.read_text(encoding="utf-8", errors="replace").strip()
                if "\n" in text:  # multi-line → not a stub
                    continue
                obj = json.loads(text)
            except (OSError, json.JSONDecodeError):
                continue
            if obj.get("type") == "ai-title" and \
               isinstance(obj.get("sessionId"), str) and \
               obj["sessionId"] == f.stem:
                stubs.append(f)
        total_stubs += len(stubs)
        if not stubs:
            continue

        if args.dry_run:
            print(f"  [dry-run] {proj_dir.name}: would delete {len(stubs)} stub(s)")
            continue

        # Backup: one tar.gz per project dir
        archive = backup_root / f"{proj_dir.name}-stubs-{ts}.tar.gz"
        try:
            with tarfile.open(archive, "w:gz") as tf:
                for s in stubs:
                    tf.add(s, arcname=s.name)
            archives.append(archive)
        except OSError as e:
            print(f"  ✗ {proj_dir.name}: backup failed ({e}) — skipping deletions",
                  file=sys.stderr)
            return 2

        # Delete only after the archive landed
        deleted = 0
        for s in stubs:
            try:
                s.unlink()
                deleted += 1
            except OSError as e:
                print(f"  ✗ {s.name}: delete failed ({e})", file=sys.stderr)
        total_deleted += deleted
        kept = len(candidates) - deleted
        total_kept += kept
        print(f"  ✓ {proj_dir.name}: {deleted} stubs deleted, {kept} real "
              f"sessions kept  (backup: {archive})")

    if total_stubs == 0:
        print("  No stubs found; nothing to clean.")
        return 0
    if args.dry_run:
        print(f"\nDry-run total: {total_stubs} stub(s) across "
              f"{len(targets)} project(s). Re-run without --dry-run to remove.")
        return 0
    print(f"\nCleaned {total_deleted} stub(s) across {len(archives)} project(s). "
          f"Backups in {backup_root}/")
    return 0


# ─── v0.7.2: file corruption scanner + file recovery ─────────────────────────

def cmd_scan_corruption(args):
    """`claude-dejavu scan-corruption [--path <dir>] [--all-projects] ...`

    Scans a directory tree (or every known project) for zeroed / partial-zero
    / shrunk files. Exit code 0 if zero actionable findings, 1 otherwise.

    See: docs/superpowers/specs/2026-05-15-file-recovery-design.md §3
    """
    import recovery as _rc

    include = args.include or None
    exclude = args.exclude or None

    targets: list[tuple[str, Path]] = []
    if args.all_projects:
        # Walk dejavu's sessions table for distinct project_path values.
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT project_path FROM sessions "
                    "WHERE project_path IS NOT NULL "
                    "  AND project_path <> '_orphan' "
                    "ORDER BY project_path")
                project_paths = [r[0] for r in cur.fetchall() if r[0]]
        except Exception as e:
            print(f"  ⚠ DB query failed ({e}); cannot scan --all-projects",
                  file=sys.stderr)
            return 1
        skipped_orphan = 0
        for pp in project_paths:
            p = Path(pp)
            if not p.is_dir():
                continue
            targets.append((pp, p))
        if skipped_orphan:
            print(f"  (skipped {skipped_orphan} orphan session(s))",
                  file=sys.stderr)
    else:
        root = Path(args.path or ".").resolve()
        targets.append((str(root), root))

    actionable = 0
    total_findings = 0
    results_json: list[dict] = []
    for label, root in targets:
        if not root.is_dir():
            print(f"  ⚠ skipping non-directory: {root}", file=sys.stderr)
            continue
        findings = _rc.scan_dir(root, include=include, exclude=exclude,
                                 verbose=args.verbose)
        if not findings:
            if not args.json:
                print(f"  ✓ {label}: 0 findings")
            continue
        if not args.json:
            print(f"\n{label}: {len(findings)} finding(s)")
            print(f"  {'status':14} {'size':>10}  path")
            print("  " + "-" * 100)
        for fi in findings:
            total_findings += 1
            if fi.status in (_rc.Status.ZEROED,
                              _rc.Status.PARTIAL_ZERO,
                              _rc.Status.SHRUNK):
                actionable += 1
            if args.json:
                results_json.append({
                    "path": str(fi.path),
                    "status": fi.status.value,
                    "size": fi.size,
                })
            else:
                print(f"  {fi.status.value:14} {fi.size:>10}  {fi.path}")

    if args.json:
        print(json.dumps(results_json, indent=2))

    if total_findings == 0:
        if not args.json:
            print("\nNo corruption detected.")
        return 0
    if not args.json:
        print(f"\nTotal: {total_findings} finding(s), {actionable} actionable.")
        if actionable:
            print("Recover with: claude-dejavu recover-file <path> [--session <uuid>]")
    return 1 if actionable else 0


def cmd_recover_file(args):
    """`claude-dejavu recover-file <path> [--session <uuid>] ...`

    Default: dry-run. Prints recovery plan + diff stat vs current file.
    Pass ``--write`` AND ``--yes`` (when writing to the original) to apply.

    See: docs/superpowers/specs/2026-05-15-file-recovery-design.md §4-6
    """
    import recovery as _rc
    import difflib
    import hashlib
    import shutil
    import time as _time

    target = os.path.realpath(args.path)
    if not args.allow_anywhere:
        home = os.path.realpath(str(Path.home()))
        if not target.startswith(home + os.sep) and target != home:
            print(f"  ✗ refusing to operate outside $HOME without --allow-anywhere "
                  f"(target={target})", file=sys.stderr)
            return 4

    # Step 1: Resolve session
    session_id = args.session
    project_path: Optional[str] = None
    if not session_id:
        # Look up the most recent session for the current project (default
        # behavior) unless --any-project is set.
        try:
            with _conn() as c, c.cursor() as cur:
                if args.any_project:
                    cur.execute(
                        "SELECT s.id, s.project_path FROM sessions s "
                        "JOIN tool_calls tc ON tc.session_id = s.id "
                        "WHERE tc.tool_name IN ('Edit','MultiEdit','Write') "
                        "  AND tc.tool_input->>'file_path' = %s "
                        "ORDER BY s.started_at DESC LIMIT 1",
                        (target,))
                else:
                    cwd_real = os.path.realpath(CWD_FOR_SCOPE)
                    cur.execute(
                        "SELECT s.id, s.project_path FROM sessions s "
                        "JOIN tool_calls tc ON tc.session_id = s.id "
                        "WHERE s.project_path = %s "
                        "  AND tc.tool_name IN ('Edit','MultiEdit','Write') "
                        "  AND tc.tool_input->>'file_path' = %s "
                        "ORDER BY s.started_at DESC LIMIT 1",
                        (cwd_real, target))
                row = cur.fetchone()
                if row:
                    session_id = str(row[0])
                    project_path = row[1]
        except Exception as e:
            print(f"  ⚠ session lookup failed ({e}); will fall back to git",
                  file=sys.stderr)
    else:
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute(
                    "SELECT project_path FROM sessions WHERE id = %s::uuid",
                    (session_id,))
                row = cur.fetchone()
                if row:
                    project_path = row[0]
        except Exception:
            pass

    # Step 2: collect ops from the session JSONL (if found)
    ops: list = []
    jsonl_path: Optional[Path] = None
    if session_id and project_path:
        encoded = project_path.replace("/", "-")
        if not encoded.startswith("-"):
            encoded = "-" + encoded
        jsonl_path = Path.home() / ".claude" / "projects" / encoded / f"{session_id}.jsonl"
        if jsonl_path.is_file():
            ops = _rc.collect_edit_ops(jsonl_path, target_path=target)
        else:
            jsonl_path = None

    if args.to:
        ops = [o for o in ops if (o[0] or "") <= args.to]

    # Step 3: pick base (or honor pins)
    file_history_dir = Path.home() / ".claude" / "file-history"
    git_repo = _find_git_repo(target)

    base_content: str = ""
    base_source: Optional[_rc.BaseSource] = None

    if args.from_git:
        if not git_repo:
            print(f"  ✗ --from-git given but no git repo found above {target}",
                  file=sys.stderr)
            return 5
        try:
            result = subprocess.run(
                ["git", "-C", str(git_repo), "show",
                 f"{args.from_git}:{os.path.relpath(target, git_repo)}"],
                capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                print(f"  ✗ git show {args.from_git}:<path> failed: "
                      f"{result.stderr.strip()}", file=sys.stderr)
                return 5
            base_content = result.stdout
            base_source = _rc.BaseSource.DB_AND_GIT
        except (OSError, subprocess.SubprocessError) as e:
            print(f"  ✗ git invocation failed: {e}", file=sys.stderr)
            return 5
    elif args.from_version is not None:
        # Pin a specific @vN snapshot. Best-effort: walk the per-session
        # file-history dir and look for *@v<N>.
        if not session_id:
            print(f"  ✗ --from-version requires --session <uuid>",
                  file=sys.stderr)
            return 5
        per_session = file_history_dir / session_id
        cand = list(per_session.glob(f"*@v{args.from_version}"))
        if not cand:
            print(f"  ✗ no snapshot @v{args.from_version} for session "
                  f"{session_id} in {per_session}", file=sys.stderr)
            return 5
        try:
            base_content = cand[0].read_text(encoding="utf-8", errors="replace")
            base_source = _rc.BaseSource.FILE_HISTORY
            # --from-version pins a snapshot; we don't know its backupTime
            # from disk alone, so callers can use --to <iso-ts> to bound
            # the replay. Default: replay every op (legacy behavior).
            since_ts = None
        except OSError as e:
            print(f"  ✗ read failed: {e}", file=sys.stderr)
            return 5
    else:
        if session_id:
            per_session = file_history_dir / session_id
            base_content, base_source, since_ts = _rc.pick_base(
                target=target, session_id=session_id, ops=ops,
                file_history_dir=per_session, git_repo=git_repo)
        else:
            since_ts = None
            # No session → try git only
            if git_repo:
                head = _rc._git_show_head(git_repo, target)
                if head is not None:
                    base_content = head
                    base_source = _rc.BaseSource.GIT_HEAD_ONLY
                else:
                    base_source = _rc.BaseSource.WRITE_ONLY
            else:
                base_source = _rc.BaseSource.WRITE_ONLY

    # Step 3.5: when strategy 1 (FILE_HISTORY) was used, drop ops whose
    # timestamp is at/before the snapshot's backupTime — those edits are
    # already baked into the snapshot content.
    if since_ts:
        before = len(ops)
        ops = [op for op in ops if not (len(op) > 0 and op[0] and op[0] <= since_ts)]
        if before != len(ops):
            print(f"  (dropped {before - len(ops)} pre-snapshot op(s); snapshot backupTime={since_ts})")

    # Step 4: apply
    try:
        recovered = _rc.apply_edits(base_content, ops)
    except _rc.ApplyError as e:
        if args.skip_failed_edits:
            print(f"  ⚠ {e}; --skip-failed-edits set, applying what we can",
                  file=sys.stderr)
            recovered = base_content
            for entry in ops:
                try:
                    recovered = _rc.apply_edits(recovered, [entry])
                except _rc.ApplyError as ee:
                    print(f"  ⚠ skipped: {ee}", file=sys.stderr)
        else:
            print(f"  ✗ apply failed: {e}", file=sys.stderr)
            print(f"     pass --skip-failed-edits to continue past mismatches",
                  file=sys.stderr)
            return 6

    # Step 5: verify (advisory)
    ext = Path(target).suffix.lower()
    if ext == ".json":
        try:
            json.loads(recovered)
        except json.JSONDecodeError as e:
            print(f"  ⚠ recovered .json does not parse: {e}", file=sys.stderr)
    elif ext in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
            yaml.safe_load(recovered)
        except Exception as e:
            print(f"  ⚠ recovered .yaml does not parse: {e}", file=sys.stderr)
    if args.verify_tsc and ext in (".ts", ".tsx", ".js", ".jsx"):
        # Best-effort: write to a temp file and run tsc --noEmit
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=ext, delete=False,
                                          encoding="utf-8") as tf:
            tf.write(recovered)
            tmpf = tf.name
        try:
            result = subprocess.run(["tsc", "--noEmit", tmpf],
                                      capture_output=True, text=True,
                                      timeout=30)
            if result.returncode != 0:
                print(f"  ⚠ tsc --noEmit reports errors:\n{result.stdout[:500]}",
                      file=sys.stderr)
            else:
                print(f"  ✓ tsc --noEmit clean")
        except (OSError, subprocess.SubprocessError) as e:
            print(f"  ⚠ tsc invocation failed: {e}", file=sys.stderr)
        finally:
            try:
                os.unlink(tmpf)
            except OSError:
                pass

    # Step 6: report + (optionally) write
    try:
        current = Path(target).read_text(encoding="utf-8", errors="replace")
    except OSError:
        current = ""
    print(f"\nRecovery summary for {target}")
    print(f"  session:       {session_id or '<none>'}")
    print(f"  base source:   {base_source.value if base_source else '<none>'}")
    print(f"  ops collected: {len(ops)} ({sum(1 for o in ops if o[1]=='Edit')} Edit, "
          f"{sum(1 for o in ops if o[1]=='Write')} Write)")
    print(f"  current size:  {len(current)} chars")
    print(f"  recovered size:{len(recovered)} chars")

    diff = list(difflib.unified_diff(
        current.splitlines(keepends=True),
        recovered.splitlines(keepends=True),
        fromfile=f"a/{target}", tofile=f"b/{target}", n=2,
    ))
    add = sum(1 for ln in diff if ln.startswith("+") and not ln.startswith("+++"))
    rem = sum(1 for ln in diff if ln.startswith("-") and not ln.startswith("---"))
    print(f"  diff stat:     +{add} -{rem} lines")

    if diff and not args.json:
        print("  first 20 diff lines:")
        for ln in diff[:20]:
            print(f"    {ln.rstrip()}")

    out_path = Path(args.to_path) if args.to_path else Path(target)
    write_to_original = (str(out_path.resolve()) == target)

    if args.write is None:
        # Dry-run mode (default)
        print(f"\n  (dry-run — pass --write to apply; "
              f"--write <out> to redirect)")
        return 0

    # --write mode
    if args.write != "":
        out_path = Path(args.write).resolve()
        write_to_original = (str(out_path) == target)

    if write_to_original and not args.yes:
        print(f"\n  ✗ refusing to overwrite original without --yes",
              file=sys.stderr)
        return 7

    # Editor-lockfile guard (vim swap, etc.)
    if not args.force_write:
        parent = out_path.parent
        basename = out_path.name
        sentinels = [
            parent / f".{basename}.swp",
            parent / f".{basename}.swo",
            parent / f"{basename}~",
        ]
        active = [s for s in sentinels if s.exists()]
        if active:
            print(f"\n  ✗ editor lockfile(s) present near target — refusing to write:",
                  file=sys.stderr)
            for s in active:
                print(f"      {s}", file=sys.stderr)
            print(f"     pass --force-write to bypass.", file=sys.stderr)
            return 8

    # Backup current to off-tree dir BEFORE writing.
    # The backup root resolves to $DATA_ROOT/recovered-files (see
    # recovery._default_recovery_backup_root) — kept off-tree so we don't
    # pollute the project working copy.
    ts = int(_time.time())
    path_hash = hashlib.sha256(target.encode("utf-8")).hexdigest()[:16]
    backup_root = _rc._default_recovery_backup_root()
    backup_dir = backup_root / (session_id or "no-session") / str(ts)
    backup_dir.mkdir(parents=True, exist_ok=True)
    original_backup = backup_dir / f"{path_hash}.original"
    diff_sidecar = backup_dir / f"{path_hash}.diff"

    try:
        if Path(target).is_file():
            shutil.copy2(target, original_backup)
        else:
            original_backup.write_text("", encoding="utf-8")
        diff_sidecar.write_text("".join(diff), encoding="utf-8")
    except OSError as e:
        print(f"  ✗ backup failed: {e}", file=sys.stderr)
        return 9

    # v0.8.0: synchronous pre-recovery snapshot + atomic write via
    # recovery.write_recovered. If the snapshot fails (repo unreachable,
    # restic crash, …), the write is aborted and the original file is
    # untouched. The atomic mechanism inside write_recovered is:
    #   tmp = out_path.parent / f"{out_path.name}.dejavu-recover.tmp"
    #   tmp.write_text(content); os.replace(tmp, out_path)
    # so the same-filesystem temp + os.replace contract from v0.7.2
    # spec §6 is preserved (just moved into recovery.py).
    try:
        _rc.write_recovered(
            out_path, recovered,
            no_pre_recovery_snapshot=getattr(
                args, "no_pre_recovery_snapshot", False))
    except RuntimeError as e:
        print(f"  ✗ {e}", file=sys.stderr)
        return 10
    except OSError as e:
        print(f"  ✗ write failed: {e}", file=sys.stderr)
        return 10

    print(f"\n  ✓ wrote {len(recovered)} chars to {out_path}")
    print(f"     backup:  {original_backup}")
    print(f"     diff:    {diff_sidecar}")
    return 0


def _find_git_repo(start: str) -> Optional[Path]:
    """Walk upward from ``start`` looking for a ``.git`` directory or file."""
    p = Path(start).resolve()
    if p.is_file():
        p = p.parent
    cur = p
    for _ in range(64):
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent
    return None


def _parse_duration(s: str) -> float:
    """'7d' → seconds, '24h' → seconds, '60m' → seconds. Defaults to 1 day if unparseable."""
    import re
    m = re.match(r"^(\d+)([dhms])$", s.strip().lower())
    if not m: return 86400.0
    n = int(m.group(1))
    unit = m.group(2)
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def cmd_workspace(args):
    with _conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if args.action == "list":
            cur.execute("SELECT name, description, repo_ids FROM workspaces ORDER BY name")
            for r in cur.fetchall():
                print(f"  {r['name']:20} ({len(r['repo_ids'])} projects)  {r.get('description') or ''}")
                print(f"    {', '.join(r['repo_ids'])}")
        elif args.action == "create":
            projects = [p.strip() for p in args.projects.split(",") if p.strip()] if args.projects else []
            cur.execute("INSERT INTO workspaces (name, description, repo_ids) VALUES (%s, %s, %s) "
                        "ON CONFLICT (name) DO UPDATE SET repo_ids = EXCLUDED.repo_ids, description = COALESCE(EXCLUDED.description, workspaces.description)",
                        (args.name, args.description, projects))
            c.commit()
            print(f"workspace '{args.name}' set: {projects}")
        elif args.action == "add":
            cur.execute("UPDATE workspaces SET repo_ids = array_append(array_remove(repo_ids, %s), %s) WHERE name = %s",
                        (args.project, args.project, args.name))
            c.commit()
            print(f"added '{args.project}' to '{args.name}'")
        elif args.action == "remove":
            cur.execute("UPDATE workspaces SET repo_ids = array_remove(repo_ids, %s) WHERE name = %s",
                        (args.project, args.name))
            c.commit()
            print(f"removed '{args.project}' from '{args.name}'")
        elif args.action == "delete":
            cur.execute("DELETE FROM workspaces WHERE name = %s", (args.name,))
            c.commit()
            print(f"workspace '{args.name}' deleted")


def _workspace_migrate_impl(conn) -> dict:
    """Map legacy slug entries in workspaces.repo_ids → repos.id by label.

    Returns a report dict:
        {"remapped": {slug: uuid, ...},
         "orphans": [slug, ...]}  # slugs with no matching repos.label

    Caller owns the transaction (no commit here).
    """
    import uuid as _u
    orphans: list[str] = []
    remapped: dict[str, str] = {}
    with conn.cursor() as cur:
        cur.execute("SELECT id, label FROM repos")
        label_to_id = {lbl: str(rid) for rid, lbl in cur.fetchall() if lbl}
        cur.execute("SELECT name, repo_ids FROM workspaces")
        rows = cur.fetchall()
        for name, ids in rows:
            new_ids: list[str] = []
            for entry in ids or []:
                # If it's already a UUID, leave it.
                try:
                    _u.UUID(entry)
                    new_ids.append(entry)
                    continue
                except (ValueError, TypeError, AttributeError):
                    pass
                # Try label match.
                if entry in label_to_id:
                    new_ids.append(label_to_id[entry])
                    remapped[entry] = label_to_id[entry]
                else:
                    orphans.append(entry)
            cur.execute("UPDATE workspaces SET repo_ids = %s WHERE name = %s",
                         (new_ids, name))
    return {"remapped": remapped, "orphans": sorted(set(orphans))}


def cmd_workspace_migrate(args):
    """`claude-dejavu workspace-migrate` — one-shot helper that maps
    legacy slug entries in workspaces.repo_ids → repo UUIDs by label."""
    with _conn() as c:
        report = _workspace_migrate_impl(c)
    print(f"remapped: {len(report['remapped'])}")
    for slug, rid in report["remapped"].items():
        print(f"  {slug} → {rid}")
    if report["orphans"]:
        print(f"\norphans (no matching repos.label):")
        for s in report["orphans"]:
            print(f"  {s}")
        print(
            "\nFix by running `claude-dejavu repo show` from each repo "
            "to populate repos.label, then re-run this command.")
    return 0


def cmd_repo(args):
    """`claude-dejavu repo {show,rebind,prune}` dispatcher."""
    import repo_id as _rid
    if args.repo_action == "show":
        start = Path(args.start) if args.start else Path.cwd()
        rid, rroot = _rid.resolve(start=start)
        payload = {"repo_id": rid, "repo_root": str(rroot),
                   "label": rroot.name}
        if args.json:
            print(json.dumps(payload))
        else:
            print(f"repo_id:   {rid}")
            print(f"repo_root: {rroot}")
            print(f"label:     {rroot.name}")
        return 0
    elif args.repo_action == "rebind":
        print("rebind is a Phase E feature; not yet implemented",
              file=sys.stderr)
        return 2
    elif args.repo_action == "prune":
        # List repos rows untouched for 90+ days. Read-only by default.
        conn = _conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, label, last_seen_path, updated_at FROM repos "
                    "WHERE updated_at < now() - interval '90 days' "
                    "ORDER BY updated_at ASC"
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        if not rows:
            print("no stale repos")
        for rid, lbl, path, upd in rows:
            print(f"  {rid}  {lbl:20s}  {path}  (last seen {upd:%Y-%m-%d})")
        return 0
    else:
        print(f"unknown repo action: {args.repo_action}", file=sys.stderr)
        return 2


# ─── v0.3.0: code symbol grounding ───────────────────────────────────────────

def _project_slug_for_cwd():
    return os.path.basename(os.path.realpath(CWD_FOR_SCOPE))


def _reindex_docs(args) -> int:
    """Walk the repo for .md files, chunk + embed each, update doc_file_state cursor."""
    from datetime import datetime, timezone
    import hashlib as _hashlib
    import repo_id as _rid
    import doc_indexer as _di
    import semantic_search as _ss

    start = Path(args.repo) if args.repo else Path.cwd()
    rid, rroot = _rid.resolve(start=start)
    print(f"reindex --docs   repo_id={rid}  repo_root={rroot}")
    if not _ss.is_available():
        print("  Weaviate not available — aborting", file=sys.stderr)
        return 2
    if not _ss.ensure_doc_class():
        print("  Could not create/verify ClaudeDejavuDoc class", file=sys.stderr)
        return 2
    conn = _conn()
    try:
        _rid.upsert_repos_row(conn, rid, rroot)
        conn.commit()

        embedded_files = 0
        embedded_chunks = 0
        for md_path in _di.walk_repo_for_md(rroot):
            rel = str(md_path.relative_to(rroot))
            try:
                chunks = list(_di.chunk_file(md_path))
            except Exception as e:
                print(f"  skip {rel}: {type(e).__name__}: {e}", file=sys.stderr)
                continue
            if not chunks and not getattr(args, "full", False):
                continue

            # File-level digest = sha256 of concatenated chunk hashes
            # (stable + cheap; mirrors the file_state semantics).
            file_hash = _hashlib.sha256(
                "".join(c.content_hash for c in chunks).encode("utf-8")
            ).hexdigest()

            # Cursor-skip: if the file is unchanged AND --full wasn't passed, skip.
            if not getattr(args, "full", False):
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT content_hash FROM doc_file_state "
                        "WHERE repo_id = %s AND file_path = %s",
                        (rid, rel),
                    )
                    cached = cur.fetchone()
                if cached and cached[0] == file_hash:
                    continue  # unchanged → skip

            # Full replace per file: delete prior chunks then re-embed.
            _ss.delete_docs_for_file(rid, rel)
            mtime_iso = datetime.fromtimestamp(
                md_path.stat().st_mtime, tz=timezone.utc).isoformat()
            n = _ss.index_docs(rid, rel, chunks, mtime_iso=mtime_iso)
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO doc_file_state
                       (repo_id, file_path, mtime, content_hash, chunk_count)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (repo_id, file_path) DO UPDATE SET
                         mtime = EXCLUDED.mtime,
                         content_hash = EXCLUDED.content_hash,
                         chunk_count = EXCLUDED.chunk_count,
                         embedded_at = now()""",
                    (rid, rel, int(md_path.stat().st_mtime),
                      file_hash, len(chunks)),
                )
            conn.commit()
            embedded_files += 1
            embedded_chunks += n
        print(f"  embedded: {embedded_files} files, {embedded_chunks} chunks")
        return 0
    finally:
        conn.close()


def cmd_reindex(args):
    """Index (or re-index) the project's source files: symbols + routes
    + (optionally) semantic embeddings.

    v0.4.2 perf: build the IgnoreFilter ONCE here and pass to every
    indexer. Each indexer used to walk the full project tree on its
    own filter construction (11 redundant traversals on a typical
    reindex). Sharing the filter eliminates that.
    """
    if getattr(args, "docs", False):
        return _reindex_docs(args)
    from symbol_indexer import index_project
    from ignore import IgnoreFilter
    project_dir = args.path or CWD_FOR_SCOPE
    project_slug = args.project or _project_slug_for_cwd()
    flt = IgnoreFilter.for_project(project_dir)
    with _conn() as c:
        c.autocommit = True
        sym_stats = index_project(project_dir, project_slug=project_slug, conn=c, force=args.force, flt=flt)
        if "error" in sym_stats:
            print(f"reindex error: {sym_stats['error']}", file=sys.stderr); sys.exit(1)
        # Routes — runs after symbols so handler_symbol_id can later be resolved.
        route_stats = {}
        if not args.skip_routes:
            try:
                from route_indexer import index_project_routes
                route_stats = index_project_routes(project_dir, project_slug=project_slug, conn=c, flt=flt, force=args.force)
            except Exception as e:
                _log_indexer_failure('route', e)
        # Pages — independent of routes; same-pass for cache-locality.
        page_stats = {}
        if not args.skip_pages:
            try:
                from page_indexer import index_project_pages
                page_stats = index_project_pages(project_dir, project_slug=project_slug, conn=c, flt=flt, force=args.force)
            except Exception as e:
                _log_indexer_failure('page', e)
        # Env vars — independent of all the above.
        env_stats = {}
        if not args.skip_env:
            try:
                from env_indexer import index_project_env_vars
                env_stats = index_project_env_vars(
                    project_dir, project_slug=project_slug, conn=c,
                    include_runtime_dotenv=args.include_runtime_dotenv,
                    flt=flt,
                    force=args.force,
                )
            except Exception as e:
                _log_indexer_failure('env', e)
        # DB schema — Prisma + Drizzle + TypeORM + SQL migrations.
        db_stats = {}
        if not args.skip_db:
            try:
                from db_indexer import index_project_db
                db_stats = index_project_db(project_dir, project_slug=project_slug, conn=c, flt=flt, force=args.force)
            except Exception as e:
                _log_indexer_failure('db', e)
        # GraphQL schema — *.graphql / *.gql + inline gql template literals.
        gql_stats = {}
        if not args.skip_graphql:
            try:
                from graphql_indexer import index_project_graphql
                gql_stats = index_project_graphql(project_dir, project_slug=project_slug, conn=c, flt=flt, force=args.force)
            except Exception as e:
                _log_indexer_failure('graphql', e)
        # Feature flags — flags.json / featureFlags.ts / LD / Unleash / GrowthBook.
        flag_stats = {}
        if not args.skip_flags:
            try:
                from flag_indexer import index_project_flags
                flag_stats = index_project_flags(project_dir, project_slug=project_slug, conn=c, flt=flt, force=args.force)
            except Exception as e:
                _log_indexer_failure('flag', e)
        # Quick-win domains: CSS classes (Tailwind + project) + i18n keys.
        quickwin_stats = {}
        if not args.skip_quickwins:
            try:
                from quickwin_indexer import index_project_quickwins
                quickwin_stats = index_project_quickwins(project_dir,
                                                            project_slug=project_slug,
                                                            conn=c, flt=flt,
                                                            force=args.force)
            except Exception as e:
                _log_indexer_failure('quickwin', e)
        # Module exports — what node_modules/<pkg> actually exports.
        module_stats = {}
        if not args.skip_modules:
            try:
                from module_indexer import index_project_modules
                module_stats = index_project_modules(project_dir,
                                                       project_slug=project_slug,
                                                       conn=c)
            except Exception as e:
                _log_indexer_failure('module', e)
        # Name registry (prose grounding for non-code projects).
        registry_stats = {}
        if not args.skip_registry:
            try:
                from registry_indexer import index_project_name_registry
                registry_stats = index_project_name_registry(
                    project_dir, project_slug=project_slug, conn=c, flt=flt)
            except Exception as e:
                _log_indexer_failure('name_registry', e)
        # Semantic — only if Weaviate is reachable AND not skipped.
        semantic_stats = {}
        if not args.skip_semantic:
            try:
                from semantic_search import is_available, reindex_project as semantic_reindex
                if is_available():
                    semantic_stats = semantic_reindex(project_slug, c)
            except Exception as e:
                _log_indexer_failure('semantic', e)

    print(f"project: {project_slug}  ({project_dir})")
    print(f"  symbols:")
    print(f"    files scanned : {sym_stats['files_scanned']}")
    print(f"    files indexed : {sym_stats['files_indexed']}")
    print(f"    files skipped : {sym_stats['files_skipped']} (unchanged)")
    print(f"    files purged  : {sym_stats.get('files_purged', 0)} (deleted)")
    print(f"    symbols added : {sym_stats['symbols_added']}")
    print(f"    parse errors  : {sym_stats['errors']}")
    def _cached_tag(s: dict) -> str:
        return "  (cached, no changes since last run)" if s.get("cached") else ""

    if route_stats:
        print(f"  routes:{_cached_tag(route_stats)}")
        if "error" in route_stats:
            print(f"    error         : {route_stats['error']}")
        else:
            print(f"    files scanned : {route_stats.get('files_scanned', 0)}")
            print(f"    files w/routes: {route_stats.get('files_with_routes', 0)}")
            print(f"    routes added  : {route_stats.get('routes_added', 0)}")
            print(f"    parse errors  : {route_stats.get('errors', 0)}")
    if page_stats:
        print(f"  pages:{_cached_tag(page_stats)}")
        if "error" in page_stats:
            print(f"    error         : {page_stats['error']}")
        else:
            print(f"    pages added   : {page_stats.get('pages_added', 0)}")
            by_fw = page_stats.get('by_framework', {})
            if by_fw:
                fw_summary = ", ".join(f"{k}={v}" for k, v in sorted(by_fw.items()))
                print(f"    by framework  : {fw_summary}")
    if env_stats:
        print(f"  env vars:{_cached_tag(env_stats)}")
        if "error" in env_stats:
            print(f"    error         : {env_stats['error']}")
        else:
            print(f"    env vars added: {env_stats.get('env_vars_added', 0)}")
            print(f"    secrets       : {env_stats.get('secrets_flagged', 0)}")
            by_src = env_stats.get('by_source', {})
            if by_src:
                src_summary = ", ".join(f"{k}={v}" for k, v in sorted(by_src.items()))
                print(f"    by source     : {src_summary}")
    if db_stats:
        print(f"  db schema:{_cached_tag(db_stats)}")
        if "error" in db_stats:
            print(f"    error         : {db_stats['error']}")
        else:
            print(f"    models added  : {db_stats.get('models_added', 0)}")
            print(f"    fields added  : {db_stats.get('fields_added', 0)}")
            by_src = db_stats.get('by_source', {})
            if by_src:
                src_summary = ", ".join(f"{k}={v}" for k, v in sorted(by_src.items()))
                print(f"    by source     : {src_summary}")
    if gql_stats:
        print(f"  graphql schema:{_cached_tag(gql_stats)}")
        if "error" in gql_stats:
            print(f"    error         : {gql_stats['error']}")
        else:
            print(f"    types added   : {gql_stats.get('types_added', 0)}")
            print(f"    fields added  : {gql_stats.get('fields_added', 0)}")
            by_src = gql_stats.get('by_source', {})
            if by_src:
                src_summary = ", ".join(f"{k}={v}" for k, v in sorted(by_src.items()))
                print(f"    by source     : {src_summary}")
    if flag_stats:
        print(f"  feature flags:{_cached_tag(flag_stats)}")
        if "error" in flag_stats:
            print(f"    error         : {flag_stats['error']}")
        else:
            print(f"    flags added   : {flag_stats.get('flags_added', 0)}")
            print(f"    kill switches : {flag_stats.get('kill_switches_flagged', 0)}")
            by_src = flag_stats.get('by_source', {})
            if by_src:
                src_summary = ", ".join(f"{k}={v}" for k, v in sorted(by_src.items()))
                print(f"    by source     : {src_summary}")
    if quickwin_stats:
        print(f"  quick-wins:{_cached_tag(quickwin_stats)}")
        if "error" in quickwin_stats:
            print(f"    error         : {quickwin_stats['error']}")
        else:
            print(f"    css classes   : {quickwin_stats.get('css_classes_added', 0)}")
            print(f"    i18n keys     : {quickwin_stats.get('i18n_keys_added', 0)}")
    if module_stats:
        print(f"  module exports:")
        if "error" in module_stats:
            print(f"    error         : {module_stats['error']}")
        else:
            print(f"    pkgs scanned  : {module_stats.get('packages_scanned', 0)}")
            print(f"    exports added : {module_stats.get('exports_added', 0)}")
    if registry_stats:
        print(f"  name registry:")
        if "error" in registry_stats:
            print(f"    error         : {registry_stats['error']}")
        else:
            print(f"    names added   : {registry_stats.get('names_added', 0)}")
            by_kind = registry_stats.get('by_kind', {})
            if by_kind:
                kind_summary = ", ".join(f"{k}={v}" for k, v in sorted(by_kind.items()))
                print(f"    by kind       : {kind_summary}")
    if semantic_stats:
        print(f"  semantic (Weaviate):")
        if "error" in semantic_stats:
            print(f"    skipped       : {semantic_stats['error']}")
        else:
            print(f"    symbols       : {semantic_stats.get('symbols', 0)}")
            print(f"    routes        : {semantic_stats.get('routes', 0)}")


def cmd_search_docs(args):
    """Convenience wrapper: dejavu_search(kinds=['doc']).

    Resolves the search scope from --workspace (multi-repo) or cwd
    (single-repo via repo_id resolver). Prints heading-anchored hits
    with their `result_id` so the user can `dejavu_expand` them.
    """
    import repo_id as _rid
    from semantic_search import search

    repo_filter: list[str] = []
    if args.workspace:
        with _conn() as c:
            with c.cursor() as cur:
                cur.execute("SELECT repo_ids FROM workspaces WHERE name = %s",
                             (args.workspace,))
                row = cur.fetchone()
        if not row:
            print(f"unknown workspace: {args.workspace}", file=sys.stderr)
            return 2
        repo_filter = list(row[0]) if row[0] else []
        if not repo_filter:
            print(f"workspace '{args.workspace}' is empty", file=sys.stderr)
            return 2
    else:
        rid, _ = _rid.resolve(start=Path.cwd())
        repo_filter = [rid]

    all_hits = []
    for rid in repo_filter:
        hits = search(args.query, kinds=["doc"], repo_id=rid, limit=args.limit)
        all_hits.extend(hits)
    # Sort by score descending so multi-repo workspaces present best-first.
    all_hits.sort(key=lambda h: -h.get("score", 0.0))
    all_hits = all_hits[: args.limit]

    if args.json:
        print(json.dumps(all_hits, indent=2))
    else:
        if not all_hits:
            print("No matches.")
        for h in all_hits:
            score = h.get("score", 0.0)
            print(f"  [{score:.2f}] {h.get('heading_path', '?')}")
            print(f"         {h.get('doc_path', '?')}")
            print(f"         → {h.get('one_line_summary', '')}")
            print(f"         expand: {h.get('result_id', '?')}")
            print()
    return 0


def cmd_symbols(args):
    """List or look up symbols. Without a query, lists everything (paginated)."""
    from symbol_indexer import resolve_symbol, list_symbols, log_outcome
    project_slug = args.project or _project_slug_for_cwd()
    with _conn() as c:
        c.autocommit = True
        if args.name:
            hits = resolve_symbol(args.name, project_slug=project_slug, conn=c,
                                  fuzzy=not args.exact, limit=args.limit,
                                  languages=[args.language] if args.language else None)
            if not hits:
                log_outcome(c, project_slug=project_slug, language=args.language,
                            mode="lookup", proposed_name=args.name, resolved_name=None,
                            candidate_count=0, outcome="unknown",
                            outcome_signal="no_candidates")
                print(f"no candidates for '{args.name}' in '{project_slug}'"); return
            top = hits[0]
            log_outcome(c, project_slug=project_slug, language=top.get("language"),
                        mode="lookup", proposed_name=args.name,
                        resolved_name=top["name"],
                        edit_distance=top.get("edit_distance"),
                        trigram_sim=top.get("trigram_sim"),
                        candidate_count=len(hits), outcome="unknown",
                        outcome_signal="logged_at_lookup")
            for h in hits:
                marker = "✓" if h["edit_distance"] == 0 else " "
                sig = h.get("signature") or h["name"]
                print(f"{marker} {h['kind']:9s} {h['name']:30s} sim={h['trigram_sim']:.2f} ed={h['edit_distance']}  {h['file_path']}:{h['start_line']}")
                if sig != h["name"]:
                    print(f"    {sig}")
        else:
            rows = list_symbols(project_slug=project_slug, conn=c,
                                file_path=args.file, kind=args.kind,
                                language=args.language, limit=args.limit, offset=args.offset)
            print(f"{len(rows)} symbol(s) in '{project_slug}'")
            for r in rows:
                print(f"  {r['kind']:9s} {r['name']:30s}  {r['file_path']}:{r['start_line']}")


def cmd_lint(args):
    """Lint a proposed file edit against the symbol index.

    Read content from --content-file, --content-stdin, or default to reading
    the existing file at --file (useful for sanity-checking already-written code).
    """
    from lint import lint_proposed_edit
    project_slug = args.project or _project_slug_for_cwd()
    file_path = args.file
    if args.content_stdin:
        content = sys.stdin.read()
    elif args.content_file:
        content = open(args.content_file, "r", encoding="utf-8").read()
    else:
        # Default: lint the existing file at file_path
        try:
            content = open(file_path, "r", encoding="utf-8").read()
        except OSError as e:
            print(f"could not read {file_path}: {e}", file=sys.stderr); sys.exit(1)

    with _conn() as c:
        c.autocommit = True
        r = lint_proposed_edit(content, file_path, project_slug=project_slug, conn=c)

    if args.json:
        print(json.dumps(r, indent=2, default=str))
        # Exit code reflects verdict — important for CI/hooks even in JSON mode.
        v = r["summary"]["verdict"]
        sys.exit({"ok": 0, "warn": 1, "block": 2}.get(v, 0))

    s = r["summary"]
    print(f"verdict: {s['verdict'].upper()}  refs={s['total_references']}  ok={s['resolved']}  warn={s['low_confidence']}  unknown={s['unknown']}")
    if s.get("note"):
        print(f"  note: {s['note']}")
    if r["unknown"]:
        print("\nunknown (likely fabricated):")
        for u in r["unknown"]:
            c_ = u.get("top_candidate")
            via = f" [receiver: {u['via_receiver_type']}]" if u.get("via_receiver_type") else ""
            note = f"  note: {u['note']}" if u.get("note") else ""
            if c_:
                print(f"  ? {u['name']:30s} closest: {c_['name']} sim={c_['trigram_sim']} ed={c_['edit_distance']}{via}{note}")
            else:
                print(f"  ? {u['name']:30s} (no candidates){via}{note}")
    if r["low_confidence"]:
        print("\nlow-confidence (likely typo):")
        for lc in r["low_confidence"]:
            c_ = lc["top_candidate"]
            print(f"  ~ {lc['name']:30s} -> {c_['name']:30s} sim={c_['trigram_sim']} ed={c_['edit_distance']}")
    if args.show_resolved and r["resolved"]:
        print(f"\nresolved ({len(r['resolved'])}):")
        for x in r["resolved"]:
            print(f"  ✓ {x['name']:30s} [{x['kind']}]")
    # ─── routes (v0.3.2.1) ─────────────────────────────────────────────
    if r.get("routes_unknown"):
        print("\nroutes — unknown (likely fabricated URLs):")
        for u in r["routes_unknown"]:
            print(f"  ? {u['name']:40s}  {u.get('reason', '')}")
    if r.get("routes_low_confidence"):
        print("\nroutes — low-confidence (typo or wrong method):")
        for lc in r["routes_low_confidence"]:
            print(f"  ~ {lc['name']:40s} -> {lc.get('candidate', '?')}  {lc.get('reason', '')}")
    if args.show_resolved and r.get("routes_resolved"):
        print(f"\nroutes — resolved ({len(r['routes_resolved'])}):")
        for x in r["routes_resolved"]:
            print(f"  ✓ {x['name']:40s} -> {x.get('candidate', '')}")
    # ─── pages (v0.3.5) ────────────────────────────────────────────────
    if r.get("pages_unknown"):
        print("\npages — DUPLICATE PAGE detected (existing page covers this URL):")
        for u in r["pages_unknown"]:
            print(f"  ✗ {u['name']:40s} -> {u.get('candidate', '?')}")
            print(f"      {u.get('reason', '')}")
            if u.get('fix'):
                print(f"      → {u['fix']}")
    if r.get("pages_low_confidence"):
        print("\npages — possible overlap (parameterized or near-name match):")
        for lc in r["pages_low_confidence"]:
            print(f"  ~ {lc['name']:40s} -> {lc.get('candidate', '?')}")
            print(f"      {lc.get('reason', '')}")
    # ─── env vars (v0.3.6) ─────────────────────────────────────────────
    if r.get("env_unknown"):
        print("\nenv vars — unknown (likely fabricated):")
        for u in r["env_unknown"]:
            print(f"  ? {u['name']:40s}  {u.get('reason', '')}")
            if u.get('fix'):
                print(f"      → {u['fix']}")
    if r.get("env_low_confidence"):
        print("\nenv vars — low-confidence (typo or undeclared but read):")
        for lc in r["env_low_confidence"]:
            print(f"  ~ {lc['name']:40s} -> {lc.get('candidate', '?')}")
            print(f"      {lc.get('reason', '')}")
            if lc.get('fix'):
                print(f"      → {lc['fix']}")
    if args.show_resolved and r.get("env_resolved"):
        print(f"\nenv vars — resolved ({len(r['env_resolved'])}):")
        for x in r["env_resolved"]:
            secret = " 🔒" if x.get("metadata", {}).get("is_secret") else ""
            print(f"  ✓ {x['name']:40s}{secret}  {x.get('reason', '')}")
    # ─── db schema (v0.3.7) ────────────────────────────────────────────
    if r.get("db_unknown"):
        print("\ndb schema — unknown (likely fabricated):")
        for u in r["db_unknown"]:
            print(f"  ? {u['name']:40s}  {u.get('reason', '')}")
            if u.get('fix'):
                print(f"      → {u['fix']}")
    if r.get("db_low_confidence"):
        print("\ndb schema — low-confidence (typo or wrong model):")
        for lc in r["db_low_confidence"]:
            print(f"  ~ {lc['name']:40s} -> {lc.get('candidate', '?')}")
            print(f"      {lc.get('reason', '')}")
            if lc.get('fix'):
                print(f"      → {lc['fix']}")
    if args.show_resolved and r.get("db_resolved"):
        print(f"\ndb schema — resolved ({len(r['db_resolved'])}):")
        for x in r["db_resolved"]:
            print(f"  ✓ {x['name']:40s}  {x.get('reason', '')}")
    # ─── graphql (v0.3.8) ──────────────────────────────────────────────
    if r.get("gql_unknown"):
        print("\ngraphql — unknown (likely fabricated):")
        for u in r["gql_unknown"]:
            print(f"  ? {u['name']:40s}  {u.get('reason', '')}")
            if u.get('fix'):
                print(f"      → {u['fix']}")
    if r.get("gql_low_confidence"):
        print("\ngraphql — low-confidence (typo or wrong type):")
        for lc in r["gql_low_confidence"]:
            print(f"  ~ {lc['name']:40s} -> {lc.get('candidate', '?')}")
            print(f"      {lc.get('reason', '')}")
            if lc.get('fix'):
                print(f"      → {lc['fix']}")
    if args.show_resolved and r.get("gql_resolved"):
        print(f"\ngraphql — resolved ({len(r['gql_resolved'])}):")
        for x in r["gql_resolved"]:
            print(f"  ✓ {x['name']:40s}  {x.get('reason', '')}")
    # ─── feature flags (v0.3.10) ───────────────────────────────────────
    if r.get("flag_unknown"):
        print("\nfeature flags — unknown (likely fabricated):")
        for u in r["flag_unknown"]:
            print(f"  ? {u['name']:40s}  {u.get('reason', '')}")
            if u.get('fix'):
                print(f"      → {u['fix']}")
    if r.get("flag_low_confidence"):
        print("\nfeature flags — low-confidence (typo or undeclared but used):")
        for lc in r["flag_low_confidence"]:
            print(f"  ~ {lc['name']:40s} -> {lc.get('candidate', '?')}")
            print(f"      {lc.get('reason', '')}")
            if lc.get('fix'):
                print(f"      → {lc['fix']}")
    if args.show_resolved and r.get("flag_resolved"):
        print(f"\nfeature flags — resolved ({len(r['flag_resolved'])}):")
        for x in r["flag_resolved"]:
            kill = " 🛑" if x.get("metadata", {}).get("is_kill_switch") else ""
            print(f"  ✓ {x['name']:40s}{kill}  {x.get('reason', '')}")
    # ─── quick-win domains (v0.3.14) + imports (v0.3.16) + prose (v0.4.0) ───
    for label, key in (("css", "css"), ("i18n", "i18n"),
                        ("npm-script", "npm"), ("asset-path", "asset"),
                        ("module-import", "import"), ("prose", "prose")):
        if r.get(f"{key}_unknown"):
            print(f"\n{label} — unknown (likely fabricated):")
            for u in r[f"{key}_unknown"]:
                print(f"  ? {u['name']:40s}  {u.get('reason', '')}")
                if u.get('fix'):
                    print(f"      → {u['fix']}")
        if r.get(f"{key}_low_confidence"):
            print(f"\n{label} — low-confidence (typo or near-match):")
            for lc in r[f"{key}_low_confidence"]:
                print(f"  ~ {lc['name']:40s} -> {lc.get('candidate', '?')}")
                print(f"      {lc.get('reason', '')}")
                if lc.get('fix'):
                    print(f"      → {lc['fix']}")

    # Exit code reflects verdict so CI / hooks can branch on it.
    if s["verdict"] == "block":
        sys.exit(2)
    elif s["verdict"] == "warn":
        sys.exit(1)
    else:
        sys.exit(0)


def cmd_routes(args):
    """List or look up indexed API routes."""
    from route_indexer import resolve_route, list_routes, normalize_path
    project_slug = args.project or _project_slug_for_cwd()
    with _conn() as c:
        if args.rcmd == "list":
            rows = list_routes(project_slug, c, framework=args.framework,
                               method=args.method, limit=args.limit)
            if not rows:
                print(f"no routes indexed for '{project_slug}'"); return
            print(f"{len(rows)} route(s) in '{project_slug}'")
            for r in rows:
                print(f"  {r['method']:6s} {r['path_pattern']:35s} [{r['framework']}/{r['source']}]  {r['source_file']}:{r['start_line']}")
        else:  # get
            hits = resolve_route(args.method, args.path, project_slug, c,
                                  fuzzy=not args.exact, limit=args.limit)
            if not hits:
                print(f"no route matching `{args.method.upper()} {normalize_path(args.path)}`")
                sys.exit(1)
            for h in hits:
                marker = "✓" if h["match_kind"] == "exact" else ("≈" if h["match_kind"] == "pattern" else "~")
                sim = f" sim={h['trigram_sim']:.2f}" if h["match_kind"] == "fuzzy" else ""
                print(f"  {marker} {h['method']:6s} {h['path_pattern']:35s} [{h['framework']}/{h['source']}]  {h['source_file']}:{h['start_line']}{sim}")


def cmd_pages(args):
    """List indexed page routes or check a proposed path for duplicates."""
    from page_indexer import list_pages, check_new_page
    project_slug = args.project or _project_slug_for_cwd()
    with _conn() as c:
        if args.pcmd == "list":
            rows = list_pages(project_slug, c, framework=args.framework,
                              kind=args.kind, limit=args.limit)
            if not rows:
                print(f"no pages indexed for '{project_slug}'"); return
            print(f"{len(rows)} page(s) in '{project_slug}'")
            for r in rows:
                print(f"  {r['kind']:10s} {r['path_pattern']:40s} [{r['framework']}]  {r['source_file']}:{r['start_line']}")
        else:  # check
            hits = check_new_page(args.path, project_slug, c, fuzzy=True, limit=args.limit)
            if not hits:
                print(f"no existing page covers `{args.path}` in '{project_slug}' — safe to create.")
                return
            print(f"{len(hits)} existing page(s) overlap with `{args.path}` in '{project_slug}':")
            for h in hits:
                marker = "✗" if h["match_kind"] == "exact" else ("≈" if h["match_kind"] == "pattern" else "~")
                sim = f" sim={h['trigram_sim']:.2f}" if h["match_kind"] == "fuzzy" else ""
                print(f"  {marker} {h['kind']:10s} {h['path_pattern']:35s} [{h['framework']}]  {h['source_file']}:{h['start_line']}{sim}")


def cmd_ignore(args):
    """Inspect / debug the .dejavuignore filter (v0.3.11)."""
    from ignore import IgnoreFilter
    project_root = Path(args.path or os.getcwd()).resolve() if args.icmd == "status" \
        else Path(os.getcwd()).resolve()
    # For both subcommands, use the project root — for `status`, take it
    # from the file's enclosing repo (walk up to a `.git`/`package.json`/
    # `pyproject.toml` marker), else cwd.
    if args.icmd == "status":
        target = Path(args.path).resolve()
        # Find project root by walking up to a marker.
        markers = (".git", "package.json", "pyproject.toml", "Cargo.toml",
                    "go.mod", ".dejavuignore")
        cur = target if target.is_dir() else target.parent
        while cur != cur.parent:
            if any((cur / m).exists() for m in markers):
                project_root = cur
                break
            cur = cur.parent
    flt = IgnoreFilter.for_project(project_root)
    if args.icmd == "list":
        rules = flt.list_rules()
        if not rules:
            print(f"no ignore rules loaded for '{project_root}'"); return
        print(f"{len(rules)} rule(s) for '{project_root}':")
        for r in rules:
            scope = "" if r["base"] == str(project_root) else f"  ({r['base']})"
            flags = []
            if r["anchored"]: flags.append("anchored")
            if r["only_dirs"]: flags.append("dirs-only")
            tag = " [" + ",".join(flags) + "]" if flags else ""
            print(f"  {r['pattern']:30s}{tag}{scope}")
        return
    # status
    info = flt.explain(args.path)
    verdict = "SKIP" if info["skipped"] else "INDEX"
    print(f"path     : {info['path']}")
    print(f"is_dir   : {info['is_dir']}")
    print(f"verdict  : {verdict}")
    if info["winning_rule"]:
        wr = info["winning_rule"]
        action = "ignored by" if not wr["negate"] else "re-included by"
        print(f"reason   : {action} `{wr['pattern']}` from {wr['base']}")
    else:
        print(f"reason   : no rule matched (default = INDEX)")
    if len(info["all_matching_rules"]) > 1:
        print(f"all matching rules ({len(info['all_matching_rules'])}, last wins):")
        for r in info["all_matching_rules"]:
            print(f"  {r['pattern']:30s}  base={r['base']}")


def cmd_thresholds(args):
    """List or recompute per-project, per-channel adaptive resolver
    thresholds (v0.3.10)."""
    from adaptive_threshold import (list_thresholds, get_threshold,
                                      compute_optimal_threshold)
    project_slug = args.project or _project_slug_for_cwd()
    with _conn() as c:
        c.autocommit = True
        if args.tcmd == "list":
            rows = list_thresholds(c, project_slug=args.project)
            if not rows:
                print(f"no thresholds tuned yet "
                      f"(project={args.project or '<all>'})")
                return
            print(f"{len(rows)} threshold row(s)")
            for r in rows:
                prec = (f"prec={r['precision_at_top']:.2f}"
                         if r["precision_at_top"] is not None else "prec=?")
                rec = (f"recall={r['recall_at_top3']:.2f}"
                        if r["recall_at_top3"] is not None else "recall=?")
                print(f"  {r['project_slug']:20s} {r['channel']:10s} "
                      f"t={r['threshold']:.2f}  obs={r['outcomes_observed']:4d}  "
                      f"{prec}  {rec}")
        elif args.tcmd == "tune":
            channels = ([args.channel] if args.channel
                         else ["symbol", "route", "page", "env",
                                "db", "graphql", "flag"])
            for ch in channels:
                res = compute_optimal_threshold(project_slug, ch, c)
                if res is None:
                    print(f"  {ch:10s}  insufficient outcomes "
                          f"(< 25) — keeping default")
                    continue
                t, obs, prec, recall = res
                print(f"  {ch:10s}  optimal={t:.2f}  "
                      f"obs={obs}  prec={prec:.2f}  recall={recall:.2f}")
                # Also write to the cache.
                get_threshold(project_slug, ch, c, recompute=True)


def cmd_flags(args):
    """Browse / resolve indexed feature flag declarations."""
    from flag_indexer import list_flags, resolve_flag
    project_slug = args.project or _project_slug_for_cwd()
    with _conn() as c:
        if args.fcmd == "list":
            rows = list_flags(project_slug, c, source=args.source,
                              kill_switches_only=args.kill_switches_only,
                              limit=args.limit)
            if not rows:
                print(f"no feature flags indexed for '{project_slug}'"); return
            print(f"{len(rows)} flag(s) in '{project_slug}'")
            for r in rows:
                marker = "🛑" if r["is_kill_switch"] else "  "
                sources = ",".join(r["sources"])
                rel = ""
                if r.get("canonical_file"):
                    rel = f"  {r['canonical_file']}:{r['canonical_line']}"
                print(f"  {marker} {r['flag_key']:35s} [{sources}]{rel}")
        else:  # check
            hits = resolve_flag(args.flag_key, project_slug, c, fuzzy=True,
                                limit=args.limit)
            if not hits:
                print(f"no flag matching `{args.flag_key}` in '{project_slug}' — "
                      f"would be flagged as fabricated.")
                sys.exit(1)
            for h in hits:
                marker = "✓" if h["match_kind"] == "exact" else "~"
                sim = f" sim={h['trigram_sim']:.2f}" if h["match_kind"] == "fuzzy" else ""
                kill = " 🛑" if h["is_kill_switch"] else ""
                print(f"  {marker} {h['flag_key']:30s} [{h['source']}]{kill}  "
                      f"{h['source_file']}:{h['start_line']}{sim}")


def cmd_gql(args):
    """Browse / resolve indexed GraphQL types + fields."""
    from graphql_indexer import (list_gql_types, list_gql_fields,
                                   resolve_gql_field, resolve_gql_type)
    project_slug = args.project or _project_slug_for_cwd()
    with _conn() as c:
        if args.gcmd == "types":
            rows = list_gql_types(project_slug, c, kind=args.kind,
                                    source=args.source, limit=args.limit)
            if not rows:
                print(f"no GraphQL types indexed for '{project_slug}'"); return
            print(f"{len(rows)} type(s) in '{project_slug}'")
            for r in rows:
                print(f"  {r['kind']:10s} {r['type_name']:25s} [{r['source']}]  "
                      f"{r['field_count']} fields  "
                      f"{r['source_file']}:{r['start_line']}")
        elif args.gcmd == "fields":
            rows = list_gql_fields(project_slug, c, type_name=args.type,
                                    limit=args.limit)
            if not rows:
                print(f"no GraphQL fields indexed in '{project_slug}'"); return
            print(f"{len(rows)} field(s)")
            for r in rows:
                badges = []
                if not r["is_nullable"]: badges.append("required")
                if r["is_list"]: badges.append("list")
                badge = (" [" + ",".join(badges) + "]") if badges else ""
                print(f"  {r['type_name']:20s}.{r['field_name']:25s} "
                      f"{r['field_type'] or '?':15s}{badge}")
        elif args.gcmd == "check":
            if "." in args.target:
                tn, fn = args.target.split(".", 1)
            else:
                tn, fn = None, args.target
            hits = resolve_gql_field(tn, fn, project_slug, c, fuzzy=True,
                                      limit=args.limit)
            if not hits:
                print(f"no GraphQL field matching `{args.target}` in '{project_slug}' — "
                      f"would be flagged as fabricated.")
                sys.exit(1)
            for h in hits:
                marker = ("✓" if h["match_kind"] == "exact" else
                          ("≈" if h["match_kind"] == "cross-type" else "~"))
                sim = (f" sim={h['trigram_sim']:.2f}"
                        if "fuzzy" in h["match_kind"] else "")
                print(f"  {marker} {h['type_name']:20s}.{h['field_name']:25s} "
                      f"[{h['source']}]  {h['source_file']}:{h['start_line']}{sim}")
        else:  # check-type
            hits = resolve_gql_type(args.target, project_slug, c,
                                      fuzzy=True, limit=args.limit)
            if not hits:
                print(f"no GraphQL type named `{args.target}` in '{project_slug}'")
                sys.exit(1)
            for h in hits:
                marker = "✓" if h["match_kind"] == "exact" else "~"
                sim = f" sim={h['trigram_sim']:.2f}" if h["match_kind"] == "fuzzy" else ""
                print(f"  {marker} {h['type_name']:25s} kind={h['kind']:12s} "
                      f"[{h['source']}]  {h['source_file']}:{h['start_line']}{sim}")


def cmd_db(args):
    """Browse / resolve indexed DB models + fields."""
    from db_indexer import (list_db_models, list_db_fields, resolve_db_field,
                              resolve_db_model)
    project_slug = args.project or _project_slug_for_cwd()
    with _conn() as c:
        if args.dcmd == "models":
            rows = list_db_models(project_slug, c, source=args.source,
                                    limit=args.limit)
            if not rows:
                print(f"no DB models indexed for '{project_slug}'"); return
            print(f"{len(rows)} model(s) in '{project_slug}'")
            for r in rows:
                tn = f" → {r['table_name']}" if r.get("table_name") else ""
                print(f"  {r['model_name']:25s} [{r['source']}]  "
                      f"{r['field_count']} fields{tn}  "
                      f"{r['source_file']}:{r['start_line']}")
        elif args.dcmd == "fields":
            rows = list_db_fields(project_slug, c, model_name=args.model,
                                    limit=args.limit)
            if not rows:
                print(f"no fields indexed in '{project_slug}'"); return
            print(f"{len(rows)} field(s)")
            for r in rows:
                badges = []
                if r["is_id"]: badges.append("id")
                if r["is_unique"]: badges.append("unique")
                if r["is_relation"]: badges.append(f"→{r['related_model']}")
                badge = (" [" + ",".join(badges) + "]") if badges else ""
                print(f"  {r['model_name']:20s}.{r['field_name']:25s} "
                      f"{r['field_type'] or '?':10s}{badge}")
        elif args.dcmd == "check":
            # Either MODEL.FIELD or just FIELD (cross-model search).
            if "." in args.target:
                mn, fn = args.target.split(".", 1)
            else:
                mn, fn = None, args.target
            hits = resolve_db_field(mn, fn, project_slug, c, fuzzy=True,
                                      limit=args.limit)
            if not hits:
                print(f"no field matching `{args.target}` in '{project_slug}' — "
                      f"would be flagged as fabricated.")
                sys.exit(1)
            for h in hits:
                marker = ("✓" if h["match_kind"] == "exact" else
                          ("≈" if h["match_kind"] == "cross-model" else "~"))
                sim = (f" sim={h['trigram_sim']:.2f}"
                        if "fuzzy" in h["match_kind"] else "")
                print(f"  {marker} {h['model_name']:20s}.{h['field_name']:25s} "
                      f"[{h['source']}]  {h['source_file']}:{h['start_line']}{sim}")
        else:  # check-model
            hits = resolve_db_model(args.target, project_slug, c,
                                      fuzzy=True, limit=args.limit)
            if not hits:
                print(f"no model named `{args.target}` in '{project_slug}'")
                sys.exit(1)
            for h in hits:
                marker = "✓" if h["match_kind"] == "exact" else "~"
                sim = f" sim={h['trigram_sim']:.2f}" if h["match_kind"] == "fuzzy" else ""
                print(f"  {marker} {h['model_name']:25s} [{h['source']}]  "
                      f"{h['source_file']}:{h['start_line']}{sim}")


def cmd_env(args):
    """List indexed env vars or resolve a proposed name."""
    from env_indexer import list_env_vars, resolve_env_var
    project_slug = args.project or _project_slug_for_cwd()
    with _conn() as c:
        if args.ecmd == "list":
            rows = list_env_vars(project_slug, c,
                                  source=args.source,
                                  secrets_only=args.secrets_only,
                                  limit=args.limit)
            if not rows:
                print(f"no env vars indexed for '{project_slug}'"); return
            print(f"{len(rows)} env var(s) in '{project_slug}'")
            for r in rows:
                marker = "🔒" if r["is_secret"] else "  "
                sources = ",".join(r["sources"])
                desc = f"  # {r['description']}" if r.get("description") else ""
                file_loc = (f"  {r['canonical_file']}:{r['canonical_line']}"
                            if r.get("canonical_file") else "")
                print(f"  {marker} {r['name']:35s} [{sources}]{file_loc}{desc}")
        else:  # check
            hits = resolve_env_var(args.name, project_slug, c, fuzzy=True,
                                    limit=args.limit)
            if not hits:
                print(f"no env var matching `{args.name}` in '{project_slug}' — "
                      f"would be flagged as fabricated.")
                sys.exit(1)
            for h in hits:
                marker = "✓" if h["match_kind"] == "exact" else "~"
                sim = f" sim={h['trigram_sim']:.2f}" if h["match_kind"] == "fuzzy" else ""
                secret = " 🔒" if h["is_secret"] else ""
                print(f"  {marker} {h['name']:30s} [{h['source']}]{secret}  "
                      f"{h['source_file']}:{h['start_line']}{sim}")


def cmd_about(args):
    """Show who built claude-dejavu, what it does, optionally with
    project stats. No side effects — never captures email."""
    project_slug = args.project or _project_slug_for_cwd()
    print("claude-dejavu — grounding for LLM-generated code")
    print()
    print("Eight grounding domains catch fabricated names BEFORE they hit")
    print("disk: symbols (9 languages), HTTP routes, frontend pages,")
    print("env vars, DB schema (Prisma/Drizzle/TypeORM/SQL), GraphQL,")
    print("feature flags, plus an LSP wire-protocol passthrough.")
    print()
    print("Built by HTE Switzerland — a digital solutions partner.")
    print("  Custom software · Cloud · Web/mobile · UX/UI · Hosting")
    print("  · Outsourcing · Digital coaching")
    print()
    print("If you liked how this is built, that's the kind of work HTE")
    print("delivers.")
    print("    https://hendrikthurau.enterprises")
    print()
    print("Public hallucination-rate benchmark:")
    print("    https://hendrikthurau.enterprises/claude-dejavu/benchmark")
    print()
    print("Source / issues / contributions:")
    print("    https://github.com/HthSolid/claude-dejavu")
    print()
    print("License: FSL-1.1-ALv2 — fair-source, fully functional forever.")
    print("Free for individual + internal corporate use; commercial license")
    print("available — dejavu@hendrikthurau.enterprises. Each version converts")
    print("to Apache 2.0 two years after release.")
    if args.project or args.with_stats:
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute("SELECT count(*) FROM symbols WHERE project_slug = %s",
                             (project_slug,))
                sym = cur.fetchone()[0] or 0
                cur.execute("SELECT count(*) FROM api_routes WHERE project_slug = %s",
                             (project_slug,))
                rt = cur.fetchone()[0] or 0
                cur.execute("SELECT count(*) FROM page_routes WHERE project_slug = %s",
                             (project_slug,))
                pg = cur.fetchone()[0] or 0
                cur.execute("SELECT count(*) FROM env_var_decls WHERE project_slug = %s",
                             (project_slug,))
                env = cur.fetchone()[0] or 0
                cur.execute("SELECT count(*) FROM db_models WHERE project_slug = %s",
                             (project_slug,))
                mdl = cur.fetchone()[0] or 0
                cur.execute("SELECT count(*) FROM graphql_types WHERE project_slug = %s",
                             (project_slug,))
                gql = cur.fetchone()[0] or 0
                cur.execute("SELECT count(*) FROM feature_flags WHERE project_slug = %s",
                             (project_slug,))
                fl = cur.fetchone()[0] or 0
            print()
            print(f"━━ what dejavu knows about '{project_slug}' ━━")
            print(f"  symbols       : {sym:>6}")
            print(f"  api routes    : {rt:>6}")
            print(f"  pages         : {pg:>6}")
            print(f"  env vars      : {env:>6}")
            print(f"  db models     : {mdl:>6}")
            print(f"  graphql types : {gql:>6}")
            print(f"  feature flags : {fl:>6}")
        except Exception as e:
            print(f"(project stats unavailable: {type(e).__name__})",
                   file=sys.stderr)


def cmd_doctor(args):
    """Run the diagnostic battery + optionally apply safe auto-fixes."""
    from doctor import run_doctor, report_to_text, report_to_dict
    rep = run_doctor(deep=bool(getattr(args, "deep", False)))
    if args.json:
        print(json.dumps(report_to_dict(rep), indent=2, default=str))
    else:
        print(report_to_text(rep))
    if args.fix:
        # Auto-fixes that are safe to attempt non-interactively.
        # Currently: re-run scripts/install.py --auto if install_status is incomplete.
        if rep.install_status and rep.install_status.get("status") == "incomplete":
            print("\n[--fix] re-running scripts/install.py --auto to recover...")
            import subprocess
            cmd = [sys.executable, str(Path(__file__).resolve().parent.parent /
                                       "scripts" / "install.py"), "--auto"]
            subprocess.run(cmd)
    sys.exit({"ok": 0, "warn": 1, "fail": 2}.get(rep.overall, 0))


def cmd_config(args):
    """Read/write claude-dejavu settings in config.env via a typed schema."""
    from config_service import (
        get as cfg_get, set_value as cfg_set, reset as cfg_reset,
        list_settings, list_to_text, SETTINGS,
    )
    action = args.action
    if action == "list":
        rows = list_settings(include_secrets=getattr(args, "show_secrets", False))
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            print(list_to_text(rows))
    elif action == "get":
        try:
            print(cfg_get(args.key))
        except KeyError as e:
            print(str(e), file=sys.stderr); sys.exit(1)
    elif action == "set":
        try:
            cfg_set(args.key, args.value)
        except (KeyError, ValueError, PermissionError) as e:
            print(str(e), file=sys.stderr); sys.exit(1)
        s = SETTINGS[args.key]
        print(f"  ✓ {args.key} = {args.value}")
        if s.runtime == "next_session":
            print("  (takes effect on next Claude Code session)")
        elif s.runtime == "next_install":
            print("  (read-only via this UI — change requires re-running install.py)")
    elif action == "reset":
        try:
            cfg_reset(args.key)
        except KeyError as e:
            print(str(e), file=sys.stderr); sys.exit(1)
        print(f"  ✓ {args.key} reset to default ({SETTINGS[args.key].default})")


def cmd_outcomes(args):
    """Inspect correction_outcomes — what corrections did we propose, and what stuck?"""
    from symbol_indexer import list_outcomes
    project_slug = args.project or _project_slug_for_cwd() if not args.global_ else None
    with _conn() as c:
        rows = list_outcomes(c, project_slug=project_slug, language=args.language,
                             mode=args.mode, limit=args.limit)
    if not rows:
        print("no outcomes recorded yet"); return
    print(f"{len(rows)} outcome(s):")
    for r in rows:
        ts = r["ts"].strftime("%Y-%m-%d %H:%M") if r.get("ts") else "-"
        proposed = r.get("proposed_name") or "-"
        resolved = r.get("resolved_name") or "-"
        ed = r.get("edit_distance"); ed = f"ed={ed}" if ed is not None else ""
        sim = r.get("trigram_sim"); sim = f"sim={sim:.2f}" if sim is not None else ""
        tags = ",".join(r.get("pattern_tags") or [])
        print(f"  {ts}  {(r.get('mode') or '-'):8s}  {proposed:25s} -> {resolved:25s}  {ed} {sim}  outcome={r.get('outcome')}  [{tags}]")


# ─── v0.8.0: session-copy CLI ───────────────────────────────────────────────

def _session_copy_spec(data_root: Optional[Path] = None):
    """Build a RepoSpec from the user's data root.

    Lazy-imports session_copy so the rest of recall.py keeps working
    when restic isn't installed yet.
    """
    import session_copy as _sc  # type: ignore[import]
    if data_root is None:
        data_root = _resolve_data_root()
    return _sc, _sc.RepoSpec(
        repo_path=data_root / "restic-repo",
        passphrase_file=data_root / ".restic-pass",
        bin_path=data_root / "bin" / "restic",
    ), data_root


def cmd_session_copy(args):
    """`claude-dejavu session-copy <sub> [...]` — dispatch for the v0.8.0
    restic-backed session-copy subsystem (spec §3.3).

    Sub-subcommands: init, take, list, diff, restore, mount, prune,
    status, check, migrate-from-rsync, reclaim-legacy.

    Exit codes: 0 success, 1 generic failure, 2 invalid args, 3 restic
    not installed, 130 user abort.
    """
    sc_sub = getattr(args, "sc_sub", None)
    try:
        _sc, spec, data_root = _session_copy_spec()
    except ImportError as exc:
        print(f"  ✗ session_copy module unavailable: {exc}\n"
              f"     run scripts/install.py to bootstrap v0.8.0",
              file=sys.stderr)
        return 3

    if sc_sub == "init":
        try:
            _sc.init_repo(spec,
                           prompt_passphrase=not getattr(args, "yes", False))
            print(f"  ✓ repo at {spec.repo_path}")
        except Exception as exc:
            print(f"  ✗ init failed: {exc}", file=sys.stderr)
            return 1
        return 0

    if sc_sub == "take":
        # Default sources: ~/.claude/projects + claude-mem.db (if present)
        sources: list = []
        proj = Path.home() / ".claude" / "projects"
        if proj.is_dir():
            sources.append(proj)
        mem_db = Path.home() / ".claude-mem" / "claude-mem.db"
        if mem_db.is_file():
            sources.append(mem_db)
        if not sources:
            print("  ✗ nothing to back up (no ~/.claude/projects, "
                  "no claude-mem.db)", file=sys.stderr)
            return 2
        include_raw = getattr(args, "include", None)
        include_set: Optional[set] = None
        if include_raw:
            include_set = set()
            for item in include_raw:
                for piece in str(item).split(","):
                    piece = piece.strip().lower()
                    if piece:
                        include_set.add(piece)
        try:
            snap = _sc.take(spec, sources,
                              tag=args.tag, message=args.message,
                              ctx_data_root=data_root,
                              include=include_set)
        except Exception as exc:
            print(f"  ✗ take failed: {exc}", file=sys.stderr)
            return 1
        if snap and snap.id:
            print(f"  ✓ snapshot {snap.id[:12]}")
        else:
            print("  (no snapshot id reported)")
        return 0

    if sc_sub == "list":
        try:
            snaps = _sc.list_snapshots(spec, tag=args.tag,
                                          last=args.last, since=args.since)
        except Exception as exc:
            print(f"  ✗ list failed: {exc}", file=sys.stderr)
            return 1
        if not snaps:
            print("  (no snapshots)")
            return 0
        print(f"  {'id':14}  {'time':22}  {'tags':25}  paths")
        print("  " + "-" * 78)
        for s in snaps:
            tags = ",".join(s.tags or [])
            paths = ",".join(s.paths or [])
            print(f"  {s.id[:12]:14}  {s.time[:22]:22}  {tags[:25]:25}  "
                  f"{paths[:60]}")
        return 0

    if sc_sub == "diff":
        try:
            d = _sc.diff(spec, args.snap_a, args.snap_b)
        except Exception as exc:
            print(f"  ✗ diff failed: {exc}", file=sys.stderr)
            return 1
        print(f"  added:   {len(d['added'])}")
        print(f"  removed: {len(d['removed'])}")
        print(f"  changed: {len(d['changed'])}")
        if args.verbose:
            for kind in ("added", "removed", "changed"):
                for p in d[kind][:20]:
                    print(f"    {kind[0]} {p}")
        return 0

    if sc_sub == "restore":
        include_raw = getattr(args, "include", None)
        include_set: Optional[set] = None
        if include_raw:
            include_set = set()
            for item in include_raw:
                for piece in str(item).split(","):
                    piece = piece.strip().lower()
                    if piece:
                        include_set.add(piece)
        try:
            res = _sc.restore(spec, args.snapshot_id,
                                  Path(args.target),
                                  include=include_set,
                                  force=getattr(args, "force", False))
            print(f"  ✓ restored {args.snapshot_id[:12]} → {args.target}")
            if isinstance(res, dict):
                if res.get("pg"):
                    print(f"    pg: {res['pg']}")
                if res.get("weaviate"):
                    print(f"    weaviate: {res['weaviate']}")
        except Exception as exc:
            print(f"  ✗ restore failed: {exc}", file=sys.stderr)
            return 1
        return 0

    if sc_sub == "mount":
        # FUSE mount — restic native on Linux; mac/Windows need extra fs.
        target = Path(args.mountpoint)
        target.mkdir(parents=True, exist_ok=True)
        try:
            _sc._run_restic(spec, ["mount", str(target)], check=True,
                              json_out=False)
        except Exception as exc:
            print(f"  ✗ mount failed: {exc}\n"
                  f"     on macOS install macFUSE; on Windows install "
                  f"WinFsp; or use 'restore' instead.",
                  file=sys.stderr)
            return 1
        return 0

    if sc_sub == "prune":
        try:
            r = _sc.prune(spec, dry_run=args.dry_run, force=args.force)
        except Exception as exc:
            print(f"  ✗ prune failed: {exc}", file=sys.stderr)
            return 1
        print(r.get("output", ""))
        if not r.get("ran"):
            print("  (sentinel-gated; pass --force to override)")
        return 0

    if sc_sub == "status":
        try:
            st = _sc.status(spec)
        except Exception as exc:
            print(f"  ✗ status failed: {exc}", file=sys.stderr)
            return 1
        size_mb = st["repo_size"] / (1024 * 1024)
        age = st.get("last_snapshot_age_s")
        age_str = f"{age/3600:.1f}h" if age is not None else "n/a"
        print(f"  repo:           {st['repo_path']}")
        print(f"  size:           {size_mb:.1f} MB")
        print(f"  snapshots:      {st['snapshot_count']}")
        print(f"  last snapshot:  {age_str} ago")
        lc = st.get("last_check") or {}
        print(f"  last check:     {'ok' if lc.get('ok') else 'unknown'} "
              f"(ts={lc.get('ts','-')})")
        return 0

    if sc_sub == "check":
        try:
            r = _sc.check(spec, read_data=args.read_data,
                           max_age_hours=24, force=args.force)
        except Exception as exc:
            print(f"  ✗ check failed: {exc}", file=sys.stderr)
            return 1
        print(f"  {'cached' if r.get('cached') else 'ran'}  "
              f"{'✓ ok' if r.get('ok') else '✗ FAIL'}")
        if not r.get("ok"):
            print(r.get("output", ""))
            return 1
        return 0

    if sc_sub == "migrate-from-rsync":
        import migrate_rsync as _mr
        try:
            rc = _mr.run(mode=args.mode, yes=args.yes,
                          data_root=data_root)
        except Exception as exc:
            print(f"  ✗ migrate failed: {exc}", file=sys.stderr)
            return 1
        return rc

    if sc_sub == "reclaim-legacy":
        # v0.8.3 post-release polish: delete the snapshots.deleteme.<ts>
        # tree left by step 9 of migrate-from-rsync. TTY-guarded +
        # typed-confirm; --yes skips the confirm; --dry-run prints plan.
        import migrate_rsync as _mr
        try:
            rc = _mr.reclaim_legacy_trees(
                home=Path.home(),
                yes=bool(getattr(args, "yes", False)),
                dry_run=bool(getattr(args, "dry_run", False)))
        except Exception as exc:
            print(f"  ✗ reclaim-legacy failed: {exc}", file=sys.stderr)
            return 1
        return rc

    print(f"  ✗ unknown session-copy subcommand: {sc_sub!r}",
          file=sys.stderr)
    return 2


# ─── v0.8.2: rescue-shard CLI ───────────────────────────────────────────────


def cmd_rescue_shard(args):
    """`claude-dejavu rescue-shard --class <C> [--shard <S>] [--dry-run]
    [--yes]` — recover records from a corrupted Weaviate shard.

    Four phases (spec §3, v0.8.2 CHANGELOG): walk segments via the Go
    binary, drop+recreate the class, batch-restore via /v1/batch/objects,
    gap-reindex missing UUIDs from the PG canonical. --dry-run stops
    after Phase 1 and prints the plan.

    Exit codes: 0 ok, 1 generic failure, 2 invalid args, 3 binary not
    installed, 130 user abort.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import rescue as _rescue  # type: ignore[import]
    except ImportError as exc:
        print(f"  ✗ rescue module unavailable: {exc}", file=sys.stderr)
        return 1

    class_name: str = args.class_name
    shard: Optional[str] = getattr(args, "shard", None)
    dry_run: bool = bool(getattr(args, "dry_run", False))
    yes: bool = bool(getattr(args, "yes", False))
    include_quarantined = getattr(args, "include_quarantined", None)
    if include_quarantined:
        include_quarantined = Path(include_quarantined)
    host_data_dir = getattr(args, "host_data_dir", None)
    if host_data_dir:
        host_data_dir = Path(host_data_dir)
    container = getattr(args, "container", "claude-dejavu-weaviate")

    # Destructive on the live Weaviate — require explicit --yes when
    # NOT in dry-run mode (or an interactive typed confirm).
    if not dry_run and not yes:
        print(f"\nrescue-shard will DELETE the Weaviate class "
              f"{class_name!r} and rebuild it from segment + PG.")
        print("All vectors not recoverable from segments and not "
              "present in PG will be permanently lost.")
        try:
            reply = input("Type 'yes' to continue: ").strip().lower()
        except EOFError:
            reply = ""
        if reply != "yes":
            print("aborted.", file=sys.stderr)
            return 130

    try:
        plan = _rescue.rescue_shard(
            class_name=class_name,
            shard=shard,
            docker_container=container,
            host_data_dir=host_data_dir,
            include_quarantined=include_quarantined,
            dry_run=dry_run,
        )
    except _rescue.RescueError as exc:
        print(f"  ✗ rescue failed: {exc}", file=sys.stderr)
        return 1

    print()
    print(f"class:   {plan.class_name}")
    print(f"shards:  {', '.join(plan.shards)}")
    print(f"mode:    {'DRY-RUN' if plan.dry_run else 'APPLY'}")
    print("walk:")
    total_records = 0
    for sid, info in plan.walk.items():
        total_records += info.get("record_count", 0)
        summary = info.get("summary", {})
        print(f"  {sid}: records={info.get('record_count', 0)}, "
              f"plausible={summary.get('plausible', '?')}, "
              f"tombstones={summary.get('tombstones', '?')}")
    print(f"total recovered: {total_records}")
    if plan.dry_run:
        would = plan.restore.get("would_send", "?")
        print(f"\n(dry-run) would send {would} object(s) to Weaviate.")
        print("Re-run without --dry-run to apply.")
        return 0

    if plan.reset.get("failed"):
        print(f"  ✗ class reset failed: {plan.reset['failed']}",
              file=sys.stderr)
        return 1
    print(f"reset:   deleted={plan.reset.get('deleted')}, "
          f"recreated={plan.reset.get('recreated')}")
    print(f"restore: sent={plan.restore.get('sent', 0)}, "
          f"succeeded={plan.restore.get('succeeded', 0)}, "
          f"failed={len(plan.restore.get('failed', []))}")
    if plan.gap.get("missing"):
        print(f"gap:     expected_in_pg={plan.gap.get('expected_in_pg', 0)}, "
              f"missing={len(plan.gap.get('missing', []))}, "
              f"reindexed={plan.gap.get('reindexed', 0)}")
    if plan.gap.get("errors"):
        for e in plan.gap["errors"]:
            print(f"  ⚠ gap: {e}", file=sys.stderr)
    return 0


# ─── v0.8.3: pg-rescue + weaviate-rescue CLI ────────────────────────────────


def cmd_pg_rescue(args):
    """`claude-dejavu pg-rescue <sub>` — PG corruption rescue toolkit.

    Sub-subcommands: scan, patch-toast, reset-flags, rebuild-table, reindex.
    Exit codes: 0 success, 1 generic failure, 2 invalid args, 130 user abort.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import pg_rescue as _pg  # type: ignore[import]
    except ImportError as exc:
        print(f"  ✗ pg_rescue module unavailable: {exc}",
              file=sys.stderr)
        return 1
    sub = getattr(args, "pg_sub", None)
    db_name = getattr(args, "db", None)
    dry_run = not bool(getattr(args, "yes", False))

    try:
        if sub == "scan":
            r = _pg.scan(db_name=db_name)
            if getattr(args, "json", False):
                print(json.dumps(r, indent=2, default=str))
            else:
                print(f"  probed_rows: {r['probed_rows']}")
                print(f"  corrupt_rows: {len(r['corrupt_rows'])}")
                for row in r["corrupt_rows"][:20]:
                    print(f"    {row['table']}#{row['id']} "
                          f"col={row['col']}: {row['error'][:80]}")
                print(f"  corrupt_indexes: {len(r['corrupt_indexes'])}")
                for idx in r["corrupt_indexes"]:
                    print(f"    {idx['name']}: {idx['error']}")
            return 0
        if sub == "patch-toast":
            r = _pg.patch_toast(db_name=db_name, dry_run=dry_run)
            mode = r["mode"]
            print(f"  [{mode}] patched: {r['patched']}, "
                  f"errored: {r['errored']}, "
                  f"skipped: {r['skipped']}")
            for row in r["per_row"][:20]:
                print(f"    id={row.get('id')} status={row.get('status')}")
            return 0
        if sub == "reset-flags":
            r = _pg.reset_flags(db_name=db_name, dry_run=dry_run)
            print(f"  [{r['mode']}] reset: {r['reset']} / {r['total']}, "
                  f"errored: {r['errored']}")
            if r["errored_ids"]:
                print(f"    first errored ids: "
                      f"{r['errored_ids'][:10]}")
            return 0
        if sub == "rebuild-table":
            tbl = args.table
            excl_raw = getattr(args, "exclude_ids", None) or ""
            excl = [int(x) for x in str(excl_raw).split(",")
                       if x.strip().isdigit()]
            r = _pg.rebuild_table(
                tbl,
                exclude_ids=excl,
                replace_from_jsonl=bool(getattr(args, "replace_from_jsonl",
                                                False)),
                db_name=db_name,
                dry_run=dry_run,
            )
            print(f"  [{r['mode']}] table={r['table']} "
                  f"copied={r['copied']} replaced={r['replaced']} "
                  f"errored={r['errored']}")
            deps = r.get("dependents") or {}
            print(f"    dependents: fks={len(deps.get('fks', []))} "
                  f"views={len(deps.get('views', []))} "
                  f"indexes={len(deps.get('indexes', []))} "
                  f"sequences={len(deps.get('sequences', []))}")
            return 0
        if sub == "reindex":
            r = _pg.reindex(db_name=db_name,
                                 all_indexes=bool(getattr(args, "all", False)),
                                 concurrently=bool(getattr(
                                     args, "concurrently", True)),
                                 dry_run=dry_run)
            print(f"  [{r['mode']}] reindexed: {len(r['reindexed'])}, "
                  f"errored: {len(r['errored'])}")
            for name in r["reindexed"][:20]:
                print(f"    ✓ {name}")
            for e in r["errored"]:
                print(f"    ✗ {e['name']}: {e['error'][:80]}")
            return 0
    except _pg.RescueError as exc:
        print(f"  ✗ pg-rescue failed: {exc}", file=sys.stderr)
        return 1
    print(f"  ✗ unknown pg-rescue subcommand: {sub!r}",
          file=sys.stderr)
    return 2


def cmd_weaviate_rescue(args):
    """`claude-dejavu weaviate-rescue <sub>` — Weaviate rescue toolkit.

    Sub-subcommands: scan, quarantine-segment, redo-class.
    Exit codes: 0 success, 1 generic failure, 2 invalid args, 130 abort.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import weaviate_rescue as _wv  # type: ignore[import]
    except ImportError as exc:
        print(f"  ✗ weaviate_rescue module unavailable: {exc}",
              file=sys.stderr)
        return 1
    sub = getattr(args, "wv_sub", None)
    dry_run = not bool(getattr(args, "yes", False))

    try:
        if sub == "scan":
            r = _wv.scan(class_filter=getattr(args, "class_filter", None))
            if getattr(args, "json", False):
                print(json.dumps(r, indent=2, default=str))
            else:
                print(f"  reachable: {r['reachable']}")
                print(f"  embed_dim: {r['embed_dim']}")
                print(f"  unhealthy_shards: "
                      f"{len(r['unhealthy_shards'])}")
                for sh in r["unhealthy_shards"][:10]:
                    print(f"    {sh['class']} / {sh['shard']}: "
                          f"{sh['status']}")
                print(f"  dim_mismatches: {len(r['dim_mismatches'])}")
                for dm in r["dim_mismatches"]:
                    print(f"    {dm['class']}: "
                          f"expected={dm['expected']} "
                          f"actual={dm['actual']}")
                print(f"  missing_classes: {r['missing_classes']}")
            return 0
        if sub == "quarantine-segment":
            r = _wv.quarantine_segment(
                args.segment_id,
                class_name=args.class_name,
                shard_id=args.shard_id,
                dry_run=dry_run,
            )
            print(f"  [{r.mode}] quarantined {len(r.moved)} files "
                  f"into {r.quarantine_dir}")
            for fn in r.moved:
                print(f"    ✓ {fn}")
            return 0
        if sub == "redo-class":
            r = _wv.redo_class(
                args.class_name,
                dim=getattr(args, "dim", None),
                vectorizer=getattr(args, "vectorizer", "none"),
                reset_pg_flags=bool(getattr(
                    args, "reset_pg_flags", True)),
                dry_run=dry_run,
            )
            print(f"  [{r['mode']}] deleted={r['deleted']} "
                  f"recreated={r['recreated']}")
            if r.get("pg_reset"):
                print(f"    pg_reset: {r['pg_reset']}")
            return 0
    except _wv.WeaviateRescueError as exc:
        print(f"  ✗ weaviate-rescue failed: {exc}", file=sys.stderr)
        return 1
    print(f"  ✗ unknown weaviate-rescue subcommand: {sub!r}",
          file=sys.stderr)
    return 2


# ─── what-shipped + memory record-version (v0.8.4 G1 — passive context priming) ─

def _git_release_commits(repo: Path, n: int) -> dict[str, str]:
    """Return `{version: short_sha}` for the most recent `chore(release):
    vX.Y.Z` commits in `repo`. Best-effort; returns {} on any git error
    (no repo, no git, no matching commits). Bounded by `-<2*n>` log scan
    so we don't read the whole history of a 10k-commit repo."""
    import re as _re
    try:
        # `-2*n` is heuristic: with one release commit per version most
        # of the time, we want at least N back, with some headroom for
        # interspersed fix-up commits between release commits.
        r = subprocess.run(
            ["git", "-C", str(repo), "log", f"-{max(20, n * 2)}",
             "--pretty=format:%h %s"],
            capture_output=True, text=True, timeout=4,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return {}
    if r.returncode != 0:
        return {}
    out: dict[str, str] = {}
    # `chore(release): vX.Y.Z — anything` or `chore(release): X.Y.Z anything`.
    pat = _re.compile(r"^chore\(release\):\s*v?(\d+\.\d+\.\d+[a-z]?)\b")
    for ln in (r.stdout or "").splitlines():
        try:
            sha, msg = ln.split(" ", 1)
        except ValueError:
            continue
        m = pat.match(msg)
        if not m:
            continue
        version = m.group(1)
        out.setdefault(version, sha)  # first (newest) commit wins
    return out


def _parse_changelog_versions(path: Path) -> list[dict[str, Optional[str]]]:
    """Return [{version, date, summary}] for every `## [X.Y.Z]` section
    in `path`, newest first. Skips `## [Unreleased]`. The `summary` is
    produced by `changelog_distill.distill_changelog_entry`."""
    import re as _re
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import changelog_distill as _cd  # type: ignore[import]
    except ImportError:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    head_re = _re.compile(
        r"^##\s+\[(?P<version>[^\]]+)\]"
        r"\s*(?:[—–-]\s*(?P<date>.+?))?\s*$"
    )
    items: list[dict[str, Optional[str]]] = []
    for ln in text.splitlines():
        m = head_re.match(ln)
        if not m:
            continue
        v = m.group("version").strip()
        if v.lower() == "unreleased":
            continue
        items.append({
            "version": v,
            "date": (m.group("date") or "").strip() or None,
            "summary": _cd.distill_changelog_entry(path, v),
        })
    return items


def cmd_what_shipped(args):
    """`claude-dejavu what-shipped [--repo PATH] [--last N] [--json]`

    Reads `<repo>/CHANGELOG.md` + recent git log, prints the last N
    release headlines. Default: human-readable. With `--json`, an array
    of {version, date, commit, summary} objects.

    Exit codes: 0 ok, 1 generic failure (e.g. no CHANGELOG.md found),
    2 invalid args (handled by argparse before we reach here).
    """
    repo = Path(args.repo or ".").resolve()
    changelog = repo / "CHANGELOG.md"
    if not changelog.is_file():
        print(f"  ✗ no CHANGELOG.md at {changelog}", file=sys.stderr)
        return 1
    n = max(1, int(args.last))
    versions = _parse_changelog_versions(changelog)[:n]
    commit_map = _git_release_commits(repo, n)

    rows: list[dict[str, Optional[str]]] = []
    for entry in versions:
        rows.append({
            "version": entry["version"],
            "date":    entry["date"],
            "commit":  commit_map.get(entry["version"]),
            "summary": entry["summary"],
        })

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    if not rows:
        print(f"  (no release sections found in {changelog})")
        return 0

    print(f"claude-dejavu — last {len(rows)} release(s) in "
          f"{repo.name}/CHANGELOG.md:")
    print()
    for row in rows:
        line = f"  v{row['version']}"
        if row["date"]:
            line += f"  ({row['date']})"
        if row["commit"]:
            line += f"  [{row['commit']}]"
        print(line)
        if row["summary"]:
            # `distill_changelog_entry` already prefixed `vX.Y.Z:`; strip
            # that here so we don't print the version twice.
            summary = row["summary"]
            tag = f"v{row['version']}:"
            if summary.startswith(tag):
                summary = summary[len(tag):].lstrip()
            # Wrap at ~78 chars × max 6 lines.
            wrapped = textwrap.wrap(summary, width=78,
                                     initial_indent="    ",
                                     subsequent_indent="    ")
            for w in wrapped[:6]:
                print(w)
        print()
    return 0


def _versions_dir() -> Path:
    """Where the SessionStart digest hook (G1.c) reads per-project
    `project_<slug>_versions.md` files from. Owned by claude-dejavu —
    NOT `~/.claude/projects/.../memory/` (that's claude-code's
    territory and the SessionStart hook docs explicitly carve it out).
    """
    return _resolve_data_root() / "versions"


def cmd_memory_record_version(args):
    """`claude-dejavu memory record-version --repo PATH [--memory-dir PATH]`

    Distills the most recent `## [X.Y.Z]` section of `<repo>/CHANGELOG.md`
    via `changelog_distill.distill_changelog_entry`, then writes/updates
    a one-line entry in `<memory-dir>/project_<slug>_versions.md`.

    Idempotent — re-running on the same version updates the line in
    place. Returns 0 on success, 1 if no CHANGELOG.md / no parseable
    version section."""
    repo = Path(args.repo or ".").resolve()
    changelog = repo / "CHANGELOG.md"
    if not changelog.is_file():
        print(f"  ✗ no CHANGELOG.md at {changelog}", file=sys.stderr)
        return 1
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import changelog_distill as _cd  # type: ignore[import]
    except ImportError as exc:
        print(f"  ✗ changelog_distill unavailable: {exc}",
              file=sys.stderr)
        return 1
    version = _cd.latest_version(changelog)
    if not version:
        print(f"  ✗ no version section found in {changelog}",
              file=sys.stderr)
        return 1
    summary = _cd.distill_changelog_entry(changelog, version)
    if summary is None:
        print(f"  ✗ failed to distill section for v{version}",
              file=sys.stderr)
        return 1
    mdir = Path(args.memory_dir) if args.memory_dir else _versions_dir()
    fp = _cd.record_version(mdir, repo.name, version, summary)
    print(f"  ✓ recorded v{version} → {fp}")
    print(f"    {summary}")
    return 0


def cmd_memory_backfill_gists(args):
    """`claude-dejavu memory backfill-gists [--limit N] [--dry-run] [--workers M] [--force]`

    Walk every turn with `gist IS NULL AND content IS NOT NULL`, run
    `distill_pipeline.make_gist`, write `gist` + `gist_method` back to
    Postgres.

    v0.8.6 (Context Economy phase 3). Shells out to `ingester.py
    --backfill-gists` so the heavy import (psycopg2 + bridge) stays
    in one process and the recall CLI start-up stays cheap.

    Exit codes: 0 success (or dry-run completed), non-zero on ingester
    subprocess error.
    """
    plugin_dir = Path(__file__).resolve().parent
    cmd = [sys.executable, str(plugin_dir / "ingester.py"),
           "--backfill-gists"]
    if args.limit is not None:
        cmd.extend(["--limit", str(args.limit)])
    if args.workers is not None:
        cmd.extend(["--workers", str(args.workers)])
    if args.dry_run:
        cmd.append("--dry-run")
    if args.force:
        cmd.append("--force")
    # subprocess.call streams output live — the user sees per-worker
    # progress lines as they happen. Quiet mode unsupported on purpose:
    # this is an interactive admin command, not a hot-path tool.
    return subprocess.call(cmd)


# ─── v0.8.5 G2 — JIT recall preview ──────────────────────────────────────────


def cmd_jit_recall_preview(args):
    """`claude-dejavu jit-recall-preview "<prompt>"`

    Runs the UserPromptSubmit hook's recall logic against the live
    Weaviate + Postgres, without Claude Code in the loop. Useful for
    eyeballing what would be injected for a given prompt before
    flipping JIT_RECALL_ENABLED on.

    Exit codes:
      0 — ran successfully (block may still be empty if the prompt
          was too short, no hits cleared the threshold, or
          JIT_RECALL_ENABLED is false).
      1 — search backend failure (currently never returned — the
          hook is defensive-by-design and prefers an empty block).
          Reserved for future use.
      2 — invalid args (handled by argparse before we reach here).
    """
    # Reach into the hook module so the preview shares one
    # implementation with the live hook. Anything else risks drift.
    plugin_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(plugin_root / "hooks"))
    try:
        import user_prompt_submit as _ups  # type: ignore[import]
    except ImportError as exc:
        print(f"  ✗ user_prompt_submit hook not importable: {exc}",
              file=sys.stderr)
        return 1

    prompt = args.prompt or ""
    try:
        block = _ups.compute_recall_block(prompt)
    except Exception as exc:
        # The hook itself never raises, but the preview is allowed to
        # surface backend failures so the user sees them.
        print(f"  ✗ search backend failure: {exc}", file=sys.stderr)
        return 1

    if not block:
        # Be explicit so the user can tell "search ran, no hits" apart
        # from "feature disabled". Both paths exit 0.
        cfg = _ups._load_cfg()
        enabled = _ups._cfg_bool(cfg, "JIT_RECALL_ENABLED", False)
        if not enabled:
            print("(empty — JIT_RECALL_ENABLED=false in config.env)")
        else:
            print("(empty — no hits cleared the score threshold, or "
                  "prompt was below JIT_RECALL_MIN_PROMPT_CHARS)")
        return 0

    print(block)
    return 0


# v0.9.1 — vector-index admin CLI handlers.

def cmd_vector_index_rebuild(args):
    """Bootstrap the local numpy vector index from Weaviate. One-shot
    paginated fetch + persistence under <data_root>/vector_index/.
    """
    try:
        from vector_index import (  # type: ignore[import]
            TurnVectorIndex, _is_available)
    except ImportError as e:
        print(f"vector_index module unavailable: {e}",
              file=sys.stderr)
        return 1
    if not _is_available():
        print("numpy not installed in this venv — install it first: "
              "`pip install numpy` in the dejavu venv",
              file=sys.stderr)
        return 1
    cfg = _load_config()
    class_name = (args.class_name
                   or os.environ.get("CLAUDE_DEJAVU_WV_CLASS")
                   or cfg.get("WV_CLASS") or "ClaudeDejavuTurn")
    wv_url = (args.wv_url
               or cfg.get("WV_URL") or "http://localhost:8080")
    data_root = _resolve_data_root()
    slug = getattr(args, "slug", "_default") or "_default"
    project_filter = None if slug == "_default" else slug
    print(f"Bootstrapping vector index from {wv_url}/{class_name} "
          f"into {data_root / 'vector_index' / slug} "
          f"(project_filter={project_filter!r}) …")
    idx = TurnVectorIndex.load_or_empty(data_root, slug=slug)
    started = time.time()
    n = idx.bootstrap_from_weaviate(
        class_name, wv_url,
        batch_size=args.batch, max_objects=args.max_objects,
        project_slug=project_filter)
    if n == 0:
        print("Bootstrap fetched 0 vectors. Check WV_URL / WV_CLASS, "
              "or whether the Weaviate class actually carries `vector` "
              "alongside `turn_id` (server-side vectorizer:none + "
              "caller-supplied vector at ingest time is required).",
              file=sys.stderr)
        return 1
    idx.save()
    elapsed = time.time() - started
    print(f"Loaded {n:,} vectors (dim={idx.dim}) in {elapsed:.1f}s. "
          f"Index persisted.")
    print(f"\nEnable the local-vector branch via:")
    print(f"  export CLAUDE_DEJAVU_LOCAL_VECTOR_INDEX=true")
    return 0


def cmd_vector_index_status(args):
    """Show current vector-index status (numpy availability,
    on-disk index size, class, source URL, build time)."""
    try:
        from vector_index import (  # type: ignore[import]
            vector_index_status)
    except ImportError as e:
        print(f"vector_index module unavailable: {e}",
              file=sys.stderr)
        return 1
    data_root = _resolve_data_root()
    s = vector_index_status(data_root)
    print(f"data root:  {data_root}")
    print(f"numpy avail:{s.get('available')}")
    print(f"enabled:    {s.get('enabled')}")
    print(f"loaded:     {s.get('loaded')}")
    print(f"count:      {s.get('count'):,}")
    print(f"dim:        {s.get('dim')}")
    print(f"class_name: {s.get('class_name') or '(empty)'}")
    print(f"source_url: {s.get('source_url') or '(empty)'}")
    ts = s.get('built_at') or 0
    if ts:
        from datetime import datetime as _dt
        print(f"built_at:   "
              f"{_dt.utcfromtimestamp(ts).strftime('%Y-%m-%d %H:%M UTC')}")
    return 0


# v0.10.0 — ranker CLI handlers.

def cmd_ranker_status(args):
    """Show ranker mode + weights + arbiter stats."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import ranker as _ranker_mod  # type: ignore[import]
    except ImportError as e:
        print(f"ranker module unavailable: {e}", file=sys.stderr)
        return 1
    data_root = _resolve_data_root()
    s = _ranker_mod.ranker_status(data_root)
    print(f"mode (env):        {s.get('mode')}")
    print(f"promote_win_rate:  {s.get('promote_win_rate')}")
    print(f"promote_min_n:     {s.get('promote_min_n')}")
    print(f"demote_window_n:   {s.get('demote_window_n')}")
    print(f"weights_present:   {s.get('weights_present')}")
    print(f"weights_file:      {s.get('weights_file')}")
    print(f"numpy_available:   {s.get('numpy_available')}")
    print(f"feature weights:")
    for name, w in s.get("weights_summary", {}).items():
        print(f"  {name:<22s} {w:>7.3f}")
    # Arbiter stats — run aggregation from CLI.
    try:
        conn = _conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT count(*) FILTER (WHERE decided),"
                "       count(*) FILTER (WHERE NOT decided)"
                " FROM recall_ranking_trials")
            n_decided, n_open = cur.fetchone()
            cur.execute("""
                WITH recent AS (
                  SELECT champion_rank, challenger_rank
                  FROM recall_ranking_trials
                  WHERE decided = TRUE AND expanded = TRUE
                  ORDER BY ts DESC LIMIT %s
                )
                SELECT
                  count(*) FILTER (
                    WHERE challenger_rank < champion_rank),
                  count(*) FILTER (
                    WHERE challenger_rank > champion_rank),
                  count(*) FILTER (
                    WHERE challenger_rank = champion_rank)
                FROM recent
            """, (max(s.get('promote_min_n'),
                       s.get('demote_window_n')),))
            wins, losses, ties = cur.fetchone()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        print()
        print(f"ranking trials:")
        print(f"  decided:        {n_decided:,}")
        print(f"  open (no exp):  {n_open:,}")
        print(f"  recent window:  challenger wins={wins:,}, "
              f"losses={losses:,}, ties={ties:,}")
        n_dec = wins + losses
        if n_dec > 0:
            wr = wins / n_dec
            promote_when = ("promote" if wr >= s.get(
                'promote_win_rate') and n_dec >= s.get(
                'promote_min_n') else "(below gate)")
            print(f"  win_rate:       {wr*100:.1f}%  → {promote_when}")
    except Exception as e:  # noqa: BLE001
        print(f"\n(could not aggregate trials: {e})")
    return 0


def cmd_ranker_train(args):
    """Re-fit the LR on accumulated feedback + trial data."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from ranker import (  # type: ignore[import]
            Ranker, extract_features, baseline_weights,
            baseline_bias, fit_baseline_ranker, reset_ranker)
    except ImportError as e:
        print(f"ranker module unavailable: {e}", file=sys.stderr)
        return 1
    # Build training data from recall_feedback (positive = up,
    # negative = down) joined with the hit's metadata.
    data_root = _resolve_data_root()
    conn = _conn()
    X: list[list[float]] = []
    y: list[int] = []
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT rf.query_text, rf.turn_id, rf.signal_type,
                   rf.weight,
                   t.gist, t.content, t.ts, s.project_slug
            FROM recall_feedback rf
            JOIN turns t ON t.id = rf.turn_id
            JOIN sessions s ON s.id = t.session_id
            WHERE rf.ts > now() - interval '90 days'
            ORDER BY rf.ts DESC
            LIMIT 50000
        """)
        rows = cur.fetchall()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if not rows:
        print("No recall_feedback rows yet — refitting the "
              "synthetic baseline.")
        r = fit_baseline_ranker()
    elif len(rows) < 50:
        print(f"Only {len(rows)} feedback rows — need 50+ to "
              "train against real data. Refitting synthetic "
              "baseline instead.")
        r = fit_baseline_ranker()
    else:
        for q, tid, sig, w, gist, content, ts, proj in rows:
            hit = {
                "turn_id": tid, "gist": gist or "",
                "content": content or "",
                "project_slug": proj,
                "ts": ts.isoformat() if ts else None,
                "_additional": {"score": 0.5,
                                  "source": "pg-fts"},
            }
            feats = extract_features(
                hit, q or "", proj, popularity_boost=1.0)
            label = 1 if sig == "explicit_up" or sig == "implicit" \
                      else 0
            X.append(feats)
            y.append(label)
        r = Ranker(baseline_weights(), baseline_bias())
        r.fit(X, y, epochs=300, lr=0.05, l2=0.001)
        print(f"Trained on {len(X):,} samples.")
    r.to_file(data_root / "ranker" / "weights.json")
    reset_ranker()
    print(f"Weights saved to "
          f"{data_root / 'ranker' / 'weights.json'}")
    for name, ww in zip(("bm25", "hnsw", "recency", "overlap",
                          "same_proj", "src_wv", "popularity"),
                          r.weights):
        print(f"  {name:<12s} {ww:>+7.3f}")
    print(f"  bias        {r.bias:>+7.3f}")
    return 0


def _cmd_ranker_force(mode: str):
    """Implementation of `ranker force-promote` and
    `ranker force-demote`. Writes a tiny override file the MCP
    process picks up on the next arbiter refresh; also flips the
    CLI-process module if it's the same process.

    v0.10.1 — atomic write via tmp + os.replace. The previous
    truncating write_text could let the MCP-side reader observe a
    zero-byte file mid-write."""
    data_root = _resolve_data_root()
    fp = data_root / "ranker" / "force_mode"
    fp.parent.mkdir(parents=True, exist_ok=True)
    tmp = fp.with_name(fp.name + ".tmp")
    tmp.write_text(mode + "\n", encoding="utf-8")
    os.replace(tmp, fp)
    print(f"Wrote {fp} → {mode!r}. MCP server will pick this up on "
          f"its next arbiter refresh (within 5 minutes).")
    print(f"To revert: claude-dejavu ranker force-{'demote' if mode == 'learned' else 'promote'}")
    sys.exit(0)


def cmd_ranker_reset(args):
    """Delete persisted weights so the next search refits the
    synthetic baseline."""
    data_root = _resolve_data_root()
    fp = data_root / "ranker" / "weights.json"
    if fp.exists():
        fp.unlink()
        print(f"Deleted {fp}. The next dejavu_search will refit "
              f"the synthetic baseline.")
    else:
        print(f"No weights file at {fp} — nothing to reset.")
    return 0


# v0.9.6 — recall_feedback CLI handlers.

def cmd_feedback_stats(args):
    """Show aggregate counts + top-boosted turn_ids."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT count(*), "
                     "       count(*) FILTER (WHERE signal_type='implicit'), "
                     "       count(*) FILTER (WHERE signal_type='explicit_up'), "
                     "       count(*) FILTER (WHERE signal_type='explicit_down') "
                     "FROM recall_feedback")
        n_total, n_imp, n_up, n_down = cur.fetchone()
        print(f"recall_feedback rows: {n_total:,}")
        print(f"  implicit:      {n_imp:,}")
        print(f"  explicit_up:   {n_up:,}")
        print(f"  explicit_down: {n_down:,}")
        cur.execute("""
            SELECT turn_id,
                   sum(weight * exp(
                     - extract(epoch FROM (now() - ts))
                     / (14.0 * 24 * 3600))) AS pop,
                   count(*)
            FROM recall_feedback
            WHERE ts > now() - interval '30 days'
            GROUP BY turn_id
            ORDER BY pop DESC
            LIMIT 10
        """)
        rows = cur.fetchall()
        if rows:
            print("\nTop-10 boosted turn_ids (last 30 days, "
                  "decay-weighted):")
            for tid, pop, n in rows:
                import math as _m
                boost = 1 + _m.log1p(max(0.0, float(pop)))
                print(f"  turn_id={tid}  pop={float(pop):>6.2f}  "
                      f"boost={boost:.2f}×  n_signals={n}")
        else:
            print("\n(No feedback rows in the last 30 days.)")
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return 0


def cmd_feedback_export(args):
    """Dump recall_feedback rows as JSONL to stdout."""
    import json as _json
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, encode(query_hash, 'hex'), query_text,
                   turn_id, session_id::text, signal_type,
                   weight, ts::text
            FROM recall_feedback
            WHERE ts > now() - (%s || ' days')::interval
            ORDER BY ts ASC
        """, (args.days,))
        for row in cur.fetchall():
            (rid, qh, qt, tid, sid, sig, w, ts) = row
            print(_json.dumps({
                "id": rid, "query_hash_hex": qh,
                "query_text": qt, "turn_id": tid,
                "session_id": sid, "signal_type": sig,
                "weight": float(w), "ts": ts,
            }))
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return 0


# ─── Argparse ────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(prog="claude-dejavu")
    sub = p.add_subparsers(dest="cmd", required=True)

    def _add_scope(sp):
        sp.add_argument("--scope", help="'global' | 'project[:name]' | 'workspace[:name]' (default: auto)")

    s = sub.add_parser("search"); s.add_argument("query"); _add_scope(s)
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--mode", choices=["grunt", "normal", "full"], default="grunt")
    # v0.8.6: --full as a top-level flag (equivalent to --mode full)
    # so callers don't need to remember mode names; also makes the
    # default-shape-vs-full split explicit per the Context Economy
    # phase 3 spec.
    s.add_argument("--full", action="store_true",
                    help="Include full turn content in each hit "
                         "(equivalent to --mode full).")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("expand"); s.add_argument("turn_ids", nargs="+", type=int)
    s.set_defaults(func=cmd_expand)

    s = sub.add_parser("sessions"); _add_scope(s); s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_sessions)

    s = sub.add_parser("resume"); s.add_argument("hint"); _add_scope(s)
    s.set_defaults(func=cmd_resume)

    s = sub.add_parser("errors"); _add_scope(s)
    s.add_argument("--tool"); s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_errors)

    s = sub.add_parser("logs", help="Show recent dejavu error-log records "
                                       "(internal plugin errors, not Bash failures)")
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--component",
                     help="filter by component prefix, e.g. indexer.env")
    s.add_argument("--level", choices=("error", "warning", "info"),
                     help="filter by severity")
    s.add_argument("--with-traceback", action="store_true",
                     help="include full Python traceback for each record")
    s.add_argument("--path", action="store_true",
                     help="just print the log file path and exit")
    s.set_defaults(func=cmd_logs)

    s = sub.add_parser("generate",
                          help="Generate runtime defense modules "
                                 "(env-trap, etc.)")
    gs = s.add_subparsers(dest="gcmd", required=True)
    gt = gs.add_parser("runtime-trap",
                          help="Generate a typed env accessor that "
                                 "throws on undeclared keys at runtime")
    gt.add_argument("--language", default="typescript",
                      choices=("typescript", "ts", "python", "py"))
    gt.add_argument("--output",
                      help="output file path (default depends on "
                             "language: TS → src/lib/env.ts, "
                             "Python → src/lib/dejavu_env.py)")
    gt.add_argument("--project",
                      help="project slug to read indexed env vars from "
                             "(default: cwd)")
    gt.add_argument("--from-file", default=".env.example",
                      help="ALSO include keys from this file "
                             "(default .env.example) in case the index "
                             "is stale")
    gt.set_defaults(func=cmd_generate_runtime_trap)

    s = sub.add_parser("fabrications",
                          help="List names the rewriter has flagged as "
                                 "RESIDUAL/REPLACE; mark each as declared / "
                                 "blacklisted / open after triage")
    s.add_argument("--project",
                     help="project slug; defaults to current cwd")
    s.add_argument("--status", choices=("open", "declared",
                                              "blacklisted"),
                     help="filter by triage status")
    s.add_argument("--min-count", type=int, default=1,
                     help="only show names that have been fabricated "
                            "at least N times")
    s.add_argument("--mark", nargs=3,
                     metavar=("NAME", "DOMAIN", "STATUS"),
                     help="set triage status for a (name, domain) "
                            "pair, e.g. "
                            "`--mark LD_SDK_KEY env declared`")
    s.add_argument("--limit", type=int, default=100)
    s.set_defaults(func=cmd_fabrications)

    s = sub.add_parser("files"); s.add_argument("path"); _add_scope(s); s.add_argument("--limit", type=int, default=30)
    s.set_defaults(func=cmd_files)

    s = sub.add_parser("restore", help="Restore deleted sessions from off-tree backup")
    s.add_argument("session_uuids", nargs="*", help="specific UUIDs to restore (optional)")
    s.add_argument("--list", action="store_true", help="list restorable sessions, don't restore")
    s.add_argument("--all", action="store_true", help="restore all missing sessions")
    s.add_argument("--since", help="only consider sessions backed up within window (e.g. 7d, 24h)")
    s.set_defaults(func=cmd_restore)

    # v0.7.1: session JSONL repair (corrupt + truncated reconstruction from DB)
    sp_rep = sub.add_parser("repair",
        help="Detect + reconstruct corrupt/truncated session JSONLs from the dejavu DB")
    sp_rep.add_argument("session_uuids", nargs="*",
                       help="Specific session UUIDs to repair (positional)")
    sp_rep.add_argument("--list", action="store_true",
                       help="List sessions with detected issues; don't repair")
    sp_rep.add_argument("--all", action="store_true",
                       help="Repair every session with detected issues")
    sp_rep.add_argument("--no-pre-recovery-snapshot", action="store_true",
                       dest="no_pre_recovery_snapshot",
                       help="(v0.8.0) bypass the synchronous restic snapshot "
                            "taken before each destructive write. ONLY use "
                            "if the repo is unreachable and you accept "
                            "rollback-impossible risk.")
    sp_rep.set_defaults(func=cmd_repair)

    # v0.7.1: cleanup of Claude Code's title-only session stubs
    sp_cs = sub.add_parser("cleanup-stubs",
        help="Remove ai-title-only JSONL stubs Claude Code writes per Bash call "
             "(they show as ghost sessions in the resume picker)")
    sp_cs.add_argument("--all-projects", action="store_true",
                        help="Scan every project dir under ~/.claude/projects/")
    sp_cs.add_argument("--project",
                        help="Encoded project dir name (e.g. -home-user-Documents-projects-X)")
    sp_cs.add_argument("--dry-run", action="store_true",
                        help="Show what would be deleted without touching anything")
    sp_cs.set_defaults(func=cmd_cleanup_stubs)

    # v0.7.2: file corruption scanner
    sp_sc = sub.add_parser("scan-corruption",
        help="Scan for zeroed / partial-zero / shrunk files (file recovery v0.7.2)")
    sp_sc.add_argument("--path", default=".",
                        help="Directory to scan (default: cwd)")
    sp_sc.add_argument("--all-projects", action="store_true",
                        help="Walk every project_path from dejavu's sessions table")
    sp_sc.add_argument("--include", action="append", default=[],
                        help="Glob to include (repeatable)")
    sp_sc.add_argument("--exclude", action="append", default=[],
                        help="Glob to exclude (repeatable; added to defaults)")
    sp_sc.add_argument("--verbose", action="store_true",
                        help="Also surface UNTRACKED_CHANGE (informational)")
    sp_sc.add_argument("--json", action="store_true",
                        help="Emit findings as JSON")
    sp_sc.set_defaults(func=cmd_scan_corruption)

    # v0.7.2: file recovery (dry-run by default; needs --write + --yes to apply)
    sp_rf = sub.add_parser("recover-file",
        help="Reconstruct a corrupted file from file-history + tool_calls Edit replay")
    sp_rf.add_argument("path", help="File to recover (absolute or cwd-relative)")
    sp_rf.add_argument("--session",
                        help="Specific session UUID to use as the replay source")
    sp_rf.add_argument("--any-project", action="store_true",
                        help="Search sessions across all projects (not just cwd's)")
    sp_rf.add_argument("--from-version", type=int, default=None,
                        help="Pin a specific file-history @vN snapshot")
    sp_rf.add_argument("--from-git",
                        help="Pin a git ref (commit / tag / branch) as the base")
    sp_rf.add_argument("--to",
                        help="Stop op replay at this ISO timestamp (default: now)")
    sp_rf.add_argument("--to-path", dest="to_path",
                        help="(reserved) explicit output path; prefer --write <out>")
    sp_rf.add_argument("--write", nargs="?", const="", default=None,
                        help="Apply the recovery. Optional <out-path> redirects "
                             "output; omit to overwrite the original (also requires --yes).")
    sp_rf.add_argument("--verify-tsc", action="store_true",
                        help="Run `tsc --noEmit` on the recovered candidate (.ts/.tsx/.js/.jsx)")
    sp_rf.add_argument("--skip-failed-edits", action="store_true",
                        help="Continue past Edit ops whose old_string is missing")
    sp_rf.add_argument("--yes", action="store_true",
                        help="Confirm overwrite of the original file")
    sp_rf.add_argument("--force-write", action="store_true",
                        help="Bypass editor-lockfile guard (vim .swp etc.)")
    sp_rf.add_argument("--allow-anywhere", action="store_true",
                        help="Allow targets outside $HOME (default: refuse)")
    sp_rf.add_argument("--json", action="store_true",
                        help="(reserved) JSON output mode")
    sp_rf.add_argument("--no-pre-recovery-snapshot", action="store_true",
                        dest="no_pre_recovery_snapshot",
                        help="(v0.8.0) bypass the synchronous restic "
                             "snapshot taken before the destructive write. "
                             "ONLY use if the repo is unreachable.")
    sp_rf.set_defaults(func=cmd_recover_file)

    # v0.8.0 — session-copy subsystem (restic-backed, spec §3.3)
    sp_sc = sub.add_parser("session-copy",
        help="Manage the restic-backed session-copy subsystem "
             "(init/take/list/diff/restore/mount/prune/status/check/"
             "migrate-from-rsync)")
    sp_sc_sub = sp_sc.add_subparsers(dest="sc_sub", required=True)

    sp_sc_init = sp_sc_sub.add_parser("init",
        help="Generate passphrase + init restic repo (idempotent)")
    sp_sc_init.add_argument("--yes", action="store_true",
                              help="skip the passphrase typed-confirm")
    sp_sc_init.set_defaults(func=cmd_session_copy)

    sp_sc_take = sp_sc_sub.add_parser("take",
        help="Take a snapshot of ~/.claude/projects + claude-mem.db")
    sp_sc_take.add_argument("--tag", default="manual",
                              help="snapshot tag (default: manual)")
    sp_sc_take.add_argument("--message", default=None,
                              help="freeform note (encoded as msg=<text> tag)")
    sp_sc_take.add_argument("--include", action="append",
                              choices=["sessions", "sqlite", "pg",
                                          "weaviate", "all"],
                              help="(v0.8.3) which sources to stage. "
                                     "Repeat or use commas: --include pg "
                                     "--include weaviate, or --include all. "
                                     "Default: all (sessions + sqlite + "
                                     "pg + weaviate).")
    sp_sc_take.set_defaults(func=cmd_session_copy)

    sp_sc_list = sp_sc_sub.add_parser("list", help="List snapshots")
    sp_sc_list.add_argument("--last", type=int, default=None,
                              help="cap to N most recent")
    sp_sc_list.add_argument("--tag", default=None,
                              help="filter by tag")
    sp_sc_list.add_argument("--since", default=None,
                              help="filter by ISO timestamp (>= since)")
    sp_sc_list.set_defaults(func=cmd_session_copy)

    sp_sc_diff = sp_sc_sub.add_parser("diff",
        help="Diff two snapshots (added/removed/changed paths)")
    sp_sc_diff.add_argument("snap_a", help="first snapshot id")
    sp_sc_diff.add_argument("snap_b", help="second snapshot id")
    sp_sc_diff.add_argument("--verbose", action="store_true",
                              help="show per-path detail")
    sp_sc_diff.set_defaults(func=cmd_session_copy)

    sp_sc_rest = sp_sc_sub.add_parser("restore",
        help="Restore a snapshot to a target directory")
    sp_sc_rest.add_argument("snapshot_id")
    sp_sc_rest.add_argument("--target", required=True,
                              help="target directory")
    sp_sc_rest.add_argument("--include", action="append",
                              choices=["sessions", "sqlite", "pg",
                                          "weaviate", "all"],
                              help="(v0.8.3) which sources to restore. "
                                     "Default: all. PG / Weaviate "
                                     "restore refuses to clobber a "
                                     "non-empty target without --force.")
    sp_sc_rest.add_argument("--force", action="store_true",
                              help="(v0.8.3) allow PG / Weaviate "
                                     "restore to clobber non-empty "
                                     "targets")
    sp_sc_rest.set_defaults(func=cmd_session_copy)

    sp_sc_mt = sp_sc_sub.add_parser("mount",
        help="FUSE-mount the repo (Linux native; mac needs macFUSE; "
             "Windows needs WinFsp)")
    sp_sc_mt.add_argument("mountpoint", help="empty dir to mount onto")
    sp_sc_mt.set_defaults(func=cmd_session_copy)

    sp_sc_pr = sp_sc_sub.add_parser("prune",
        help="Apply retention policy (default: dry-run)")
    sp_sc_pr.add_argument("--dry-run", action="store_true",
                            help="print what would be pruned, no writes")
    sp_sc_pr.add_argument("--force", action="store_true",
                            help="bypass the 1h sentinel")
    sp_sc_pr.set_defaults(func=cmd_session_copy)

    sp_sc_st = sp_sc_sub.add_parser("status",
        help="Repo size / snapshot count / last-snapshot age")
    sp_sc_st.set_defaults(func=cmd_session_copy)

    sp_sc_chk = sp_sc_sub.add_parser("check",
        help="restic check (24h-cached metadata audit)")
    sp_sc_chk.add_argument("--read-data", action="store_true",
                             help="full audit (slow); default: metadata-only")
    sp_sc_chk.add_argument("--force", action="store_true",
                             help="bypass the 24h cache")
    sp_sc_chk.set_defaults(func=cmd_session_copy)

    sp_sc_mig = sp_sc_sub.add_parser("migrate-from-rsync",
        help="Migrate the legacy ~/.claude-backups/snapshots/ chain "
             "into the new restic repo (idempotent + resumable)")
    sp_sc_mig.add_argument("--mode", choices=("cold", "warm", "dry-run"),
                             default="cold",
                             help="cold (default): archive old tree; "
                                  "warm: also ingest old snapshots; "
                                  "dry-run: print plan")
    sp_sc_mig.add_argument("--yes", action="store_true",
                             help="skip user prompts (for --auto installs)")
    sp_sc_mig.set_defaults(func=cmd_session_copy)

    # v0.8.3 post-release polish — reclaim disk used by the renamed
    # legacy tree left behind by migrate-from-rsync step 9.
    sp_sc_rl = sp_sc_sub.add_parser("reclaim-legacy",
        help="Delete the ~/.claude-backups/snapshots.deleteme.<ts> "
             "tree left behind by `migrate-from-rsync` (TTY-guarded + "
             "typed confirmation)")
    sp_sc_rl.add_argument("--yes", action="store_true",
                            help="skip the typed-confirm phrase")
    sp_sc_rl.add_argument("--dry-run", action="store_true",
                            help="print the plan and exit without deleting")
    sp_sc_rl.set_defaults(func=cmd_session_copy)

    # v0.8.2 — rescue-shard (torn-segment recovery + PG-gap reindex)
    sp_rs = sub.add_parser("rescue-shard",
        help="Recover records from a corrupted Weaviate shard "
             "(v0.8.2 — Go segment reader + class reset + gap-reindex)")
    sp_rs.add_argument("--class", dest="class_name", required=True,
                        help="Weaviate class name (e.g. ClaudeDejavuTurn)")
    sp_rs.add_argument("--shard",
                        help="specific shard id (default: all shards in class)")
    sp_rs.add_argument("--dry-run", action="store_true",
                        help="walk + report; no DELETE, no batch insert, "
                             "no gap-reindex")
    sp_rs.add_argument("--yes", action="store_true",
                        help="skip interactive typed-confirm before "
                             "destructive class reset")
    sp_rs.add_argument("--include-quarantined",
                        help="path to an extra (quarantined) segment .db "
                             "to walk alongside the live shard files")
    sp_rs.add_argument("--container", default="claude-dejavu-weaviate",
                        help="docker container hosting Weaviate "
                             "(default: claude-dejavu-weaviate)")
    sp_rs.add_argument("--host-data-dir",
                        help="bypass docker cp and read shards directly "
                             "from this host path (skips docker exec / cp)")
    sp_rs.set_defaults(func=cmd_rescue_shard)

    # v0.8.3 — pg-rescue toolkit (5 subcommands)
    sp_pg = sub.add_parser("pg-rescue",
        help="Postgres corruption rescue toolkit (v0.8.3): scan / "
              "patch-toast / reset-flags / rebuild-table / reindex")
    sp_pg_sub = sp_pg.add_subparsers(dest="pg_sub", required=True)

    sp_pg_scan = sp_pg_sub.add_parser("scan",
        help="Probe every row + index for TOAST corruption / index "
              "invalidity")
    sp_pg_scan.add_argument("--db", default=None,
                              help="dejavu DB name (default: auto)")
    sp_pg_scan.add_argument("--json", action="store_true",
                              help="emit structured JSON")
    sp_pg_scan.set_defaults(func=cmd_pg_rescue)

    sp_pg_pt = sp_pg_sub.add_parser("patch-toast",
        help="Patch corrupt TOAST chunks by re-deriving content from "
              "the source JSONL (per-row SAVEPOINT)")
    sp_pg_pt.add_argument("--db", default=None)
    sp_pg_pt.add_argument("--yes", action="store_true",
                             help="actually UPDATE (default: dry-run)")
    sp_pg_pt.add_argument("--from-jsonl", action="store_true",
                             default=True,
                             help="(default true) derive new content from "
                                    "~/.claude/projects JSONL")
    sp_pg_pt.set_defaults(func=cmd_pg_rescue)

    sp_pg_rf = sp_pg_sub.add_parser("reset-flags",
        help="Row-by-row UPDATE: turns.vectorized=FALSE, "
              "weaviate_id=NULL (per-row SAVEPOINT)")
    sp_pg_rf.add_argument("--db", default=None)
    sp_pg_rf.add_argument("--yes", action="store_true",
                             help="actually commit (default: dry-run)")
    sp_pg_rf.set_defaults(func=cmd_pg_rescue)

    sp_pg_rb = sp_pg_sub.add_parser("rebuild-table",
        help="Filtered index-scan rebuild of a table (drops dependents, "
              "copies, swaps, recreates)")
    sp_pg_rb.add_argument("table",
                            help="name of the table to rebuild")
    sp_pg_rb.add_argument("--db", default=None)
    sp_pg_rb.add_argument("--exclude-ids", default=None,
                            help="comma-separated row ids to omit "
                                   "(canonical use: irreparable heap rows)")
    sp_pg_rb.add_argument("--replace-from-jsonl", action="store_true",
                            help="also INSERT JSONL-recovered "
                                   "replacements for excluded rows")
    sp_pg_rb.add_argument("--yes", action="store_true",
                            help="actually apply (default: dry-run)")
    sp_pg_rb.set_defaults(func=cmd_pg_rescue)

    sp_pg_ri = sp_pg_sub.add_parser("reindex",
        help="REINDEX INDEX CONCURRENTLY for indexes flagged by scan "
              "(or all)")
    sp_pg_ri.add_argument("--db", default=None)
    sp_pg_ri.add_argument("--all", action="store_true",
                              help="reindex EVERY public-schema index")
    sp_pg_ri.add_argument("--concurrently", action="store_true",
                              default=True,
                              help="(default) use CONCURRENTLY")
    sp_pg_ri.add_argument("--yes", action="store_true",
                              help="actually run (default: dry-run)")
    sp_pg_ri.set_defaults(func=cmd_pg_rescue)

    # v0.8.3 — weaviate-rescue toolkit (3 subcommands)
    sp_wv = sub.add_parser("weaviate-rescue",
        help="Weaviate corruption rescue toolkit (v0.8.3): scan / "
              "quarantine-segment / redo-class")
    sp_wv_sub = sp_wv.add_subparsers(dest="wv_sub", required=True)

    sp_wv_scan = sp_wv_sub.add_parser("scan",
        help="Check /v1/nodes + /v1/schema + vector dim consistency")
    sp_wv_scan.add_argument("--class", dest="class_filter", default=None,
                              help="restrict to one class name")
    sp_wv_scan.add_argument("--json", action="store_true",
                              help="emit structured JSON")
    sp_wv_scan.set_defaults(func=cmd_weaviate_rescue)

    sp_wv_qs = sp_wv_sub.add_parser("quarantine-segment",
        help="Move the 5 files of a corrupt LSM segment to "
              "$DATA_ROOT/weaviate-quarantine/<ts>/")
    sp_wv_qs.add_argument("segment_id",
                            help="segment numeric id "
                                   "(e.g. 1778777077583721728)")
    sp_wv_qs.add_argument("--class", dest="class_name", required=True,
                            help="Weaviate class name "
                                   "(e.g. ClaudeDejavuTurn)")
    sp_wv_qs.add_argument("--shard", dest="shard_id", required=True,
                            help="shard id (e.g. aFEA9t3Ikz8D)")
    sp_wv_qs.add_argument("--yes", action="store_true",
                            help="actually move (default: dry-run)")
    sp_wv_qs.set_defaults(func=cmd_weaviate_rescue)

    sp_wv_rc = sp_wv_sub.add_parser("redo-class",
        help="DELETE + recreate a class at a new vector dim / "
              "vectorizer (optionally resets PG flags)")
    sp_wv_rc.add_argument("class_name",
                            help="Weaviate class name")
    sp_wv_rc.add_argument("--dim", type=int, default=None,
                            help="(informational) target vector dim")
    sp_wv_rc.add_argument("--vectorizer", default="none",
                            choices=("none", "text2vec-transformers"),
                            help="(default: none) new vectorizer config")
    sp_wv_rc.add_argument("--reset-pg-flags", action="store_true",
                            default=True,
                            help="(default true) reset turns.vectorized "
                                   "+ weaviate_id when class is "
                                   "ClaudeDejavuTurn")
    sp_wv_rc.add_argument("--yes", action="store_true",
                            help="actually run (default: dry-run)")
    sp_wv_rc.set_defaults(func=cmd_weaviate_rescue)

    # v0.3.0: code symbol grounding
    s = sub.add_parser("reindex", help="(Re)index project source files: symbols + routes + semantic")
    s.add_argument("--path", help="project directory (default: cwd)")
    s.add_argument("--project", help="project slug to record under (default: cwd basename)")
    s.add_argument("--force", action="store_true", help="re-parse all files, ignore mtime cache")
    s.add_argument("--skip-routes", action="store_true", help="skip API route indexing")
    s.add_argument("--skip-pages", action="store_true", help="skip frontend page-route indexing")
    s.add_argument("--skip-env", action="store_true", help="skip env var declaration indexing")
    s.add_argument("--include-runtime-dotenv", action="store_true",
                    help="also index .env / .env.local / .env.production (off by default — dev secrets)")
    s.add_argument("--skip-db", action="store_true",
                    help="skip Prisma/Drizzle/TypeORM/SQL schema indexing")
    s.add_argument("--skip-graphql", action="store_true",
                    help="skip GraphQL SDL + inline-gql indexing")
    s.add_argument("--skip-flags", action="store_true",
                    help="skip feature-flag config indexing")
    s.add_argument("--skip-quickwins", action="store_true",
                    help="skip CSS / i18n quick-win indexing (v0.3.14)")
    s.add_argument("--skip-modules", action="store_true",
                    help="skip npm module export indexing (v0.3.16)")
    s.add_argument("--skip-registry", action="store_true",
                    help="skip name registry indexing for prose grounding (v0.4.0)")
    s.add_argument("--skip-semantic", action="store_true", help="skip Weaviate semantic embedding")
    s.add_argument("--docs", action="store_true",
                    help="Re-embed markdown documentation (docs-only; skips other indexers)")
    s.add_argument("--full", action="store_true",
                    help="Force full re-embed (ignore cursor)")
    s.add_argument("--repo", help="Repo root path (default: cwd)")
    s.set_defaults(func=cmd_reindex)

    s = sub.add_parser("routes", help="List, search, or look up indexed API routes")
    rs = s.add_subparsers(dest="rcmd", required=True)
    rl = rs.add_parser("list", help="list every indexed route")
    rl.add_argument("--project", help="project slug (default: cwd)")
    rl.add_argument("--framework", help="filter by framework (express|fastapi|nestjs|...)")
    rl.add_argument("--method", help="filter by HTTP method")
    rl.add_argument("--limit", type=int, default=200)
    rg = rs.add_parser("get", help="resolve a specific (method, path)")
    rg.add_argument("method")
    rg.add_argument("path")
    rg.add_argument("--project", help="project slug (default: cwd)")
    rg.add_argument("--exact", action="store_true", help="exact match only (no fuzzy)")
    rg.add_argument("--limit", type=int, default=5)
    s.set_defaults(func=cmd_routes)

    s = sub.add_parser("pages", help="List or check frontend page routes (Next.js / React Router / Vue Router / Remix / SvelteKit / Astro)")
    ps = s.add_subparsers(dest="pcmd", required=True)
    pl = ps.add_parser("list", help="list every indexed page route")
    pl.add_argument("--project", help="project slug (default: cwd)")
    pl.add_argument("--framework", help="filter by framework")
    pl.add_argument("--kind", help="page|layout|api|loader|error|middleware")
    pl.add_argument("--limit", type=int, default=300)
    pc = ps.add_parser("check", help="check a proposed path for existing duplicates")
    pc.add_argument("path", help="proposed route path, e.g. /dashboard/profile")
    pc.add_argument("--project", help="project slug (default: cwd)")
    pc.add_argument("--limit", type=int, default=5)
    s.set_defaults(func=cmd_pages)

    s = sub.add_parser("db", help="Browse / resolve indexed DB schema (Prisma / Drizzle / TypeORM / SQL)")
    ds = s.add_subparsers(dest="dcmd", required=True)
    dm = ds.add_parser("models", help="list every indexed model / table")
    dm.add_argument("--project", help="project slug (default: cwd)")
    dm.add_argument("--source", help="filter by source: prisma|drizzle|typeorm|sql-migration")
    dm.add_argument("--limit", type=int, default=200)
    df = ds.add_parser("fields", help="list every indexed field")
    df.add_argument("--project", help="project slug (default: cwd)")
    df.add_argument("--model", help="filter to one model")
    df.add_argument("--limit", type=int, default=500)
    dc = ds.add_parser("check", help="resolve MODEL.FIELD (or just FIELD across all models)")
    dc.add_argument("target", help="MODEL.FIELD or FIELD")
    dc.add_argument("--project", help="project slug (default: cwd)")
    dc.add_argument("--limit", type=int, default=5)
    dcm = ds.add_parser("check-model", help="resolve a model name (exact or fuzzy)")
    dcm.add_argument("target", help="model name")
    dcm.add_argument("--project", help="project slug (default: cwd)")
    dcm.add_argument("--limit", type=int, default=5)
    s.set_defaults(func=cmd_db)

    s = sub.add_parser("graphql", help="Browse / resolve indexed GraphQL types + fields (SDL + inline-gql)")
    gs = s.add_subparsers(dest="gcmd", required=True)
    gt = gs.add_parser("types", help="list every indexed GraphQL type")
    gt.add_argument("--project", help="project slug (default: cwd)")
    gt.add_argument("--kind", help="filter: object|input|enum|interface|union|scalar|schema")
    gt.add_argument("--source", help="filter: sdl-file|inline-gql|codegen")
    gt.add_argument("--limit", type=int, default=200)
    gf = gs.add_parser("fields", help="list every indexed GraphQL field")
    gf.add_argument("--project", help="project slug (default: cwd)")
    gf.add_argument("--type", help="filter to one type")
    gf.add_argument("--limit", type=int, default=500)
    gc = gs.add_parser("check", help="resolve TYPE.FIELD or just FIELD")
    gc.add_argument("target")
    gc.add_argument("--project", help="project slug (default: cwd)")
    gc.add_argument("--limit", type=int, default=5)
    gck = gs.add_parser("check-type", help="resolve a GraphQL type name")
    gck.add_argument("target")
    gck.add_argument("--project", help="project slug (default: cwd)")
    gck.add_argument("--limit", type=int, default=5)
    s.set_defaults(func=cmd_gql)

    s = sub.add_parser("ignore", help="Inspect the .dejavuignore filter — list rules or explain why a file is/isn't indexed")
    is_ = s.add_subparsers(dest="icmd", required=True)
    il = is_.add_parser("list", help="show every loaded ignore rule")
    isn = is_.add_parser("status", help="explain why a path would or wouldn't be indexed")
    isn.add_argument("path", help="absolute or relative file path")
    s.set_defaults(func=cmd_ignore)

    s = sub.add_parser("thresholds", help="List or recompute adaptive resolver thresholds (per-project, per-channel)")
    ts_ = s.add_subparsers(dest="tcmd", required=True)
    tl = ts_.add_parser("list", help="show all tuned thresholds")
    tl.add_argument("--project", help="filter by project slug")
    tt = ts_.add_parser("tune", help="recompute thresholds for this project from correction_outcomes")
    tt.add_argument("--project", help="project slug (default: cwd)")
    tt.add_argument("--channel", help="symbol|route|page|env|db|graphql|flag (default: all)")
    s.set_defaults(func=cmd_thresholds)

    s = sub.add_parser("flags", help="Browse / resolve indexed feature flag declarations (flags.json + featureFlags.ts + LD + Unleash + GrowthBook)")
    fs = s.add_subparsers(dest="fcmd", required=True)
    fl = fs.add_parser("list", help="list every indexed flag (deduped by key)")
    fl.add_argument("--project", help="project slug (default: cwd)")
    fl.add_argument("--source", help="filter: flags-json|feature-flags-ts|launchdarkly|unleash|growthbook|source-read")
    fl.add_argument("--kill-switches-only", action="store_true",
                     help="only show flags whose key matches kill/emergency/disable/panic/circuit heuristic")
    fl.add_argument("--limit", type=int, default=500)
    fc = fs.add_parser("check", help="resolve a flag key (exact or fuzzy)")
    fc.add_argument("flag_key")
    fc.add_argument("--project", help="project slug (default: cwd)")
    fc.add_argument("--limit", type=int, default=5)
    s.set_defaults(func=cmd_flags)

    s = sub.add_parser("env", help="List or resolve indexed environment variable declarations")
    es = s.add_subparsers(dest="ecmd", required=True)
    el = es.add_parser("list", help="list every indexed env var (deduped by name)")
    el.add_argument("--project", help="project slug (default: cwd)")
    el.add_argument("--source", help="filter by source: env-example|zod-schema|envalid|docker-compose|github-actions|source-read|dotenv-runtime")
    el.add_argument("--secrets-only", action="store_true",
                     help="only show vars matching SECRET/TOKEN/KEY/PASSWORD heuristic")
    el.add_argument("--limit", type=int, default=500)
    ec = es.add_parser("check", help="resolve a proposed env var name (exact or fuzzy)")
    ec.add_argument("name", help="env var name, e.g. DATABASE_URL")
    ec.add_argument("--project", help="project slug (default: cwd)")
    ec.add_argument("--limit", type=int, default=5)
    ep = es.add_parser("propose",
                          help="Append a new env var declaration to "
                                 ".env.example (or similar) — useful "
                                 "for SDK conventions the project "
                                 "hasn't declared yet")
    ep.add_argument("name",
                      help="env var name, e.g. LAUNCHDARKLY_SDK_KEY")
    ep.add_argument("--package",
                      help="NPM/PyPI package whose convention this is "
                             "(used in the inline comment)")
    ep.add_argument("--file", default=".env.example",
                      help="target file (default: .env.example)")
    ep.add_argument("--apply", action="store_true",
                      help="actually write the change (default: dry run)")
    ep.set_defaults(func=cmd_env_propose)
    s.set_defaults(func=cmd_env)

    s = sub.add_parser("symbols", help="List indexed symbols or fuzzy-resolve a name")
    s.add_argument("name", nargs="?", help="symbol name to resolve (fuzzy by default)")
    s.add_argument("--project", help="project slug (default: cwd)")
    s.add_argument("--file", help="filter to a single file path")
    s.add_argument("--kind", choices=["function","class","method","type","interface","enum","const","variable"])
    s.add_argument("--language", choices=["python","typescript","javascript","go","rust","java","ruby","bash","php","c_sharp","kotlin"])
    s.add_argument("--exact", action="store_true", help="exact name match only (no trigram)")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--offset", type=int, default=0)
    s.set_defaults(func=cmd_symbols)

    s = sub.add_parser("lint", help="Lint a proposed edit — flag fabricated names + typos against the symbol index")
    s.add_argument("file", help="target file path (used for language detection)")
    s.add_argument("--project", help="project slug (default: cwd)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--content-file", help="read proposed content from this file")
    g.add_argument("--content-stdin", action="store_true", help="read proposed content from stdin")
    s.add_argument("--show-resolved", action="store_true", help="also print resolved references")
    s.add_argument("--json", action="store_true", help="output structured JSON")
    s.set_defaults(func=cmd_lint)

    s = sub.add_parser("about", help="Show who built claude-dejavu + project stats. No side effects.")
    s.add_argument("--project", help="project slug (default: cwd)")
    s.add_argument("--with-stats", action="store_true",
                    help="show indexed-domain counts even if --project is omitted")
    s.set_defaults(func=cmd_about)

    s = sub.add_parser("doctor", help="Diagnose install / runtime health (PG, Weaviate, venv, hooks, ...)")
    s.add_argument("--fix", action="store_true", help="apply safe auto-fixes (e.g. re-run install.py if marker says incomplete)")
    s.add_argument("--json", action="store_true", help="output structured JSON")
    s.add_argument("--deep", action="store_true",
                     help="(v0.8.3) run extended diagnostics: pg-rescue "
                            "scan + weaviate-rescue scan (slow on large DBs)")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("config", help="Read/write claude-dejavu settings (config.env) via a typed schema")
    cs = s.add_subparsers(dest="action", required=True)
    cl = cs.add_parser("list", help="list every known setting + current/default")
    cl.add_argument("--json", action="store_true")
    cl.add_argument("--show-secrets", action="store_true", help="unmask secret values like PG_PASS")
    cg = cs.add_parser("get", help="print the current value of a setting")
    cg.add_argument("key")
    cse = cs.add_parser("set", help="write a value to config.env (validated against the schema)")
    cse.add_argument("key")
    cse.add_argument("value")
    cr = cs.add_parser("reset", help="remove a setting from config.env (revert to its default)")
    cr.add_argument("key")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("outcomes", help="Inspect correction_outcomes (what corrections were proposed)")
    s.add_argument("--project", help="project slug (default: cwd)")
    s.add_argument("--global", dest="global_", action="store_true", help="all projects")
    s.add_argument("--language", choices=["python","typescript","javascript","go","rust","java","ruby","bash","php","c_sharp","kotlin"])
    s.add_argument("--mode", choices=["lookup","lint","warn","suggest","autofix"])
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_outcomes)

    # workspace subcommand: list / create / add / remove / delete
    s = sub.add_parser("workspace")
    sw = s.add_subparsers(dest="action", required=True)
    sl = sw.add_parser("list")
    sc = sw.add_parser("create"); sc.add_argument("name"); sc.add_argument("--projects", required=True); sc.add_argument("--description")
    sa = sw.add_parser("add"); sa.add_argument("name"); sa.add_argument("project")
    sr = sw.add_parser("remove"); sr.add_argument("name"); sr.add_argument("project")
    sd = sw.add_parser("delete"); sd.add_argument("name")
    s.set_defaults(func=cmd_workspace)

    # repo subcommand: show / rebind / prune
    sp_repo = sub.add_parser("repo", help="Show / rebind / prune repo identities")
    sp_repo_sub = sp_repo.add_subparsers(dest="repo_action", required=True)

    sp_show = sp_repo_sub.add_parser("show", help="Print resolved repo id + root + label")
    sp_show.add_argument("--start", help="Starting directory (default: cwd)")
    sp_show.add_argument("--json", action="store_true", help="Emit JSON instead of human text")
    sp_show.set_defaults(func=cmd_repo)

    sp_rebind = sp_repo_sub.add_parser("rebind", help="Rebind every row from one repo_id to another")
    sp_rebind.add_argument("old"); sp_rebind.add_argument("new")
    sp_rebind.set_defaults(func=cmd_repo)

    sp_prune = sp_repo_sub.add_parser("prune", help="List repos untouched for 90+ days")
    sp_prune.set_defaults(func=cmd_repo)

    # workspace-migrate: one-shot legacy slug → uuid remap
    sp_wm = sub.add_parser("workspace-migrate",
        help="One-shot: map workspaces.repo_ids slug entries to UUIDs by label")
    sp_wm.set_defaults(func=cmd_workspace_migrate)

    # search-docs: docs-only convenience wrapper over `dejavu_search`
    sp_sd = sub.add_parser("search-docs", help="Search documentation embeddings (docs-only)")
    sp_sd.add_argument("query", help="Search query (natural language)")
    sp_sd.add_argument("--workspace",
                         help="Search across a saved workspace's repos (default: cwd's repo)")
    sp_sd.add_argument("--limit", type=int, default=10,
                         help="Max hits to return (default: 10)")
    sp_sd.add_argument("--json", action="store_true",
                         help="Emit JSON instead of human text")
    sp_sd.set_defaults(func=cmd_search_docs)

    # cloud subcommands: login / logout / status
    from cloud_cli import cmd_login, cmd_logout, cmd_status

    sl = sub.add_parser("login",
                        help="sign in to claude-dejavu cloud (Pro tier)")
    sl.add_argument("--paste", action="store_true",
                    help="skip browser flow; paste a key directly")
    sl.add_argument("--license-url",
                    help="override license server URL (default: "
                         "https://license.hte.digital)")
    sl.set_defaults(func=lambda a: sys.exit(cmd_login(a)))

    sl = sub.add_parser("logout",
                        help="sign out — revoke server-side + erase local key")
    sl.add_argument("--license-url",
                    help="override license server URL")
    sl.add_argument("--skip-revoke", action="store_true",
                    help="erase local key only; don't ask server to revoke")
    sl.set_defaults(func=lambda a: sys.exit(cmd_logout(a)))

    sl = sub.add_parser("status",
                        help="show cloud-tier status (mode, key, quota)")
    sl.add_argument("--cloud", action="store_true",
                    help="(default already shows cloud info; reserved for future "
                         "subsections)")
    sl.set_defaults(func=lambda a: sys.exit(cmd_status(a)))

    # Cloud migration: drops + recreates Weaviate ClaudeDejavuTurn class with
    # vectorizer:none, preserving all existing 1024-dim vectors. One-shot;
    # idempotent (skips if already migrated).
    sl = sub.add_parser("migrate-to-cloud",
                        help="migrate Weaviate Turn class to vectorizer:none "
                             "(needed when fully switching to cloud mode and "
                             "stopping the local transformers container)")
    sl.add_argument("--wv-url", default="http://localhost:8888",
                    help="Weaviate URL (default: http://localhost:8888)")
    sl.add_argument("--apply", action="store_true",
                    help="actually execute (default is dry-run)")
    sl.add_argument("--limit", type=int, default=None,
                    help="cap re-insert (for testing on a subset)")
    def _run_migrate(a):
        import subprocess
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        cmd = [sys.executable, os.path.join(plugin_dir, "migrate_to_cloud.py"),
               "--wv-url", a.wv_url]
        if a.apply:
            cmd.append("--apply")
        if a.limit is not None:
            cmd.extend(["--limit", str(a.limit)])
        sys.exit(subprocess.call(cmd))
    sl.set_defaults(func=_run_migrate)

    # ─── v0.8.4 G1.a — what-shipped ─────────────────────────────────
    sp_ws = sub.add_parser(
        "what-shipped",
        help="Print the last N CHANGELOG release headlines (passive "
             "context priming).",
        description="Reads <repo>/CHANGELOG.md + recent `chore(release):` "
                    "git commits and prints a compact summary of the last "
                    "N releases.")
    sp_ws.add_argument("--repo", default=".",
                       help="Repo root containing CHANGELOG.md "
                            "(default: cwd).")
    sp_ws.add_argument("--last", type=int, default=10,
                       help="How many releases to show (default 10).")
    sp_ws.add_argument("--json", action="store_true",
                       help="Emit JSON array instead of human text.")
    sp_ws.set_defaults(func=cmd_what_shipped)

    # ─── v0.8.4 G1.b — memory record-version ────────────────────────
    sp_mem = sub.add_parser(
        "memory",
        help="Lightweight memory primitives (record-version, etc.).",
        description="Subcommands that write into the dejavu-owned "
                    "versions / memory directory (DATA_ROOT/versions/).")
    sp_mem_sub = sp_mem.add_subparsers(dest="mem_sub", required=True)
    sp_mem_rv = sp_mem_sub.add_parser(
        "record-version",
        help="Distill the most recent CHANGELOG section and append a "
             "one-line entry to DATA_ROOT/versions/.")
    sp_mem_rv.add_argument("--repo", default=".",
                            help="Repo root containing CHANGELOG.md "
                                 "(default: cwd).")
    sp_mem_rv.add_argument("--memory-dir", default=None,
                            help="Override the versions output directory. "
                                 "Default: DATA_ROOT/versions/.")
    sp_mem_rv.set_defaults(func=cmd_memory_record_version)

    # ─── v0.8.6 Context Economy phase 3 — backfill-gists ──────────
    sp_mem_bg = sp_mem_sub.add_parser(
        "backfill-gists",
        help="Populate turns.gist via distill_pipeline for every "
             "row where gist IS NULL.",
        description="v0.8.6 Context Economy phase 3. Walks every "
                    "turn with gist IS NULL and writes a compressed "
                    "load-bearing-anchor-preserving gist via the "
                    "closed-source dejavu_bridge wheel (or the stub "
                    "fallback when the wheel is unavailable).")
    sp_mem_bg.add_argument("--limit", type=int, default=None,
                            help="Cap on rows to process (default: all).")
    sp_mem_bg.add_argument("--workers", type=int, default=None,
                            help="Parallel workers (default: 4).")
    sp_mem_bg.add_argument("--dry-run", action="store_true",
                            help="Compute gists but don't write to DB "
                                 "(prints distill/skip/error counts).")
    sp_mem_bg.add_argument("--force", action="store_true",
                            help="Re-distill rows that already have a "
                                 "gist (default: idempotent skip).")
    sp_mem_bg.set_defaults(func=cmd_memory_backfill_gists)

    # ─── v0.8.5 G2 — JIT recall preview ─────────────────────────────
    sp_jit = sub.add_parser(
        "jit-recall-preview",
        help="Preview what the UserPromptSubmit JIT recall hook "
             "would inject for a given prompt.",
        description="Runs the UserPromptSubmit hook's recall logic "
                    "against the live Weaviate + Postgres, without "
                    "Claude Code in the loop. Useful for eyeballing "
                    "the injected block before flipping "
                    "JIT_RECALL_ENABLED on.")
    sp_jit.add_argument("prompt",
                         help="The user prompt to simulate.")
    sp_jit.set_defaults(func=cmd_jit_recall_preview)

    # v0.9.1 — local in-process vector index admin.
    sp_vi = sub.add_parser(
        "vector-index",
        help="Manage the in-process numpy vector index (v0.9.1).")
    sp_vi_sub = sp_vi.add_subparsers(dest="vector_index_cmd")
    sp_vi_rebuild = sp_vi_sub.add_parser(
        "rebuild",
        help="Bootstrap the local vector index from Weaviate "
        "(one-shot, paginated). v0.9.5: pass --slug <name> to build "
        "a per-project index; default builds the global "
        "`_default` index.")
    sp_vi_rebuild.add_argument("--class-name", default=None)
    sp_vi_rebuild.add_argument("--wv-url", default=None)
    sp_vi_rebuild.add_argument("--batch", type=int, default=500)
    sp_vi_rebuild.add_argument("--max", type=int, default=200_000,
                                  dest="max_objects")
    sp_vi_rebuild.add_argument(
        "--slug", default="_default",
        help="Per-project index slug (subdir under "
        "<data_root>/vector_index/). Default `_default` is the "
        "global shared index. When set, also filters Weaviate "
        "objects to project_slug==<slug>.")
    sp_vi_rebuild.set_defaults(func=cmd_vector_index_rebuild)
    sp_vi_status = sp_vi_sub.add_parser(
        "status", help="Show current vector-index status.")
    sp_vi_status.set_defaults(func=cmd_vector_index_status)

    # v0.10.0 — learned ranker admin.
    sp_rk = sub.add_parser(
        "ranker",
        help="Manage the v0.10.0 learned ranker (champion/challenger "
        "LR over 7 features). Default mode is `auto` — heuristic "
        "active, LR shadow-logged; arbiter promotes the LR when it "
        "wins 60%+ of 1000 decided trials.")
    sp_rk_sub = sp_rk.add_subparsers(dest="ranker_cmd")
    sp_rk_status = sp_rk_sub.add_parser(
        "status", help="Show ranker mode + weights + arbiter stats.")
    sp_rk_status.set_defaults(func=cmd_ranker_status)
    sp_rk_train = sp_rk_sub.add_parser(
        "train",
        help="Re-fit the LR on accumulated `recall_feedback` + "
        "`recall_ranking_trials` rows. Persists to "
        "<data_root>/ranker/weights.json. Falls back to the "
        "synthetic baseline when there's not enough data yet.")
    sp_rk_train.set_defaults(func=cmd_ranker_train)
    sp_rk_force_promote = sp_rk_sub.add_parser(
        "force-promote",
        help="Force the arbiter into LEARNED mode immediately. "
        "Persists for the current MCP process; clears on restart "
        "unless the env is also set.")
    sp_rk_force_promote.set_defaults(
        func=lambda a: _cmd_ranker_force("learned"))
    sp_rk_force_demote = sp_rk_sub.add_parser(
        "force-demote",
        help="Force the arbiter back to HEURISTIC mode immediately.")
    sp_rk_force_demote.set_defaults(
        func=lambda a: _cmd_ranker_force("heuristic"))
    sp_rk_reset = sp_rk_sub.add_parser(
        "reset",
        help="Delete the persisted weights file so the next "
        "search re-fits the synthetic baseline.")
    sp_rk_reset.set_defaults(func=cmd_ranker_reset)

    # v0.9.6 — feedback dump / aggregate.
    sp_fb = sub.add_parser(
        "feedback",
        help="Manage the v0.9.6 recall_feedback table "
        "(implicit + explicit relevance signals).")
    sp_fb_sub = sp_fb.add_subparsers(dest="feedback_cmd")
    sp_fb_stats = sp_fb_sub.add_parser(
        "stats",
        help="Show aggregate counts + top-boosted turn_ids.")
    sp_fb_stats.set_defaults(func=cmd_feedback_stats)
    sp_fb_export = sp_fb_sub.add_parser(
        "export",
        help="Dump recall_feedback rows as JSONL to stdout (for "
        "offline analysis / future ML training).")
    sp_fb_export.add_argument("--days", type=int, default=30)
    sp_fb_export.set_defaults(func=cmd_feedback_export)

    args = p.parse_args()
    # v0.7.1 (bug 8.2 fix): propagate handler returncode through sys.exit.
    # Cloud subcommands (login/logout/status/migrate-to-cloud) use
    # `lambda a: sys.exit(cmd_xxx(a))` wrappers that self-exit, so the
    # `rc = args.func(args)` line never gets a value back from them —
    # the lambda's sys.exit() bypasses us. Safe to keep both styles.
    rc = args.func(args)
    sys.exit(rc if isinstance(rc, int) else 0)


if __name__ == "__main__":
    main()
