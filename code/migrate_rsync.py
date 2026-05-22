#!/usr/bin/env python3
"""claude-dejavu — v0.5/0.6/0.7 → v0.8.0 migration orchestrator.

The 10-step ``migrate-from-rsync`` workflow specified in
``docs/superpowers/specs/2026-05-15-snapshot-subsystem-design.md`` §3.6.

Layout — one function per spec step plus a top-level :func:`run`
dispatcher with phase-marker resume:

  * ``detect_legacy_state`` — §3.10.1 trigger detection.
  * ``at_risk_inventory`` — §3.6 Step 2 query (UUID regex, ::text cast,
    zero-turns gate).
  * ``preserve_at_risk`` — §3.6 Step 3 off-tree archive + sha256 + the
    one-shot programmatic ingester call. Idempotent on re-run.
  * ``step_1_stop_legacy`` … ``step_9b_enable_scheduler`` — each step
    raises on failure with the rollback contract documented inline.
  * ``run`` — reads ``$DATA_ROOT/migration-state.json``, dispatches to
    the next step, advances the marker on success.

All destructive primitives are imported from ``session_copy`` and
``scheduling`` (Pass A); tests monkey-patch those.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional


# Late-bound imports: callers (CLI, install.py) inject the dejavu
# venv's ``code/`` on sys.path before importing this module. Both the
# package and flat-layout cases need to work.
try:
    import session_copy as _sc  # type: ignore[no-redef]
    import scheduling as _sch  # type: ignore[no-redef]
except ImportError:  # pragma: no cover — direct dejavu install path
    from . import session_copy as _sc  # type: ignore[no-redef]
    from . import scheduling as _sch  # type: ignore[no-redef]


# ── Constants ──────────────────────────────────────────────────────────────

PHASES: list[str] = [
    "unstarted",
    "legacy_stopped",
    "preserved",
    "repo_inited",
    "initial_taken",
    "verified",
    "new_scheduler_installed_disabled",
    "renamed",
    "done",
]


UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$")


# Overridable test seam — at_risk_inventory walks this path when set,
# letting tests assert that preserve_at_risk does NOT touch the live
# projects tree.
_LIVE_ROOT_OVERRIDE: Optional[Path] = None


# ── Dataclasses ────────────────────────────────────────────────────────────

@dataclass
class MigrationState:
    phase: str = "unstarted"
    started_at: float = 0.0
    preserved_count: int = 0
    preserved_manifest_path: Optional[str] = None
    legacy_units_backed_up_to: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_path(cls, p: Path) -> "MigrationState":
        if not p.is_file():
            return cls()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        st = cls()
        st.phase = data.get("phase", "unstarted")
        st.started_at = float(data.get("started_at", 0))
        st.preserved_count = int(data.get("preserved_count", 0))
        st.preserved_manifest_path = data.get("preserved_manifest_path")
        st.legacy_units_backed_up_to = data.get("legacy_units_backed_up_to")
        return st


@dataclass
class AtRiskSession:
    session_id: str
    project_encoded: str
    source_path: Path
    size: int
    sha256: str


@dataclass
class Manifest:
    path: Optional[Path] = None
    entries: list[dict] = field(default_factory=list)


# ── State helpers ──────────────────────────────────────────────────────────

def _state_path(data_root: Path) -> Path:
    return Path(data_root) / "migration-state.json"


def _write_state(data_root: Path, state: MigrationState) -> None:
    p = _state_path(data_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(state.to_json(), encoding="utf-8")


def _delete_state(data_root: Path) -> None:
    try:
        _state_path(data_root).unlink()
    except FileNotFoundError:
        pass


# ── Step 0: detection (§3.10.1) ────────────────────────────────────────────

def detect_legacy_state(home: Path, data_root: Path,
                          conn: Any = None) -> bool:
    """Return True if any v0.5/v0.6/v0.7 legacy signal is present.

    Signals (any-of):
      * ``~/.claude-backups/snapshots/`` exists with a ``20*`` subdir
      * ``~/.claude-backups/scripts/snapshot.sh`` exists
      * (Linux only) ``claude-backup.timer`` enabled
      * (macOS only) ``~/Library/LaunchAgents/*claude-backup*.plist``
      * (Windows only) ``ClaudeDejavu_Snapshot`` scheduled task exists
    """
    home = Path(home)
    snaps = home / ".claude-backups" / "snapshots"
    if snaps.is_dir():
        try:
            for child in snaps.iterdir():
                if child.is_dir() and child.name.startswith("20"):
                    return True
        except OSError:
            pass
    script = home / ".claude-backups" / "scripts" / "snapshot.sh"
    if script.is_file():
        return True
    sysname = (platform.system() or "").lower()
    if sysname.startswith("linux"):
        try:
            r = subprocess.run(
                ["systemctl", "--user", "is-enabled", "claude-backup.timer"],
                check=False, capture_output=True, text=True, timeout=5)
            if (r.stdout or "").strip() == "enabled":
                return True
        except (OSError, subprocess.SubprocessError):
            pass
    elif sysname.startswith("darwin"):
        agents = home / "Library" / "LaunchAgents"
        if agents.is_dir():
            for f in agents.glob("*claude-backup*.plist"):
                if f.is_file():
                    return True
    elif sysname.startswith("windows"):
        try:
            r = subprocess.run(
                ["schtasks", "/Query", "/TN", "ClaudeDejavu_Snapshot"],
                check=False, capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
    return False


# ── Step 2: at-risk inventory (§3.6) ───────────────────────────────────────

def _oldest_snapshot_dir(rsync_root: Path) -> Optional[Path]:
    if not rsync_root.is_dir():
        return None
    candidates: list[Path] = []
    try:
        for child in rsync_root.iterdir():
            if child.is_dir() and child.name.startswith("20"):
                candidates.append(child)
    except OSError:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.name)
    return candidates[0]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def at_risk_inventory(rsync_root: Path, live_root: Path,
                        conn: Any) -> list[AtRiskSession]:
    """Compute the at-risk set per spec §3.6 Step 2.

    Walk the OLDEST rsync snapshot for canonical-UUID JSONLs; subtract
    those reachable from the live filesystem; subtract those reachable
    from PG with a non-zero ``turns`` count. The remainder is at-risk.

    The SQL string is built explicitly with a ``::text`` cast on
    ``sessions.id`` and a ``count(*) > 0`` filter on ``turns`` (the
    zero-turns gate — sessions with rows but 0 turns are not properly
    ingested, so the rsync copy is the authoritative record).
    """
    oldest = _oldest_snapshot_dir(rsync_root)
    rsync_uuids: dict[str, tuple[Path, str]] = {}
    if oldest is not None:
        # Walk *.jsonl under .claude/projects/<encoded>/
        for jsonl in oldest.rglob("*.jsonl"):
            stem = jsonl.stem
            if not UUID_RE.fullmatch(stem):
                continue
            project_enc = jsonl.parent.name
            rsync_uuids[stem] = (jsonl, project_enc)
    # Live tree subtraction
    live_uuids: set[str] = set()
    if live_root is not None and Path(live_root).is_dir():
        for jsonl in Path(live_root).rglob("*.jsonl"):
            stem = jsonl.stem
            if UUID_RE.fullmatch(stem):
                live_uuids.add(stem)
    # PG subtraction with explicit ::text cast + zero-turns gate
    pg_safe: set[str] = set()
    pending = list(rsync_uuids.keys() - live_uuids)
    if conn is not None and pending:
        sql = (
            "SELECT s.id::text "
            "FROM sessions s "
            "WHERE s.id::text = ANY(%s) "
            "  AND (SELECT count(*) FROM turns t "
            "       WHERE t.session_id = s.id) > 0"
        )
        try:
            with conn.cursor() as cur:
                cur.execute(sql, (pending,))
                pg_safe = {row[0] for row in cur.fetchall()}
        except Exception:  # pragma: no cover — defensive
            pg_safe = set()
    at_risk_ids = set(rsync_uuids.keys()) - live_uuids - pg_safe
    out: list[AtRiskSession] = []
    for uuid_ in sorted(at_risk_ids):
        src, proj = rsync_uuids[uuid_]
        try:
            sz = src.stat().st_size
            sha = _sha256_file(src)
        except OSError:
            continue
        out.append(AtRiskSession(
            session_id=uuid_, project_encoded=proj,
            source_path=src, size=sz, sha256=sha))
    return out


# ── Step 3: preserve at-risk (§3.6) ────────────────────────────────────────

def _call_ingester(jsonl_path: str, session_uuid: str,
                     project_encoded: Optional[str], conn: Any) -> int:
    """Indirection seam for tests + lazy import — keep the real
    ``ingester.ingest_jsonl`` import inside this function so the
    migration module loads even when ingester deps are missing.
    """
    try:
        import ingester  # type: ignore[no-redef]
    except ImportError:  # pragma: no cover
        return 0
    fn = getattr(ingester, "ingest_jsonl", None)
    if fn is None:
        return 0
    try:
        return int(fn(jsonl_path, session_uuid,
                       project_encoded, conn) or 0)
    except Exception:
        return 0


def _verify_sha256(path: Path, expected: str) -> bool:
    try:
        return _sha256_file(path) == expected
    except OSError:
        return False


def preserve_at_risk(items: list[AtRiskSession], archive_dir: Path,
                       conn: Any) -> Manifest:
    """Spec §3.6 Step 3.

    * Copy each at-risk JSONL to
      ``<archive_dir>/<project_encoded>/<uuid>.jsonl`` (mode 0o600).
    * Verify sha256 on both source and destination.
    * Programmatically ingest the off-tree copy into PG via
      :func:`_call_ingester` (never touches ``~/.claude/projects/``).
    * Append to ``manifest-<ts>.json`` at the archive root; merges on
      re-run (idempotent — entries with matching sha256 are skipped).
    """
    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    # Load existing manifest entries (resume case)
    existing: dict[str, dict] = {}
    existing_manifest_path: Optional[Path] = None
    for cand in sorted(archive_dir.glob("manifest-*.json")):
        try:
            data = json.loads(cand.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        existing_manifest_path = cand
        for e in data.get("entries", []):
            sid = e.get("session_id")
            if sid:
                existing[sid] = e

    merged: dict[str, dict] = dict(existing)
    for item in items:
        proj_dir = archive_dir / item.project_encoded
        proj_dir.mkdir(parents=True, exist_ok=True)
        dst = proj_dir / f"{item.session_id}.jsonl"
        # Resume-skip: matching sha256 on disk + manifest entry
        if (dst.is_file()
              and item.session_id in existing
              and existing[item.session_id].get("sha256") == item.sha256
              and _verify_sha256(dst, item.sha256)):
            continue
        # Copy + verify both ends
        if not _verify_sha256(item.source_path, item.sha256):
            # Source mutated since the inventory pass — record as failed
            merged[item.session_id] = {
                "session_id": item.session_id,
                "project_encoded": item.project_encoded,
                "size": item.size,
                "sha256": item.sha256,
                "source_path": str(item.source_path),
                "preserved_at": time.time(),
                "ingested_into_pg": False,
                "error": "source-sha-changed-since-inventory",
            }
            continue
        try:
            shutil.copy2(item.source_path, dst)
            try:
                os.chmod(dst, 0o600)
            except OSError:
                pass
        except OSError as exc:
            merged[item.session_id] = {
                "session_id": item.session_id,
                "project_encoded": item.project_encoded,
                "size": item.size,
                "sha256": item.sha256,
                "source_path": str(item.source_path),
                "preserved_at": time.time(),
                "ingested_into_pg": False,
                "error": f"copy-failed: {exc}",
            }
            continue
        if not _verify_sha256(dst, item.sha256):
            merged[item.session_id] = {
                "session_id": item.session_id,
                "project_encoded": item.project_encoded,
                "size": item.size,
                "sha256": item.sha256,
                "source_path": str(item.source_path),
                "preserved_at": time.time(),
                "ingested_into_pg": False,
                "error": "post-copy-sha-mismatch",
            }
            continue
        ingested = _call_ingester(str(dst), item.session_id,
                                     item.project_encoded, conn)
        merged[item.session_id] = {
            "session_id": item.session_id,
            "project_encoded": item.project_encoded,
            "size": item.size,
            "sha256": item.sha256,
            "source_path": str(item.source_path),
            "destination_path": str(dst),
            "preserved_at": time.time(),
            "ingested_into_pg": bool(ingested),
            "ingester_returned": ingested,
        }
    # Write a fresh manifest file (we don't mutate the older one — keeps
    # forensics intact). Re-uses the earliest manifest path on second
    # run if one already exists to keep things tidy.
    ts = int(time.time())
    out_path = existing_manifest_path or (archive_dir / f"manifest-{ts}.json")
    payload = {
        "schema": "claude-dejavu-migration-manifest-v1",
        "written_at": ts,
        "entries": list(merged.values()),
    }
    out_path.write_text(json.dumps(payload, indent=2),
                         encoding="utf-8")
    return Manifest(path=out_path, entries=list(merged.values()))


# ── Rollback helpers ───────────────────────────────────────────────────────

def _re_enable_legacy_timer(home: Path) -> None:
    """Best-effort re-enable of ``claude-backup.timer`` for rollback
    paths. Never raises — used inside except-blocks."""
    sysname = (platform.system() or "").lower()
    try:
        if sysname.startswith("linux"):
            subprocess.run(
                ["systemctl", "--user", "enable", "--now",
                 "claude-backup.timer"],
                check=False, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def _rollback_to_legacy(state: MigrationState, data_root: Path,
                          home: Path) -> None:
    """Full rollback used when a downstream step fails irrecoverably:

      * Re-enable legacy timer.
      * Delete a freshly-written repo + passphrase (if any).
      * Leave preserved-sessions archive intact (it's a real off-tree
        copy and remains valid regardless of restic state).
    """
    _re_enable_legacy_timer(home)
    try:
        shutil.rmtree(Path(data_root) / "restic-repo")
    except (FileNotFoundError, OSError):
        pass
    try:
        (Path(data_root) / ".restic-pass").unlink()
    except FileNotFoundError:
        pass


# ── Per-step functions ─────────────────────────────────────────────────────

def step_1_stop_legacy(*, data_root: Path, home: Path) -> None:
    """Pause + disable the legacy ``claude-backup.timer`` + back up
    the unit files to ``$DATA_ROOT/legacy-backup-units/<ts>/``."""
    sysname = (platform.system() or "").lower()
    if sysname.startswith("linux"):
        for cmd in (
            ["systemctl", "--user", "stop", "claude-backup.timer"],
            ["systemctl", "--user", "stop", "claude-backup.service"],
            ["systemctl", "--user", "disable", "claude-backup.timer"],
        ):
            try:
                subprocess.run(cmd, check=False,
                                 capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
        # Poll up to 60s for service to exit.
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                r = subprocess.run(
                    ["systemctl", "--user", "is-active",
                     "claude-backup.service"],
                    check=False, capture_output=True, text=True,
                    timeout=5)
                if (r.stdout or "").strip() not in ("active",
                                                       "activating"):
                    break
            except (OSError, subprocess.SubprocessError):
                break
            time.sleep(2)
    # Back up unit files for forensics + rollback
    ts = int(time.time())
    backup_root = Path(data_root) / "legacy-backup-units" / str(ts)
    backup_root.mkdir(parents=True, exist_ok=True)
    cfg = (Path(os.environ.get("XDG_CONFIG_HOME") or
                  (home / ".config")) / "systemd" / "user")
    for unit in ("claude-backup.timer", "claude-backup.service"):
        src = cfg / unit
        if src.is_file():
            try:
                shutil.copy2(src, backup_root / unit)
            except OSError:
                pass


def step_2_inventory(*, data_root: Path, home: Path,
                      conn: Any = None) -> list[AtRiskSession]:
    """Compute the at-risk inventory; never destructive — pure read."""
    rsync_root = Path(home) / ".claude-backups" / "snapshots"
    live_root = _LIVE_ROOT_OVERRIDE or (Path(home) / ".claude" / "projects")
    return at_risk_inventory(rsync_root, live_root, conn)


def step_3_preserve(items: list[AtRiskSession], *,
                      data_root: Path, home: Path,
                      conn: Any = None) -> Manifest:
    archive = Path(data_root) / "preserved-sessions"
    return preserve_at_risk(items, archive, conn)


def step_5_init_repo(*, data_root: Path, home: Path) -> None:
    """Initialize the restic repo. Rollback contract: any failure
    deletes the freshly-written passphrase + raises so :func:`run`
    leaves phase at ``preserved``."""
    spec = _sc.RepoSpec(
        repo_path=Path(data_root) / "restic-repo",
        passphrase_file=Path(data_root) / ".restic-pass",
        bin_path=Path(_resolve_restic_bin(data_root)),
    )
    try:
        _sc.init_repo(spec, prompt_passphrase=False)
    except Exception:
        # Rollback: delete the freshly-written passphrase so a re-run
        # can start over cleanly. The half-init'd repo dir (if any) is
        # also removed — restic refuses to re-init into a non-empty dir.
        try:
            spec.passphrase_file.unlink()
        except FileNotFoundError:
            pass
        try:
            shutil.rmtree(spec.repo_path)
        except (FileNotFoundError, OSError):
            pass
        raise


def step_6_initial_snapshot(*, data_root: Path, home: Path) -> None:
    spec = _sc.RepoSpec(
        repo_path=Path(data_root) / "restic-repo",
        passphrase_file=Path(data_root) / ".restic-pass",
        bin_path=Path(_resolve_restic_bin(data_root)),
    )
    sources: list[Path] = []
    proj = Path(home) / ".claude" / "projects"
    if proj.is_dir():
        sources.append(proj)
    mem_db = Path(home) / ".claude-mem" / "claude-mem.db"
    if mem_db.is_file():
        sources.append(mem_db)
    chat = Path(home) / ".claude-backups" / "chat-logs"
    if chat.is_dir():
        sources.append(chat)
    if not sources:
        return
    _sc.take(spec, sources, tag="migration-initial",
              ctx_data_root=Path(data_root))


def step_7_verify(*, data_root: Path, home: Path) -> None:
    """``restic check`` against the freshly-taken initial snapshot.

    Rollback: if check fails, re-enable legacy timer + clean up the
    fresh restic state + raise so :func:`run` resets phase to
    ``legacy_stopped`` (preserved sessions remain valid).
    """
    spec = _sc.RepoSpec(
        repo_path=Path(data_root) / "restic-repo",
        passphrase_file=Path(data_root) / ".restic-pass",
        bin_path=Path(_resolve_restic_bin(data_root)),
    )
    res = _sc.check(spec, force=True)
    if not res.get("ok"):
        _re_enable_legacy_timer(home)
        try:
            shutil.rmtree(spec.repo_path)
        except (FileNotFoundError, OSError):
            pass
        try:
            spec.passphrase_file.unlink()
        except FileNotFoundError:
            pass
        raise RuntimeError(
            f"step 7 verify failed: {res.get('output', '')[:200]}")


def step_8_install_scheduler_disabled(*, data_root: Path,
                                          home: Path) -> None:
    """Write the platform scheduler files but DO NOT enable. On any
    write failure, call disable_scheduler() to undo partial writes and
    re-raise so the orchestrator leaves phase at ``verified``."""
    bin_path = Path(_resolve_restic_bin(data_root)).parent / "claude-dejavu"
    try:
        _sch.install_scheduler(bin_path, cadence_min=5)
    except Exception:
        # Best-effort cleanup of half-written files.
        try:
            _sch.disable_scheduler()
        except Exception:
            pass
        raise


def step_9_rename_old_tree(*, data_root: Path, home: Path) -> None:
    """Rename ``~/.claude-backups/snapshots`` → ``.deleteme.<ts>``.

    Refuses cross-filesystem rename per reviewer N4 — both src and the
    parent of dst must share ``os.statvfs.f_fsid``.
    """
    src = Path(home) / ".claude-backups" / "snapshots"
    if not src.is_dir():
        return
    ts = int(time.time())
    dst = src.parent / f"snapshots.deleteme.{ts}"
    statvfs = getattr(os, "statvfs", None)
    if statvfs is not None:
        try:
            src_fsid = statvfs(str(src)).f_fsid
            parent_fsid = statvfs(str(src.parent)).f_fsid
        except OSError:
            src_fsid = parent_fsid = None
        if (src_fsid is not None and parent_fsid is not None
              and src_fsid != parent_fsid):
            raise RuntimeError(
                "step 9 refusing rename: src and dst parent live on "
                "different filesystems (cross-filesystem rename is "
                "non-atomic). Resolve by moving snapshots onto the "
                "same fs, or migrate manually.")
    os.replace(src, dst)


def step_9b_enable_scheduler(*, data_root: Path, home: Path) -> None:
    """Enable the new scheduler. If enable fails, fall back to
    re-enabling the legacy timer so the user is not snapshot-less."""
    try:
        _sch.enable_scheduler()
    except Exception:
        _re_enable_legacy_timer(home)
        raise


# ── Reclaim legacy renamed tree (v0.8.3 post-release polish) ────────────────

# step_9 renames ~/.claude-backups/snapshots → snapshots.deleteme.<ts>; once
# the user has verified the new restic snapshot is good, the renamed tree
# can be deleted to reclaim disk. We do NOT auto-delete during migration —
# the legacy tree is the only off-tree copy of the old chain until a
# successful restic check, and silent deletion would be a footgun on
# partial-migration retries. This function is invoked from the
# `claude-dejavu session-copy reclaim-legacy` CLI handler.

_RECLAIM_RE = re.compile(r"^snapshots\.deleteme\.\d+$")


def _du_bytes_tree(path: Path) -> int:
    """Cheap recursive byte-size. Mirrors session_copy._du_bytes but
    kept local so migrate_rsync.py stays import-clean from session_copy
    callers in the reclaim path."""
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


def _list_legacy_renamed_trees(home: Path) -> list[Path]:
    """Return all ``~/.claude-backups/snapshots.deleteme.<digits>`` dirs
    in deterministic order. Anchored regex; no glob fall-through into
    unrelated paths."""
    backups = Path(home) / ".claude-backups"
    if not backups.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(backups.iterdir()):
        if not child.is_dir():
            continue
        if _RECLAIM_RE.match(child.name):
            out.append(child)
    return out


def reclaim_legacy_trees(*, home: Optional[Path] = None,
                            yes: bool = False,
                            dry_run: bool = False) -> int:
    """Delete every ``snapshots.deleteme.<ts>`` directory under
    ``~/.claude-backups/``.

    Behavior matrix:

      * ``dry_run=True``                → print plan + sizes, exit 0.
      * ``yes=False`` (default)         → print plan + require typed
                                            confirmation. TTY-guarded
                                            (refuses if stdin/stdout are
                                            not a real terminal — same
                                            posture as other destructive
                                            scripts in this repo).
      * ``yes=True``                    → delete each in turn, progress
                                            line per directory.

    Returns 0 on success or no-op, non-zero on user-abort / TTY-refused
    / partial deletion. Refusal to run with ``--yes`` outside a TTY
    catches ``echo y | claude-dejavu ... reclaim-legacy --yes`` and
    similar accidents; per the destructive-script feedback rule, a
    ``read -p`` is not a real gate when stdin is closed.
    """
    home = Path(home) if home is not None else Path.home()
    trees = _list_legacy_renamed_trees(home)
    if not trees:
        print("  nothing to reclaim "
               f"(no snapshots.deleteme.* under {home / '.claude-backups'})")
        return 0

    sizes = [_du_bytes_tree(t) for t in trees]
    total = sum(sizes)

    def _mb(n: int) -> str:
        return f"{n / (1024 * 1024):.1f} MB"

    print()
    print("─" * 60)
    print("  reclaim-legacy — plan")
    print("─" * 60)
    for t, sz in zip(trees, sizes):
        print(f"  {t}  ({_mb(sz)})")
    print(f"  total reclaimable: {_mb(total)}")
    print("─" * 60)

    if dry_run:
        print("  (--dry-run — nothing deleted)")
        return 0

    if not yes:
        # TTY-guard: refuse to proceed if stdin or stdout is not a real
        # terminal. Destructive ops require an actual human to type the
        # phrase; piping `yes` should NOT advance.
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print("  ✗ refusing typed-confirm outside a TTY",
                   file=sys.stderr)
            print("    re-run with --yes (or in an interactive shell)",
                   file=sys.stderr)
            return 2
        phrase = "reclaim legacy snapshots"
        try:
            reply = input(f"  Type '{phrase}' to delete: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  aborted")
            return 130
        if reply != phrase:
            print("  ✗ phrase did not match — nothing deleted")
            return 1

    failures: list[tuple[Path, str]] = []
    for i, t in enumerate(trees, start=1):
        print(f"  [{i}/{len(trees)}] deleting {t} ...", flush=True)
        try:
            shutil.rmtree(t)
        except OSError as exc:
            failures.append((t, str(exc)))
            print(f"      ✗ {exc}", file=sys.stderr)
            continue
        print(f"      ✓ removed")

    if failures:
        print(f"  partial reclaim: {len(failures)}/{len(trees)} failed",
               file=sys.stderr)
        return 1
    print(f"  ✓ reclaimed {len(trees)} legacy tree(s) "
           f"({_mb(total)})")
    return 0


# ── Resolver helpers ───────────────────────────────────────────────────────

def _resolve_restic_bin(data_root: Path) -> str:
    """Prefer the dejavu-managed binary at ``<data_root>/bin/restic``
    over the PATH lookup. Falls back to ``restic`` so install.py's
    bin_path argument plumbing is the source of truth in production."""
    cand = Path(data_root) / "bin" / "restic"
    if cand.is_file():
        return str(cand)
    cand_exe = Path(data_root) / "bin" / "restic.exe"
    if cand_exe.is_file():
        return str(cand_exe)
    return "restic"


# ── Orchestrator ───────────────────────────────────────────────────────────

def _print_dry_run_plan(state: MigrationState, items_preview: int = 0) -> None:
    print()
    print("─" * 60)
    print("  migrate-from-rsync — DRY RUN (nothing will be modified)")
    print("─" * 60)
    print(f"  current phase: {state.phase}")
    print(f"  ~{items_preview} at-risk session(s) would be preserved")
    print("  steps (cold mode):")
    for i, p in enumerate(PHASES[1:], start=1):
        print(f"    {i}. transition → {p}")
    print()


def run(mode: str = "cold", yes: bool = False, *,
          data_root: Optional[Path] = None,
          home: Optional[Path] = None,
          conn: Any = None) -> int:
    """Orchestrate the migration. Dispatches based on phase marker.

    Returns 0 on success (or no-op), non-zero on failure.
    """
    data_root = Path(data_root) if data_root is not None \
                  else Path.home() / ".local" / "share" / "claude-dejavu"
    home = Path(home) if home is not None else Path.home()

    if mode == "dry-run":
        st = MigrationState.from_path(_state_path(data_root))
        _print_dry_run_plan(st)
        return 0

    state = MigrationState.from_path(_state_path(data_root))
    if state.phase == "unstarted":
        state.started_at = time.time()
        _write_state(data_root, state)

    # Step 1 — stop legacy
    if state.phase == "unstarted":
        try:
            step_1_stop_legacy(data_root=data_root, home=home)
            state.phase = "legacy_stopped"
            _write_state(data_root, state)
        except Exception as exc:
            print(f"step 1 failed: {exc}", file=sys.stderr)
            return 1

    # Step 2+3 — inventory + preserve
    if state.phase == "legacy_stopped":
        try:
            items = step_2_inventory(data_root=data_root, home=home,
                                       conn=conn)
            manifest = step_3_preserve(items, data_root=data_root,
                                          home=home, conn=conn)
            if manifest is None:
                # Tests may mock step_3 to a no-op; treat as empty.
                state.preserved_count = 0
                state.preserved_manifest_path = None
            else:
                state.preserved_count = len(manifest.entries)
                state.preserved_manifest_path = (
                    str(manifest.path) if manifest.path else None)
            state.phase = "preserved"
            _write_state(data_root, state)
        except Exception as exc:
            print(f"step 2/3 failed: {exc}", file=sys.stderr)
            return 1

    # Step 4 — prompt (skipped on --yes)
    if state.phase == "preserved" and not yes:
        try:
            print()
            print(f"  preserved: {state.preserved_count} at-risk "
                   "session(s)")
            print("  Press Enter to start, Ctrl-C to abort.")
            try:
                input("")
            except EOFError:
                pass
        except KeyboardInterrupt:
            print("aborted by user")
            return 130

    # Step 5 — init repo
    if state.phase == "preserved":
        try:
            step_5_init_repo(data_root=data_root, home=home)
            state.phase = "repo_inited"
            _write_state(data_root, state)
        except Exception as exc:
            print(f"step 5 failed: {exc}", file=sys.stderr)
            return 1

    # Step 6 — initial snapshot
    if state.phase == "repo_inited":
        try:
            step_6_initial_snapshot(data_root=data_root, home=home)
            state.phase = "initial_taken"
            _write_state(data_root, state)
        except Exception as exc:
            print(f"step 6 failed: {exc}", file=sys.stderr)
            return 1

    # Step 7 — verify
    if state.phase == "initial_taken":
        try:
            step_7_verify(data_root=data_root, home=home)
            state.phase = "verified"
            _write_state(data_root, state)
        except Exception as exc:
            # Step 7 rolls back internally and resets to legacy_stopped.
            state.phase = "legacy_stopped"
            _write_state(data_root, state)
            print(f"step 7 failed (rolled back): {exc}", file=sys.stderr)
            return 1

    # Step 8 — install scheduler (disabled)
    if state.phase == "verified":
        try:
            step_8_install_scheduler_disabled(data_root=data_root,
                                                  home=home)
            state.phase = "new_scheduler_installed_disabled"
            _write_state(data_root, state)
        except Exception as exc:
            print(f"step 8 failed: {exc}", file=sys.stderr)
            return 1

    # Step 9 — rename old tree
    if state.phase == "new_scheduler_installed_disabled":
        try:
            step_9_rename_old_tree(data_root=data_root, home=home)
            state.phase = "renamed"
            _write_state(data_root, state)
        except Exception as exc:
            print(f"step 9 failed: {exc}", file=sys.stderr)
            return 1

    # Step 9b — enable scheduler
    if state.phase == "renamed":
        try:
            step_9b_enable_scheduler(data_root=data_root, home=home)
            state.phase = "done"
            _delete_state(data_root)
        except Exception as exc:
            print(f"step 9b failed (legacy re-enabled): {exc}",
                  file=sys.stderr)
            return 1

    return 0


__all__ = [
    "PHASES",
    "MigrationState", "AtRiskSession", "Manifest",
    "detect_legacy_state", "at_risk_inventory", "preserve_at_risk",
    "step_1_stop_legacy", "step_2_inventory", "step_3_preserve",
    "step_5_init_repo", "step_6_initial_snapshot",
    "step_7_verify", "step_8_install_scheduler_disabled",
    "step_9_rename_old_tree", "step_9b_enable_scheduler",
    "_rollback_to_legacy", "_re_enable_legacy_timer",
    "reclaim_legacy_trees",
    "run",
]
