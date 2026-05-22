#!/usr/bin/env python3
"""claude-dejavu — Weaviate corruption rescue toolkit (v0.8.3).

Three subcommands codify the post-disaster Weaviate fixes from the
2026-05-14 → 2026-05-16 rescue session:

* :func:`scan` — checks ``/v1/nodes``, ``/v1/schema``, and vector
  dimension consistency between the configured cloud embed endpoint
  and the class config.
* :func:`quarantine_segment` — moves the 5 files of an LSM segment
  (``.db``, ``.bloom``, ``.cna``, ``.secondary.0.bloom``,
  ``.secondary.1.bloom``) into ``$DATA_ROOT/weaviate-quarantine/<ts>/``.
  Stops + restarts Weaviate around the move.
* :func:`redo_class` — delete + recreate a class with a new vector dim /
  vectorizer. Optionally resets PG ``vectorized`` flags so a subsequent
  ingester bootstrap re-embeds at the new dim.

References (canonical behavior bibles):

* ``/tmp/quarantine-bad-weaviate-segment.sh``
* ``/tmp/redo-weaviate-at-1024.sh``

See ``docs/superpowers/specs/2026-05-16-pg-weaviate-rescue-toolkit-design.md``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
WEAVIATE_CLASS_FILE = PLUGIN_ROOT / "code" / "weaviate-class.json"


# The 5 file extensions that make up an LSM "objects" segment group.
# Per /tmp/quarantine-bad-weaviate-segment.sh.
SEGMENT_EXTENSIONS: tuple[str, ...] = (
    "db", "bloom", "cna",
    "secondary.0.bloom", "secondary.1.bloom",
)


# ─── Config helpers (mirrors rescue.py + pg_rescue.py) ────────────────


def _resolve_data_root() -> Path:
    override = os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "claude-dejavu"
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
    f = _resolve_data_root() / "config.env"
    if not f.exists():
        return cfg
    for line in f.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


class WeaviateRescueError(RuntimeError):
    """Hard failure during a Weaviate-rescue phase."""


# ─── HTTP helpers ─────────────────────────────────────────────────────


def _http(method: str, url: str, *, body: Optional[bytes] = None,
            timeout: float = 30.0) -> Optional[dict]:
    """One-shot HTTP request. Returns parsed JSON dict/list on 2xx,
    None on 404, raises on other errors."""
    headers = {"Content-Type": "application/json"} if body else {}
    req = urllib.request.Request(url, data=body, method=method,
                                   headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _wv_url(override: Optional[str] = None) -> str:
    if override:
        return override
    cfg = _load_cfg()
    return cfg.get("WV_URL", "http://localhost:8888")


# ─── scan ─────────────────────────────────────────────────────────────


def _probe_embed_dim(probe_text: str = "claude-dejavu probe") -> Optional[int]:
    """Call the configured cloud embed endpoint with a known-good probe
    string and return the vector dimension. Returns None if no endpoint
    is configured (the user is on local-only mode)."""
    try:
        from embedding_provider import embed_text  # type: ignore[import]
    except Exception:
        return None
    try:
        v = embed_text(probe_text)
    except Exception:
        return None
    if v is None:
        return None
    try:
        return len(v)
    except Exception:
        return None


def scan(*, class_filter: Optional[str] = None,
         wv_url: Optional[str] = None,
         probe_embed_fn=None) -> dict:
    """Check shard health + dimension consistency + missing classes.

    Returns ``{unhealthy_shards: [{class, shard, status}],
    dim_mismatches: [{class, expected, actual}],
    missing_classes: [name], reachable: bool, embed_dim: int|null}``.
    """
    url = _wv_url(wv_url)
    out: dict[str, Any] = {
        "unhealthy_shards": [],
        "dim_mismatches": [],
        "missing_classes": [],
        "reachable": False,
        "embed_dim": None,
    }

    # Reachability + nodes
    try:
        nodes = _http("GET", f"{url}/v1/nodes", timeout=5) or {}
        out["reachable"] = True
    except Exception:
        return out

    for node in (nodes.get("nodes") or []):
        for shard in (node.get("shards") or []):
            cname = shard.get("class") or ""
            if class_filter and cname != class_filter:
                continue
            status = (shard.get("status") or "").upper()
            if status not in ("", "READY"):
                out["unhealthy_shards"].append({
                    "class": cname,
                    "shard": shard.get("name") or "",
                    "status": status,
                })

    # Schema
    try:
        schema = _http("GET", f"{url}/v1/schema", timeout=5) or {}
    except Exception:
        return out
    classes = schema.get("classes") or []

    # Known canonical class names
    expected_classes = ["ClaudeDejavuTurn", "ClaudeDejavuDoc"]
    existing_names = {c.get("class") for c in classes if isinstance(c, dict)}
    if class_filter:
        if class_filter not in existing_names:
            out["missing_classes"].append(class_filter)
    else:
        for ec in expected_classes:
            if ec not in existing_names:
                out["missing_classes"].append(ec)

    # Dimension consistency: probe the configured cloud endpoint, compare
    # against the first object's vector dim per class (sample via GraphQL
    # _additional { vector }).
    probe = probe_embed_fn or _probe_embed_dim
    embed_dim = probe()
    out["embed_dim"] = embed_dim
    if embed_dim is None:
        return out

    for c in classes:
        if not isinstance(c, dict):
            continue
        cname = c.get("class") or ""
        if class_filter and cname != class_filter:
            continue
        # Sample one object's vector
        try:
            r = _http(
                "POST", f"{url}/v1/graphql",
                body=json.dumps({
                    "query": (
                        f"{{ Get {{ {cname}(limit: 1) "
                        f"{{ _additional {{ vector }} }} }} }}"
                    )
                }).encode("utf-8"),
                timeout=10,
            )
        except Exception:
            continue
        try:
            objs = (r or {}).get("data", {}).get("Get", {}).get(cname, [])
            if not objs:
                continue
            v = objs[0].get("_additional", {}).get("vector")
            if not v:
                continue
            actual = len(v)
        except Exception:
            continue
        if actual != embed_dim:
            out["dim_mismatches"].append({
                "class": cname,
                "expected": embed_dim,
                "actual": actual,
            })

    return out


# ─── quarantine_segment ───────────────────────────────────────────────


@dataclass
class QuarantineResult:
    moved: list[str]
    quarantine_dir: Path
    mode: str  # 'apply' | 'dry-run'
    container_stopped: bool = False
    container_restarted: bool = False


def _docker_run(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True,
                            timeout=timeout)


def quarantine_segment(segment_id: str, *,
                         class_name: str,
                         shard_id: str,
                         data_root: Optional[Path] = None,
                         container: str = "claude-dejavu-weaviate",
                         volume_name: str = "claude-dejavu_claude-dejavu-weaviate",
                         dry_run: bool = True,
                         docker_run=None) -> QuarantineResult:
    """Move the 5 files of an LSM segment into a timestamped quarantine.

    Mirrors /tmp/quarantine-bad-weaviate-segment.sh.

    The ``docker_run`` parameter is a test seam — tests inject a fake
    subprocess runner.
    """
    runner = docker_run or _docker_run
    ts = int(time.time())
    root = data_root or _resolve_data_root()
    qdir = root / "weaviate-quarantine" / str(ts) / shard_id
    shard_path = f"/var/lib/weaviate/{class_name.lower()}/{shard_id}/lsm/objects"
    bad_prefix = f"segment-{segment_id}"

    res = QuarantineResult(moved=[], quarantine_dir=qdir,
                              mode="apply" if not dry_run else "dry-run")

    # Identify files that exist in the container.
    expected_files = [f"{bad_prefix}.{ext}" for ext in SEGMENT_EXTENSIONS]
    if dry_run:
        # Probe via `docker exec ls` to confirm which files exist.
        r = runner(["docker", "exec", container, "ls", shard_path])
        present = set()
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                line = line.strip()
                if line.startswith(bad_prefix):
                    present.add(line)
        res.moved = [f for f in expected_files if f in present]
        return res

    # Apply path
    qdir.mkdir(parents=True, exist_ok=True)

    # Stop container
    stop = runner(["docker", "stop", container])
    res.container_stopped = (stop.returncode == 0)

    # Copy each file out, then delete via throwaway alpine container that
    # mounts the volume directly (the only safe way to delete a file
    # inside a stopped container's volume).
    runner(["docker", "start", container])  # need running for cp+exec
    time.sleep(1)
    for fname in expected_files:
        src = f"{container}:{shard_path}/{fname}"
        dst = str(qdir / fname)
        probe = runner(["docker", "exec", container,
                          "test", "-f", f"{shard_path}/{fname}"])
        if probe.returncode != 0:
            continue
        cp = runner(["docker", "cp", src, dst])
        if cp.returncode == 0:
            res.moved.append(fname)
    runner(["docker", "stop", container])

    # Delete the originals via mounted alpine
    if res.moved:
        rm_glob = (
            f"rm -f /data/{class_name.lower()}/{shard_id}/lsm/objects/"
            f"{bad_prefix}.*")
        runner(["docker", "run", "--rm",
                  "-v", f"{volume_name}:/data",
                  "alpine", "sh", "-c", rm_glob], timeout=60)

    # Restart
    rs = runner(["docker", "start", container])
    res.container_restarted = (rs.returncode == 0)
    return res


# ─── redo_class ───────────────────────────────────────────────────────


# Schemas for the two canonical dejavu classes when running at
# vectorizer:none (cloud-only mode). The Doc class is duplicated from
# semantic_search._doc_class_schema() with vectorizer flipped to none.
def _turn_class_schema_vec_none() -> dict:
    return {
        "class": "ClaudeDejavuTurn",
        "description": (
            "One turn from a Claude Code session. Vectors supplied by "
            "caller (cloud embed); dim locked by first insert."),
        "vectorizer": "none",
        "vectorIndexType": "hnsw",
        "vectorIndexConfig": {"distance": "cosine"},
        "invertedIndexConfig": {"bm25": {"b": 0.75, "k1": 1.2}},
        "properties": [
            {"name": "session_id",   "dataType": ["text"],
              "tokenization": "field"},
            {"name": "turn_id",      "dataType": ["int"]},
            {"name": "project_slug", "dataType": ["text"],
              "tokenization": "field"},
            {"name": "role",         "dataType": ["text"],
              "tokenization": "field"},
            {"name": "content",      "dataType": ["text"],
              "tokenization": "word"},
            {"name": "ts",           "dataType": ["date"]},
            {"name": "model",        "dataType": ["text"],
              "tokenization": "field"},
            {"name": "ai_title",     "dataType": ["text"],
              "tokenization": "word"},
        ],
    }


def _doc_class_schema_vec_none() -> dict:
    return {
        "class": "ClaudeDejavuDoc",
        "description": "Markdown chunk keyed by repo_id (vectorizer:none).",
        "vectorizer": "none",
        "vectorIndexType": "hnsw",
        "vectorIndexConfig": {"distance": "cosine"},
        "invertedIndexConfig": {"bm25": {"b": 0.75, "k1": 1.2}},
        "properties": [
            {"name": "repo_id", "dataType": ["text"],
              "tokenization": "field"},
            {"name": "file_path", "dataType": ["text"],
              "tokenization": "field"},
            {"name": "heading_path", "dataType": ["text"],
              "tokenization": "word"},
            {"name": "chunk_idx", "dataType": ["int"]},
            {"name": "one_line_summary", "dataType": ["text"],
              "tokenization": "word"},
            {"name": "content", "dataType": ["text"],
              "tokenization": "word"},
            {"name": "mtime", "dataType": ["date"]},
            {"name": "content_hash", "dataType": ["text"],
              "tokenization": "field"},
        ],
    }


def _schema_for(class_name: str, vectorizer: str) -> dict:
    if class_name == "ClaudeDejavuTurn":
        schema = _turn_class_schema_vec_none()
    elif class_name == "ClaudeDejavuDoc":
        schema = _doc_class_schema_vec_none()
    else:
        raise WeaviateRescueError(
            f"unknown class for redo: {class_name!r}")
    schema["vectorizer"] = vectorizer
    return schema


def redo_class(class_name: str, *,
                 dim: Optional[int] = None,
                 vectorizer: str = "none",
                 reset_pg_flags: bool = True,
                 wv_url: Optional[str] = None,
                 dry_run: bool = True,
                 pg_conn=None,
                 db_name: Optional[str] = None) -> dict:
    """Idempotently delete + recreate ``class_name`` at the new config.

    If ``reset_pg_flags=True`` and the class is ClaudeDejavuTurn, also
    resets ``turns.vectorized=FALSE, weaviate_id=NULL`` via per-row
    SAVEPOINT (delegates to :func:`pg_rescue.reset_flags`).

    Returns ``{deleted: bool, recreated: bool, pg_reset: {...}|None,
    mode: ...}``.
    """
    url = _wv_url(wv_url)
    out: dict[str, Any] = {
        "deleted": False,
        "recreated": False,
        "pg_reset": None,
        "mode": "apply" if not dry_run else "dry-run",
        "schema": None,
    }
    _ = dim  # dim is locked by first vector insert; flag is for clarity

    schema = _schema_for(class_name, vectorizer)
    out["schema"] = schema

    if dry_run:
        return out

    # DELETE
    try:
        _http("DELETE", f"{url}/v1/schema/{class_name}", timeout=30)
        out["deleted"] = True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            out["deleted"] = False
        else:
            raise WeaviateRescueError(
                f"DELETE class {class_name} failed: HTTP {e.code} "
                f"{e.reason}") from e

    # CREATE
    try:
        _http("POST", f"{url}/v1/schema",
                 body=json.dumps(schema).encode("utf-8"), timeout=30)
        out["recreated"] = True
    except urllib.error.HTTPError as e:
        raise WeaviateRescueError(
            f"POST /v1/schema failed: HTTP {e.code} {e.reason}") from e

    # Reset PG flags so the next bootstrap reseeds the new dim
    if reset_pg_flags and class_name == "ClaudeDejavuTurn":
        try:
            import pg_rescue  # type: ignore[import]
            out["pg_reset"] = pg_rescue.reset_flags(
                db_name=db_name, dry_run=False, conn=pg_conn)
        except Exception as exc:
            out["pg_reset"] = {"error": str(exc)}

    return out


__all__ = [
    "WeaviateRescueError",
    "SEGMENT_EXTENSIONS",
    "scan", "quarantine_segment", "redo_class",
    "_schema_for", "_turn_class_schema_vec_none",
    "_doc_class_schema_vec_none",
    "_http", "_wv_url",
]
