#!/usr/bin/env python3
"""claude-dejavu — rescue-shard orchestrator (v0.8.2).

Recovers records from a corrupted Weaviate shard using the
`dejavu-rescue` Go binary (see ``code/rescue/``) as the segment reader.
Implements the user's four-phase directive:

  1. **Walk** — call the Go binary on the shard's LSM objects bucket,
     parse the JSONL stream into Python.
  2. **Reset class** — delete the broken Weaviate class via REST and
     re-create it from ``code/weaviate-class.json``.
  3. **Restore** — batch-insert the recovered records (with vectors) via
     ``/v1/batch/objects``.
  4. **Gap-reindex** — diff PG-canonical UUIDs against the restored set,
     and re-embed any missing turns from the dejavu PG ``turns`` table.

The shard data files live inside the Weaviate Docker container's named
volume; we resolve them either via direct host path (when ``WV_DATA_DIR``
is configured) OR via ``docker cp`` into a temp staging directory.

CLI entry: ``claude-dejavu rescue-shard --class <C> [--shard <id>]
[--dry-run] [--yes]`` — wired in ``recall.py``.

See: ``docs/superpowers/specs/2026-05-16-rescue-shard-design.md`` (TBD)
and the v0.8.2 CHANGELOG entry.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
WEAVIATE_CLASS_FILE = PLUGIN_ROOT / "code" / "weaviate-class.json"
RESTORE_BATCH_SIZE = 32
REQUEST_TIMEOUT_S = 120


class RescueError(RuntimeError):
    """Hard failure during a rescue phase."""


# ─── Data root + binary resolution ─────────────────────────────────────


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


def rescue_binary_path() -> Path:
    """Return the installed dejavu-rescue binary path.

    Resolution order:
      1. ``DEJAVU_RESCUE_BIN`` env override (test seam).
      2. ``$DATA_ROOT/bin/dejavu-rescue[.exe]``.
      3. ``dejavu-rescue`` on PATH.
    """
    override = os.environ.get("DEJAVU_RESCUE_BIN")
    if override:
        return Path(override)
    suffix = ".exe" if sys.platform.startswith("win") else ""
    candidate = _resolve_data_root() / "bin" / f"dejavu-rescue{suffix}"
    if candidate.is_file():
        return candidate
    onpath = shutil.which("dejavu-rescue")
    if onpath:
        return Path(onpath)
    raise RescueError(
        "dejavu-rescue binary not installed. Run scripts/install.py "
        "(step [7/9] installs both restic and dejavu-rescue).")


# ─── Shard discovery ───────────────────────────────────────────────────


def _shard_class_dir_name(class_name: str) -> str:
    """Weaviate stores classes in their lowercased name under the
    persistence root."""
    return class_name.lower()


def _list_docker_shards(container: str, class_dir: str) -> list[str]:
    cmd = ["docker", "exec", container, "ls",
           f"/var/lib/weaviate/{class_dir}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return []
    return [s.strip() for s in r.stdout.splitlines() if s.strip()]


def _list_host_shards(host_path: Path) -> list[str]:
    if not host_path.is_dir():
        return []
    return sorted(p.name for p in host_path.iterdir() if p.is_dir())


def discover_shards(class_name: str, *,
                    docker_container: Optional[str] = None,
                    host_data_dir: Optional[Path] = None,
                    ) -> list[str]:
    """List shard IDs for ``class_name``. Prefers host_data_dir when
    set; falls back to ``docker exec ls`` on docker_container."""
    class_dir = _shard_class_dir_name(class_name)
    if host_data_dir is not None:
        return _list_host_shards(host_data_dir / class_dir)
    if docker_container is not None:
        return _list_docker_shards(docker_container, class_dir)
    return []


def stage_shard(class_name: str, shard: str, *,
                docker_container: Optional[str] = None,
                host_data_dir: Optional[Path] = None,
                staging_dir: Optional[Path] = None,
                include_quarantined: Optional[Path] = None,
                ) -> tuple[Path, Optional[Path]]:
    """Stage one shard's LSM objects bucket where the Go binary can
    read it.

    Returns ``(stage_root, quarantined_copy)``. ``stage_root`` is what
    you pass to ``dejavu-rescue scan-shard``; it contains
    ``lsm/objects/*.db``. ``quarantined_copy`` is the path of any extra
    quarantined .db file staged alongside (or None).
    """
    class_dir = _shard_class_dir_name(class_name)

    # Direct host access — fastest path, no copy required.
    if host_data_dir is not None:
        candidate = host_data_dir / class_dir / shard
        if not candidate.is_dir():
            raise RescueError(
                f"shard not found on host: {candidate}")
        return candidate, include_quarantined

    # Docker-volume path — copy out into a temp staging dir.
    if docker_container is None:
        raise RescueError(
            "neither host_data_dir nor docker_container provided")

    if staging_dir is None:
        staging_dir = Path(tempfile.mkdtemp(prefix="dejavu-rescue-stage-"))
    objects_target = staging_dir / "lsm" / "objects"
    objects_target.mkdir(parents=True, exist_ok=True)
    src = (f"{docker_container}:/var/lib/weaviate/"
           f"{class_dir}/{shard}/lsm/objects/.")
    r = subprocess.run(["docker", "cp", src, str(objects_target)],
                        capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RescueError(
            f"docker cp failed for {src}: {r.stderr.strip()}")

    # Optionally stage a quarantined external file too.
    quar_copy: Optional[Path] = None
    if include_quarantined is not None:
        if not Path(include_quarantined).is_file():
            raise RescueError(
                f"quarantined file missing: {include_quarantined}")
        quar_copy = staging_dir / "quarantined.db"
        shutil.copy2(include_quarantined, quar_copy)
    return staging_dir, quar_copy


# ─── Phase 1: walk segments via the Go binary ──────────────────────────


@dataclass
class WalkResult:
    records: list[dict]
    summary: dict
    binary_stderr: str = ""


def walk_shard(stage_root: Path, *,
                  include_quarantined: Optional[Path] = None,
                  binary: Optional[Path] = None,
                  ) -> WalkResult:
    """Run ``dejavu-rescue scan-shard`` and parse the JSONL output.

    Returns a :class:`WalkResult` whose ``summary`` is the per-segment
    stats line the Go binary writes to stderr (a JSON object).
    """
    b = binary or rescue_binary_path()
    args = [str(b), "scan-shard"]
    if include_quarantined is not None:
        args += ["--include-quarantined", str(include_quarantined)]
    args.append(str(stage_root))
    r = subprocess.run(args, capture_output=True, text=True,
                          timeout=600)
    if r.returncode != 0:
        raise RescueError(
            f"dejavu-rescue scan-shard exited {r.returncode}: "
            f"{r.stderr.strip()}")
    records: list[dict] = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise RescueError(
                f"binary emitted unparseable JSONL line: {exc}") from exc
    # Last stderr line is the summary JSON.
    summary: dict = {}
    for line in reversed(r.stderr.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                summary = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    return WalkResult(records=records, summary=summary, binary_stderr=r.stderr)


# ─── Phase 2: drop + recreate class ────────────────────────────────────


def _http(method: str, url: str, *, body: Optional[bytes] = None,
            timeout: float = REQUEST_TIMEOUT_S) -> Optional[dict]:
    """One-shot HTTP request. Returns parsed JSON on 2xx, raises on
    network error, returns None on 404."""
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


def delete_class(wv_url: str, class_name: str) -> bool:
    """DELETE /v1/schema/<C>. Returns True if a deletion happened, False
    if the class was already absent."""
    url = f"{wv_url}/v1/schema/{class_name}"
    try:
        _http("DELETE", url)
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise RescueError(
            f"DELETE {url} failed: HTTP {e.code} {e.reason}") from e


def recreate_class(wv_url: str, class_def_path: Optional[Path] = None,
                       ) -> dict:
    """POST /v1/schema with the class definition from
    ``code/weaviate-class.json`` (the canonical schema dejavu bootstraps
    on fresh install)."""
    p = class_def_path or WEAVIATE_CLASS_FILE
    if not p.is_file():
        raise RescueError(f"class definition file missing: {p}")
    body = p.read_text(encoding="utf-8").encode("utf-8")
    try:
        return _http("POST", f"{wv_url}/v1/schema", body=body) or {}
    except urllib.error.HTTPError as e:
        raise RescueError(
            f"POST /v1/schema failed: HTTP {e.code} {e.reason}") from e


# ─── Phase 3: restore records via batch insert ─────────────────────────


def restore_records(wv_url: str, class_name: str,
                       records: Iterable[dict], *,
                       batch_size: int = RESTORE_BATCH_SIZE,
                       progress: Optional[Callable[[str], None]] = None,
                       ) -> dict:
    """Batch-insert recovered records into the freshly-created class.

    Skips tombstones (caller's decision to surface them in walk output).
    Sends the recovered vector inline so Weaviate does NOT re-vectorize
    via the configured embedder — preserving the bytes already paid for.

    Returns ``{"sent": N, "succeeded": M, "failed": [{"uuid":..., "error":...}, ...]}``.
    """
    out = {"sent": 0, "succeeded": 0, "failed": []}
    batch: list[dict] = []

    def _emit():
        nonlocal batch
        if not batch:
            return
        body = json.dumps({"objects": batch}).encode("utf-8")
        try:
            res = _http("POST", f"{wv_url}/v1/batch/objects", body=body)
        except urllib.error.HTTPError as e:
            err_body = e.read()[:500].decode(errors="replace") if e.fp else ""
            raise RescueError(
                f"batch POST failed: HTTP {e.code} {e.reason}: {err_body}") from e
        out["sent"] += len(batch)
        if isinstance(res, list):
            for i, item in enumerate(res):
                rs = (item or {}).get("result", {}) or {}
                status = rs.get("status")
                errors = rs.get("errors")
                if status == "SUCCESS" or (status is None and not errors):
                    out["succeeded"] += 1
                else:
                    err_msg = "unknown"
                    if errors and isinstance(errors, dict):
                        err_msg = (errors.get("error") or
                                     [{"message": "unknown"}])
                        if isinstance(err_msg, list) and err_msg:
                            err_msg = err_msg[0].get("message", "unknown")
                    out["failed"].append({
                        "uuid": batch[i].get("id"),
                        "error": err_msg,
                    })
        if progress is not None:
            progress(f"  ✓ batch sent {len(batch)} "
                    f"(total succeeded={out['succeeded']})")
        batch = []

    for rec in records:
        if rec.get("tombstone"):
            continue
        if not rec.get("plausible", False):
            continue
        if not rec.get("uuid") or not rec.get("properties"):
            continue
        obj = {
            "class": class_name,
            "id": rec["uuid"],
            "properties": rec["properties"],
        }
        if rec.get("vector"):
            obj["vector"] = rec["vector"]
        batch.append(obj)
        if len(batch) >= batch_size:
            _emit()
    _emit()
    return out


# ─── Phase 4: gap-reindex (PG-canonical re-embed) ──────────────────────


def gap_reindex(restored_uuids: set[str], *,
                   class_name: str,
                   pg_conn=None,
                   ingester_path: Optional[Path] = None,
                   ) -> dict:
    """Find UUIDs in PG ``turns`` not present in the restored set, then
    invoke the existing ingester's reindex path on them.

    Returns ``{"expected_in_pg": N, "missing": [uuid...], "reindexed": M,
                "errors": [...]}``. Currently delegates the reindex itself
    to ``ingester.py --bootstrap`` — the cheapest reuse path.
    """
    out: dict = {"expected_in_pg": 0, "missing": [],
                  "reindexed": 0, "errors": []}
    if class_name != "ClaudeDejavuTurn":
        # PG ``turns`` only mirrors the ClaudeDejavuTurn class. Other
        # classes (Doc, Code) have separate tables not covered here.
        out["errors"].append(
            f"gap_reindex: PG canonical not available for class "
            f"{class_name!r} — skipping")
        return out

    if pg_conn is None:
        try:
            import psycopg2  # type: ignore[import]
        except ImportError:
            out["errors"].append("psycopg2 not available")
            return out
        cfg = _load_cfg()
        _OS_USER = os.environ.get("USER") or "unknown"
        try:
            pg_conn = psycopg2.connect(
                host=cfg.get("PG_HOST", "localhost"),
                port=cfg.get("PG_PORT", "5450"),
                user=cfg.get("PG_USER", "postgres"),
                password=cfg.get("PG_PASS", "postgres"),
                dbname=cfg.get("PG_DB") or f"claude_dejavu_{_OS_USER}",
                connect_timeout=5,
            )
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"pg connect failed: {exc}")
            return out

    # Build canonical UUID set from PG. We use the same uuid5 derivation
    # ingester.weaviate_id_for(session_id, turn_id) uses, where `turn_id`
    # is the parameter name but the actual VALUE passed is `turns.id`
    # (the bigint PK). The PG column is named `id`, not `turn_id`. See
    # ingester.py:215-216 + the upsert at ingester.py:480 that writes
    # `weaviate_id` back keyed on `turns.id`.
    import uuid as _uuid

    def _wv_id(session_id: str, turn_pk_id: int) -> str:
        return str(_uuid.uuid5(_uuid.NAMESPACE_URL,
                                f"claudeturn:{session_id}:{turn_pk_id}"))

    expected: set[str] = set()
    try:
        with pg_conn.cursor() as cur:
            cur.execute("SELECT session_id, id FROM turns")
            for sid, tid in cur.fetchall():
                expected.add(_wv_id(str(sid), int(tid)))
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"pg query failed: {exc}")
        return out

    out["expected_in_pg"] = len(expected)
    missing = sorted(expected - restored_uuids)
    out["missing"] = missing
    if not missing:
        return out

    # Delegate reindex to ingester.py --bootstrap. We could push UUID
    # filtering down into the ingester, but the bootstrap path is
    # idempotent — re-runs of already-restored turns are no-ops at the
    # cursor level, so calling it here just catches up the gap.
    ip = ingester_path or (PLUGIN_ROOT / "code" / "ingester.py")
    try:
        r = subprocess.run(
            [sys.executable, str(ip), "--bootstrap"],
            capture_output=True, text=True, timeout=600,
        )
        if r.returncode != 0:
            out["errors"].append(
                f"ingester bootstrap exit {r.returncode}: "
                f"{r.stderr.strip()[:400]}")
        else:
            out["reindexed"] = len(missing)
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"ingester spawn failed: {exc}")
    return out


# ─── Orchestrator ──────────────────────────────────────────────────────


@dataclass
class RescuePlan:
    class_name: str
    shards: list[str]
    docker_container: Optional[str] = None
    host_data_dir: Optional[Path] = None
    include_quarantined: Optional[Path] = None
    dry_run: bool = True
    weaviate_url: str = "http://localhost:8888"
    walk: dict = field(default_factory=dict)            # per-shard walk results
    reset: dict = field(default_factory=dict)
    restore: dict = field(default_factory=dict)
    gap: dict = field(default_factory=dict)


def rescue_shard(class_name: str, *,
                    shard: Optional[str] = None,
                    weaviate_url: Optional[str] = None,
                    docker_container: str = "claude-dejavu-weaviate",
                    host_data_dir: Optional[Path] = None,
                    include_quarantined: Optional[Path] = None,
                    dry_run: bool = True,
                    progress: Optional[Callable[[str], None]] = None,
                    pg_conn=None,
                    binary: Optional[Path] = None,
                    ) -> RescuePlan:
    """End-to-end rescue orchestrator. With ``dry_run=True`` (the
    default) only Phase 1 (walk) runs and the result describes what
    WOULD happen. Phases 2-4 require ``dry_run=False``.

    Aborts before Phase 3 (restore) if Phase 2 (class re-create) failed.
    """
    log = progress or (lambda msg: print(msg, file=sys.stderr))

    cfg = _load_cfg()
    if weaviate_url is None:
        weaviate_url = cfg.get("WV_URL", "http://localhost:8888")

    shards = ([shard] if shard
              else discover_shards(class_name,
                                       docker_container=docker_container,
                                       host_data_dir=host_data_dir))
    if not shards:
        raise RescueError(
            f"no shards discovered for class {class_name!r} "
            f"(host_data_dir={host_data_dir}, "
            f"docker_container={docker_container})")

    plan = RescuePlan(
        class_name=class_name,
        shards=shards,
        docker_container=docker_container,
        host_data_dir=host_data_dir,
        include_quarantined=include_quarantined,
        dry_run=dry_run,
        weaviate_url=weaviate_url,
    )

    # Phase 1 — walk every shard's segments.
    all_records: list[dict] = []
    for s in shards:
        log(f"  → walking shard {s}")
        stage_root, _ = stage_shard(
            class_name, s,
            docker_container=docker_container,
            host_data_dir=host_data_dir,
            include_quarantined=include_quarantined,
        )
        res = walk_shard(stage_root,
                            include_quarantined=include_quarantined,
                            binary=binary)
        plan.walk[s] = {"summary": res.summary,
                          "record_count": len(res.records)}
        log(f"    recovered {len(res.records)} records "
            f"(plausible={res.summary.get('plausible', '?')}, "
            f"tombstones={res.summary.get('tombstones', '?')})")
        all_records.extend(res.records)

    if dry_run:
        plan.reset = {"skipped": "dry-run"}
        plan.restore = {"skipped": "dry-run",
                          "would_send": sum(
                              1 for r in all_records
                              if r.get("plausible") and not r.get("tombstone"))}
        plan.gap = {"skipped": "dry-run"}
        return plan

    # Phase 2 — drop + recreate the class.
    log(f"  → resetting class {class_name}")
    try:
        deleted = delete_class(weaviate_url, class_name)
        recreated = recreate_class(weaviate_url)
        plan.reset = {"deleted": deleted, "recreated": bool(recreated),
                          "schema": recreated}
    except RescueError as exc:
        plan.reset = {"failed": str(exc)}
        return plan

    # Phase 3 — restore records.
    log(f"  → restoring {len(all_records)} records into {class_name}")
    plan.restore = restore_records(weaviate_url, class_name, all_records,
                                          progress=progress)

    # Phase 4 — gap-reindex missing UUIDs from PG canonical.
    restored_uuids = {r["uuid"] for r in all_records
                        if r.get("plausible") and not r.get("tombstone")
                        and r.get("uuid")}
    log("  → gap-reindex (PG canonical diff)")
    plan.gap = gap_reindex(restored_uuids,
                              class_name=class_name,
                              pg_conn=pg_conn)
    return plan


__all__ = [
    "RescueError", "RescuePlan", "WalkResult",
    "rescue_binary_path", "discover_shards", "stage_shard",
    "walk_shard", "delete_class", "recreate_class",
    "restore_records", "gap_reindex", "rescue_shard",
]
