#!/usr/bin/env python3
"""
claude-dejavu — session JSONL repair (F1 primitives).

Detects two failure modes per session:
  CORRUPTED:  JSONL has parse-failing lines (power-outage truncation,
              filesystem fault, etc.)
  TRUNCATED:  JSONL line count is far below dejavu's turn count for the
              session (Claude Code rotation, partial restore, etc.)

Reconstructs missing entries by reading the `turns` table and converting
each row back to a JSONL-shaped dict. Synthetic id prefixes
(``msg_recon_``, ``toolu_recon_``) mark reconstructed entries so a future
audit tool can distinguish them from originals.

See: docs/superpowers/specs/2026-05-14-session-repair-design.md
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ── Diagnosis ──────────────────────────────────────────────────────────────

@dataclass
class Diagnosis:
    """Detection result for a single session.

    Truncation comparator is ``db_turn_count`` (NOT ``db_max_idx``):
    ``code/ingester.py`` computes ``idx = line_no * 10 + len(text) % 9``,
    so ``max(idx) ≈ max(line_no) * 10`` — comparing live_lines against
    db_max_idx would flag every healthy session. See spec section 3.
    """
    session_id: str
    live_path: Path
    valid_lines: int
    corrupted_lines: int
    db_turn_count: int
    db_max_idx: int

    @property
    def corrupted(self) -> bool:
        return self.corrupted_lines > 0

    @property
    def truncated(self) -> bool:
        # Need DB rows to compare against. db_turn_count=0 → not truncated
        # (nothing recoverable, treated as healthy by this signal alone).
        if self.db_turn_count <= 0:
            return False
        return self.valid_lines < self.db_turn_count * 0.95

    @property
    def healthy(self) -> bool:
        return not (self.corrupted or self.truncated)


# ── Parse-failure scanner ──────────────────────────────────────────────────

def count_parse_failures(jsonl_path) -> tuple[int, int]:
    """Return ``(valid_line_count, parse_fail_line_count)``.

    Blank / whitespace-only lines are skipped before counting parse
    failures (many writers leave trailing newlines that would otherwise
    register as false-positive corruptions).
    """
    p = Path(jsonl_path)
    valid = 0
    bad = 0
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            try:
                json.loads(line)
                valid += 1
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                bad += 1
    return valid, bad


# ── diagnose ───────────────────────────────────────────────────────────────

def diagnose(session_id: str, live_path, conn) -> Diagnosis:
    """Probe JSONL + DB to build a Diagnosis.

    ``conn`` is any object with a ``cursor()`` context manager whose
    cursor supports ``.execute(sql, params)`` + ``.fetchone()`` (we test
    against a fake; production passes a psycopg2 connection).
    """
    live = Path(live_path)
    valid, bad = count_parse_failures(live) if live.is_file() else (0, 0)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), coalesce(max(idx), 0) FROM turns "
            "WHERE session_id = %s",
            (session_id,))
        row = cur.fetchone()
    db_turn_count = int(row[0]) if row else 0
    db_max_idx = int(row[1]) if row else 0
    return Diagnosis(
        session_id=session_id,
        live_path=live,
        valid_lines=valid,
        corrupted_lines=bad,
        db_turn_count=db_turn_count,
        db_max_idx=db_max_idx,
    )


# ── Synthetic id helpers ───────────────────────────────────────────────────

_RECON_MSG_PREFIX = "msg_recon_"
_RECON_TOOL_PREFIX = "toolu_recon_"


def _recon_msg_id(message_uuid: str) -> str:
    """Synthesize a deterministic ``msg_recon_<uid>`` id.

    Length guard: short uuids (synthetic test ids) pad to a stable
    8-char suffix so the result never collapses below
    ``len(prefix)+8`` chars.
    """
    uid = (message_uuid or "")[:24].ljust(8, "x")
    return f"{_RECON_MSG_PREFIX}{uid}"


def _recon_tool_id(message_uuid: str) -> str:
    """Synthesize a deterministic ``toolu_recon_<uid>`` id (see
    ``_recon_msg_id`` for the length-guard rationale)."""
    uid = (message_uuid or "")[:24].ljust(8, "x")
    return f"{_RECON_TOOL_PREFIX}{uid}"


# ── Per-row reconstruction ─────────────────────────────────────────────────

def reconstruct_entry(row: dict, *, cwd: str,
                       prev_tool_use_id: Optional[str] = None) -> dict:
    """Build a JSONL entry dict from a ``turns`` row.

    ``prev_tool_use_id`` is the synthetic tool_use id from the
    immediately preceding assistant tool_use turn — used to pair a
    reconstructed tool_result. When None, the pairing falls back to
    ``_recon_tool_id(parent_uuid or message_uuid)`` so resume doesn't
    crash.
    """
    role = row["role"]
    content_type = (row.get("content_type") or "text").lower()
    raw_content = row.get("content") or ""
    ts = row["ts"]

    if hasattr(ts, "isoformat"):
        ts_str = ts.isoformat()
        if ts_str.endswith("+00:00"):
            # Claude Code prefers 'Z' over '+00:00'
            ts_str = ts_str[:-6] + "Z"
    else:
        ts_str = str(ts)

    # Build content blocks based on type.
    if content_type == "tool_use":
        try:
            parsed = json.loads(raw_content) if raw_content else {}
            if not isinstance(parsed, dict):
                parsed = {"name": "unknown", "input": {"_raw": raw_content}}
        except json.JSONDecodeError:
            parsed = {"name": "unknown", "input": {"_raw": raw_content}}
        blocks = [{
            "type": "tool_use",
            "id": _recon_tool_id(row["message_uuid"]),
            "name": parsed.get("name", "unknown"),
            "input": parsed.get("input", {}),
        }]
        top_type = "assistant"
    elif content_type == "tool_result":
        tool_use_id = (
            prev_tool_use_id
            or _recon_tool_id(row.get("parent_uuid") or row["message_uuid"])
        )
        blocks = [{
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": raw_content,
        }]
        top_type = "user"
    elif content_type == "thinking":
        blocks = [{
            "type": "thinking",
            "thinking": raw_content,
        }]
        top_type = "assistant"
    elif content_type == "image":
        # Image bytes are not stored; we round-trip a textual stub so
        # ``--resume`` doesn't crash. A future viewer can show
        # "image not recoverable" based on the ``text-stub`` source type.
        blocks = [{
            "type": "image",
            "source": {"type": "text-stub", "data": raw_content},
        }]
        top_type = role  # usually 'user' (image attachment)
    elif content_type == "attachment":
        blocks = [{
            "type": "attachment",
            "content": [raw_content],
        }]
        top_type = "attachment"
    else:
        # Unrecognized → text fallback (preserves whatever was stored).
        blocks = [{"type": "text", "text": raw_content}]
        top_type = role  # 'user' or 'assistant'

    entry: dict = {
        "parentUuid": row.get("parent_uuid"),
        "uuid": row["message_uuid"],
        "type": top_type,
        "timestamp": ts_str,
        "sessionId": row["session_id"],
        "cwd": cwd,
        "isSidechain": False,
        "userType": "external",
        "entrypoint": "claude-vscode",
        "message": {
            "role": role,
            "content": blocks,
        },
    }
    if row.get("model"):
        entry["message"]["model"] = row["model"]
        entry["message"]["id"] = _recon_msg_id(row["message_uuid"])
        entry["message"]["type"] = "message"
    return entry


# ── Full-session reconstruction ────────────────────────────────────────────

def reconstruct_jsonl(diagnosis: Diagnosis, conn,
                       *, project_path: str) -> list[dict]:
    """Build the full set of JSONL entries (valid live + reconstructed)
    ordered by ``idx``. Live entries are kept verbatim; missing-from-live
    DB rows are reconstructed via :func:`reconstruct_entry`.

    Tool-result pairing strategy:
      1. If the live entry exists and starts with a ``tool_use`` block,
         we capture its real id as ``prev_tool_use_id`` for any
         reconstructed tool_result that follows.
      2. For reconstructed tool_use rows, we use the synthetic
         ``_recon_tool_id``.
      3. For a tool_result with no prev_tool_use_id at all, the
         per-row reconstructor falls back to
         ``_recon_tool_id(parent_uuid)`` so the field is always present.
    """
    live = diagnosis.live_path
    live_by_uuid: dict[str, tuple[int, dict]] = {}
    # Claude Code emits metadata entries (queue-operation, file-history-snapshot,
    # last-prompt, ai-title, etc.) that have no `uuid` field. They are not
    # conversation messages and never appear in the DB's `turns` table, but
    # `--resume` reads them, so dropping them would silently truncate state.
    live_no_uuid: list[tuple[int, dict]] = []

    # 1. Parse live JSONL, indexing message entries by uuid and keeping
    #    no-uuid metadata entries in a separate list.
    if live.is_file():
        line_no = 0
        with open(live, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line_no += 1
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                    continue  # corrupted, drop
                uuid = obj.get("uuid")
                if uuid:
                    live_by_uuid[uuid] = (line_no, obj)
                else:
                    live_no_uuid.append((line_no, obj))

    # 2. Pull every DB turn for the session, ordered by idx.
    # TODO(v0.7.2): LEFT JOIN tool_calls ON tool_calls.turn_id = turns.id
    # to recover the original ``toolu_xxx`` ids (spec section 5 — primary
    # pairing source). Right now we synthesize ``toolu_recon_<uuid>`` via
    # the prev_tool_use_id chain only; functional for ``--resume`` but
    # loses the real id continuity an agent would prefer to see when
    # referring back to earlier tool calls.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT idx, parent_uuid, message_uuid, role, ts, content, "
            "content_type, model "
            "FROM turns WHERE session_id = %s ORDER BY idx",
            (diagnosis.session_id,))
        db_rows = cur.fetchall()

    # 3. Walk DB rows; emit live verbatim if present, else reconstruct.
    #    Track which live uuids we've already accounted for so the
    #    "live-only" sweep below doesn't double-emit them.
    out: list[tuple[int, dict]] = []
    prev_tool_use_id: Optional[str] = None
    db_seen_uuids: set[str] = set()
    for db_row in db_rows:
        idx, parent_uuid, message_uuid, role, ts, content, content_type, \
            model = db_row
        db_seen_uuids.add(message_uuid)
        if message_uuid in live_by_uuid:
            _, live_obj = live_by_uuid[message_uuid]
            out.append((idx, live_obj))
            try:
                blocks = live_obj.get("message", {}).get("content") or []
                if blocks and isinstance(blocks, list) \
                        and isinstance(blocks[0], dict) \
                        and blocks[0].get("type") == "tool_use":
                    prev_tool_use_id = blocks[0].get("id")
            except Exception:
                pass
            continue
        row = {
            "session_id": diagnosis.session_id,
            "idx": idx,
            "parent_uuid": parent_uuid,
            "message_uuid": message_uuid,
            "role": role,
            "ts": ts,
            "content": content,
            "content_type": content_type,
            "model": model,
        }
        recon = reconstruct_entry(row, cwd=project_path,
                                    prev_tool_use_id=prev_tool_use_id)
        if (content_type or "").lower() == "tool_use":
            prev_tool_use_id = _recon_tool_id(message_uuid)
        out.append((idx, recon))

    # 4. Live-only sweep: any valid live entry whose uuid is NOT in the DB
    #    is dejavu-uningested but still valid Claude Code content (e.g. the
    #    Stop hook hadn't caught up before corruption). We MUST keep these
    #    or the repair would silently truncate the resumable transcript.
    #    Use the line_no-derived synthetic idx so sort order matches the
    #    original JSONL position relative to DB-known entries.
    for uuid, (line_no, live_obj) in live_by_uuid.items():
        if uuid in db_seen_uuids:
            continue
        synthetic_idx = line_no * 10
        out.append((synthetic_idx, live_obj))

    # 5. No-uuid metadata sweep: keep all queue-operation / file-history /
    #    last-prompt / ai-title / etc. entries verbatim at their original
    #    line position. These are session state Claude Code reads on resume.
    for line_no, live_obj in live_no_uuid:
        out.append((line_no * 10, live_obj))

    # 6. Sort by idx (real for DB-known, synthetic line_no*10 for live-only)
    #    and strip the idx tag.
    out.sort(key=lambda kv: kv[0])
    return [obj for _, obj in out]


# ── Atomic repaired write + sidecars ───────────────────────────────────────

def _default_backup_root() -> Path:
    """Resolve the off-tree backup root (kept OUTSIDE Claude Code's projects
    dir so backups don't pollute the session-history scanner).

    Order of precedence:
      1. ``DATA_ROOT`` env var → ``$DATA_ROOT/pre-repair``
      2. ``$HOME/.local/share/claude-dejavu/pre-repair``
    """
    data_root = os.environ.get("DATA_ROOT")
    if data_root:
        return Path(data_root) / "pre-repair"
    return Path.home() / ".local" / "share" / "claude-dejavu" / "pre-repair"


def _pre_recovery_snapshot(*, feature: str, target: Path,
                              data_root: Optional[Path] = None) -> None:
    """Synchronously take a `tag=pre-recovery` snapshot before any
    destructive write (spec §3.4 — v0.8.0).

    Lazy-imports session_copy + sqlite_staging so callers on pre-v0.8.0
    installs (no restic, no repo) get a clear error rather than an
    ImportError at module load.

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
    # Resolve repo + passphrase from data root.
    if data_root is None:
        env_override = os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
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


def write_repaired_jsonl(diagnosis: Diagnosis, entries: list[dict],
                          *,
                          mirror_path: Optional[Path] = None,
                          backup_root: Optional[Path] = None,
                          no_pre_recovery_snapshot: bool = False
                          ) -> tuple[Path, int]:
    """Atomically replace the live JSONL with reconstructed content.

    Side-effects:
      * **(v0.8.0)** Synchronously takes a ``tag=pre-recovery`` restic
        snapshot BEFORE any other side-effect. If the snapshot fails the
        function raises and the live file is never touched. Pass
        ``no_pre_recovery_snapshot=True`` to opt out.
      * Backs up the live file to
        ``<backup_root>/<session_id>/<ts>.jsonl`` (default backup_root is
        ``~/.local/share/claude-dejavu/pre-repair``). Backups live OUTSIDE
        ``~/.claude/projects/<encoded>/`` so Claude Code's history scanner
        doesn't see them as ghost sessions.
      * If ``mirror_path`` is given AND exists, also snapshots it into the
        same per-session backup dir as ``<ts>.mirror.jsonl``.
      * Writes ``<backup_root>/<session_id>/<ts>.meta.json`` sidecar with
        the diagnosis stats.
      * Writes ``<uuid>.dejavu-repair-fingerprint`` sidecar (alongside the
        live JSONL) so ``ingester.py`` can skip the next re-ingest
        (spec section 7.1).
      * Verifies the tmp file parses line-by-line before
        ``os.replace()``; raises ``RuntimeError`` on verification failure
        without touching the live file.

    Returns ``(backup_path, written_line_count)``.
    """
    live = diagnosis.live_path
    # v0.8.0 — synchronous pre-recovery snapshot BEFORE any write.
    if not no_pre_recovery_snapshot:
        _pre_recovery_snapshot(feature=f"repair {diagnosis.session_id}",
                                  target=live)
    ts = int(time.time())
    root = backup_root if backup_root is not None else _default_backup_root()
    backup_dir = root / diagnosis.session_id
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{ts}.jsonl"
    meta = backup_dir / f"{ts}.meta.json"
    tmp = live.with_suffix(".jsonl.repair.tmp")
    fingerprint = live.parent / f"{live.stem}.dejavu-repair-fingerprint"

    # 1. Backup live (cp, not rename — original stays until os.replace)
    if live.is_file():
        backup.write_bytes(live.read_bytes())

    # 2. Optional: snapshot off-tree mirror into the same per-session dir
    if mirror_path is not None:
        try:
            mp = Path(mirror_path)
            if mp.is_file():
                mirror_backup = backup_dir / f"{ts}.mirror.jsonl"
                mirror_backup.write_bytes(mp.read_bytes())
        except OSError:
            # Mirror snapshot is best-effort — never block the repair.
            pass

    # 3. Write tmp
    with open(tmp, "w", encoding="utf-8") as f:
        for obj in entries:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    # 4. Verify: re-parse line by line
    bad = 0
    with open(tmp, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            try:
                json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                bad += 1
    if bad:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise RuntimeError(
            f"repair verify failed: {bad} parse errors in tmp output")

    # 5. Atomic replace
    os.replace(tmp, live)

    # 6. Sidecar metadata
    meta.write_text(
        json.dumps({
            "session_id": diagnosis.session_id,
            "repaired_at": ts,
            "db_turn_count": diagnosis.db_turn_count,
            "db_max_idx": diagnosis.db_max_idx,
            "live_line_count_before":
                diagnosis.valid_lines + diagnosis.corrupted_lines,
            "parse_fail_lines_before": diagnosis.corrupted_lines,
            "reconstructed_count": len(entries),
            "source": "dejavu-db",
        }, indent=2) + "\n",
        encoding="utf-8")

    # 7. Re-ingest fingerprint (spec section 7.1)
    fingerprint.write_text(
        json.dumps({
            "session_id": diagnosis.session_id,
            "repaired_at": ts,
            "source": "dejavu-db",
        }) + "\n",
        encoding="utf-8")

    return backup, len(entries)
