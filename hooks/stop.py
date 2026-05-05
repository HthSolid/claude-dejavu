#!/usr/bin/env python3
"""
claude-dejavu Stop hook (cross-platform).

Replaces three bash scripts:
  - save-chat-log.sh   (off-tree mirror)
  - ingest-stop-hook.sh (incremental PG/Weaviate ingestion)
  - snapshot.sh         (rsync hardlink snapshot, formerly systemd-driven)

Plus adds:
  - throttled snapshot   (every 5 min during use, never during idle)
  - throttled healthcheck (weekly)
  - deletion-detection   (warns if a known session has gone missing from live)

Reads {session_id, transcript_path} JSON from stdin.
Returns a JSON systemMessage object on stdout.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ─── Cross-platform data root ────────────────────────────────────────────────

def data_root() -> Path:
    """Return the platform-correct data dir.
    Linux:   ~/.local/share/claude-dejavu
    macOS:   ~/Library/Application Support/claude-dejavu
    Windows: %LOCALAPPDATA%\\claude-dejavu  (or %APPDATA%\\claude-dejavu fallback)
    """
    override = os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
    if override:
        return Path(override)
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "claude-dejavu"
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(home / "AppData" / "Local")
        return Path(base) / "claude-dejavu"
    # Linux + others
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg) / "claude-dejavu" if xdg else home / ".local" / "share" / "claude-dejavu"


DATA_ROOT = data_root()
SNAPSHOT_INTERVAL_S = 300   # 5 min
HEALTHCHECK_INTERVAL_S = 7 * 86400
SNAPSHOT_RETENTION_DAYS = 90

# ─── Atomic copy + lockfile helpers ──────────────────────────────────────────

def atomic_copy(src: Path, dst: Path) -> None:
    """Cross-platform atomic file replace via tmp + rename."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=str(dst.parent))
    os.close(fd)
    tmp = Path(tmp_path)
    try:
        shutil.copy2(src, tmp)
        # os.replace is atomic on POSIX and Windows (Python ≥3.3)
        os.replace(tmp, dst)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def acquire_lock(path: Path, ttl_s: int = 600) -> bool:
    """Best-effort file lock. Returns True if we got it. Stale locks (older than ttl) are reaped."""
    try:
        if path.exists():
            age = time.time() - path.stat().st_mtime
            if age < ttl_s:
                return False
            path.unlink(missing_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(os.getpid()))
        return True
    except Exception:
        return False


def release_lock(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


# ─── Session encoding helpers ────────────────────────────────────────────────

def encoded_project_from_transcript(transcript: str) -> str:
    """~/.claude/projects/<encoded-project>/<uuid>.jsonl → <encoded-project>"""
    p = Path(transcript).resolve()
    # parent dir name is the encoded project
    return p.parent.name


# ─── Step 1: off-tree mirror ─────────────────────────────────────────────────

def save_chat_log_mirror(session_id: str, transcript: str) -> tuple[bool, int]:
    src = Path(transcript)
    if not src.is_file():
        return False, 0
    sz = src.stat().st_size
    if sz < 2000:  # skip trivial sessions
        return False, sz
    encoded = encoded_project_from_transcript(transcript)
    dst = DATA_ROOT / "chat-logs" / encoded / f"{session_id}.jsonl"
    atomic_copy(src, dst)
    return True, sz


# ─── Step 2: incremental ingest ──────────────────────────────────────────────

def ingest_incremental(session_id: str, transcript: str) -> str:
    """Run the Python ingester for this session. Detached; non-blocking.

    Detachment is critical so the Stop hook returns to Claude Code in
    milliseconds even when the ingester takes minutes to process a large
    session. The detachment was incomplete pre-fix — only Windows got
    DETACHED_PROCESS, while on Linux/macOS the child stayed in the
    parent's session group and got killed by SIGHUP when Claude Code
    closed. That used to lose ingest progress on session-end-and-quit.

    Errors don't surface synchronously (we want fire-and-forget) but the
    ingester itself logs to error_log on crashes, and dejavu doctor
    surfaces those — closing the silent-failure loop.
    """
    here = Path(__file__).resolve().parent
    ingester = here.parent / "code" / "ingester.py"
    # Resolve the venv python in a Windows/POSIX-safe way; fall back to
    # the interpreter that's running this hook if the bundled venv
    # isn't there (e.g. mid-install). The fallback path won't have
    # psycopg2 — that fails fast with ImportError, which the ingester
    # logs to error_log so doctor can surface it.
    venv_dir = DATA_ROOT / "venv"
    candidates = [
        venv_dir / "Scripts" / "python.exe",
        venv_dir / "bin" / "python",
        venv_dir / "bin" / "python3",
    ]
    py = next((str(c) for c in candidates if c.exists()), sys.executable)

    log = DATA_ROOT / "ingest.log"
    payload = json.dumps({"session_id": session_id, "transcript_path": transcript})

    is_windows = sys.platform.startswith("win")
    popen_kwargs: dict = {
        "stdin": subprocess.PIPE,
        "stdout": open(str(log), "ab"),
        "stderr": subprocess.STDOUT,
    }
    if is_windows:
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — survive parent exit.
        popen_kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        # POSIX: start_new_session() makes us a session leader, so SIGHUP
        # to Claude Code does NOT propagate to us. close_fds prevents
        # us from holding the parent's stdin/stdout open after detach.
        popen_kwargs["start_new_session"] = True
        popen_kwargs["close_fds"] = True

    try:
        proc = subprocess.Popen([py, str(ingester), "--stop-hook"], **popen_kwargs)
        proc.stdin.write(payload.encode())
        proc.stdin.close()
        return "ingest scheduled"
    except Exception as e:
        # Surface the schedule-time failure — the ingester never even
        # started. Log to ingest.log AND to error_log so doctor sees it.
        msg = f"ingest schedule failed: {type(e).__name__}: {e}"
        try:
            with open(str(log), "ab") as f:
                f.write(f"[stop-hook] {msg}\n".encode())
        except Exception:
            pass
        try:
            sys.path.insert(0, str(here.parent / "code"))
            from error_log import log_error
            log_error("hook.stop.ingest", msg)
        except Exception:
            pass
        return msg


# ─── Step 3: throttled snapshot ──────────────────────────────────────────────

def maybe_snapshot() -> str | None:
    last_file = DATA_ROOT / "last_snapshot"
    try:
        if last_file.exists():
            last = float(last_file.read_text().strip() or 0)
            if time.time() - last < SNAPSHOT_INTERVAL_S:
                return None
    except Exception:
        pass

    lock = DATA_ROOT / ".snapshot.lock"
    if not acquire_lock(lock):
        return None
    try:
        snapshots_dir = DATA_ROOT / "snapshots"
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        latest = snapshots_dir / "latest"
        ts = time.strftime("%Y%m%d-%H%M%S")
        new_dir = snapshots_dir / ts

        sources = [
            Path.home() / ".claude" / "projects",
            Path.home() / ".claude-mem" / "claude-mem.db",
            DATA_ROOT / "chat-logs",
        ]

        if shutil.which("rsync"):
            for src in sources:
                if not src.exists():
                    continue
                rel = src.name if src.is_dir() else src.name
                dst = new_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                cmd = ["rsync", "-a"]
                if latest.is_symlink() or latest.is_dir():
                    link_dest = latest / rel
                    if link_dest.exists():
                        cmd += [f"--link-dest={link_dest}"]
                src_arg = f"{src}/" if src.is_dir() else str(src)
                cmd += [src_arg, str(dst) + ("/" if src.is_dir() else "")]
                subprocess.run(cmd, check=False, capture_output=True)
        else:
            # No rsync (Windows native): fall back to copytree + plain copy.
            # This loses hardlink dedup but works.
            for src in sources:
                if not src.exists(): continue
                dst = new_dir / src.name
                if src.is_dir():
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)

        # Update "latest" symlink (or copy on Windows)
        if sys.platform.startswith("win"):
            latest_marker = snapshots_dir / "latest.txt"
            latest_marker.write_text(ts)
        else:
            tmp_link = snapshots_dir / "latest.tmp"
            if tmp_link.exists() or tmp_link.is_symlink():
                tmp_link.unlink()
            tmp_link.symlink_to(ts)
            os.replace(tmp_link, latest)

        last_file.write_text(str(time.time()))

        # Prune old snapshots
        cutoff = time.time() - SNAPSHOT_RETENTION_DAYS * 86400
        for d in snapshots_dir.iterdir():
            if d.name.startswith("20") and d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)

        return f"snapshot {ts}"
    finally:
        release_lock(lock)


# ─── Step 4: deletion-detection ──────────────────────────────────────────────

def detect_missing_sessions(max_warn: int = 3) -> str | None:
    """Compare backup chat-logs/ against live ~/.claude/projects/. Warn if any
    backed-up sessions disappeared from live."""
    backup_root = DATA_ROOT / "chat-logs"
    live_root = Path.home() / ".claude" / "projects"
    if not backup_root.is_dir() or not live_root.is_dir():
        return None
    missing = []
    for proj_dir in backup_root.iterdir():
        if not proj_dir.is_dir() or proj_dir.name.startswith("_"):
            continue
        for jsonl in proj_dir.glob("*.jsonl"):
            uuid = jsonl.stem
            live_path = live_root / proj_dir.name / f"{uuid}.jsonl"
            if not live_path.exists():
                missing.append(uuid)
                if len(missing) >= max_warn:
                    break
        if len(missing) >= max_warn:
            break
    if not missing:
        return None
    sample = ", ".join(uuid[:8] for uuid in missing[:max_warn])
    return f"⚠ claude-dejavu: {len(missing)}+ session(s) missing from ~/.claude/projects/ ({sample}…). Run `claude-dejavu restore --list` to see all."


# ─── Step 5: throttled healthcheck ───────────────────────────────────────────

def maybe_healthcheck() -> str | None:
    last_file = DATA_ROOT / "last_healthcheck"
    try:
        if last_file.exists():
            last = float(last_file.read_text().strip() or 0)
            if time.time() - last < HEALTHCHECK_INTERVAL_S:
                return None
    except Exception:
        pass
    lock = DATA_ROOT / ".healthcheck.lock"
    if not acquire_lock(lock):
        return None
    try:
        # Trivial healthcheck: just touch a heartbeat file. Full healthcheck
        # script can be invoked by `claude-dejavu healthcheck` manually.
        last_file.write_text(str(time.time()))
        return "healthcheck-tick"
    finally:
        release_lock(lock)


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    # Recursion guard for nested `claude -p` invocations from our summarizer.
    if os.environ.get("CLAUDE_DEJAVU_NESTED") == "1":
        print(json.dumps({"systemMessage": ""}))
        return

    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    raw = sys.stdin.read() or "{}"
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {}
    session_id = payload.get("session_id", "")
    transcript = payload.get("transcript_path", "")

    notes = []

    if session_id and transcript:
        ok, sz = save_chat_log_mirror(session_id, transcript)
        if ok:
            notes.append(f"mirror {sz}b")
        ingest_incremental(session_id, transcript)

    snap = maybe_snapshot()
    if snap:
        notes.append(snap)

    miss = detect_missing_sessions()
    msg_for_ui = miss  # this surfaces to the user via systemMessage

    maybe_healthcheck()

    # systemMessage is what Claude Code shows the user. Only emit if there's
    # something worth saying (deletion warning).
    out = {"systemMessage": msg_for_ui} if msg_for_ui else {"systemMessage": ""}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
