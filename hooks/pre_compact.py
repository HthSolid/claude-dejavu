#!/usr/bin/env python3
"""claude-dejavu PreCompact hook (v0.8.16).

Fires JUST BEFORE Claude Code compacts the working context — i.e.
RIGHT before older turns get dropped from what the assistant can
directly see. Claude Code emits this event so plugins can preserve
critical state for the post-compaction window.

What this hook does
===================
The Stop hook already incrementally ingests every assistant turn
into the dejavu PG/Weaviate corpus as it lands, so the data isn't
LOST when compaction runs — it's just no longer in the active
context window. The post-compact assistant CAN recover detail by
calling dejavu_session_export(session_id) or dejavu_global_search.

But the assistant has to KNOW it was compacted, KNOW its own
session_id, and KNOW which tool to call. This hook injects a
systemMessage that tells it exactly that.

We deliberately do NOT call the LLM here — PreCompact runs on a
short budget (Claude Code aborts hooks that take too long) and the
ingest is already up-to-date from the Stop hook. The memo is
deterministic and < 2 KB.

Stdin contract: {"session_id": str, "transcript_path": str, ...}
Stdout contract: JSON `{"systemMessage": "..."}` per Claude Code
                  hook spec.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _pg_dsn() -> str:
    """Build the PG DSN the same way mcp/server.py does — config.env
    overrides + sensible defaults. Returned as a connect-string."""
    import os as _os
    cfg: dict[str, str] = {}
    data_root_str = _os.environ.get(
        "CLAUDE_DEJAVU_DATA_ROOT") or str(
        Path.home() / ".local" / "share" / "claude-dejavu")
    cfg_fp = Path(data_root_str) / "config.env"
    if cfg_fp.exists():
        for ln in cfg_fp.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            cfg[k.strip()] = v.strip()
    os_user = (_os.environ.get("USER")
                or _os.environ.get("LOGNAME") or "unknown")
    pg_db = cfg.get("PG_DB") or f"claude_dejavu_{os_user}"
    return (f"host={cfg.get('PG_HOST', 'localhost')} "
            f"port={cfg.get('PG_PORT', '5432')} "
            f"user={cfg.get('PG_USER', 'postgres')} "
            f"password={cfg.get('PG_PASS', 'postgres')} "
            f"dbname={pg_db}")


def _fetch_recent_turn_text(sid: str, max_chars: int = 12_000
                              ) -> str:
    """v0.9.0 — pull the most recent turns of this session for
    synthesis. Caps total chars so the LLM prompt stays small even
    on long sessions. Returns role-prefixed text or empty on
    failure."""
    try:
        import psycopg2  # type: ignore[import]
    except Exception:
        return ""
    try:
        c = psycopg2.connect(_pg_dsn(), connect_timeout=1)
    except Exception:
        return ""
    try:
        cur = c.cursor()
        # Newest-first so the most-relevant-to-now content fits if
        # we hit the char cap.
        cur.execute(
            "SELECT idx, role, "
            "  COALESCE(content, gist, '') AS body "
            "FROM turns "
            "WHERE session_id = %s::uuid "
            "  AND content_type = 'text' "
            "  AND role IN ('user', 'assistant') "
            "ORDER BY idx DESC LIMIT 200", (sid,))
        rows = cur.fetchall()
    except Exception:
        return ""
    finally:
        try:
            c.close()
        except Exception:
            pass

    # Build oldest→newest so the LLM sees natural temporal order,
    # while honoring the char cap (drop from the head if needed —
    # newest matter more).
    rows.reverse()
    pieces: list[str] = []
    total = 0
    for idx, role, body in rows:
        line = f"[{idx}][{role}] {(body or '')[:1500]}"
        if total + len(line) + 1 > max_chars:
            break
        pieces.append(line)
        total += len(line) + 1
    return "\n".join(pieces)


def _build_synthesis_prompt(recent_text: str) -> str:
    """Prompt that asks the cascade for the 5-field session digest
    in JSON. Identical structure to session_end so the parser at
    `summarize._parse_json_loose` succeeds on the response."""
    return (
        "You are summarizing a partially-completed coding-assistant "
        "session for the SAME assistant. The conversation is about "
        "to be compacted — your output will be injected as a "
        "systemMessage so the assistant can recall context without "
        "tool calls.\n\n"
        "Return a JSON object with these keys (each value is a "
        "concise string of facts; no prose):\n"
        "  request      — what the user asked for in this session\n"
        "  investigated — what was looked at (files, services, IPs)\n"
        "  learned      — concrete discoveries / decisions\n"
        "  completed    — what is done\n"
        "  next_steps   — what is pending\n\n"
        "Be DENSE. Names, paths, IPs, hashes, ports, versions, "
        "error messages — keep them verbatim. Drop filler.\n\n"
        "Session content (oldest → newest):\n"
        f"{recent_text}\n\n"
        "JSON:")


def _format_synthesis_memo(parsed: dict, model_label: str) -> str:
    """Render the 5 synthesis fields as an inline block."""
    out: list[str] = [f"  [{model_label}]"]
    for fld in ("request", "investigated", "learned",
                 "completed", "next_steps"):
        v = (parsed.get(fld) or "").strip()
        if v:
            out.append(f"  {fld:13s}: {v[:1200]}")
    return "\n".join(out)


def _silent_exit() -> None:
    """Every failure mode lands here — never break compaction.

    PreCompact errors are non-blocking by default but a malformed
    stdout could still confuse downstream parsing, so we emit a
    no-op JSON envelope on any unexpected condition."""
    print(json.dumps({"continue": True, "suppressOutput": True}))
    sys.exit(0)


def main() -> int:
    # Read stdin payload (Claude Code injects {session_id, ...}).
    try:
        raw = sys.stdin.read()
    except Exception:
        _silent_exit()
        return 0
    if not raw:
        _silent_exit()
        return 0
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        _silent_exit()
        return 0

    sid = (payload.get("session_id") or "").strip()
    if not sid:
        _silent_exit()
        return 0

    # 8-char prefix is enough for dejavu_session_export to resolve
    # the row deterministically — and short enough to read at a
    # glance in the systemMessage.
    sid8 = sid[:8]

    # Sanity-check: do we have any turns ingested for this session
    # yet? If yes, mention the count so the assistant has confidence
    # the recovery path will work. We hit PG via a SEPARATE
    # short-timeout connection so a stalled PG can never block
    # compaction. On any failure we still emit the memo (recovery is
    # still possible, we just can't quote a count).
    n_turns: int | None = None
    try:
        # Lazy import keeps the no-PG path zero-cost.
        import os
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "code"))
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
        # Same DSN-building logic as mcp/server.py. Inline here to
        # avoid pulling the full MCP module into the hook process.
        import psycopg2  # type: ignore[import]
        try:
            c = psycopg2.connect(_pg_dsn(), connect_timeout=1)
            try:
                cur = c.cursor()
                cur.execute(
                    "SELECT count(*) FROM turns "
                    "  WHERE session_id = %s::uuid", (sid,))
                row = cur.fetchone()
                n_turns = row[0] if row else None
            finally:
                try:
                    c.close()
                except Exception:
                    pass
        except Exception:
            # Stalled PG, bad DSN, etc. — emit memo without count.
            n_turns = None
    except Exception:
        n_turns = None

    parts = [
        "Claude Code is about to compact older turns out of the "
        "active context window.",
    ]
    if n_turns is not None and n_turns > 0:
        parts.append(
            f"All {n_turns:,} turns of this session are already "
            f"ingested in the dejavu corpus (PG/Weaviate) — "
            f"compaction only affects YOUR working memory, not the "
            f"persistent store.")
    else:
        parts.append(
            "Earlier turns of this session are persisted in the "
            "dejavu corpus (PG/Weaviate) — compaction only affects "
            "your working memory, not the persistent store.")

    # v0.9.0 — INLINE SYNTHESIS. Generate a recovery memo from the
    # actual recent turns so the model has the facts directly in the
    # systemMessage instead of having to make tool calls. Reuses the
    # same provider cascade as session_end (claude -p → anthropic
    # SDK → openrouter → ollama → rule-based). Strict 4s budget so
    # the hook never exceeds the 5s claude-code default. Falls back
    # silently to the recovery-tools list when synthesis is off,
    # times out, or every provider fails.
    enable_synth = (os.environ.get(
        "CLAUDE_DEJAVU_PRECOMPACT_SYNTHESIS", "true").lower()
                     not in ("0", "false", "off", "no"))
    synth_text: str | None = None
    if enable_synth and n_turns is not None and n_turns > 0:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                                     / "code"))
            from summarize import generate_summary  # type: ignore[import]
            recent_text = _fetch_recent_turn_text(sid)
            if recent_text:
                prompt = _build_synthesis_prompt(recent_text)
                # Hard time-budget so a slow provider can't blow the
                # claude-code hook timeout. We re-use summarize's
                # internal cascade, which itself respects
                # CASCADE_TIMEOUT_S — but we also wrap the whole
                # thing in a wall-clock check.
                import time as _t
                t0 = _t.time()
                parsed, model_label = generate_summary(prompt)
                if (_t.time() - t0) < 4.0:
                    synth_text = _format_synthesis_memo(
                        parsed, model_label)
        except Exception:
            synth_text = None
    if synth_text:
        parts.append("")  # blank line before block
        parts.append("Recovery memo (synthesized from PG turns "
                     f"just now):")
        parts.append(synth_text)
        parts.append("")  # blank line after block
    # v0.8.18 — phrase the recovery hints as TOOL pointers rather
    # than prescribed argument shapes. The right `mode` / `limit` /
    # `offset` depend on what the user actually asks for; over-
    # specifying steered earlier models toward gist-only retrieval
    # even for "write a full document" use cases.
    parts.append(
        f"This session's id (prefix) is '{sid8}'. To recover detail "
        f"from compacted-away turns:")
    parts.append(
        f"  • dejavu_session_export(session_id='{sid8}', ...) — "
        f"paginated turn dump. mode='gist' (default, ~50 turns / "
        f"page) for an overview; mode='full' for the entire "
        f"content. offset+limit paginate.")
    parts.append(
        "  • dejavu_global_search(query=...) — cross-session, "
        "identifier-aware (IPs / UUIDs / hashes / paths / URLs / "
        "IPv6 all literal-matched).")
    parts.append(
        "  • dejavu_summary(limit=1) — one-shot project digest "
        "across all sessions for the current project.")
    parts.append(
        "  • dejavu_find_sessions(query=...) — rank prior sessions "
        "by topic match when you want a list, not a single recall.")

    out = {
        "continue": True,
        "suppressOutput": False,
        "systemMessage": " ".join(parts),
    }
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
