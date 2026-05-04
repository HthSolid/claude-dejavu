#!/usr/bin/env python3
"""
ingester.py — Parse Claude Code .jsonl transcripts into Postgres + Weaviate.

Idempotent: tracks last-ingested line per session via ingest_cursors.
Run modes:
  - Backfill all:   python ingester.py --backfill
  - Single session: python ingester.py --session <uuid> --jsonl <path>
  - Stop hook:      python ingester.py --stop-hook  (reads {session_id, transcript_path} from stdin)
"""
import argparse
import json
import os
import sys
import uuid as uuidlib
import hashlib
import re
import time
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

import psycopg2
import psycopg2.extras

# Read connection from config.env if present, else fallback.
# Resolution order:
#   1. CLAUDE_DEJAVU_DATA_ROOT (sandbox/tests/explicit override)
#   2. XDG_DATA_HOME/claude-dejavu (Linux default)
#   3. ~/Library/Application Support/claude-dejavu (macOS)
#   4. ~/.local/share/claude-dejavu (Linux fallback)
def _load_config():
    cfg = {}
    override = os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
    if override:
        home_data = Path(override)
    elif sys.platform == "darwin":
        home_data = Path.home() / "Library" / "Application Support" / "claude-dejavu"
    elif sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home() / "AppData/Local")
        home_data = Path(base) / "claude-dejavu"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        home_data = (Path(xdg) / "claude-dejavu") if xdg else Path.home() / ".local" / "share" / "claude-dejavu"
    f = home_data / "config.env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"): continue
            if "=" in line:
                k, v = line.split("=", 1); cfg[k.strip()] = v.strip()
    return cfg

_CFG = _load_config()
_OS_USER = os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"
_PG_DB  = _CFG.get("PG_DB") or f"claude_dejavu_{_OS_USER}"
PG_DSN = (f"host={_CFG.get('PG_HOST','localhost')} port={_CFG.get('PG_PORT','5450')} "
          f"user={_CFG.get('PG_USER','postgres')} password={_CFG.get('PG_PASS','postgres')} "
          f"dbname={_PG_DB}")
WEAVIATE_URL = _CFG.get("WV_URL", "http://localhost:8888")
WEAVIATE_CLASS = "ClaudeDejavuTurn"
TOOL_OUTPUT_TRUNC = 8000
CONTENT_VECTORIZE_MAX = 4000
CONTENT_VECTORIZE_MIN = 50
WEAVIATE_BATCH_SIZE = 32
WEAVIATE_TIMEOUT = 600

# Local imports
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from gist import make_gist
except Exception:
    def make_gist(text, role=None, content_type=None, prefer_llm=False):
        return text[:80], "rule"


def decode_project_path(encoded: str) -> str:
    """Best-effort decode. The Claude Code encoding (`/` → `-`) is lossy
    for project names that contain hyphens, so we just store the encoded form
    when we cannot reliably split. Used only for `project_path` text field."""
    if encoded == "_orphan" or not encoded:
        return ""
    return encoded  # store verbatim to avoid lossy splitting


def project_slug(encoded: str) -> str:
    """Heuristic slug derivation from Claude Code's encoded project path.

    Claude Code stores transcripts under ~/.claude/projects/<encoded>/<uuid>.jsonl
    where <encoded> is the absolute project path with '/' replaced by '-'.
    e.g.  /home/alice/projects/my-app  →  -home-alice-projects-my-app

    Resolution order:
      1. '-projects-<slug>'         (most common: ~/Documents/projects/<slug>, ~/projects/<slug>)
      2. '-home-<user>--<slug>'     (double-hyphen sentinel for hidden dirs like ~/.claude-…)
      3. '-home-<user>-<rest>'      (trailing component after the home prefix)
      4. fallback: encoded path stripped of leading hyphens
    """
    if encoded == "_orphan" or not encoded:
        return "_orphan"
    e = encoded
    # 1. Most common: …-projects-<slug>
    if "-projects-" in e:
        return e.split("-projects-", 1)[1] or e
    # 2. -home-<user>--<rest>  (double-hyphen marks where '/.<dir>' became '/-<dir>')
    m = re.match(r"^-home-[^-]+--(.+)$", e)
    if m:
        return m.group(1) or e
    # 3. -home-<user>-<rest>
    m = re.match(r"^-home-[^-]+-(.+)$", e)
    if m:
        return m.group(1) or e
    # 4. fallback
    return e.lstrip("-")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def extract_text_blocks(content) -> list[tuple[str, str]]:
    """Return list of (content_type, text) pairs from a message.content field."""
    out = []
    if isinstance(content, str):
        out.append(("text", content))
    elif isinstance(content, list):
        for c in content:
            if not isinstance(c, dict):
                continue
            ct = c.get("type")
            if ct == "text":
                out.append(("text", c.get("text", "")))
            elif ct == "thinking":
                out.append(("thinking", c.get("thinking", "")))
            elif ct == "tool_use":
                out.append(("tool_use", json.dumps({"name": c.get("name"), "input": c.get("input"), "id": c.get("id")})))
            elif ct == "tool_result":
                tr = c.get("content", "")
                if isinstance(tr, list):
                    tr = "\n".join(x.get("text", "") for x in tr if isinstance(x, dict) and x.get("type") == "text")
                out.append(("tool_result", tr if isinstance(tr, str) else json.dumps(tr)))
    return out


def weaviate_id_for(session_id: str, turn_id: int) -> str:
    return str(uuidlib.uuid5(uuidlib.NAMESPACE_URL, f"claudeturn:{session_id}:{turn_id}"))


def weaviate_batch_upsert(items: list[tuple[int, dict]]) -> dict[int, str]:
    """Upsert a batch of items via /v1/batch/objects. Returns {turn_id: weaviate_id}.
    Uses deterministic UUIDs so re-runs overwrite the same object."""
    if not items:
        return {}
    objects = []
    out = {}
    for turn_id, props in items:
        oid = weaviate_id_for(props["session_id"], turn_id)
        out[turn_id] = oid
        objects.append({"class": WEAVIATE_CLASS, "id": oid, "properties": props})

    body = json.dumps({"objects": objects}).encode()
    req = urllib.request.Request(f"{WEAVIATE_URL}/v1/batch/objects", data=body, method="POST", headers={"Content-Type": "application/json"})
    succeeded = {}
    try:
        with urllib.request.urlopen(req, timeout=WEAVIATE_TIMEOUT) as r:
            results = json.loads(r.read())
        for i, res in enumerate(results):
            turn_id = items[i][0]
            status = (res.get("result", {}) or {}).get("status")
            errors = (res.get("result", {}) or {}).get("errors")
            if status == "SUCCESS" or (status is None and not errors):
                succeeded[turn_id] = out[turn_id]
            else:
                err = errors.get("error") if errors else "unknown"
                print(f"  weaviate batch item {turn_id} failed: {err}", file=sys.stderr)
    except urllib.error.HTTPError as e:
        err_body = e.read()[:500].decode(errors='replace')
        print(f"  weaviate batch HTTP {e.code}: {err_body}", file=sys.stderr)
    except Exception as e:
        print(f"  weaviate batch error: {type(e).__name__}: {e}", file=sys.stderr)
    return succeeded


def ingest_jsonl(jsonl_path: str, session_uuid: str, project_encoded: str | None, conn):
    """Ingest a single jsonl file. Idempotent via ingest_cursors."""
    if not os.path.exists(jsonl_path):
        print(f"  jsonl not found: {jsonl_path}", file=sys.stderr)
        return 0

    file_size = os.path.getsize(jsonl_path)
    file_sha = sha256_file(jsonl_path) if file_size < 200_000_000 else None

    if not project_encoded:
        # try to derive from path
        m = re.search(r'/projects/(-home-[^/]+)/', jsonl_path)
        project_encoded = m.group(1) if m else "_orphan"

    slug = project_slug(project_encoded)
    project_path_decoded = decode_project_path(project_encoded)

    cur = conn.cursor()

    # Upsert session (now stamped with os_user for multi-user isolation)
    cur.execute("""
        INSERT INTO sessions (id, project_path, project_slug, os_user, jsonl_path, jsonl_size, jsonl_sha256)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            jsonl_path = EXCLUDED.jsonl_path,
            jsonl_size = EXCLUDED.jsonl_size,
            jsonl_sha256 = COALESCE(EXCLUDED.jsonl_sha256, sessions.jsonl_sha256),
            os_user = COALESCE(sessions.os_user, EXCLUDED.os_user)
    """, (session_uuid, project_path_decoded, slug, _OS_USER, jsonl_path, file_size, file_sha))

    # Get last cursor
    cur.execute("SELECT last_line FROM ingest_cursors WHERE session_id = %s", (session_uuid,))
    row = cur.fetchone()
    last_line = row[0] if row else 0

    # Stream the jsonl from last_line
    new_turns = 0
    new_vectors = 0
    ai_title = None
    started_at = None
    last_ts = None
    line_no = 0
    pending_vec: list[tuple[int, dict]] = []  # (turn_id, props)

    def flush_vectors():
        nonlocal new_vectors
        if not pending_vec:
            return
        ok = weaviate_batch_upsert(pending_vec)
        if ok:
            psycopg2.extras.execute_values(
                cur,
                "UPDATE turns AS t SET vectorized=TRUE, weaviate_id=v.wid::uuid FROM (VALUES %s) AS v(tid, wid) WHERE t.id = v.tid",
                [(tid, wid) for tid, wid in ok.items()],
            )
            new_vectors += len(ok)
            conn.commit()
        pending_vec.clear()

    try:
        fh = open(jsonl_path)
    except FileNotFoundError:
        # Source jsonl was deleted or moved (common when bootstrapping
        # against an aged ingest_cursors row pointing at a removed
        # project). Skip cleanly — the cursor stays untouched so a
        # later run can pick it up if the file returns.
        print(f"[ingest] source missing, skipping: {jsonl_path}",
              file=sys.stderr)
        return None
    with fh:
        for line in fh:
            line_no += 1
            if line_no <= last_line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue

            # Capture aiTitle
            if obj.get("type") == "ai-title":
                ai_title = obj.get("aiTitle")
                continue

            # Skip non-message types we don't track as turns
            if obj.get("type") not in ("user", "assistant"):
                continue

            ts = parse_ts(obj.get("timestamp"))
            if ts and not started_at:
                started_at = ts
            if ts:
                last_ts = ts

            msg = obj.get("message", {}) or {}
            role = obj.get("type") if obj.get("type") in ("user", "assistant") else msg.get("role")
            model = msg.get("model") if obj.get("type") == "assistant" else None
            usage = msg.get("usage") or {}
            tokens_in = usage.get("input_tokens")
            tokens_out = usage.get("output_tokens")

            content = msg.get("content")
            blocks = extract_text_blocks(content)

            for content_type, text in blocks:
                if not text:
                    continue
                # For tool_result blocks, attribute back to the previous tool_use turn
                # but still create a turn row for the relationship.
                # Compute gist for user/asst text (cheapest case in the hot path)
                gist_val, gist_method = (None, None)
                if role in ("user", "assistant") and content_type == "text" and len(text) >= 30:
                    gist_val, gist_method = make_gist(text, role=role, content_type=content_type, prefer_llm=False)

                cur.execute("""
                    INSERT INTO turns (session_id, idx, parent_uuid, message_uuid, role, ts, content, content_type, tokens_in, tokens_out, model, gist, gist_method)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (session_id, idx) DO NOTHING
                    RETURNING id
                """, (session_uuid, line_no * 10 + len(text) % 9,
                      obj.get("parentUuid"), obj.get("uuid"),
                      role, ts, text, content_type, tokens_in, tokens_out, model,
                      gist_val, gist_method))
                row = cur.fetchone()
                if row is None:
                    continue
                turn_id = row[0]
                new_turns += 1

                # Tool calls + file touches
                if content_type == "tool_use":
                    try:
                        d = json.loads(text)
                    except Exception:
                        d = {}
                    tool_name = d.get("name", "")
                    tool_input = d.get("input") or {}
                    cur.execute("""
                        INSERT INTO tool_calls (turn_id, session_id, tool_use_id, tool_name, tool_input, ts)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        RETURNING id
                    """, (turn_id, session_uuid, d.get("id"), tool_name, psycopg2.extras.Json(tool_input), ts))
                    tc_id = cur.fetchone()[0]
                    # File touches
                    if tool_name in ("Write", "Edit", "MultiEdit") and isinstance(tool_input, dict):
                        fp = tool_input.get("file_path")
                        if fp:
                            op = {"Write": "write", "Edit": "edit", "MultiEdit": "multi_edit"}[tool_name]
                            cur.execute("""
                                INSERT INTO file_touches (tool_call_id, session_id, file_path, op, ts)
                                VALUES (%s, %s, %s, %s, %s)
                            """, (tc_id, session_uuid, fp, op, ts))

                # Vectorize ONLY user/assistant text (skip tool_use/tool_result/thinking)
                # AND skip trivially short messages.
                if role in ("user", "assistant") and content_type == "text" and len(text) >= CONTENT_VECTORIZE_MIN:
                    snippet = text[:CONTENT_VECTORIZE_MAX]
                    pending_vec.append((turn_id, {
                        "session_id": session_uuid,
                        "turn_id": turn_id,
                        "project_slug": slug,
                        "role": role,
                        "content": snippet,
                        "ts": ts.isoformat() if ts else None,
                        "model": model or "",
                        "ai_title": ai_title or "",
                    }))
                    if len(pending_vec) >= WEAVIATE_BATCH_SIZE:
                        flush_vectors()

    # Flush any remaining vectorization pending
    flush_vectors()

    # Update session aggregates
    cur.execute("""
        UPDATE sessions SET
            ai_title = COALESCE(%s, ai_title),
            started_at = COALESCE(started_at, %s),
            ended_at = %s,
            total_turns = (SELECT count(*) FROM turns WHERE session_id = %s)
        WHERE id = %s
    """, (ai_title, started_at, last_ts, session_uuid, session_uuid))

    # Save cursor
    cur.execute("""
        INSERT INTO ingest_cursors (session_id, last_line, last_byte, last_run_at)
        VALUES (%s, %s, %s, now())
        ON CONFLICT (session_id) DO UPDATE SET last_line = EXCLUDED.last_line, last_byte = EXCLUDED.last_byte, last_run_at = now()
    """, (session_uuid, line_no, file_size))

    conn.commit()
    return new_turns, new_vectors, line_no


def cmd_bootstrap_from_live(conn):
    """One-shot: scan ~/.claude/projects/<encoded>/<uuid>.jsonl (Claude
    Code's live transcript directory), mirror each to chat-logs/, and
    ingest. Used by:
      - install.py at first-install time (so a freshly-installed plugin
        immediately knows about every past session — closes the
        'reinstall after 4 months gives me back nothing' gap).
      - SessionStart auto-trigger when the live tree has many .jsonl
        files but the DB has few.

    Idempotent — re-runs are no-ops because ingest_cursors tracks per-
    session position. Mirroring uses os.path.exists + size check to
    skip files that are already mirrored at the same size.
    """
    import shutil
    home = Path.home()
    live_root = home / ".claude" / "projects"
    if not live_root.is_dir():
        print("[bootstrap] no ~/.claude/projects directory — nothing to do.")
        return
    data_root = _CFG.get("DATA_ROOT") or str(home / ".local" / "share" / "claude-dejavu")
    backup_root = Path(data_root) / "chat-logs"
    backup_root.mkdir(parents=True, exist_ok=True)

    n_mirrored = n_sess = n_turn = n_vec = 0
    skipped_mirror = 0
    for proj_enc in sorted(os.listdir(live_root)):
        pdir_live = live_root / proj_enc
        if not pdir_live.is_dir():
            continue
        pdir_back = backup_root / proj_enc
        pdir_back.mkdir(parents=True, exist_ok=True)
        for f in sorted(os.listdir(pdir_live)):
            if not f.endswith(".jsonl"):
                continue
            src = pdir_live / f
            dst = pdir_back / f
            try:
                src_size = src.stat().st_size
            except OSError:
                continue
            if dst.exists() and dst.stat().st_size == src_size:
                skipped_mirror += 1
            else:
                try:
                    shutil.copy2(src, dst)
                    n_mirrored += 1
                except OSError as e:
                    print(f"  (mirror failed: {src} -> {dst}: {e})", file=sys.stderr)
                    continue
            session_uuid = f.replace(".jsonl", "")
            res = ingest_jsonl(str(dst), session_uuid, proj_enc, conn)
            if res:
                turns, vecs, _ = res
                if turns or vecs:
                    n_sess += 1
                    n_turn += turns
                    n_vec += vecs
    print(f"[bootstrap] mirrored {n_mirrored} new (.jsonl), skipped {skipped_mirror} unchanged")
    print(f"[bootstrap] ingested {n_sess} session(s), {n_turn} turns, {n_vec} vectors")


def cmd_backfill(conn):
    """Walk <DATA_ROOT>/chat-logs/<encoded-project>/<uuid>.jsonl"""
    data_root = _CFG.get("DATA_ROOT")
    if not data_root:
        # mirror the same resolution chain as _load_config
        override = os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
        data_root = override or str(Path.home() / ".local" / "share" / "claude-dejavu")
    backup_root = f"{data_root}/chat-logs"
    n_sess = n_turn = n_vec = 0
    for proj_enc in sorted(os.listdir(backup_root)):
        pdir = os.path.join(backup_root, proj_enc)
        if not os.path.isdir(pdir):
            continue
        for f in sorted(os.listdir(pdir)):
            if not f.endswith(".jsonl"):
                continue
            session_uuid = f.replace(".jsonl", "")
            jsonl = os.path.join(pdir, f)
            print(f"[ingest] {proj_enc}/{session_uuid}  size={os.path.getsize(jsonl)/1e6:.1f}MB")
            t0 = time.time()
            res = ingest_jsonl(jsonl, session_uuid, proj_enc, conn)
            if res:
                turns, vecs, lines = res
                dt = time.time() - t0
                print(f"  -> +{turns} turns, +{vecs} vectors, lines={lines}, took {dt:.1f}s")
                n_sess += 1; n_turn += turns; n_vec += vecs
    print(f"\nDone: {n_sess} sessions, {n_turn} turns, {n_vec} vectors")


def cmd_stop_hook(conn):
    """Read hook payload from stdin and ingest the named session incrementally."""
    payload = json.loads(sys.stdin.read())
    session_id = payload.get("session_id")
    transcript = payload.get("transcript_path")
    if not (session_id and transcript):
        return
    # Derive project from transcript path
    m = re.search(r'/projects/(-home-[^/]+)/', transcript)
    proj_enc = m.group(1) if m else "_orphan"
    res = ingest_jsonl(transcript, session_id, proj_enc, conn)
    if res:
        print(f"[stop-hook] +{res[0]} turns, +{res[1]} vectors")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--backfill", action="store_true",
                   help="re-ingest from chat-logs/ mirror (post-Stop-hook directory)")
    p.add_argument("--bootstrap", action="store_true",
                   help="one-shot: scan ~/.claude/projects/ for live transcripts, "
                        "mirror + ingest each one. Used by install.py to import "
                        "pre-existing sessions on first install.")
    p.add_argument("--stop-hook", action="store_true")
    p.add_argument("--session")
    p.add_argument("--jsonl")
    p.add_argument("--project")
    args = p.parse_args()

    conn = psycopg2.connect(PG_DSN)
    try:
        if args.backfill:
            cmd_backfill(conn)
        elif args.bootstrap:
            cmd_bootstrap_from_live(conn)
        elif args.stop_hook:
            cmd_stop_hook(conn)
        elif args.session and args.jsonl:
            res = ingest_jsonl(args.jsonl, args.session, args.project, conn)
            print(f"Ingested: {res}")
        else:
            p.print_help()
    finally:
        conn.close()
