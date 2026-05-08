#!/usr/bin/env python3
"""
One-shot migration: switch the local Weaviate `ClaudeDejavuTurn` class
from `vectorizer: text2vec-transformers` to `vectorizer: none`, preserving
existing vectors byte-for-byte (since both modes use the same
intfloat/multilingual-e5-large-instruct model and identical pooling).

Steps:
  1. Inspect current class — is it already `vectorizer: none`?
  2. Stream all objects via /v1/objects?include=vector with cursor
  3. Drop the class
  4. Recreate with `vectorizer: none`
  5. Bulk re-insert objects with explicit `vector=...` field
  6. Verify count matches

Usage:
  python migrate_to_cloud.py [--dry-run] [--wv-url URL] [--limit N]

Default is --dry-run: shows what WOULD be done. Pass --apply to actually
execute.

This script does NOT touch the user's plugin config. After successful
migration, set DEJAVU_EMBED_MODE=cloud and DEJAVU_CLOUD_KEY=... in your
config and the next ingester run will route through the dejavu-cloud
Worker. Until then it falls through to the local transformers container
unchanged (set ENABLE_CUDA / docker-compose adjustments as you prefer).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Iterable

CLASS_NAME = "ClaudeDejavuTurn"
DEFAULT_WV = "http://localhost:8888"
PAGE_SIZE = 100


def http(method: str, url: str, body: dict | None = None,
         timeout: float = 30.0) -> dict | list | None:
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8", errors="replace")
            return json.loads(txt) if txt else None
    except urllib.error.HTTPError as e:
        body_txt = e.read()[:500].decode(errors="replace")
        sys.stderr.write(f"HTTP {e.code} on {method} {url}: {body_txt}\n")
        return None


def fetch_class(wv: str) -> dict | None:
    return http("GET", f"{wv}/v1/schema/{CLASS_NAME}")


def stream_objects(wv: str, limit: int | None = None) -> Iterable[dict]:
    """Yield each object via the cursor API, including vectors."""
    cursor = None
    yielded = 0
    while True:
        url = (f"{wv}/v1/objects?class={CLASS_NAME}"
               f"&include=vector&limit={PAGE_SIZE}")
        if cursor:
            url += f"&after={cursor}"
        page = http("GET", url, timeout=60)
        if not isinstance(page, dict):
            return
        objs = page.get("objects") or []
        if not objs:
            return
        for o in objs:
            yield o
            yielded += 1
            if limit is not None and yielded >= limit:
                return
        cursor = objs[-1]["id"]


def class_def_no_vectorizer(orig: dict) -> dict:
    """Strip the text2vec-transformers config from an existing class def
    while preserving everything else (properties, inverted index, etc.)."""
    new = json.loads(json.dumps(orig))  # deep copy
    new["vectorizer"] = "none"
    new.pop("moduleConfig", None)
    for p in new.get("properties", []):
        p.pop("moduleConfig", None)
    return new


def insert_with_vector(wv: str, batch: list[dict]) -> int:
    """POST /v1/batch/objects with explicit vectors. Returns success count."""
    payload = {"objects": [{
        "class": CLASS_NAME,
        "id": o["id"],
        "properties": o.get("properties") or {},
        "vector": o.get("vector") or [],
    } for o in batch]}
    res = http("POST", f"{wv}/v1/batch/objects", body=payload, timeout=60)
    if not isinstance(res, list):
        return 0
    return sum(1 for r in res
               if isinstance(r, dict)
               and (r.get("result") or {}).get("status") == "SUCCESS")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wv-url", default=DEFAULT_WV)
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--apply", dest="dry_run", action="store_false")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap re-insert (for testing on a subset)")
    args = ap.parse_args()
    wv = args.wv_url.rstrip("/")

    print(f"=== migrate {CLASS_NAME} → vectorizer:none on {wv} ===")
    print(f"   mode: {'DRY-RUN' if args.dry_run else 'APPLY'}")

    cls = fetch_class(wv)
    if cls is None:
        print(f"  class {CLASS_NAME} not found on {wv}")
        return 2
    current_vec = cls.get("vectorizer")
    print(f"  current vectorizer: {current_vec}")
    if current_vec == "none":
        print("  already migrated — nothing to do")
        return 0

    # Buffer all objects in memory (one full pass) before destroying the class.
    # ~5 KB per object → 80 MB for 16k turns, fits comfortably. For installs
    # over ~500k turns this should be staged to a tmp jsonl file instead.
    print("  scanning objects (buffering in memory)...")
    buffered: list[dict] = []
    sample = []
    for o in stream_objects(wv, limit=args.limit):
        buffered.append(o)
        if len(sample) < 3:
            sample.append({
                "id": o["id"],
                "vector_dim": len(o.get("vector") or []),
                "props_keys": sorted((o.get("properties") or {}).keys()),
            })
    n = len(buffered)
    print(f"  scanned {n} objects")
    print(f"  sample: {json.dumps(sample, indent=2)}")
    if n > 0:
        bytes_total = sum(len(json.dumps(o)) for o in buffered[:100]) * (n / 100)
        print(f"  estimated buffer size: {bytes_total / 1024 / 1024:.1f} MB")

    new_cls = class_def_no_vectorizer(cls)
    print(f"  new class def vectorizer: {new_cls.get('vectorizer')}")
    print(f"  new class def has moduleConfig: {bool(new_cls.get('moduleConfig'))}")

    if args.dry_run:
        print("\n  DRY-RUN — pass --apply to actually drop+recreate+reinsert")
        return 0

    if n == 0:
        print("  nothing to migrate, just swapping schema")
    else:
        print(f"  about to delete class {CLASS_NAME} and re-insert {n} objects with preserved vectors")

    print("  dropping class...")
    http("DELETE", f"{wv}/v1/schema/{CLASS_NAME}", timeout=60)

    print("  creating class with vectorizer:none...")
    res = http("POST", f"{wv}/v1/schema", body=new_cls, timeout=30)
    if res is None:
        print("  FAIL: class create returned no response")
        print("  ROLLBACK NEEDED: re-apply original schema manually before retry")
        return 1

    print("  re-inserting objects with preserved vectors...")
    inserted = 0
    for i in range(0, n, 50):
        batch = buffered[i:i + 50]
        inserted += insert_with_vector(wv, batch)
        if i % 500 == 0:
            print(f"    inserted {inserted}/{n}...")
    print(f"  done — re-inserted {inserted}/{n} objects")
    if inserted < n:
        print(f"  WARN: re-insert count {inserted} < scanned {n}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
