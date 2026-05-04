#!/usr/bin/env python3
"""
claude-dejavu SessionEnd hook (v0.2).

After Claude Code closes a session:
  1. Generate an LLM-distilled summary via code/summarize.py
  2. Extract typed observations from all assistant text turns in this session
  3. Persist both to Postgres

Reads {session_id, transcript_path} from stdin (Claude Code hook format).
Returns a small JSON systemMessage.

Designed to be cheap: under 10s on this machine for a 100-turn session
(2-5s for the LLM summarize call + a few hundred ms for regex extraction).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Local imports — same data-root resolution as stop.py
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "code"))


def _resolve_data_root() -> Path:
    override = os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "claude-dejavu"
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home() / "AppData/Local")
        return Path(base) / "claude-dejavu"
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg) / "claude-dejavu" if xdg else Path.home() / ".local" / "share" / "claude-dejavu"


def _load_pg_dsn() -> str:
    cfg = {}
    f = _resolve_data_root() / "config.env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"): continue
            if "=" in line:
                k, v = line.split("=", 1); cfg[k.strip()] = v.strip()
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"
    db = cfg.get("PG_DB") or f"claude_dejavu_{user}"
    return (
        f"host={cfg.get('PG_HOST','localhost')} port={cfg.get('PG_PORT','5432')} "
        f"user={cfg.get('PG_USER','postgres')} password={cfg.get('PG_PASS','postgres')} "
        f"dbname={db}"
    )


def main():
    # Recursion guard: if we're inside a `claude -p` we ourselves spawned for
    # summarization, do nothing — otherwise SessionEnd would fire summarize,
    # which spawns claude -p, which fires SessionEnd, which spawns…
    if os.environ.get("CLAUDE_DEJAVU_NESTED") == "1":
        print(json.dumps({"systemMessage": ""}))
        return

    raw = sys.stdin.read() or "{}"
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {}

    session_id = payload.get("session_id", "")
    if not session_id:
        print(json.dumps({"systemMessage": ""}))
        return

    try:
        import psycopg2
        import summarize, observations as obs_mod  # noqa: E402
    except Exception as e:
        print(json.dumps({"systemMessage": f"claude-dejavu summarize: import failed ({e})"}))
        return

    notes = []
    try:
        with psycopg2.connect(_load_pg_dsn()) as conn:
            # 1. Summary
            try:
                summary = summarize.summarize_session(conn, session_id)
                conn.commit()
                if summary.get("id"):
                    notes.append(f"summary[{summary.get('model','?')}]")
            except Exception as e:
                conn.rollback()
                notes.append(f"summary-failed:{type(e).__name__}")

            # 2. Observations from assistant text turns
            try:
                cur = conn.cursor()
                cur.execute("""
                    SELECT id, content
                    FROM turns
                    WHERE session_id = %s::uuid AND role='assistant' AND content_type='text'
                """, (session_id,))
                rows = cur.fetchall()
                n_obs = 0
                for turn_id, content in rows:
                    for obs in obs_mod.extract(content or ""):
                        if obs_mod.insert(conn, session_id, turn_id, obs):
                            n_obs += 1
                conn.commit()
                if n_obs:
                    notes.append(f"+{n_obs} observations")
            except Exception as e:
                conn.rollback()
                notes.append(f"obs-failed:{type(e).__name__}")
    except Exception as e:
        notes.append(f"db-failed:{type(e).__name__}")

    msg = "claude-dejavu: " + " ".join(notes) if notes else ""
    print(json.dumps({"systemMessage": msg}))


if __name__ == "__main__":
    main()
