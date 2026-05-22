#!/usr/bin/env python3
"""claude-dejavu — pg_dump / pg_restore wrapper for session-copy (v0.8.3).

Stages a ``pg_dump --format=custom`` of each ``claude_dejavu_*`` database
into ``$DATA_ROOT/staging/pg/`` so the next ``restic backup`` picks it up.
Restore is the symmetric ``pg_restore --clean --if-exists``.

Two execution paths (Linux + macOS + Windows):

* **Docker exec**: when ``pg_dump`` lives inside the running Postgres
  container (the default for dejavu's bundled stack). Preferred — no
  host binary required.
* **Host binary**: when ``pg_dump`` is on PATH; fallback for users who
  point dejavu at an external PG cluster.

Refuses to clobber a non-empty target DB during restore unless
``force=True`` is passed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


# Default values mirror ingester.py + pg_rescue.py.
DEFAULT_PG_HOST = "localhost"
DEFAULT_PG_PORT = "5450"
DEFAULT_PG_USER = "postgres"
DEFAULT_PG_PASS = "postgres"
DEFAULT_PG_CONTAINER = "claude-dejavu-postgres"


class PgBackupError(RuntimeError):
    """Hard failure in pg backup / restore."""


def _load_cfg() -> dict[str, str]:
    cfg: dict[str, str] = {}
    # Inline the data-root resolver to avoid a circular import with
    # session_copy.py.
    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support" / "claude-dejavu"
    elif sys.platform.startswith("win"):
        base = (os.environ.get("LOCALAPPDATA")
                or os.environ.get("APPDATA")
                or str(Path.home() / "AppData" / "Local"))
        root = Path(base) / "claude-dejavu"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        root = (Path(xdg) / "claude-dejavu" if xdg
                else Path.home() / ".local" / "share" / "claude-dejavu")
    f = root / "config.env"
    if not f.is_file():
        return cfg
    for line in f.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


@dataclass
class PgConnInfo:
    host: str = DEFAULT_PG_HOST
    port: str = DEFAULT_PG_PORT
    user: str = DEFAULT_PG_USER
    password: str = DEFAULT_PG_PASS
    container: str = DEFAULT_PG_CONTAINER


def _conn_from_cfg(cfg: Optional[dict[str, str]] = None) -> PgConnInfo:
    cfg = cfg if cfg is not None else _load_cfg()
    return PgConnInfo(
        host=cfg.get("PG_HOST", DEFAULT_PG_HOST),
        port=cfg.get("PG_PORT", DEFAULT_PG_PORT),
        user=cfg.get("PG_USER", DEFAULT_PG_USER),
        password=cfg.get("PG_PASS", DEFAULT_PG_PASS),
        container=cfg.get("PG_CONTAINER", DEFAULT_PG_CONTAINER),
    )


def _default_db_names() -> list[str]:
    """Resolve dejavu-managed DB names. Mirrors ingester.py convention."""
    cfg = _load_cfg()
    if cfg.get("PG_DB"):
        return [cfg["PG_DB"]]
    user = os.environ.get("USER") or "unknown"
    return [f"claude_dejavu_{user}"]


# ─── argv builders ────────────────────────────────────────────────────


def _docker_pg_dump_argv(conn: PgConnInfo, db_name: str,
                            *, container_out: str = "/tmp/pgdump.out") -> list[str]:
    """Build the docker-exec pg_dump argv. The dump is written INSIDE
    the container at ``container_out``; caller is responsible for the
    subsequent ``docker cp`` to host."""
    return [
        "docker", "exec",
        "-e", f"PGPASSWORD={conn.password}",
        conn.container,
        "pg_dump",
        "-h", conn.host, "-p", conn.port,
        "-U", conn.user,
        "--format=custom",
        "--no-owner", "--no-acl",
        "--file", container_out,
        db_name,
    ]


def _host_pg_dump_argv(conn: PgConnInfo, db_name: str,
                          *, file_out: str) -> list[str]:
    return [
        "pg_dump",
        "-h", conn.host, "-p", conn.port,
        "-U", conn.user,
        "--format=custom",
        "--no-owner", "--no-acl",
        "--file", file_out,
        db_name,
    ]


def _docker_pg_restore_argv(conn: PgConnInfo, db_name: str,
                                  *, container_in: str = "/tmp/pgrestore.in",
                                  force: bool = False) -> list[str]:
    args = [
        "docker", "exec",
        "-e", f"PGPASSWORD={conn.password}",
        conn.container,
        "pg_restore",
        "-h", conn.host, "-p", conn.port,
        "-U", conn.user,
        "--dbname", db_name,
        "--clean", "--if-exists",
        "--no-owner", "--no-acl",
        container_in,
    ]
    if force:
        args.insert(args.index("--clean") + 1, "--exit-on-error")
    return args


def _host_pg_restore_argv(conn: PgConnInfo, db_name: str, *,
                                file_in: str, force: bool = False) -> list[str]:
    args = [
        "pg_restore",
        "-h", conn.host, "-p", conn.port,
        "-U", conn.user,
        "--dbname", db_name,
        "--clean", "--if-exists",
        "--no-owner", "--no-acl",
        file_in,
    ]
    if force:
        args.insert(args.index("--clean") + 1, "--exit-on-error")
    return args


# ─── docker-exec availability probe ───────────────────────────────────


def _docker_pg_dump_available(conn: PgConnInfo, *, runner=None) -> bool:
    r = (runner or subprocess.run)(
        ["docker", "exec", conn.container, "which", "pg_dump"],
        capture_output=True, text=True, timeout=10)
    return getattr(r, "returncode", 1) == 0 and bool(r.stdout.strip())


def _host_pg_dump_available() -> bool:
    return shutil.which("pg_dump") is not None


# ─── stage ────────────────────────────────────────────────────────────


def stage_pg_dump(staging_dir: Path, *,
                    db_names: Optional[list[str]] = None,
                    conn: Optional[PgConnInfo] = None,
                    runner=None) -> dict:
    """Run ``pg_dump --format=custom`` for each DB into ``staging_dir``.

    Tries docker-exec first; falls back to host binary. Returns
    ``{dumped: [{db, file, size}], errored: [{db, error}], mode: 'docker'|'host'}``.
    """
    sh_run = runner or subprocess.run
    dbs = db_names or _default_db_names()
    conn = conn or _conn_from_cfg()
    staging_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"dumped": [], "errored": [], "mode": ""}

    mode: Optional[str] = None
    if _docker_pg_dump_available(conn, runner=sh_run):
        mode = "docker"
    elif _host_pg_dump_available():
        mode = "host"
    else:
        raise PgBackupError(
            "Neither docker-exec pg_dump nor host pg_dump available")
    report["mode"] = mode

    for db in dbs:
        dst = staging_dir / f"{db}.dump"
        try:
            if mode == "docker":
                ctmp = f"/tmp/{db}.dump"
                argv = _docker_pg_dump_argv(conn, db, container_out=ctmp)
                r = sh_run(argv, capture_output=True, text=True,
                              timeout=900)
                if r.returncode != 0:
                    raise PgBackupError(
                        f"pg_dump exit {r.returncode}: "
                        f"{r.stderr.strip()[:300]}")
                # Pull file from container to host
                cp = sh_run(
                    ["docker", "cp",
                      f"{conn.container}:{ctmp}", str(dst)],
                    capture_output=True, text=True, timeout=120)
                if cp.returncode != 0:
                    raise PgBackupError(
                        f"docker cp failed: {cp.stderr.strip()[:300]}")
                # Best-effort cleanup
                sh_run(["docker", "exec", conn.container, "rm", "-f",
                          ctmp], capture_output=True, text=True,
                         timeout=10)
            else:
                env = dict(os.environ)
                env["PGPASSWORD"] = conn.password
                argv = _host_pg_dump_argv(conn, db, file_out=str(dst))
                r = sh_run(argv, capture_output=True, text=True,
                              env=env, timeout=900)
                if r.returncode != 0:
                    raise PgBackupError(
                        f"pg_dump exit {r.returncode}: "
                        f"{r.stderr.strip()[:300]}")
            size = dst.stat().st_size if dst.is_file() else 0
            report["dumped"].append({
                "db": db, "file": str(dst), "size": size,
            })
        except Exception as exc:
            report["errored"].append({"db": db, "error": str(exc)})
    return report


# ─── restore ──────────────────────────────────────────────────────────


def _is_db_empty(conn: PgConnInfo, db_name: str, *, runner=None) -> bool:
    """Detect whether the target DB has any user tables. Used as a
    guard so restore refuses to clobber populated databases."""
    sh_run = runner or subprocess.run
    sql = (
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'public'")
    # docker-exec psql
    if _docker_pg_dump_available(conn, runner=sh_run):
        r = sh_run(
            ["docker", "exec",
              "-e", f"PGPASSWORD={conn.password}",
              conn.container,
              "psql", "-h", conn.host, "-p", conn.port,
              "-U", conn.user, "-d", db_name,
              "-tA", "-c", sql],
            capture_output=True, text=True, timeout=10)
    elif shutil.which("psql"):
        env = dict(os.environ)
        env["PGPASSWORD"] = conn.password
        r = sh_run(
            ["psql", "-h", conn.host, "-p", conn.port,
              "-U", conn.user, "-d", db_name,
              "-tA", "-c", sql],
            capture_output=True, text=True, env=env, timeout=10)
    else:
        # Can't probe → conservatively report non-empty so restore is
        # gated.
        return False
    if r.returncode != 0:
        return False
    try:
        return int(r.stdout.strip()) == 0
    except ValueError:
        return False


def restore_pg_dump(dump_file: Path, *,
                       db_name: str,
                       conn: Optional[PgConnInfo] = None,
                       force: bool = False,
                       runner=None) -> dict:
    """Restore one ``.dump`` file into ``db_name``. Refuses to clobber
    a non-empty DB unless ``force=True``.

    Returns ``{db, file, mode, restored: bool, error: str|None}``.
    """
    sh_run = runner or subprocess.run
    conn = conn or _conn_from_cfg()
    if not Path(dump_file).is_file():
        raise PgBackupError(f"dump file missing: {dump_file}")

    if not force and not _is_db_empty(conn, db_name, runner=sh_run):
        return {
            "db": db_name,
            "file": str(dump_file),
            "mode": "skipped",
            "restored": False,
            "error": (
                f"target DB {db_name!r} is non-empty; pass force=True "
                f"to clobber"),
        }

    mode = "docker" if _docker_pg_dump_available(conn, runner=sh_run) \
                       else ("host" if shutil.which("pg_restore") else "")
    if not mode:
        raise PgBackupError(
            "Neither docker-exec pg_restore nor host pg_restore available")

    try:
        if mode == "docker":
            ctmp = f"/tmp/{db_name}.restore.in"
            cp = sh_run(
                ["docker", "cp", str(dump_file),
                  f"{conn.container}:{ctmp}"],
                capture_output=True, text=True, timeout=120)
            if cp.returncode != 0:
                raise PgBackupError(
                    f"docker cp failed: {cp.stderr.strip()[:300]}")
            argv = _docker_pg_restore_argv(conn, db_name,
                                              container_in=ctmp,
                                              force=force)
            r = sh_run(argv, capture_output=True, text=True, timeout=900)
            sh_run(["docker", "exec", conn.container, "rm", "-f", ctmp],
                     capture_output=True, text=True, timeout=10)
        else:
            env = dict(os.environ)
            env["PGPASSWORD"] = conn.password
            argv = _host_pg_restore_argv(conn, db_name,
                                            file_in=str(dump_file),
                                            force=force)
            r = sh_run(argv, capture_output=True, text=True, env=env,
                          timeout=900)
        if r.returncode != 0:
            return {
                "db": db_name, "file": str(dump_file), "mode": mode,
                "restored": False,
                "error": (f"pg_restore exit {r.returncode}: "
                            f"{r.stderr.strip()[:300]}"),
            }
        return {"db": db_name, "file": str(dump_file), "mode": mode,
                  "restored": True, "error": None}
    except Exception as exc:
        return {"db": db_name, "file": str(dump_file), "mode": mode,
                  "restored": False, "error": str(exc)}


__all__ = [
    "PgBackupError",
    "PgConnInfo",
    "DEFAULT_PG_CONTAINER",
    "stage_pg_dump",
    "restore_pg_dump",
    "_docker_pg_dump_argv", "_host_pg_dump_argv",
    "_docker_pg_restore_argv", "_host_pg_restore_argv",
    "_conn_from_cfg",
    "_default_db_names",
    "_docker_pg_dump_available", "_host_pg_dump_available",
    "_is_db_empty",
]
