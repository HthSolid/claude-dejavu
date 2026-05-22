#!/usr/bin/env python3
"""claude-dejavu — Weaviate volume backup wrapper for session-copy (v0.8.3).

Tars the Weaviate Docker volume into ``$DATA_ROOT/staging/weaviate/``
so the next ``restic backup`` picks it up. Stops Weaviate around the
tar for consistency (~5-10 s downtime), restarts on completion (even
on tar failure).

The tar runs inside a throwaway ``alpine`` container that mounts the
named volume read-only. This keeps the host independent of where the
volume actually lives on disk (Linux + macOS + Windows all work).

Restore is symmetric: stop Weaviate, extract the tarball over the
volume, restart.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


DEFAULT_WV_CONTAINER = "claude-dejavu-weaviate"
DEFAULT_WV_VOLUME = "claude-dejavu_claude-dejavu-weaviate"


class WeaviateBackupError(RuntimeError):
    """Hard failure in weaviate backup / restore."""


def _load_cfg() -> dict[str, str]:
    cfg: dict[str, str] = {}
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
class WvConfig:
    container: str = DEFAULT_WV_CONTAINER
    volume: str = DEFAULT_WV_VOLUME


def _cfg(cfg: Optional[dict[str, str]] = None) -> WvConfig:
    cfg = cfg if cfg is not None else _load_cfg()
    return WvConfig(
        container=cfg.get("WV_CONTAINER", DEFAULT_WV_CONTAINER),
        volume=cfg.get("WV_VOLUME", DEFAULT_WV_VOLUME),
    )


# ─── argv builders ────────────────────────────────────────────────────


def _docker_stop_argv(container: str) -> list[str]:
    return ["docker", "stop", container]


def _docker_start_argv(container: str) -> list[str]:
    return ["docker", "start", container]


def _tar_argv(volume: str, *, host_out: str) -> list[str]:
    """Throwaway alpine container that tars the named volume into a
    host-mounted destination dir.

    Layout: ``-v <volume>:/data:ro -v <host_out_dir>:/out``, command
    runs ``tar -czf /out/<basename> -C /data .``.
    """
    out_path = Path(host_out)
    out_dir = out_path.parent
    basename = out_path.name
    return [
        "docker", "run", "--rm",
        "-v", f"{volume}:/data:ro",
        "-v", f"{out_dir}:/out",
        "alpine",
        "sh", "-c",
        f"tar -czf /out/{basename} -C /data .",
    ]


def _untar_argv(volume: str, *, host_in: str) -> list[str]:
    in_path = Path(host_in)
    in_dir = in_path.parent
    basename = in_path.name
    return [
        "docker", "run", "--rm",
        "-v", f"{volume}:/data",
        "-v", f"{in_dir}:/in:ro",
        "alpine",
        "sh", "-c",
        f"rm -rf /data/* && tar -xzf /in/{basename} -C /data",
    ]


# ─── stage ────────────────────────────────────────────────────────────


def stage_weaviate_volume(staging_dir: Path, *,
                              cfg: Optional[WvConfig] = None,
                              runner=None) -> dict:
    """Stop Weaviate → tar volume → restart Weaviate.

    Returns ``{file, size, mode, errored: bool, error: str|None,
    container_restarted: bool}``.

    The container is restarted in a ``finally`` block so an aborted tar
    never leaves the user without their Weaviate service.
    """
    sh_run = runner or subprocess.run
    conf = cfg or _cfg()
    staging_dir.mkdir(parents=True, exist_ok=True)
    out = staging_dir / "weaviate-volume.tgz"
    report: dict[str, Any] = {
        "file": str(out),
        "size": 0,
        "mode": "alpine-tar",
        "errored": False,
        "error": None,
        "container_restarted": False,
    }
    stopped = False
    try:
        # Stop
        r_stop = sh_run(_docker_stop_argv(conf.container),
                          capture_output=True, text=True, timeout=30)
        stopped = (r_stop.returncode == 0)
        if not stopped:
            # Container might already be stopped — proceed but record.
            report["error"] = (
                f"docker stop returned {r_stop.returncode}: "
                f"{r_stop.stderr.strip()[:300]}")
        # Tar
        argv = _tar_argv(conf.volume, host_out=str(out))
        r_tar = sh_run(argv, capture_output=True, text=True, timeout=1800)
        if r_tar.returncode != 0:
            report["errored"] = True
            report["error"] = (f"tar exit {r_tar.returncode}: "
                                  f"{r_tar.stderr.strip()[:400]}")
        else:
            report["size"] = (out.stat().st_size if out.is_file()
                                 else 0)
    finally:
        # Always restart Weaviate, even if tar blew up.
        rs = sh_run(_docker_start_argv(conf.container),
                      capture_output=True, text=True, timeout=30)
        report["container_restarted"] = (rs.returncode == 0)
    return report


def restore_weaviate_volume(tarball: Path, *,
                                 cfg: Optional[WvConfig] = None,
                                 force: bool = False,
                                 runner=None) -> dict:
    """Stop Weaviate → extract tarball over volume → restart.

    ``force=True`` is required to confirm clobbering the live volume
    (the ``rm -rf /data/*`` step inside the untar argv is irreversible
    for the existing vectors).
    """
    sh_run = runner or subprocess.run
    conf = cfg or _cfg()
    if not Path(tarball).is_file():
        raise WeaviateBackupError(f"tarball missing: {tarball}")
    if not force:
        return {
            "file": str(tarball),
            "restored": False,
            "container_restarted": False,
            "error": (
                "restore would clobber the live Weaviate volume; "
                "pass force=True to confirm"),
        }
    report: dict[str, Any] = {
        "file": str(tarball),
        "restored": False,
        "container_restarted": False,
        "error": None,
    }
    try:
        sh_run(_docker_stop_argv(conf.container),
                 capture_output=True, text=True, timeout=30)
        argv = _untar_argv(conf.volume, host_in=str(tarball))
        r = sh_run(argv, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            report["error"] = (f"untar exit {r.returncode}: "
                                  f"{r.stderr.strip()[:400]}")
        else:
            report["restored"] = True
    finally:
        rs = sh_run(_docker_start_argv(conf.container),
                      capture_output=True, text=True, timeout=30)
        report["container_restarted"] = (rs.returncode == 0)
    return report


__all__ = [
    "WeaviateBackupError",
    "WvConfig",
    "DEFAULT_WV_CONTAINER", "DEFAULT_WV_VOLUME",
    "stage_weaviate_volume", "restore_weaviate_volume",
    "_docker_stop_argv", "_docker_start_argv",
    "_tar_argv", "_untar_argv",
    "_cfg",
]
