#!/usr/bin/env python3
"""claude-dejavu UserPromptSubmit hook — JIT recall (v0.8.5 G2).

OPT-IN. Disabled by default. Enable with `JIT_RECALL_ENABLED=true`
in `<DATA_ROOT>/config.env`. Default behavior of the plugin is
unchanged after upgrade.

What it does
============
For every prompt the user submits, extract keywords, run the same
hybrid Weaviate search the `claude-dejavu search` CLI uses, and if
the top hit clears `JIT_RECALL_SCORE_THRESHOLD`, print a compact
block to stdout. Claude Code picks the stdout up as additional
context for the upcoming turn.

What it deliberately does NOT do
================================
- Never blocks the prompt. Every failure mode (PG down, Weaviate
  down, search timeout, unhandled exception, empty hits, disabled
  flag, prompt-too-short, …) exits 0 with empty stdout.
- Never exceeds the 2s timeout set in `hooks/hooks.json`. The
  semantic search call gets a tight deadline; we abort and emit
  nothing if it doesn't return in time.
- Never injects below threshold. A weak top hit is silently
  dropped — better to stay quiet than surface noise.
- Never shells out to `claude-dejavu search`. We call
  `semantic_search.search` (well, its Weaviate hybrid analogue)
  in-process. Subprocess fork would blow the 2s budget on cold
  Python start.

Hook input contract (Claude Code UserPromptSubmit JSON, on stdin)
=================================================================
  {
    "session_id": "...",
    "transcript_path": "...",
    "hook_event_name": "UserPromptSubmit",
    "prompt": "<the user's text>",
    "cwd": "/optional/working/dir"
  }

The `prompt` key is the documented field carrying the user's text.

Hook output contract
====================
Stdout: a single block of plain text (or empty). Claude Code
treats stdout from UserPromptSubmit as additional context that
gets prepended to the assistant's next response.

Exit code: always 0. Treat this hook as a logger that happens to
emit context — never a gate.
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path


# Stopwords kept deliberately small + English-only. Goal is to drop
# the obvious filler ("the", "is", "we") without doing real NLP. If
# the prompt is non-English the hook just searches with more terms,
# which is still a useful signal — Weaviate hybrid handles it.
_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "and", "or", "but", "if", "then", "else", "of", "to", "in", "on",
    "for", "with", "from", "by", "at", "as", "it", "this", "that",
    "these", "those", "i", "we", "you", "they", "he", "she", "them",
    "us", "our", "your", "their", "my", "me", "him", "her", "his",
    "hers", "its", "do", "does", "did", "doing", "have", "has", "had",
    "having", "will", "would", "could", "should", "can", "may", "might",
    "must", "shall", "ought", "into", "out", "up", "down", "over",
    "under", "again", "further", "more", "most", "less", "least",
    "very", "much", "many", "few", "no", "not", "yes", "ok", "okay",
    "now", "any", "some", "all", "each", "every", "both", "either",
    "neither", "what", "which", "who", "whom", "whose", "where", "when",
    "why", "how", "than", "so", "just", "also", "too", "only", "even",
    "still", "yet", "about", "while", "because", "though", "although",
    "however",
})

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
_MAX_KEYWORDS = 8

# v0.8.16 — identifier patterns the standard _WORD_RE strips out but
# which carry the most recall signal when a user types them: IPv4,
# UUIDs, long hex (commit / hash), URLs, file paths, emails. The
# JIT hook now extracts these as-is so a prompt containing
# "203.0.113.42" actually searches for that string instead of zero
# tokens after stopword/<3-char drops.
_IDENTIFIER_RES = [
    re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),    # IPv4
    # IPv6 — full 8-group OR any `::`-compressed form. Conservative
    # so `12:34:56` time strings don't match.
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


def extract_identifiers(prompt: str, max_n: int = 4) -> list[str]:
    """Return up to `max_n` identifier-shaped substrings from `prompt`
    in first-seen order. Pure function — exposed for unit tests."""
    if not prompt:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for pat in _IDENTIFIER_RES:
        for m in pat.finditer(prompt):
            tok = m.group(0)
            # Strip trailing punctuation that grammar attached to the
            # identifier ("...203.0.113.42," / "url.html.").
            tok = tok.rstrip(".,;:)'\"")
            if not tok or tok in seen:
                continue
            seen.add(tok)
            found.append(tok)
            if len(found) >= max_n:
                return found
    return found


def _silent_exit() -> None:
    """The only valid exit. Every failure mode lands here."""
    sys.exit(0)


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


def _load_cfg() -> dict[str, str]:
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


def _cfg_bool(cfg: dict[str, str], key: str, default: bool) -> bool:
    raw = cfg.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _cfg_int(cfg: dict[str, str], key: str, default: int) -> int:
    raw = cfg.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def _cfg_float(cfg: dict[str, str], key: str, default: float) -> float:
    raw = cfg.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default


def extract_keywords(prompt: str,
                     max_keywords: int = _MAX_KEYWORDS) -> list[str]:
    """Lowercase, dedupe, strip stopwords, keep tokens ≥3 chars, take
    the top N by frequency (stable: first-seen-first on tie).

    Pure function — no side effects, exposed for unit tests.
    """
    if not prompt:
        return []
    counts: dict[str, int] = {}
    order: dict[str, int] = {}
    for i, m in enumerate(_WORD_RE.finditer(prompt.lower())):
        tok = m.group(0)
        if tok in _STOPWORDS:
            continue
        if tok not in counts:
            order[tok] = i
        counts[tok] = counts.get(tok, 0) + 1
    if not counts:
        return []
    # Sort by count desc, then by first-seen order (stable).
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], order[kv[0]]))
    return [tok for tok, _ in ranked[:max_keywords]]


def _format_block(hits: list[dict], top_n: int,
                  token_cap: int) -> str:
    """Format the hits into the `<system-reminder>`-style block.

    `token_cap` is in tokens; the hard char cap is token_cap*4.
    Truncates with a marker line if exceeded.
    """
    if not hits:
        return ""
    char_cap = max(40, token_cap * 4)
    header = (
        f"[claude-dejavu past-context — top {min(top_n, len(hits))} hits, "
        f"showing only above threshold]"
    )
    lines: list[str] = [header]
    overflow = False
    for h in hits[:top_n]:
        session_short = (h.get("session_id") or "")[:8] or "?"
        ts = (h.get("ts") or "")[:10] or "?"
        score = float(h.get("score") or 0.0)
        gist = (h.get("gist") or h.get("ai_title")
                or "(no gist)").replace("\n", " ").strip()
        # Per-hit body — single line so the cap math stays predictable.
        body = (f"  [{session_short}] {ts} score={score:.2f}  "
                f"{gist}")
        proposed = "\n".join(lines + [body])
        if len(proposed) > char_cap:
            overflow = True
            break
        lines.append(body)
    if overflow:
        lines.append("  … (truncated — JIT_RECALL_TOKEN_CAP hit)")
    out = "\n".join(lines)
    # Belt-and-suspenders cap.
    if len(out) > char_cap:
        out = out[:char_cap - 30].rstrip() + "\n  … (truncated)"
    return out


def _bm25_jit(query: str, top_n: int, wv_url: str, wv_class: str,
                deadline_s: float) -> list[dict]:
    """BM25 Weaviate query for the JIT hook. Factored so RRF wiring
    can call it alongside the nearVector branch."""
    import json as _json
    import urllib.request as _ur
    gql = (
        "{ Get { "
        f"{wv_class}("
        f"bm25: {{ query: {_json.dumps(query)} }} "
        f"limit: {int(top_n)}"
        ") { session_id turn_id project_slug role ts ai_title "
        "_additional { score } } } }"
    )
    body = _json.dumps({"query": gql}).encode("utf-8")
    req = _ur.Request(f"{wv_url}/v1/graphql", data=body,
                      headers={"Content-Type": "application/json"})
    try:
        with _ur.urlopen(req, timeout=deadline_s) as r:
            raw = _json.loads(r.read())
    except Exception:
        return []
    try:
        return raw["data"]["Get"][wv_class] or []
    except (KeyError, TypeError):
        return []


def _near_vector_jit(vec: list[float], top_n: int, wv_url: str,
                       wv_class: str, deadline_s: float) -> list[dict]:
    """nearVector Weaviate query for the JIT hook. Synthesizes
    `_additional.score = 1 - distance` so downstream display code
    treats both result lists uniformly."""
    import json as _json
    import urllib.request as _ur
    vec_lit = "[" + ",".join(repr(float(x)) for x in vec) + "]"
    gql = (
        "{ Get { "
        f"{wv_class}("
        f"nearVector: {{ vector: {vec_lit} }} "
        f"limit: {int(top_n)}"
        ") { session_id turn_id project_slug role ts ai_title "
        "_additional { distance } } } }"
    )
    body = _json.dumps({"query": gql}).encode("utf-8")
    req = _ur.Request(f"{wv_url}/v1/graphql", data=body,
                      headers={"Content-Type": "application/json"})
    try:
        with _ur.urlopen(req, timeout=deadline_s) as r:
            raw = _json.loads(r.read())
    except Exception:
        return []
    try:
        out = raw["data"]["Get"][wv_class] or []
    except (KeyError, TypeError):
        return []
    for h in out:
        d = (h.get("_additional") or {}).get("distance")
        try:
            score = 1.0 - float(d) if d is not None else 0.0
        except (ValueError, TypeError):
            score = 0.0
        h.setdefault("_additional", {})["score"] = score
    return out


def _pg_substring_jit(needles: list[str], top_n: int,
                       deadline_s: float) -> list[dict]:
    """v0.8.18 — literal-LIKE PG fallback for identifier-bearing
    prompts. BM25 stems identifiers (`203.0.113.42` → nothing) so
    a hot recall hole exists for the exact case where the user
    pastes an IP / hash / path / URL. This helper queries PG with
    `t.content ILIKE %needle%` for each detected identifier,
    returns the union in the same hit shape as `_bm25_jit`.

    Cheap: trigram index hits keep the per-needle query under
    ~20ms on a ~100k-turn corpus. We cap total work at top_n hits
    per needle.

    Returns [] on any failure (PG unreachable, malformed config,
    timeout) — the caller decides what to do."""
    if not needles or deadline_s <= 0:
        return []
    try:
        import psycopg2  # type: ignore[import]
    except Exception:
        return []
    cfg = _load_cfg()
    os_user = (os.environ.get("USER")
                or os.environ.get("LOGNAME") or "unknown")
    pg_db = cfg.get("PG_DB") or f"claude_dejavu_{os_user}"
    dsn = (f"host={cfg.get('PG_HOST', 'localhost')} "
           f"port={cfg.get('PG_PORT', '5432')} "
           f"user={cfg.get('PG_USER', 'postgres')} "
           f"password={cfg.get('PG_PASS', 'postgres')} "
           f"dbname={pg_db}")
    out: list[dict] = []
    seen_tids: set[int] = set()
    try:
        c = psycopg2.connect(
            dsn, connect_timeout=min(1, max(0.1, deadline_s)))
        try:
            cur = c.cursor()
            for needle in needles:
                if not needle:
                    continue
                cur.execute(
                    "SELECT t.id, t.session_id::text, "
                    "  s.project_slug, t.ts, "
                    "  COALESCE(t.gist, LEFT(t.content, 200)) AS gist, "
                    "  s.ai_title "
                    "FROM turns t "
                    "  JOIN sessions s ON s.id = t.session_id "
                    "WHERE t.role IN ('user','assistant') "
                    "  AND t.content_type = 'text' "
                    "  AND t.content ILIKE %s "
                    "ORDER BY t.id DESC LIMIT %s",
                    (f"%{needle}%", top_n))
                for tid, sid, slug, ts, gist, ai_title in cur.fetchall():
                    if tid in seen_tids:
                        continue
                    seen_tids.add(tid)
                    out.append({
                        "session_id": sid,
                        "turn_id": tid,
                        "project_slug": slug,
                        "ts": ts.isoformat() if ts else None,
                        "gist": gist or "",
                        "ai_title": ai_title or "",
                        # Synthetic score: literal matches are
                        # exact by construction, so we pin them at
                        # 1.0 so the threshold cut at the caller
                        # always lets them through.
                        "score": 1.0,
                        "source": "pg-substring",
                    })
        finally:
            try:
                c.close()
            except Exception:
                pass
    except Exception:
        return out
    return out


def _run_search(query: str, top_n: int,
                deadline_s: float) -> list[dict]:
    """In-process hybrid Weaviate turn search. Returns up to top_n
    hits as `{session_id, turn_id, ts, score, gist, ai_title}`.

    v0.8.7: when the caller-side cloud embed succeeds (DEJAVU_CLOUD_KEY
    set + cloud reachable within 0.4 * deadline), runs BM25 + nearVector
    in sequence and fuses via dejavu_bridge.retrieve_rrf. When the
    embed step returns None (no key / outage / timeout) falls back
    to v0.8.6's BM25-only path — degraded mode behaves bit-for-bit
    like v0.8.6.

    Returns [] on any failure (config missing, urllib error, JSON
    parse error, timeout, …). The caller decides what to do with
    an empty result.

    `deadline_s` is enforced via urlopen's `timeout` parameter — we
    don't try to interrupt mid-call beyond what urllib supports.
    """
    if deadline_s <= 0:
        return []
    cfg = _load_cfg()
    wv_url = cfg.get("WV_URL") or "http://localhost:8888"
    wv_class = (os.environ.get("CLAUDE_DEJAVU_WV_CLASS")
                or cfg.get("WV_CLASS") or "ClaudeDejavuTurn")

    # v0.8.7: try the caller-side cloud embed first. Strict budget so
    # we still fit in the 2s hook deadline (embed at most 40% of the
    # remaining time, capped at 0.8s — well under 1s so the next
    # urlopen has slack). If the embed returns None we go BM25-only
    # AND log nothing extra; matches v0.8.6 behavior.
    embed_deadline = min(0.8, max(0.1, deadline_s * 0.4))
    vec: list[float] | None = None
    try:
        _here = Path(__file__).resolve().parent
        if str(_here.parent / "code") not in sys.path:
            sys.path.insert(0, str(_here.parent / "code"))
        from query_embed import embed_query  # type: ignore[import]
        vec = embed_query(query, timeout_s=embed_deadline)
    except Exception:
        vec = None

    # Carve remaining time for the Weaviate calls. If we embedded,
    # we have less budget left for BM25 + nearVector. Each gets the
    # remainder split in half (cheap, bounded; Weaviate is ~150ms
    # in normal operation).
    remaining = max(0.1, deadline_s - embed_deadline if vec is not None
                    else deadline_s)
    branch_deadline = remaining / 2 if vec is not None else remaining

    bm25_hits = _bm25_jit(query, top_n, wv_url, wv_class, branch_deadline)
    if vec is not None:
        vec_hits = _near_vector_jit(vec, top_n, wv_url, wv_class,
                                       branch_deadline)
    else:
        vec_hits = []

    # Fuse via the bridge if we have both lists. Defensive: any
    # failure falls back to the BM25 list (v0.8.6 path).
    if vec_hits and bm25_hits:
        try:
            from distill_pipeline import _bridge as _bridge_mod  # type: ignore[import]

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
            fused = _bridge_mod.retrieve_rrf(
                [_to_id(bm25_hits), _to_id(vec_hits)])
            # Re-attach original turn payload using the bridge's fused
            # ordering. retrieve_rrf in the real wheel returns bare
            # {id: …} dicts (only id preserved), so we need this map
            # to bring back ts/score/ai_title/etc for the formatter.
            ordered = []
            for entry in fused:
                if not isinstance(entry, dict):
                    continue
                key = entry.get("id")
                if key is None:
                    continue
                orig = payload.get(str(key))
                if orig is not None:
                    ordered.append(orig)
            wv_hits = ordered[:top_n]
            _rrf_used = True
        except Exception:
            wv_hits = bm25_hits
            _rrf_used = False
    else:
        wv_hits = vec_hits or bm25_hits
        _rrf_used = False

    # SessionStart-style breadcrumb to stderr so jit-recall-preview
    # users can tell which path ran. Hooks never log to stdout
    # (that goes to Claude). Stderr is fine.
    try:
        sys.stderr.write(
            f"[jit-recall] mode={'rrf' if _rrf_used else 'bm25'} "
            f"bm25={len(bm25_hits)} vec={len(vec_hits)} "
            f"fused={len(wv_hits)}\n"
        )
    except Exception:
        pass

    # Pull gists from Postgres in one shot. Best-effort: if PG is
    # down we still return hits keyed off `ai_title`.
    turn_ids = [int(h.get("turn_id") or 0) for h in wv_hits
                if h.get("turn_id")]
    gist_map: dict[int, str] = {}
    if turn_ids:
        try:
            import psycopg2  # type: ignore
            os_user = (os.environ.get("USER")
                       or os.environ.get("LOGNAME") or "unknown")
            pg_db = cfg.get("PG_DB") or f"claude_dejavu_{os_user}"
            with psycopg2.connect(
                host=cfg.get("PG_HOST", "localhost"),
                port=cfg.get("PG_PORT", "5450"),
                user=cfg.get("PG_USER", "postgres"),
                password=cfg.get("PG_PASS", "postgres"),
                dbname=pg_db,
                connect_timeout=1,
            ) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, gist FROM turns WHERE id = ANY(%s)",
                        (turn_ids,),
                    )
                    gist_map = {r[0]: r[1] for r in cur.fetchall() if r[1]}
        except Exception:
            pass

    out: list[dict] = []
    for h in wv_hits:
        try:
            tid = int(h.get("turn_id") or 0)
        except (ValueError, TypeError):
            tid = 0
        score = 0.0
        try:
            score = float(((h.get("_additional") or {}).get("score")) or 0)
        except (ValueError, TypeError):
            pass
        out.append({
            "session_id": h.get("session_id") or "",
            "turn_id":    tid,
            "ts":         h.get("ts") or "",
            "project_slug": h.get("project_slug") or "",
            "score":      score,
            "gist":       gist_map.get(tid) or "",
            "ai_title":   h.get("ai_title") or "",
        })

    # v0.8.9 Context Economy phase 6 — opt-in topic_reweight. No-op
    # when TOPIC_REWEIGHT_ENABLED != true, so v0.8.8 hook behavior
    # is preserved bit-for-bit by default.
    out = _topic_reweight_jit(out, query, cfg)
    return out


def _topic_reweight_jit(hits: list[dict], query: str,
                         cfg: dict[str, str]) -> list[dict]:
    """JIT-hook equivalent of recall._topic_reweight. Operates on the
    flat hit dicts the hook returns (`{turn_id, score, ai_title,
    ...}`) rather than Weaviate's `_additional.score` nesting.

    Resilient: any failure (no ai_title, no topic, bridge raises)
    returns the input list unchanged. Reweighted scores are sorted
    desc so `compute_recall_block`'s threshold + disclose step see
    the new ranking.
    """
    if not hits:
        return hits
    if not _cfg_bool(cfg, "TOPIC_REWEIGHT_ENABLED", False):
        return hits
    same_f = _cfg_float(cfg, "TOPIC_REWEIGHT_SAME_FACTOR", 0.7)
    cross_f = _cfg_float(cfg, "TOPIC_REWEIGHT_CROSS_FACTOR", 1.3)
    try:
        _here = Path(__file__).resolve().parent
        if str(_here.parent / "code") not in sys.path:
            sys.path.insert(0, str(_here.parent / "code"))
        from topic import extract_topic  # type: ignore[import]
        current_topic = extract_topic(query or "")
        if not current_topic:
            return hits
        hit_topics: dict[str, str] = {}
        candidates: list[dict] = []
        for h in hits:
            tid = h.get("turn_id")
            if not tid:
                continue
            key = str(tid)
            ai_title = h.get("ai_title") or ""
            topic = extract_topic(ai_title)
            if topic:
                hit_topics[key] = topic
            candidates.append({"id": key, "score": float(h.get("score") or 0.0)})
        from distill_pipeline import _bridge as _bridge_mod  # type: ignore
        reweighted = _bridge_mod.topic_reweight(
            candidates, current_topic, hit_topics,
            same_topic_factor=same_f,
            cross_topic_factor=cross_f,
        )
        # Build a score-by-id map and re-merge into the original hits.
        new_score: dict[str, float] = {}
        for entry in reweighted:
            if not isinstance(entry, dict):
                continue
            key = entry.get("id")
            if key is None:
                continue
            try:
                new_score[str(key)] = float(entry.get("score", 0.0))
            except (TypeError, ValueError):
                continue
        out: list[dict] = []
        for h in hits:
            tid = h.get("turn_id")
            key = str(tid) if tid else ""
            merged = dict(h)
            if key in new_score:
                merged["score"] = new_score[key]
            out.append(merged)
        out.sort(key=lambda h: -float(h.get("score") or 0.0))
        return out
    except Exception:
        return hits


def compute_recall_block(prompt: str,
                          cfg: dict[str, str] | None = None,
                          *,
                          now_fn=time.monotonic,
                          search_fn=None,
                          ) -> str:
    """Pure-ish core: takes a prompt + cfg dict, returns the formatted
    block (possibly empty). Exposed for unit tests so they can mock
    `search_fn` without touching Weaviate.

    Honors every guard:
      - JIT_RECALL_ENABLED off → ""
      - prompt shorter than JIT_RECALL_MIN_PROMPT_CHARS → ""
      - no extracted keywords → ""
      - search returns [] → ""
      - top hit score < JIT_RECALL_SCORE_THRESHOLD → ""
      - 2s deadline → search aborted, "" returned
    """
    if cfg is None:
        cfg = _load_cfg()
    if not _cfg_bool(cfg, "JIT_RECALL_ENABLED", False):
        return ""

    min_chars = _cfg_int(cfg, "JIT_RECALL_MIN_PROMPT_CHARS", 40)
    # v0.8.16 — bypass the min-chars guard when the prompt contains an
    # identifier. Short prompts like "what about 203.0.113.42?" or
    # "trace ./fleet/k3sBootstrap.service.ts" carry strong recall
    # signal even at <40 chars — the heuristic is for chatty prompts
    # that match too many irrelevant turns, not for these.
    ids = extract_identifiers(prompt)
    if not ids and len(prompt.strip()) < min_chars:
        return ""

    top_n = max(1, _cfg_int(cfg, "JIT_RECALL_TOP_N", 3))
    threshold = _cfg_float(cfg, "JIT_RECALL_SCORE_THRESHOLD", 0.6)
    token_cap = max(50, _cfg_int(cfg, "JIT_RECALL_TOKEN_CAP", 500))

    # v0.8.16 — identifiers first, then keywords. If the prompt
    # mentions an IP / UUID / hash / URL / path / email the literal
    # string carries more recall signal than any stemmed token. We
    # prepend identifiers so the BM25 path treats them as required
    # terms; the keyword tail keeps semantic context.
    kws = extract_keywords(prompt)
    if not ids and not kws:
        return ""
    query = " ".join(ids + kws).strip()

    # Reserve ~200ms of the 2s window for everything-not-search:
    # config load, keyword extract, format. The bulk goes to search.
    started = now_fn()
    overall_budget_s = 1.8
    deadline_s = max(0.1, overall_budget_s - (now_fn() - started))

    fn = search_fn if search_fn is not None else _run_search
    try:
        hits = fn(query, top_n, deadline_s)
    except Exception:
        hits = []

    # v0.8.18 — identifier-literal fallback. BM25 still stems away
    # the dots/slashes in IPs / URLs / paths even when we prepend
    # them to the query, so the standard search can return zero hits
    # for a prompt like "what about 203.0.113.42?". When identifiers
    # ARE present, run a small PG-substring query against each one
    # and merge the results into `hits` (identifier matches are given
    # synthetic score 1.0 so they survive the per-hit threshold cut
    # — they're literal matches, by construction relevant).
    if ids:
        try:
            sub_hits = _pg_substring_jit(ids, top_n, deadline_s)
        except Exception:
            sub_hits = []
        if sub_hits:
            seen_tids = {str(h.get("turn_id") or "") for h in hits}
            for h in sub_hits:
                if str(h.get("turn_id") or "") not in seen_tids:
                    hits.append(h)

    if not hits:
        return ""
    # Sort desc by score in case the backend doesn't already.
    hits.sort(key=lambda h: -float(h.get("score") or 0.0))
    if float(hits[0].get("score") or 0.0) < threshold:
        return ""
    # Drop hits below threshold individually too — a hit at 0.3 next
    # to a hit at 0.9 is still noise.
    hits = [h for h in hits if float(h.get("score") or 0.0) >= threshold]
    if not hits:
        return ""

    # v0.8.6 Context Economy phase 3 — replace fixed top_n with a
    # token-budget-aware fill via dejavu_bridge.disclose. The bridge
    # greedily packs candidates in score-desc order until the budget
    # runs out. Falls back to the input order if the bridge import /
    # call fails — never let a wheel bug break the recall path.
    #
    # Real-wheel signature is `disclose(candidates, budget_tokens)`
    # with each candidate `{id: str, text: str, score: float}`.
    # v0.8.7 audit caught that v0.8.6 was calling this with the wrong
    # kwarg name (`token_budget`) and raw Weaviate hits (missing
    # `id`/`text` fields), so it was silently `except: pass`-ing
    # every JIT invocation and falling back to the v0.8.5 path. Fix
    # surfaced via tests/test_bridge_signature_parity.py.
    try:
        # Lazy import — pulls the bridge only when JIT recall actually
        # fires, keeping hook cold-start under the 2s budget.
        _here = Path(__file__).resolve().parent
        if str(_here.parent / "code") not in sys.path:
            sys.path.insert(0, str(_here.parent / "code"))
        from distill_pipeline import _bridge as _bridge_mod  # type: ignore

        payload_by_id: dict[str, dict] = {}
        candidates: list[dict] = []
        for h in hits:
            tid = h.get("turn_id")
            if tid is None:
                continue
            key = str(tid)
            payload_by_id.setdefault(key, h)
            candidates.append({
                "id": key,
                "text": (h.get("gist") or h.get("ai_title") or ""),
                "score": float(h.get("score") or 0.0),
            })
        kept = _bridge_mod.disclose(candidates, budget_tokens=token_cap)
        # The bridge returns the surviving candidates in its chosen
        # order — re-hydrate them back to the original turn dicts so
        # the formatter still has ts / project_slug / session_id /
        # ai_title to print.
        rehydrated: list[dict] = []
        for entry in kept:
            if not isinstance(entry, dict):
                continue
            key = entry.get("id")
            if key is None or key not in payload_by_id:
                continue
            rehydrated.append(payload_by_id[key])
        if rehydrated:
            hits = rehydrated
    except Exception:
        # Defensive: bridge unavailable, malformed, mis-imported —
        # the old top_n cut-off in _format_block still applies, so
        # nothing's worse than v0.8.5 behaviour.
        pass

    if not hits:
        return ""
    return _format_block(hits, top_n, token_cap)


def main() -> int:
    """Read prompt from stdin, run recall, print to stdout. Always
    exits 0. Wrap everything — a hook that crashes would break every
    user prompt."""
    try:
        raw = sys.stdin.read()
    except Exception:
        _silent_exit()
        return 0  # unreachable
    if not raw.strip():
        _silent_exit()
        return 0  # unreachable
    try:
        import json as _json
        payload = _json.loads(raw)
    except Exception:
        _silent_exit()
        return 0  # unreachable

    prompt = (payload.get("prompt")
              or payload.get("user_prompt")
              or payload.get("text") or "")
    if not prompt or not isinstance(prompt, str):
        _silent_exit()
        return 0  # unreachable

    try:
        block = compute_recall_block(prompt)
    except Exception:
        block = ""

    if block:
        sys.stdout.write(block)
        sys.stdout.write("\n")
    sys.exit(0)
    return 0  # unreachable


if __name__ == "__main__":
    main()
