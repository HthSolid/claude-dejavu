#!/usr/bin/env python3
"""
claude-dejavu — file corruption detection + recovery primitives (v0.7.2).

Pure-Python detection + replay layer for the `claude-dejavu scan-corruption`
and `claude-dejavu recover-file` subcommands. The strategy stack picks a base
file content from four sources, in order:

  1. File-history snapshot (`~/.claude/file-history/<session>/<hash>@vN`)
  2. DB-tracked edits + `git show HEAD:<path>` as the base
  3. Git HEAD only (with a warning — uncommitted edits cannot replay)
  4. Write-only: file was created via Write tool, no prior base needed

See: docs/superpowers/specs/2026-05-15-file-recovery-design.md
"""
from __future__ import annotations

import enum
import fnmatch
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Enums + exceptions ─────────────────────────────────────────────────────

class Status(enum.Enum):
    OK = "OK"
    ZEROED = "ZEROED"
    PARTIAL_ZERO = "PARTIAL_ZERO"
    SHRUNK = "SHRUNK"
    UNTRACKED_CHANGE = "UNTRACKED_CHANGE"


class BaseSource(enum.Enum):
    FILE_HISTORY = "file_history"
    DB_AND_GIT = "db_and_git"
    GIT_HEAD_ONLY = "git_head_only"
    WRITE_ONLY = "write_only"  # strategy 4


class ApplyError(RuntimeError):
    """An Edit's old_string was not found in the working buffer."""


# Binary extensions are exempt from the PARTIAL_ZERO 5% NUL heuristic. A
# 100%-NUL file in this set still flags ZEROED (size > 0 + all-NUL is
# unambiguous corruption regardless of extension).
_BINARY_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf",
    ".zip", ".gz", ".tar", ".so", ".o", ".a", ".wasm",
    ".bin", ".lock", ".woff", ".woff2", ".ttf", ".otf",
    ".mp3", ".mp4", ".webm", ".mov", ".webp", ".avif",
    ".br", ".zst", ".xz", ".pyc", ".class",
})

# Default exclude globs for `scan_dir`. Matches the spec section 3 list.
_DEFAULT_EXCLUDES = (
    "node_modules/**", ".git/**", "dist/**", "build/**",
    "target/**", ".next/**", "**/__pycache__/**",
)

# Known-text extensions for the UNTRACKED_CHANGE check (informational only).
_TEXT_EXTS_FOR_UNTRACKED = frozenset({
    ".ts", ".tsx", ".js", ".jsx", ".py", ".rs", ".go",
    ".java", ".json", ".yaml", ".md",
})


# ── Detection ──────────────────────────────────────────────────────────────

def detect_corruption(path: Path,
                       snapshot_size: Optional[int] = None) -> Status:
    """Pure detection. Caller may supply ``snapshot_size`` for SHRUNK
    comparison; without it only the NUL-based heuristics run.

    Decision order (matches spec section 3):
      1. size == 0 + snapshot_size > 100 → SHRUNK; else OK
      2. UTF-16 BOM in first 2 bytes → OK (carve-out)
      3. body is 100% NUL → ZEROED
      4. > 5% NUL AND extension not in _BINARY_EXTS → PARTIAL_ZERO
      5. size < 50% of snapshot_size → SHRUNK
      6. else OK
    """
    try:
        size = path.stat().st_size
    except OSError:
        return Status.OK
    if size == 0:
        if (snapshot_size or 0) > 100:
            return Status.SHRUNK
        return Status.OK
    try:
        with open(path, "rb") as f:
            head = f.read(2)
            if head in (b"\xff\xfe", b"\xfe\xff"):
                return Status.OK  # UTF-16 BOM carve-out
            f.seek(0)
            body = f.read()
    except OSError:
        return Status.OK
    nul = body.count(0)
    if nul == size:
        return Status.ZEROED
    if path.suffix.lower() not in _BINARY_EXTS:
        if nul / size > 0.05:
            return Status.PARTIAL_ZERO
    if snapshot_size and snapshot_size > 0 and size < snapshot_size * 0.5:
        return Status.SHRUNK
    return Status.OK


# ── Path normalization ─────────────────────────────────────────────────────

def resolve_path(p: str, *, project_path: Optional[str] = None) -> str:
    """Resolve a candidate path to its absolute realpath form.

    - Absolute → ``os.path.realpath(p)``
    - Relative + project_path → ``os.path.realpath(project_path / p)``
    - Relative + no project_path → ``os.path.realpath(p)`` (cwd-relative)
    """
    if not p:
        return ""
    if os.path.isabs(p):
        return os.path.realpath(p)
    if project_path:
        return os.path.realpath(os.path.join(project_path, p))
    return os.path.realpath(p)


# ── Edit op collection from session JSONL ──────────────────────────────────

def collect_edit_ops(jsonl_path: Path, *, target_path: str) -> list:
    """Walk session JSONL, return ``[(ts, name, input_dict), ...]`` for
    every Edit / MultiEdit / Write tool_use on ``target_path``.

    MultiEdit is exploded into multiple ``('Edit', input_dict)`` tuples in
    declaration order. Match is by ``endswith`` on slash-normalized paths,
    so callers can pass either an absolute path or a basename — both will
    match the JSONL's absolute `file_path` field.

    Output is sorted by timestamp (chronological).
    """
    target_norm = target_path.replace("\\", "/")
    target_suffix = target_norm.lstrip("/")
    out: list = []
    try:
        f = open(jsonl_path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return out
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                continue
            if obj.get("type") != "assistant":
                continue
            ts = obj.get("timestamp", "")
            msg = obj.get("message") or {}
            blocks = msg.get("content") or []
            if not isinstance(blocks, list):
                continue
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if b.get("type") != "tool_use":
                    continue
                name = b.get("name") or ""
                if name not in ("Edit", "MultiEdit", "Write"):
                    continue
                inp = b.get("input") or {}
                fp = (inp.get("file_path") or "").replace("\\", "/")
                if not fp:
                    continue
                # Match by suffix (handles absolute JSONL vs relative target)
                if not (fp.endswith(target_suffix)
                        or target_norm in fp
                        or fp == target_norm):
                    continue
                if name == "MultiEdit":
                    for sub in (inp.get("edits") or []):
                        if not isinstance(sub, dict):
                            continue
                        sub_in = dict(sub)
                        sub_in.setdefault("file_path", fp)
                        out.append((ts, "Edit", sub_in))
                else:
                    out.append((ts, name, inp))
    out.sort(key=lambda x: x[0] or "")
    return out


# ── Apply collected ops ────────────────────────────────────────────────────

def apply_edits(base: str, ops: list) -> str:
    """Replay ops chronologically against ``base``.

    - Edit: ``buf = buf.replace(old, new, 1)`` (single replacement) unless
      ``replace_all=True`` in which case all occurrences are replaced.
      Missing ``old_string`` raises :class:`ApplyError`.
    - Write: discard buffer, set to ``content``.
    """
    buf = base
    for entry in ops:
        # Tolerate both 3-tuples and longer sequences from callers
        ts = entry[0] if len(entry) > 0 else ""
        name = entry[1] if len(entry) > 1 else ""
        inp = entry[2] if len(entry) > 2 else {}
        if name == "Write":
            buf = inp.get("content", "") or ""
            continue
        if name in ("Edit", "MultiEdit"):
            old = inp.get("old_string", "") or ""
            new = inp.get("new_string", "") or ""
            replace_all = bool(inp.get("replace_all", False))
            if old not in buf:
                raise ApplyError(
                    f"old_string not found at {ts}: {old[:80]!r}")
            if replace_all:
                buf = buf.replace(old, new)
            else:
                buf = buf.replace(old, new, 1)
        # Unknown tool names are ignored (forward-compat).
    return buf


# ── Base picker (strategy stack) ───────────────────────────────────────────

def pick_base(*, target: str, session_id: str, ops: list,
              file_history_dir: Path,
              git_repo: Optional[Path]
              ) -> tuple[str, BaseSource, Optional[str]]:
    """Return ``(base_content, source, since_ts)`` per spec section 4.

    ``since_ts`` is the snapshot's ``backupTime`` when strategy 1 (file-
    history) hits — callers MUST filter ops with ``ts > since_ts`` so
    pre-snapshot edits don't replay against post-snapshot state. Returns
    ``None`` for strategies 2-4 (caller replays every op).

    Strategy 4 (Write-only) supersedes 1-3 when the op stream contains
    only Write entries and no Edits.
    """
    has_edit = any((len(o) > 1 and o[1] == "Edit") for o in ops)
    has_write = any((len(o) > 1 and o[1] == "Write") for o in ops)
    if has_write and not has_edit:
        return "", BaseSource.WRITE_ONLY, None

    # Strategy 1: file-history snapshot
    snap_result = _find_latest_snapshot(file_history_dir, target,
                                          session_id=session_id)
    if snap_result is not None:
        snap, snap_ts = snap_result
        if snap.is_file():
            try:
                return snap.read_text(encoding="utf-8", errors="replace"), \
                       BaseSource.FILE_HISTORY, snap_ts
            except OSError:
                pass

    # Strategy 2 / 3: git base
    if git_repo:
        base = _git_show_head(git_repo, target)
        if base is not None:
            # If the first Edit's old_string is in the git base, strategy 2.
            for entry in ops:
                if len(entry) > 1 and entry[1] == "Edit":
                    inp = entry[2] if len(entry) > 2 else {}
                    old = inp.get("old_string", "") or ""
                    if old and old in base:
                        return base, BaseSource.DB_AND_GIT, None
                    break
            return base, BaseSource.GIT_HEAD_ONLY, None

    # Strategy 4 fallback: empty base, hope replay is all-Writes (or fail
    # later in apply_edits with ApplyError).
    return "", BaseSource.WRITE_ONLY, None


# ── Internal helpers ───────────────────────────────────────────────────────

def _find_latest_snapshot(file_history_dir: Path, target: str,
                            *, session_id: Optional[str] = None,
                            to_ts: Optional[str] = None
                            ) -> Optional[tuple[Path, str]]:
    """Walk the session JSONL's ``file-history-snapshot`` entries and
    resolve the latest ``backupFileName`` for ``target``.

    ``file_history_dir`` may be either:
      - the per-session dir (``~/.claude/file-history/<session>/``), in
        which case ``backupFileName`` is resolved directly against it; or
      - the parent ``~/.claude/file-history/`` dir, in which case the
        session_id is appended.

    Resolution strategy:
      1. Look for a JSONL companion: walk
         ``~/.claude/projects/<encoded>/<session_id>.jsonl`` for
         ``file-history-snapshot`` entries.
      2. Extract ``snapshot.trackedFileBackups`` and find the key whose
         resolved-absolute form matches ``target``.
      3. Pick the entry with the largest ``backupTime`` <= ``to_ts``
         (default = now).
      4. Resolve ``backupFileName`` against ``file_history_dir``.

    Falls back to ``None`` if anything is missing (caller advances to the
    next strategy).
    """
    if not file_history_dir or not session_id:
        return None
    file_history_dir = Path(file_history_dir)
    # Locate the session JSONL via ~/.claude/projects/* search
    projects_root = Path.home() / ".claude" / "projects"
    candidate_jsonls: list[Path] = []
    try:
        for proj in projects_root.iterdir():
            if not proj.is_dir():
                continue
            jp = proj / f"{session_id}.jsonl"
            if jp.is_file():
                candidate_jsonls.append(jp)
    except OSError:
        pass

    target_norm = target.replace("\\", "/")
    target_suffix = target_norm.lstrip("/")
    best_ts: Optional[str] = None
    best_backup_name: Optional[str] = None

    for jp in candidate_jsonls:
        try:
            f = open(jp, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if obj.get("type") != "file-history-snapshot":
                    continue
                snap = obj.get("snapshot") or {}
                tfb = snap.get("trackedFileBackups") or {}
                for key, info in tfb.items():
                    if not isinstance(info, dict):
                        continue
                    key_norm = key.replace("\\", "/")
                    if not (key_norm == target_norm
                            or key_norm.endswith(target_suffix)
                            or target_norm.endswith(key_norm.lstrip("/"))):
                        continue
                    bt = info.get("backupTime") or ""
                    if to_ts and bt and bt > to_ts:
                        continue
                    if best_ts is None or (bt and bt > best_ts):
                        best_ts = bt
                        bf = info.get("backupFileName")
                        if isinstance(bf, str) and bf:
                            best_backup_name = bf

    if not best_backup_name:
        return None

    # Resolve backupFileName: if file_history_dir is the per-session dir,
    # use it directly; if it's the parent, append session_id.
    candidates = [
        file_history_dir / best_backup_name,
        file_history_dir / session_id / best_backup_name,
    ]
    for c in candidates:
        if c.is_file():
            return (c, best_ts)
    return None


def _git_show_head(repo: Path, target: str) -> Optional[str]:
    """``git show HEAD:<relpath>`` against ``repo``. Returns the file
    content on success, ``None`` on non-zero exit (file not in HEAD)."""
    if not repo:
        return None
    try:
        target_abs = os.path.realpath(target)
        repo_abs = os.path.realpath(repo)
        if target_abs.startswith(repo_abs + os.sep):
            relpath = target_abs[len(repo_abs) + 1:]
        else:
            relpath = target
        result = subprocess.run(
            ["git", "-C", str(repo), "show", f"HEAD:{relpath}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _default_recovery_backup_root() -> Path:
    """Resolve the recovery backup root (kept OFF-tree).

    Order of precedence:
      1. ``DATA_ROOT`` env var → ``$DATA_ROOT/recovered-files``
      2. ``CLAUDE_DEJAVU_DATA_ROOT`` env var → ``$.../recovered-files``
      3. ``$HOME/.local/share/claude-dejavu/recovered-files``
    """
    data_root = os.environ.get("DATA_ROOT") or \
                 os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
    if data_root:
        return Path(data_root) / "recovered-files"
    return Path.home() / ".local" / "share" / "claude-dejavu" / "recovered-files"


# ── Scan a directory tree ──────────────────────────────────────────────────

@dataclass
class ScanFinding:
    path: Path
    status: Status
    size: int = 0
    snapshot_size: Optional[int] = None
    note: str = ""


def scan_dir(root: Path, *,
              include: Optional[list] = None,
              exclude: Optional[list] = None,
              verbose: bool = False) -> list:
    """Walk ``root`` recursively. Returns a list of :class:`ScanFinding`
    for every file with status != OK (or every file when verbose=True).

    Default excludes: node_modules/, .git/, dist/, build/, target/,
    .next/, __pycache__/.
    """
    root = Path(root).resolve()
    excludes = list(exclude or []) + list(_DEFAULT_EXCLUDES)
    findings: list = []
    if not root.is_dir():
        return findings
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        # Prune dirs matching excludes
        pruned = []
        for d in list(dirnames):
            rel = os.path.join(rel_dir, d) if rel_dir != "." else d
            rel_norm = rel.replace(os.sep, "/")
            if any(fnmatch.fnmatch(rel_norm, pat)
                   or fnmatch.fnmatch(d, pat.rstrip("/*"))
                   for pat in excludes):
                pruned.append(d)
        for d in pruned:
            dirnames.remove(d)

        for fn in filenames:
            full = Path(dirpath) / fn
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            # Include filter (if given)
            if include:
                if not any(fnmatch.fnmatch(rel, pat)
                           or fnmatch.fnmatch(fn, pat)
                           for pat in include):
                    continue
            # Exclude filter
            if any(fnmatch.fnmatch(rel, pat) for pat in excludes):
                continue
            try:
                size = full.stat().st_size
            except OSError:
                continue
            status = detect_corruption(full)
            if status == Status.OK and not verbose:
                continue
            findings.append(ScanFinding(
                path=full, status=status, size=size,
                note="",
            ))
    return findings


# ── v0.8.0: pre-recovery snapshot + write_recovered ────────────────────────


def _pre_recovery_snapshot(*, feature: str, target: Path,
                              data_root: Optional[Path] = None) -> None:
    """Synchronously take a `tag=pre-recovery` snapshot before any
    destructive write (spec §3.4 — v0.8.0).

    Mirrors ``repair._pre_recovery_snapshot``. Lazy-imports session_copy.
    Raises ``RuntimeError`` on snapshot failure — callers MUST treat that
    as an abort signal and refuse to write.
    """
    try:
        import session_copy  # type: ignore[import]
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            f"pre-recovery snapshot failed: session_copy module unavailable "
            f"({exc}). Re-run install.py to bootstrap the v0.8.0 "
            f"session-copy subsystem, or pass --no-pre-recovery-snapshot "
            f"to bypass.")
    if data_root is None:
        env_override = (os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
                          or os.environ.get("DATA_ROOT"))
        if env_override:
            data_root = Path(env_override)
        else:
            data_root = Path.home() / ".local" / "share" / "claude-dejavu"
    repo_path = data_root / "restic-repo"
    pass_path = data_root / ".restic-pass"
    spec = session_copy.RepoSpec(
        repo_path=repo_path,
        passphrase_file=pass_path,
        bin_path=data_root / "bin" / "restic",
    )
    try:
        session_copy.take(spec, [Path(target)],
                            tag="pre-recovery",
                            message=f"before {feature}",
                            ctx_data_root=data_root)
    except Exception as exc:
        raise RuntimeError(
            f"pre-recovery snapshot failed: {exc}. The destructive write "
            f"has been aborted. Re-try after fixing the repo, or pass "
            f"--no-pre-recovery-snapshot to bypass.")


def write_recovered(target: Path, content: str, *,
                      no_pre_recovery_snapshot: bool = False) -> Path:
    """Atomic temp-then-replace write of `content` into `target`.

    v0.8.0 — synchronously takes a ``tag=pre-recovery`` restic snapshot
    BEFORE the write. If the snapshot fails, raises ``RuntimeError`` and
    the target is never touched. Pass ``no_pre_recovery_snapshot=True``
    to opt out.

    The caller is still responsible for any off-tree backup of the
    original — this helper exists to give the destructive write a clean
    grep target plus a single place to hang the pre-recovery snapshot
    contract.

    Returns the resolved target path.
    """
    target = Path(target)
    if not no_pre_recovery_snapshot:
        _pre_recovery_snapshot(feature=f"recover-file {target.name}",
                                  target=target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f"{target.name}.dejavu-recover.tmp"
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return target


__all__ = [
    "Status", "BaseSource", "ApplyError",
    "detect_corruption", "resolve_path",
    "collect_edit_ops", "apply_edits", "pick_base",
    "scan_dir", "ScanFinding",
    "_find_latest_snapshot", "_git_show_head",
    "_default_recovery_backup_root",
    "_pre_recovery_snapshot", "write_recovered",
]
