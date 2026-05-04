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


def _wv_url() -> str:
    return os.environ.get("WV_URL", "http://localhost:8080").rstrip("/")


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
    the class exists (or was just created) and is ready for ingestion."""
    if not is_available():
        return False
    existing = _http("GET", f"/v1/schema/{CLASS_NAME}", timeout=2)
    if existing and isinstance(existing, dict) and existing.get("class") == CLASS_NAME:
        return True
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
    return result is not None or _http("GET", f"/v1/schema/{CLASS_NAME}") is not None


def _tenant() -> str:
    return os.environ.get("USER") or os.environ.get("LOGNAME") or "default"


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


def reindex_project(project_slug: str, conn) -> dict:
    """Convenience: embed both symbols and routes for a project."""
    if not is_available():
        return {"error": "weaviate_unavailable", "symbols": 0, "routes": 0}
    return {
        "symbols": index_symbols(project_slug, conn),
        "routes": index_routes(project_slug, conn),
    }


# ─── Query ──────────────────────────────────────────────────────────────────


def search(intent: str, project_slug: str, *, kind: str | None = None,
           limit: int = 5) -> list[dict]:
    """Semantic search. Returns hits ordered by cosine similarity.

    Args:
      intent: natural-language description of what we're looking for
              ('function that parses ISO dates', 'user creation endpoint').
      project_slug: scope filter.
      kind: 'symbol' | 'route' | None (any).
    """
    if not is_available():
        return []
    where: dict | None = {
        "operator": "And",
        "operands": [
            {"path": ["project_slug"], "operator": "Equal", "valueText": project_slug},
        ],
    }
    if kind:
        where["operands"].append({
            "path": ["kind"], "operator": "Equal", "valueText": kind,
        })
    fields = ("kind project_slug name signature docstring file_path language "
              "symbol_id route_id _additional { id distance }")
    where_str = json.dumps(where).replace('"', '\\"')
    gql = (
        f'{{ Get {{ {CLASS_NAME}('
        f'limit: {limit} '
        f'nearText: {{ concepts: [{json.dumps(intent)}] }} '
        f'where: {{ operator: And operands: ['
        f'{{ path: ["project_slug"], operator: Equal, valueText: {json.dumps(project_slug)} }}'
    )
    if kind:
        gql += f', {{ path: ["kind"], operator: Equal, valueText: {json.dumps(kind)} }}'
    gql += f'] }}'
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
        out.append({
            "kind": h.get("kind"),
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
    result = _http("POST", "/v1/batch/objects/delete", body=body, timeout=30)
    return result is not None
