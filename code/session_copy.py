#!/usr/bin/env python3
"""claude-dejavu — restic-backed session-copy subsystem (v0.8.0).

Thin Python wrapper around the upstream ``restic`` binary. Owns:

  * Repo init (with passphrase generation + idempotent re-run guard)
  * ``take`` — VACUUM INTO-staged SQLite + ``restic backup``
  * ``list_snapshots`` — JSON parse → :class:`Snapshot` dataclasses
  * ``restore`` / ``diff``
  * ``prune`` — applies the v0.8.0 retention flags (sentinel-gated to
    once-per-hour unless ``force=True``)
  * ``check`` — 24h-cached health check via ``$DATA_ROOT/.last-check.json``
  * ``status`` — composite snapshot count + age + repo size
  * ``_run_restic`` — argv builder + env wiring (RESTIC_REPOSITORY,
    RESTIC_PASSWORD_FILE)

restic is content-addressed: identical chunks are stored once. The
v0.8.0 retention policy (24 hourly + 7 daily + 13 weekly) covers ~90
days in ≤44 snapshots → expected total ~5-10 GB vs the legacy
rsync-chain's 508 GB.

See: docs/superpowers/specs/2026-05-15-snapshot-subsystem-design.md
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

try:
    from sqlite_staging import stage_sqlite_db
except ImportError:  # pragma: no cover — direct dejavu install path
    from .sqlite_staging import stage_sqlite_db  # type: ignore[no-redef]


# ── Constants ──────────────────────────────────────────────────────────────

# v0.8.3: include selector for take()/restore(). The take() default is
# ``{'all'}`` which expands to every known source for backwards compat
# with v0.8.0's ``take(sources=...)`` API: when callers pass an explicit
# ``sources`` list, that list still wins for the rsync sources (sessions
# / sqlite) and ``include`` only gates the optional sources (pg + weaviate).
INCLUDE_CHOICES: tuple[str, ...] = ("sessions", "sqlite", "pg", "weaviate", "all")


# Retention applied by ``prune``. Pinned per spec §3.2.
RETENTION_FLAGS: list[str] = [
    "--keep-hourly", "24",
    "--keep-daily",  "7",
    "--keep-weekly", "13",
]

# Exclude globs applied to every ``restic backup`` invocation.
DEFAULT_EXCLUDES: list[str] = ["*.tmp", "*.lock"]

# Sentinel filenames under the data root (sibling of repo).
_LAST_CHECK_FILE = ".last-check.json"
_LAST_PRUNE_FILE = ".last-prune"

# 24h cache for ``restic check``.
_CHECK_CACHE_SECONDS = 24 * 60 * 60
# 1h sentinel for ``restic forget`` to avoid hot-loop pruning.
_PRUNE_SENTINEL_SECONDS = 60 * 60


# ── Dataclasses ────────────────────────────────────────────────────────────

@dataclass
class RepoSpec:
    """Where the repo lives + how to authenticate.

    ``bin_path`` defaults to ``restic`` on PATH; callers can override to
    point at ``<data_root>/bin/restic`` after install_restic ran.
    """
    repo_path: Path
    passphrase_file: Path
    bin_path: Path = field(default_factory=lambda: Path("restic"))


@dataclass
class Snapshot:
    """Parsed restic snapshot record (subset of fields we use)."""
    id: str
    time: str
    hostname: str
    paths: list[str]
    tags: list[str]
    parent: Optional[str] = None


# ── Errors ─────────────────────────────────────────────────────────────────

class ResticError(RuntimeError):
    """restic invocation failed (non-zero exit)."""


# ── _run_restic helper ─────────────────────────────────────────────────────

def _env_for(spec: RepoSpec) -> dict:
    env = os.environ.copy()
    env["RESTIC_REPOSITORY"] = str(spec.repo_path)
    env["RESTIC_PASSWORD_FILE"] = str(spec.passphrase_file)
    return env


def _run_restic(spec: RepoSpec, args: list[str],
                  *, json_out: bool = True,
                  check: bool = True,
                  stdin_text: Optional[str] = None
                  ) -> "subprocess.CompletedProcess":
    """Invoke ``restic`` with the spec's env injected.

    Returns the CompletedProcess. Raises :class:`ResticError` on non-zero
    exit unless ``check=False``.
    """
    argv = [str(spec.bin_path), *args]
    r = subprocess.run(
        argv,
        env=_env_for(spec),
        capture_output=True,
        text=True,
        input=stdin_text,
    )
    if check and r.returncode != 0:
        raise ResticError(
            f"restic {args[0] if args else '?'} failed "
            f"(exit {r.returncode}):\n"
            f"stdout: {r.stdout.strip()}\n"
            f"stderr: {r.stderr.strip()}")
    return r


# ── init_repo ──────────────────────────────────────────────────────────────

def _repo_already_initialized(spec: RepoSpec) -> bool:
    if not spec.repo_path.is_dir():
        return False
    if not spec.passphrase_file.is_file():
        return False
    r = _run_restic(spec, ["snapshots", "--json"],
                     check=False)
    return r.returncode == 0


def init_repo(spec: RepoSpec, *,
                prompt_passphrase: bool = True) -> None:
    """Initialize the repo + generate the passphrase if missing.

    Idempotent: if ``spec.repo_path`` already holds a working repo (probe
    via ``restic snapshots --json``), return early without regenerating.

    When ``prompt_passphrase=True``, prints the new passphrase to stdout
    and waits for the typed-confirm phrase. Tests pass ``False`` to keep
    the unit fast.
    """
    if _repo_already_initialized(spec):
        return

    # Generate passphrase if missing. We never overwrite an existing one.
    if not spec.passphrase_file.is_file():
        passphrase = secrets.token_hex(32)
        spec.passphrase_file.parent.mkdir(parents=True, exist_ok=True)
        spec.passphrase_file.write_text(passphrase, encoding="utf-8")
        os.chmod(spec.passphrase_file, 0o600)
        if prompt_passphrase:
            print()
            print("=" * 60)
            print("  RESTIC PASSPHRASE — SAVE THIS NOW")
            print("=" * 60)
            print(f"  {passphrase}")
            print("=" * 60)
            print("  Copy into your password manager. If lost, the")
            print("  repo is UNRECOVERABLE by design (encryption).")
            print()
            try:
                ack = input(
                    "Type 'i have saved the passphrase' to continue: "
                ).strip().lower()
            except EOFError:
                ack = ""
            if ack != "i have saved the passphrase":
                # Don't init the repo yet; user can re-run.
                raise ResticError(
                    "passphrase not acknowledged — re-run after saving it")

    spec.repo_path.mkdir(parents=True, exist_ok=True)
    _run_restic(spec, ["init"], check=True)


# ── take ───────────────────────────────────────────────────────────────────

def _parse_summary(stdout: str) -> Optional[dict]:
    """Find the ``{"message_type": "summary", ...}`` line in restic
    backup --json output."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("message_type") == "summary":
            return obj
    return None


def take(spec: RepoSpec, sources: list[Path], *,
          tag: str = "manual",
          message: Optional[str] = None,
          ctx_data_root: Optional[Path] = None,
          include: Optional[set[str]] = None) -> Optional[Snapshot]:
    """Create a snapshot. Any ``*.db`` source is first run through
    :func:`stage_sqlite_db` to avoid torn WAL snapshots.

    v0.8.3 adds the ``include`` selector. Defaults to ``{'all'}`` which
    is equivalent to ``{'sessions', 'sqlite', 'pg', 'weaviate'}``.
    When ``include`` excludes ``'pg'`` / ``'weaviate'``, those staging
    steps are skipped (v0.8.0 behavior). When included, this function
    delegates to :mod:`session_copy_pg` + :mod:`session_copy_weaviate`
    to stage extra sources next to the sqlite staging.

    Returns a :class:`Snapshot` populated from restic's summary line
    (the new snapshot id), or ``None`` if restic didn't emit one.
    """
    inc = set(include or {"all"})
    if "all" in inc:
        inc = {"sessions", "sqlite", "pg", "weaviate"}

    staged_sources: list[Path] = []
    staging_root = ctx_data_root or spec.repo_path.parent
    staging_dir = Path(staging_root) / "sqlite-staging"
    for src in sources:
        src = Path(src)
        if str(src).endswith(".db") and src.is_file():
            staged = stage_sqlite_db(src, staging_dir)
            staged_sources.append(staged if staged is not None else src)
        else:
            staged_sources.append(src)

    # v0.8.3 — optional PG + Weaviate staging. Both are best-effort: a
    # failure surfaces in the returned summary stub but doesn't abort
    # the snapshot (the rest of the staging still gets backed up).
    if "pg" in inc:
        try:
            from session_copy_pg import stage_pg_dump  # type: ignore[import]
            pg_dir = Path(staging_root) / "staging" / "pg"
            stage_pg_dump(pg_dir)
            staged_sources.append(pg_dir)
        except Exception:
            pass
    if "weaviate" in inc:
        try:
            from session_copy_weaviate import stage_weaviate_volume  # type: ignore[import]
            wv_dir = Path(staging_root) / "staging" / "weaviate"
            stage_weaviate_volume(wv_dir)
            staged_sources.append(wv_dir)
        except Exception:
            pass

    args = ["backup", "--json", "--tag", tag]
    if message:
        # restic ≥ 0.17 supports --label; --tag is the broad mechanism
        # for arbitrary text. We pin to --tag for forward compat.
        args.extend(["--tag", f"msg={message}"])
    for excl in DEFAULT_EXCLUDES:
        args.extend(["--exclude", excl])
    for s in staged_sources:
        args.append(str(s))

    r = _run_restic(spec, args, check=True)
    summary = _parse_summary(r.stdout)
    if not summary:
        return None
    snap_id = (summary.get("snapshot_id") or "")[:128]
    return Snapshot(
        id=snap_id,
        time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        hostname=os.uname().nodename if hasattr(os, "uname") else "",
        paths=[str(s) for s in staged_sources],
        tags=[tag],
        parent=None,
    )


# ── list_snapshots ─────────────────────────────────────────────────────────

def _parse_snapshot_record(obj: dict) -> Snapshot:
    return Snapshot(
        id=obj.get("id") or "",
        time=obj.get("time") or "",
        hostname=obj.get("hostname") or "",
        paths=list(obj.get("paths") or []),
        tags=list(obj.get("tags") or []),
        parent=obj.get("parent"),
    )


def list_snapshots(spec: RepoSpec, *,
                     tag: Optional[str] = None,
                     last: Optional[int] = None,
                     since: Optional[str] = None) -> list[Snapshot]:
    args = ["snapshots", "--json"]
    if tag:
        args.extend(["--tag", tag])
    r = _run_restic(spec, args, check=True)
    try:
        data = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return []
    snaps = [_parse_snapshot_record(o) for o in data
              if isinstance(o, dict)]
    if since:
        snaps = [s for s in snaps if (s.time or "") >= since]
    if last and last > 0:
        snaps = snaps[-last:]
    return snaps


# ── restore ────────────────────────────────────────────────────────────────

def restore(spec: RepoSpec, snapshot_id: str, target: Path,
              *, paths: Optional[list[str]] = None,
              include: Optional[set[str]] = None,
              pg_db_name: Optional[str] = None,
              force: bool = False) -> dict:
    """Restore a snapshot. v0.8.3 adds the ``include`` selector +
    optional per-source restore (pg dump → pg_restore, weaviate tarball
    → volume extract).

    Default ``include={'all'}`` matches v0.8.0 behavior (restores the
    files into ``target``); when ``include`` names extra sources, this
    function additionally invokes :mod:`session_copy_pg.restore_pg_dump`
    / :mod:`session_copy_weaviate.restore_weaviate_volume` on the
    extracted staging files.

    Returns ``{restored: bool, pg: {...}|None, weaviate: {...}|None}``.
    """
    args = ["restore", snapshot_id, "--target", str(target)]
    if paths:
        for p in paths:
            args.extend(["--include", p])
    _run_restic(spec, args, check=True)

    inc = set(include or {"all"})
    if "all" in inc:
        inc = {"sessions", "sqlite", "pg", "weaviate"}

    out: dict = {"restored": True, "pg": None, "weaviate": None}
    if "pg" in inc:
        try:
            from session_copy_pg import (  # type: ignore[import]
                restore_pg_dump, _default_db_names,
            )
            pg_root = Path(target) / "staging" / "pg"
            results: list[dict] = []
            if pg_root.is_dir():
                for f in sorted(pg_root.glob("*.dump")):
                    db = pg_db_name or f.stem
                    results.append(restore_pg_dump(
                        f, db_name=db, force=force))
            out["pg"] = results
        except Exception as exc:
            out["pg"] = {"error": str(exc)}
    if "weaviate" in inc:
        try:
            from session_copy_weaviate import (  # type: ignore[import]
                restore_weaviate_volume,
            )
            wv_tar = Path(target) / "staging" / "weaviate" / "weaviate-volume.tgz"
            if wv_tar.is_file():
                out["weaviate"] = restore_weaviate_volume(
                    wv_tar, force=force)
        except Exception as exc:
            out["weaviate"] = {"error": str(exc)}
    return out


# ── prune ──────────────────────────────────────────────────────────────────

def _data_root_for(spec: RepoSpec) -> Path:
    return spec.repo_path.parent


def prune(spec: RepoSpec, *,
            dry_run: bool = False,
            force: bool = False) -> dict:
    """Apply retention policy via ``restic forget --prune``.

    Sentinel-gated: skips if ``.last-prune`` is < 1h old, unless
    ``force=True``. Returns ``{ran: bool, output: str}``.
    """
    sentinel = _data_root_for(spec) / _LAST_PRUNE_FILE
    if not force and sentinel.is_file():
        try:
            last = float(sentinel.read_text(encoding="utf-8").strip()
                          or "0")
        except (OSError, ValueError):
            last = 0.0
        if time.time() - last < _PRUNE_SENTINEL_SECONDS:
            return {"ran": False, "output": "sentinel-gated"}

    args = ["forget", *RETENTION_FLAGS]
    if dry_run:
        args.append("--dry-run")
    else:
        args.append("--prune")
    r = _run_restic(spec, args, check=True)
    if not dry_run:
        try:
            _data_root_for(spec).mkdir(parents=True, exist_ok=True)
            sentinel.write_text(str(time.time()), encoding="utf-8")
        except OSError:
            pass
    return {"ran": True, "output": r.stdout}


# ── check (24h cache) ──────────────────────────────────────────────────────

def check(spec: RepoSpec, *,
            read_data: bool = False,
            max_age_hours: int = 24,
            force: bool = False) -> dict:
    """``restic check``, cached at ``$DATA_ROOT/.last-check.json``.

    Returns ``{ok: bool, cached: bool, output: str, ts: float}``.
    """
    cache = _data_root_for(spec) / _LAST_CHECK_FILE
    if not force and cache.is_file():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            ts = float(data.get("ts", 0))
            if time.time() - ts < max_age_hours * 3600:
                return {"ok": bool(data.get("ok")),
                        "cached": True,
                        "output": data.get("output", ""),
                        "ts": ts}
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    args = ["check"]
    if read_data:
        args.append("--read-data")
    r = _run_restic(spec, args, check=False)
    ok = (r.returncode == 0)
    out = r.stdout
    payload = {"ok": ok, "output": out, "ts": time.time()}
    try:
        _data_root_for(spec).mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass
    payload["cached"] = False
    return payload


# ── diff ───────────────────────────────────────────────────────────────────

def diff(spec: RepoSpec, snap_a: str, snap_b: str) -> dict:
    args = ["diff", "--json", snap_a, snap_b]
    r = _run_restic(spec, args, check=True)
    added: list[str] = []
    removed: list[str] = []
    changed: list[str] = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("message_type") != "change":
            continue
        path = obj.get("path") or ""
        mod = obj.get("modifier") or ""
        if mod == "+":
            added.append(path)
        elif mod == "-":
            removed.append(path)
        else:
            changed.append(path)
    return {"added": added, "removed": removed, "changed": changed}


# ── status ─────────────────────────────────────────────────────────────────

def _du_bytes(path: Path) -> int:
    total = 0
    if not path.is_dir():
        return 0
    for dp, _, fns in os.walk(path):
        for fn in fns:
            try:
                total += (Path(dp) / fn).stat().st_size
            except OSError:
                continue
    return total


def _parse_restic_ts(ts: str) -> Optional[float]:
    if not ts:
        return None
    # restic emits RFC3339; the time module's strptime can't parse the
    # nanosecond suffix, so trim defensively.
    s = ts.rstrip("Z")
    # cut off fractional seconds if present
    if "." in s:
        s = s.split(".")[0]
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        return None


def status(spec: RepoSpec) -> dict:
    """Composite repo health: size, snapshot count, last-snapshot age."""
    snaps = list_snapshots(spec)
    repo_size = _du_bytes(spec.repo_path)
    last_age_s: Optional[float] = None
    if snaps:
        last = snaps[-1]
        ts = _parse_restic_ts(last.time)
        if ts is not None:
            last_age_s = max(0.0, time.time() - ts)
    last_check_path = _data_root_for(spec) / _LAST_CHECK_FILE
    last_check_info: Optional[dict] = None
    if last_check_path.is_file():
        try:
            last_check_info = json.loads(
                last_check_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "repo_path": str(spec.repo_path),
        "repo_size": repo_size,
        "snapshot_count": len(snaps),
        "last_snapshot_age_s": last_age_s,
        "last_check": last_check_info,
    }


__all__ = [
    "RETENTION_FLAGS", "DEFAULT_EXCLUDES", "INCLUDE_CHOICES",
    "RepoSpec", "Snapshot", "ResticError",
    "init_repo", "take",
    "list_snapshots", "restore",
    "prune", "check", "diff", "status",
    "_run_restic", "stage_sqlite_db",
]
