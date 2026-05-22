#!/usr/bin/env python3
"""
Semantic search layer — embeds symbols + routes into Weaviate so we can
match on intent, not just lexical similarity.

Why: trigram catches typos but not naming-convention drift. Real cases
the lexical layer misses:

  proposed_call: getDate         lexical match: parser (sim=0.40, wrong)
                                 semantic match: fetchDate (cosine=0.86, right)

  proposed_url:  /api/order/create   lexical match: /api/orders (sim=0.55)
                                     semantic match: POST /api/orders (right verb + path)

Architecture:
  - Weaviate class `ClaudeDejavuCode` (per-tenant) with text2vec-transformers
    vectorizer. We already run the transformers-inference container — same
    infra, different class.
  - Indexed at the same time as the symbol/route reindex passes.
  - Two query modes: by symbol-intent string ("function that parses ISO dates")
    and by route-path-shape ("user creation endpoint").

Failure modes:
  - Weaviate down → all calls return [] and the lint pipeline still works
    via the lexical path. Semantic is strictly additive.
  - Class doesn't exist → ensure_class() creates it.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Iterable

CLASS_NAME = "ClaudeDejavuCode"
DOC_CLASS_NAME = "ClaudeDejavuDoc"


def _wv_url() -> str:
    """Resolve Weaviate base URL.

    Order: WV_URL env → config.env WV_URL → localhost:8080 default.
    The config.env fallback is essential because the dejavu CLI is
    typically invoked from a shell that has NOT sourced config.env —
    install.py writes WV_URL there but doesn't export it. Without
    this read, every reindex --docs / search-docs / ensure_class call
    hits the wrong port (caught 2026-05-17 evening).
    """
    env_val = os.environ.get("WV_URL")
    if env_val:
        return env_val.rstrip("/")
    # config.env fallback (matches rescue.py:_load_cfg pattern)
    try:
        if sys.platform.startswith("win"):
            base = (os.environ.get("LOCALAPPDATA")
                    or os.environ.get("APPDATA")
                    or str(os.path.expanduser("~\\AppData\\Local")))
            cfg_path = os.path.join(base, "claude-dejavu", "config.env")
        else:
            xdg = os.environ.get("XDG_DATA_HOME")
            if xdg:
                cfg_path = os.path.join(xdg, "claude-dejavu", "config.env")
            else:
                cfg_path = os.path.expanduser(
                    "~/.local/share/claude-dejavu/config.env")
        if os.path.isfile(cfg_path):
            with open(cfg_path, "r", encoding="utf-8",
                      errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("WV_URL=") and "=" in line:
                        return line.split("=", 1)[1].strip().rstrip("/")
    except Exception:
        # never crash _wv_url — caller already handles connect errors.
        pass
    return "http://localhost:8080"


def _http(method: str, path: str, body: dict | None = None,
          timeout: float = 5.0) -> dict | None:
    url = f"{_wv_url()}{path}"
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8", errors="replace")
            return json.loads(txt) if txt else {}
    except urllib.error.HTTPError as e:
        if e.code == 422 and method == "POST":
            # Weaviate returns 422 on duplicate-class create — non-fatal
            return None
        sys.stderr.write(f"semantic_search: {method} {path} → {e}\n")
        return None
    except Exception as e:
        sys.stderr.write(f"semantic_search: {method} {path} → {type(e).__name__}: {e}\n")
        return None


def is_available() -> bool:
    """Return True iff Weaviate is up + has the transformers vectorizer."""
    meta = _http("GET", "/v1/meta", timeout=2)
    if not isinstance(meta, dict):
        return False
    mods = meta.get("modules", {})
    return "text2vec-transformers" in mods


def ensure_class() -> bool:
    """Idempotently create the ClaudeDejavuCode class. Returns True if
    the class exists (or was just created) and is ready for ingestion.

    Also defensively adds the `repo_id` property to existing classes
    that pre-date v0.7 (per-repo identity model) — safe to call on
    every install because Weaviate returns 422 on duplicate property,
    which `_http` swallows as a no-op.
    """
    if not is_available():
        return False
    existing = _http("GET", f"/v1/schema/{CLASS_NAME}", timeout=2)
    if not (existing and isinstance(existing, dict)
              and existing.get("class") == CLASS_NAME):
        schema = {
            "class": CLASS_NAME,
            "vectorizer": "text2vec-transformers",
            "moduleConfig": {
                "text2vec-transformers": {
                    "vectorizeClassName": False,
                    "poolingStrategy": "masked_mean",
                }
            },
            "properties": [
                {"name": "kind", "dataType": ["text"],
                 "description": "'symbol' | 'route'",
                 "moduleConfig": {"text2vec-transformers": {"skip": True}}},
                {"name": "project_slug", "dataType": ["text"],
                 "moduleConfig": {"text2vec-transformers": {"skip": True}}},
                {"name": "repo_id", "dataType": ["text"],
                 "tokenization": "field",
                 "moduleConfig": {"text2vec-transformers": {"skip": True}}},
                {"name": "name", "dataType": ["text"],
                 "description": "symbol name OR HTTP method+path (`POST /api/users`)"},
                {"name": "signature", "dataType": ["text"]},
                {"name": "docstring", "dataType": ["text"]},
                {"name": "file_path", "dataType": ["text"],
                 "moduleConfig": {"text2vec-transformers": {"skip": True}}},
                {"name": "language", "dataType": ["text"],
                 "moduleConfig": {"text2vec-transformers": {"skip": True}}},
                {"name": "symbol_id", "dataType": ["int"],
                 "moduleConfig": {"text2vec-transformers": {"skip": True}}},
                {"name": "route_id", "dataType": ["int"],
                 "moduleConfig": {"text2vec-transformers": {"skip": True}}},
            ],
        }
        result = _http("POST", "/v1/schema", body=schema)
        if not (result is not None
                  or _http("GET", f"/v1/schema/{CLASS_NAME}") is not None):
            return False

    # Defensive add of repo_id for older installs (pre-v0.7). Idempotent:
    # safe to call on every install since Weaviate's addProperty endpoint
    # returns 422 on duplicate-property which _http handles as None.
    schema = _http("GET", f"/v1/schema/{CLASS_NAME}", timeout=2)
    if schema and isinstance(schema, dict):
        existing_props = {p["name"] for p in schema.get("properties", [])}
        if "repo_id" not in existing_props:
            _http("POST", f"/v1/schema/{CLASS_NAME}/properties",
                   body={
                       "name": "repo_id",
                       "dataType": ["text"],
                       "tokenization": "field",
                       "moduleConfig": {
                           "text2vec-transformers": {"skip": True},
                       },
                   })
    return True


def _tenant() -> str:
    return os.environ.get("USER") or os.environ.get("LOGNAME") or "default"


def backfill_repo_ids_into_code() -> dict:
    """One-shot: tag every existing ClaudeDejavuCode row with the
    corresponding repos.id (matched by repos.label == project_slug).

    Idempotent: rows that already have a non-empty repo_id are skipped.

    Returns:
        {"tagged": int, "orphan_slugs": [slug, ...]}

    Strategy:
      1. Use GraphQL to fetch unique project_slugs from ClaudeDejavuCode.
      2. For each slug, look up repos.label == slug to get the repo UUID.
      3. Use Weaviate batch update to set repo_id on rows missing it
         (where project_slug = slug AND repo_id is null/empty).

    Failure modes:
      - Weaviate down → returns {tagged: 0, orphan_slugs: []}, no-op
      - Postgres down → returns {tagged: 0, orphan_slugs: []}, logs to stderr
      - Per-slug update failure → counts as orphan, surfaces in result
    """
    if not is_available():
        return {"tagged": 0, "failed": 0, "orphan_slugs": []}
    # Need PG to resolve slug → uuid
    try:
        import psycopg2  # noqa
    except ImportError:
        sys.stderr.write("backfill: psycopg2 not available; skipping\n")
        return {"tagged": 0, "failed": 0, "orphan_slugs": []}

    # Read repos table for label → id map.
    try:
        import pathlib as _pl
        sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
        from recall import _conn  # type: ignore
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, label FROM repos WHERE label IS NOT NULL")
            label_to_id: dict[str, str] = {}
            for rid, lbl in cur.fetchall():
                if not lbl:
                    continue
                if lbl in label_to_id:
                    sys.stderr.write(
                        f"backfill: duplicate repos.label {lbl!r}; "
                        f"keeping last-seen (was {label_to_id[lbl]}, "
                        f"now {rid})\n")
                label_to_id[lbl] = str(rid)
    except Exception as e:
        sys.stderr.write(f"backfill: pg lookup failed: {e}\n")
        return {"tagged": 0, "failed": 0, "orphan_slugs": []}

    # Get all unique project_slugs in ClaudeDejavuCode via GraphQL
    # aggregate. (Weaviate's Aggregate supports groupBy.)
    body = {"query":
        "{ Aggregate { " + CLASS_NAME + "(groupBy: \"project_slug\") "
        "{ groupedBy { value } } } }"}
    r = _http("POST", "/v1/graphql", body=body)
    if not r:
        return {"tagged": 0, "failed": 0, "orphan_slugs": []}
    groups = (r.get("data", {}).get("Aggregate", {}).get(CLASS_NAME, []) or [])
    unique_slugs = [g.get("groupedBy", {}).get("value") for g in groups]
    unique_slugs = [s for s in unique_slugs if s]

    tagged = 0
    failed = 0
    orphan_slugs: list[str] = []
    for slug in unique_slugs:
        rid = label_to_id.get(slug)
        if not rid:
            orphan_slugs.append(slug)
            continue
        # Batch update: set repo_id on all rows with this slug that
        # don't already have one. Weaviate's REST batch-update doesn't
        # support conditional update, so we use GraphQL Get + per-object
        # PATCH. For an MVP, fetch IDs of rows missing repo_id and PATCH.
        list_body = {"query":
            "{ Get { " + CLASS_NAME + "(where: {"
            "operator: And, operands: ["
            f'{{path: ["project_slug"], operator: Equal, valueText: "{slug}"}}'
            "] }, limit: 10000) { _additional { id } repo_id } } }"}
        lr = _http("POST", "/v1/graphql", body=list_body)
        if not lr:
            continue
        hits = (lr.get("data", {}).get("Get", {}).get(CLASS_NAME, []) or [])
        for h in hits:
            existing_repo_id = (h.get("repo_id") or "").strip()
            if existing_repo_id:
                continue
            object_uuid = h.get("_additional", {}).get("id")
            if not object_uuid:
                continue
            # PATCH the single object.
            patch_body = {
                "class": CLASS_NAME,
                "properties": {"repo_id": rid},
            }
            if _http("PATCH", f"/v1/objects/{CLASS_NAME}/{object_uuid}",
                      body=patch_body) is not None:
                tagged += 1
            else:
                failed += 1
    return {
        "tagged": tagged,
        "failed": failed,
        "orphan_slugs": sorted(set(orphan_slugs)),
    }


# ─── Ingestion ──────────────────────────────────────────────────────────────


def index_symbols(project_slug: str, conn, *, batch_size: int = 100) -> int:
    """Pull every symbol for the project from Postgres, embed via
    text2vec-transformers, store in Weaviate. Idempotent — re-runs
    overwrite by deterministic UUID derived from (project_slug, symbol_id).
    Returns count of objects sent to Weaviate.
    """
    if not ensure_class():
        return 0
    import uuid
    NS = uuid.UUID("12345678-9abc-def0-1234-56789abcdef0")  # arbitrary fixed ns

    sent = 0
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, name, qualified_name, signature, doc, file_path, kind, language
               FROM symbols WHERE project_slug = %s""",
            (project_slug,),
        )
        rows = cur.fetchall()

    objects: list[dict] = []
    for sid, name, qn, sig, doc, file_path, kind, language in rows:
        # Build the embedding text by concatenating semantically-rich fields.
        # Skip-flagged fields (file_path, kind) won't be embedded but stay
        # filterable.
        obj_id = str(uuid.uuid5(NS, f"sym:{project_slug}:{sid}"))
        objects.append({
            "class": CLASS_NAME,
            "id": obj_id,
            "properties": {
                "kind": "symbol",
                "project_slug": project_slug,
                "name": name or "",
                "signature": sig or qn or name or "",
                "docstring": doc or "",
                "file_path": file_path or "",
                "language": language or "",
                "symbol_id": int(sid),
                "route_id": 0,
            },
        })
        if len(objects) >= batch_size:
            sent += _flush_batch(objects)
            objects = []
    if objects:
        sent += _flush_batch(objects)
    return sent


def index_routes(project_slug: str, conn, *, batch_size: int = 100) -> int:
    """Same as index_symbols but for api_routes."""
    if not ensure_class():
        return 0
    import uuid
    NS = uuid.UUID("12345678-9abc-def0-1234-56789abcdef0")
    sent = 0
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, framework, source, method, path_pattern, raw_path,
                      source_file, description
               FROM api_routes WHERE project_slug = %s""",
            (project_slug,),
        )
        rows = cur.fetchall()
    objects: list[dict] = []
    for rid, framework, source, method, path_pat, raw_path, file_path, desc in rows:
        obj_id = str(uuid.uuid5(NS, f"route:{project_slug}:{rid}"))
        # Embedding text composes the searchable summary
        sig = f"{method} {path_pat}"
        if desc:
            sig += f" — {desc}"
        objects.append({
            "class": CLASS_NAME,
            "id": obj_id,
            "properties": {
                "kind": "route",
                "project_slug": project_slug,
                "name": f"{method} {path_pat}",
                "signature": sig,
                "docstring": desc or "",
                "file_path": file_path or "",
                "language": framework or "",
                "symbol_id": 0,
                "route_id": int(rid),
            },
        })
        if len(objects) >= batch_size:
            sent += _flush_batch(objects)
            objects = []
    if objects:
        sent += _flush_batch(objects)
    return sent


def _flush_batch(objects: list[dict]) -> int:
    """POST /v1/batch/objects. Returns count of objects accepted."""
    if not objects:
        return 0
    body = {"objects": objects}
    result = _http("POST", "/v1/batch/objects", body=body, timeout=30)
    if not isinstance(result, list):
        return 0
    # Each entry has {result: {status: 'SUCCESS' | ...}}
    return sum(1 for r in result
               if isinstance(r, dict)
               and (r.get("result") or {}).get("status") == "SUCCESS")


# ─── ClaudeDejavuDoc (markdown chunks, keyed by repo_id) ───────────────────


def _doc_class_schema() -> dict:
    return {
        "class": DOC_CLASS_NAME,
        "description": "One heading-anchored section of a markdown doc, "
                        "keyed by repo_id.",
        "vectorizer": "text2vec-transformers",
        "moduleConfig": {
            "text2vec-transformers": {
                "poolingStrategy": "masked_mean",
                "vectorizeClassName": False,
            }
        },
        "invertedIndexConfig": {"bm25": {"b": 0.75, "k1": 1.2}},
        "properties": [
            {"name": "repo_id", "dataType": ["text"],
              "tokenization": "field",
              "moduleConfig": {"text2vec-transformers": {"skip": True}}},
            {"name": "file_path", "dataType": ["text"],
              "tokenization": "field",
              "moduleConfig": {"text2vec-transformers": {"skip": True}}},
            {"name": "heading_path", "dataType": ["text"],
              "tokenization": "word"},
            {"name": "chunk_idx", "dataType": ["int"]},
            {"name": "one_line_summary", "dataType": ["text"],
              "tokenization": "word"},
            {"name": "content", "dataType": ["text"],
              "tokenization": "word"},
            {"name": "mtime", "dataType": ["date"]},
            {"name": "content_hash", "dataType": ["text"],
              "tokenization": "field",
              "moduleConfig": {"text2vec-transformers": {"skip": True}}},
        ],
    }


def ensure_doc_class() -> bool:
    """Idempotently create the ClaudeDejavuDoc class if missing."""
    if not is_available():
        return False
    existing = _http("GET", f"/v1/schema/{DOC_CLASS_NAME}", timeout=2)
    if existing and isinstance(existing, dict) and existing.get("class") == DOC_CLASS_NAME:
        return True
    _http("POST", "/v1/schema", body=_doc_class_schema())
    # Re-check.
    again = _http("GET", f"/v1/schema/{DOC_CLASS_NAME}", timeout=2)
    return bool(again and again.get("class") == DOC_CLASS_NAME)


def index_docs(repo_id: str, file_path: str, chunks, *,
                  mtime_iso: str, batch_size: int = 50) -> int:
    """Embed `chunks` (iterable of doc_indexer.Chunk) into ClaudeDejavuDoc
    keyed by (repo_id, file_path). Returns count embedded.

    `chunks` is consumed as an iterable so generators are safe. The
    return value is the total objects flushed — useful for caller
    metrics. Caller's responsibility: delete prior chunks for
    (repo_id, file_path) BEFORE calling this (full file replace
    semantics).
    """
    if not is_available():
        return 0
    if not ensure_doc_class():
        return 0
    objects: list = []
    total_flushed = 0
    for ch in chunks:
        objects.append({
            "class": DOC_CLASS_NAME,
            "properties": {
                "repo_id": repo_id,
                "file_path": file_path,
                "heading_path": ch.heading_path,
                "chunk_idx": ch.chunk_idx,
                "one_line_summary": ch.one_line_summary,
                "content": ch.content,
                "mtime": mtime_iso,
                "content_hash": ch.content_hash,
            },
        })
        if len(objects) >= batch_size:
            total_flushed += _flush_batch(objects)
            objects = []
    if objects:
        total_flushed += _flush_batch(objects)
    return total_flushed


def delete_docs_for_file(repo_id: str, file_path: str) -> int:
    """Tombstone all chunks for this (repo_id, file_path)."""
    if not is_available():
        return 0
    # Weaviate v1.20+ batch-delete: DELETE /v1/batch/objects with
    # {match: {class, where}}. The old POST /v1/batch/objects/delete
    # path was retired (404 in 1.27+). Caught 2026-05-17.
    body = {
        "match": {
            "class": DOC_CLASS_NAME,
            "where": {
                "operator": "And",
                "operands": [
                    {"path": ["repo_id"], "operator": "Equal", "valueText": repo_id},
                    {"path": ["file_path"], "operator": "Equal", "valueText": file_path},
                ],
            },
        },
        "output": "minimal",
    }
    r = _http("DELETE", "/v1/batch/objects", body=body)
    return int(r.get("results", {}).get("matches", 0)) if r else 0


def reindex_project(project_slug: str, conn) -> dict:
    """Convenience: embed both symbols and routes for a project."""
    if not is_available():
        return {"error": "weaviate_unavailable", "symbols": 0, "routes": 0}
    return {
        "symbols": index_symbols(project_slug, conn),
        "routes": index_routes(project_slug, conn),
    }


# ─── Query ──────────────────────────────────────────────────────────────────


def _rrf_fuse(rankings: list[list[dict]], k: float = 60.0) -> list[dict]:
    """Reciprocal Rank Fusion across multiple ranked hit lists.

    Each list is dicts with at least a 'result_id' (or 'id') key. We
    score each hit as `sum(1 / (k + rank+1))` across the lists it
    appears in, then sort descending. Stable tie-break by ascending
    result_id so the output is deterministic.
    """
    scores: dict[str, float] = {}
    keep: dict[str, dict] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking):
            rid = hit.get("result_id") or hit.get("id") or str(hit)
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + rank + 1)
            keep.setdefault(rid, hit)
    return [keep[rid] for rid, _ in sorted(
        scores.items(), key=lambda kv: (-kv[1], kv[0]))]


def _search_code(intent: str, *, project_slug: str | None,
                  kind: str | None, limit: int) -> list[dict]:
    """Query ClaudeDejavuCode for symbols/routes — preserves pre-v0.7
    behavior of `search()`. Returns hits as the legacy shape (with
    `result_id` added so RRF can fuse cross-kind)."""
    if not is_available():
        return []
    fields = ("kind project_slug name signature docstring file_path language "
              "symbol_id route_id _additional { id distance }")
    operands = []
    if project_slug:
        operands.append(
            f'{{ path: ["project_slug"], operator: Equal, valueText: '
            f'{json.dumps(project_slug)} }}'
        )
    if kind:
        operands.append(
            f'{{ path: ["kind"], operator: Equal, valueText: '
            f'{json.dumps(kind)} }}'
        )
    gql = (
        f'{{ Get {{ {CLASS_NAME}('
        f'limit: {limit} '
        f'nearText: {{ concepts: [{json.dumps(intent)}] }} '
    )
    if operands:
        gql += (
            f'where: {{ operator: And operands: [' + ", ".join(operands) + f'] }}'
        )
    gql += f') {{ {fields} }} }} }}'
    result = _http("POST", "/v1/graphql", body={"query": gql}, timeout=10)
    if not isinstance(result, dict):
        return []
    try:
        hits = result["data"]["Get"][CLASS_NAME] or []
    except (KeyError, TypeError):
        return []
    out = []
    for h in hits:
        d = (h.get("_additional") or {}).get("distance")
        hit_kind = h.get("kind")
        # Build a stable result_id so RRF can dedupe across kinds.
        if hit_kind == "route":
            rid = f"route|{project_slug or ''}|{h.get('route_id') or 0}"
        else:
            rid = f"symbol|{project_slug or ''}|{h.get('symbol_id') or 0}"
        out.append({
            "kind": hit_kind,
            "result_id": rid,
            "name": h.get("name"),
            "signature": h.get("signature"),
            "docstring": h.get("docstring"),
            "file_path": h.get("file_path"),
            "language": h.get("language"),
            "symbol_id": h.get("symbol_id"),
            "route_id": h.get("route_id"),
            "distance": float(d) if d is not None else None,
            "similarity": 1.0 - float(d) if d is not None else 0.0,
        })
    return out


def _search_doc(intent: str, *, repo_id: str | None, limit: int) -> list[dict]:
    """Query ClaudeDejavuDoc with optional repo_id filter.

    Defensive interpolation: repo_id is a UUID (no metacharacters),
    but escape just in case."""
    if not is_available():
        return []
    def _esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    where_clause = ""
    if repo_id:
        where_clause = (
            ', where: {path: ["repo_id"], operator: Equal, valueText: "'
            + _esc(repo_id) + '"}'
        )
    intent_esc = _esc(intent)
    body = {
        "query": (
            "{ Get { " + DOC_CLASS_NAME + "("
            'nearText: {concepts: ["' + intent_esc + '"]}, '
            "limit: " + str(limit)
            + where_clause +
            ") { repo_id file_path heading_path chunk_idx "
            "one_line_summary _additional { id distance } } } }"
        )
    }
    r = _http("POST", "/v1/graphql", body=body)
    if not r:
        return []
    raw = r.get("data", {}).get("Get", {}).get(DOC_CLASS_NAME, []) or []
    return [
        {
            "kind": "doc",
            "result_id": f"doc|{h['repo_id']}|{h['file_path']}|{h['chunk_idx']}",
            "doc_path": h["file_path"],
            "heading_path": h["heading_path"],
            "one_line_summary": h["one_line_summary"],
            "repo_id": h["repo_id"],
            "score": 1.0 - float(h.get("_additional", {}).get("distance", 1.0)),
        }
        for h in raw
    ]


def _search_turn(intent: str, *, project_slug: str | None,
                  limit: int) -> list[dict]:
    """Turn search lives in recall.py (Postgres FTS + Weaviate hybrid).
    semantic_search.search's `kinds=['turn']` path is here for symmetry —
    callers wanting actual turn search should use the recall.py CLI or
    the dejavu_search MCP tool, which queries the right backend.

    For now this returns [] — the legacy `search()` callers never used
    kinds=['turn'] (they passed plain code-kind filters); the new MCP
    layer handles turn search itself."""
    return []


def search(intent: str,
            project_slug: str | None = None,
            *,
            kinds: list[str] | None = None,
            repo_id: str | None = None,
            kind: str | None = None,        # legacy code-only filter
            limit: int = 5,
            ) -> list[dict]:
    """Hybrid search across kinds.

    `kinds`: subset of {"turn", "doc", "symbol", "route"}. Default
              behavior preserves pre-v0.7 callers — if `kinds` is None
              we infer from the legacy `kind` arg (or default to
              ["symbol", "route"] which mirrors the old code-only
              search). When the new MCP layer calls us with explicit
              `kinds=["turn"]` the dispatcher will route accordingly.
    `repo_id`: filter doc hits (and, post-Phase-E, code/turn/route
                that carry repo_id) to a single repo.
    `project_slug`: legacy slug filter; preserved for back-compat.
    `kind`: legacy code-kind filter ("symbol" or "route"). Kept for
            callers from before the v0.7 refactor.
    """
    if kinds is None:
        # Back-compat: legacy callers passed `kind` (or neither). Old
        # `search()` queried ClaudeDejavuCode regardless — fall back to
        # the code search path so rewriter.py + semantic_intent.py keep
        # working unchanged.
        return _search_code(intent, project_slug=project_slug,
                             kind=kind, limit=limit)

    rankings: list[list[dict]] = []
    for k in kinds:
        if k == "doc":
            rankings.append(_search_doc(intent, repo_id=repo_id, limit=limit))
        elif k == "symbol":
            rankings.append(_search_code(intent, project_slug=project_slug,
                                           kind="symbol", limit=limit))
        elif k == "route":
            rankings.append(_search_code(intent, project_slug=project_slug,
                                           kind="route", limit=limit))
        elif k == "turn":
            rankings.append(_search_turn(intent, project_slug=project_slug,
                                           limit=limit))
        # unknown kinds silently skipped

    if not rankings:
        return []
    if len(rankings) == 1:
        return rankings[0][:limit]
    return _rrf_fuse(rankings)[:limit]


def delete_project(project_slug: str) -> bool:
    """Wipe all objects for a project — used when we re-index from scratch
    or when the user removes a project."""
    if not is_available():
        return False
    body = {
        "match": {
            "class": CLASS_NAME,
            "where": {"path": ["project_slug"], "operator": "Equal", "valueText": project_slug},
        },
        "output": "minimal",
    }
    # Weaviate v1.20+ batch-delete: DELETE /v1/batch/objects (see
    # delete_doc_chunks above for the rationale).
    result = _http("DELETE", "/v1/batch/objects", body=body, timeout=30)
    return result is not None
