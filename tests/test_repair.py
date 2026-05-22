"""Unit tests for code/repair.py detection + reconstruction primitives.
Lives outside any live PG/Weaviate dependency for unit-level scope.

F1 scope: pure-Python detection + reconstruction. The DB cursor is faked
via a tiny stand-in class so we can run on machines without a live PG.
"""
from __future__ import annotations
import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(m): PASS.append(m); print(f"  \033[32m✓\033[0m {m}", flush=True)
def _f(m, d=""): FAIL.append((m, d)); print(f"  \033[31m✗\033[0m {m}"); d and print(f"      {d}")
def _step(n): print(f"\n\033[36m── {n} ──\033[0m", flush=True)
def _aT(n, c, d=""): _ok(n) if c else _f(n, d)


FIXTURES = ROOT / "tests" / "fixtures" / "repair"


# ── Fake psycopg2 cursor/conn for unit-scope DB tests ──────────────────────

class _FakeCursor:
    """Tiny cursor stand-in. `responses` is a dict mapping the first 30
    chars of an executed SQL string to a fetch result (single row for
    fetchone, list-of-rows for fetchall)."""
    def __init__(self, responses: dict):
        self.responses = responses
        self._last_key = None
        self._last_is_count = False

    def execute(self, sql, params=None):
        norm = " ".join(sql.split()).strip()
        # Crude classifier: 'SELECT count(*)' → fetchone; 'SELECT idx,...' → fetchall.
        if norm.lower().startswith("select count"):
            self._last_is_count = True
            self._last_key = "count"
        else:
            self._last_is_count = False
            self._last_key = "rows"

    def fetchone(self):
        return self.responses.get(self._last_key, (0, 0))

    def fetchall(self):
        return self.responses.get(self._last_key, [])

    def close(self):
        pass


class _FakeConn:
    def __init__(self, responses: dict):
        self.responses = responses

    @contextmanager
    def cursor(self):
        cur = _FakeCursor(self.responses)
        try:
            yield cur
        finally:
            cur.close()


# ── count_parse_failures ──────────────────────────────────────────────────

def test_count_parse_failures_clean_file():
    _step("count_parse_failures: clean file → 0 bad")
    from repair import count_parse_failures
    valid, bad = count_parse_failures(FIXTURES / "clean.jsonl")
    _aT("3 valid, 0 bad", valid == 3 and bad == 0, f"v={valid} b={bad}")


def test_count_parse_failures_corrupted_file():
    _step("count_parse_failures: corrupted file → 5 valid + 3 bad")
    from repair import count_parse_failures
    valid, bad = count_parse_failures(FIXTURES / "corrupted.jsonl")
    _aT("5 valid, 3 bad",
        valid == 5 and bad == 3, f"v={valid} b={bad}")


def test_count_parse_failures_mid_corrupt():
    _step("count_parse_failures: mid_corrupt → keeps counting past bad line")
    from repair import count_parse_failures
    valid, bad = count_parse_failures(FIXTURES / "mid_corrupt.jsonl")
    _aT("5 valid, 1 bad",
        valid == 5 and bad == 1, f"v={valid} b={bad}")


def test_count_parse_failures_skips_blank_lines():
    _step("count_parse_failures: skips blank/whitespace-only lines")
    from repair import count_parse_failures
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "blanks.jsonl"
        p.write_text(
            '{"uuid":"a","type":"user"}\n'
            "\n"
            "   \n"
            '{"uuid":"b","type":"user"}\n'
            "\n\n",
            encoding="utf-8")
        valid, bad = count_parse_failures(p)
    _aT("2 valid, 0 bad (blanks ignored)",
        valid == 2 and bad == 0, f"v={valid} b={bad}")


# ── Diagnosis flags ────────────────────────────────────────────────────────

def test_diagnose_thresholds():
    _step("Diagnosis.truncated uses db_turn_count × 0.95 (not db_max_idx)")
    from repair import Diagnosis
    # 95 valid lines out of 100 turns → still truncated (< 0.95*100=95 is False; need strict <)
    # at exactly 95 (the threshold), it's NOT truncated (5% gap is the boundary)
    d_borderline = Diagnosis(session_id="x", live_path=Path("/tmp/x"),
                              valid_lines=95, corrupted_lines=0,
                              db_turn_count=100, db_max_idx=5000)
    _aT("95/100 → NOT truncated (at threshold)",
        not d_borderline.truncated, str(d_borderline))

    # 94 valid out of 100 → truncated
    d_under = Diagnosis(session_id="x", live_path=Path("/tmp/x"),
                         valid_lines=94, corrupted_lines=0,
                         db_turn_count=100, db_max_idx=5000)
    _aT("94/100 → truncated", d_under.truncated, str(d_under))

    # 8495 vs 1044 db turns (the historical case, inverted): live way bigger
    # → NOT truncated.
    d_inverse = Diagnosis(session_id="x", live_path=Path("/tmp/x"),
                           valid_lines=8495, corrupted_lines=0,
                           db_turn_count=1044, db_max_idx=29672)
    _aT("8495 live vs 1044 db_turn_count → NOT truncated "
         "(comparator is db_turn_count, NOT db_max_idx)",
         not d_inverse.truncated, str(d_inverse))

    # Edge case: db_turn_count = 0 → not truncated (nothing to compare).
    d_zero = Diagnosis(session_id="x", live_path=Path("/tmp/x"),
                        valid_lines=10, corrupted_lines=0,
                        db_turn_count=0, db_max_idx=0)
    _aT("db_turn_count=0 → NOT truncated (no DB rows to compare)",
         not d_zero.truncated, str(d_zero))

    # Healthy derived property
    d_clean = Diagnosis(session_id="x", live_path=Path("/tmp/x"),
                         valid_lines=100, corrupted_lines=0,
                         db_turn_count=100, db_max_idx=5000)
    _aT("100/100, 0 corrupt → healthy", d_clean.healthy, str(d_clean))


def test_diagnose_corrupted_flag():
    _step("Diagnosis.corrupted is True when corrupted_lines >= 1")
    from repair import Diagnosis
    d = Diagnosis(session_id="x", live_path=Path("/tmp/x"),
                   valid_lines=100, corrupted_lines=1,
                   db_turn_count=100, db_max_idx=5000)
    _aT("1 corrupt line → corrupted=True", d.corrupted)
    _aT("not healthy when corrupted", not d.healthy)


def test_diagnose_with_fake_conn():
    _step("diagnose: integrates count_parse_failures + fake DB query")
    from repair import diagnose
    conn = _FakeConn({"count": (100, 5000)})
    d = diagnose("sess-1", FIXTURES / "clean.jsonl", conn)
    _aT("valid_lines from JSONL", d.valid_lines == 3, f"got {d.valid_lines}")
    _aT("corrupted_lines from JSONL", d.corrupted_lines == 0)
    _aT("db_turn_count from query", d.db_turn_count == 100)
    _aT("db_max_idx from query", d.db_max_idx == 5000)
    _aT("flagged truncated (3 << 100*0.95)", d.truncated)


# ── reconstruct_entry per content_type ─────────────────────────────────────

def test_reconstruct_text_entry():
    _step("reconstruct_entry: text content_type → text block")
    from repair import reconstruct_entry
    row = {
        "session_id": "sess-1", "idx": 100,
        "parent_uuid": "p-1", "message_uuid": "m-1",
        "role": "user",
        "ts": "2026-05-08T13:00:00.000000+00:00",
        "content": "hello world",
        "content_type": "text",
        "model": None,
    }
    cwd = "/tmp/test"
    entry = reconstruct_entry(row, cwd=cwd)
    _aT("type=user", entry["type"] == "user", str(entry))
    _aT("uuid matches", entry["uuid"] == "m-1")
    _aT("sessionId matches", entry["sessionId"] == "sess-1")
    _aT("cwd matches", entry["cwd"] == cwd)
    _aT("message.role=user", entry["message"]["role"] == "user")
    blocks = entry["message"]["content"]
    _aT("text block present",
         isinstance(blocks, list)
         and blocks[0]["type"] == "text"
         and blocks[0]["text"] == "hello world",
         str(blocks))


def test_reconstruct_tool_use_entry():
    _step("reconstruct_entry: tool_use → tool_use block with toolu_recon_ id")
    from repair import reconstruct_entry
    row = {
        "session_id": "sess-1", "idx": 101, "parent_uuid": "m-1",
        "message_uuid": "m-2", "role": "assistant",
        "ts": "2026-05-08T13:00:01.000000+00:00",
        "content": '{"name": "Bash", "input": {"command": "ls"}}',
        "content_type": "tool_use",
        "model": "claude-opus-4-7",
    }
    entry = reconstruct_entry(row, cwd="/tmp/test")
    blocks = entry["message"]["content"]
    _aT("tool_use block",
         blocks[0]["type"] == "tool_use", str(blocks))
    _aT("synthesized id has toolu_recon_ prefix",
         blocks[0]["id"].startswith("toolu_recon_"), blocks[0].get("id"))
    _aT("name preserved", blocks[0]["name"] == "Bash")
    _aT("input preserved",
         blocks[0]["input"] == {"command": "ls"}, str(blocks[0]["input"]))
    _aT("top-level type=assistant", entry["type"] == "assistant")


def test_reconstruct_tool_result_entry():
    _step("reconstruct_entry: tool_result with paired prev_tool_use_id")
    from repair import reconstruct_entry
    row = {
        "session_id": "sess-1", "idx": 102, "parent_uuid": "m-2",
        "message_uuid": "m-3", "role": "user",
        "ts": "2026-05-08T13:00:02.000000+00:00",
        "content": "file1\nfile2",
        "content_type": "tool_result",
        "model": None,
    }
    entry = reconstruct_entry(row, cwd="/tmp/test",
                               prev_tool_use_id="toolu_recon_m-2xxxxxxx")
    blocks = entry["message"]["content"]
    _aT("tool_result block", blocks[0]["type"] == "tool_result", str(blocks))
    _aT("tool_use_id paired",
         blocks[0]["tool_use_id"] == "toolu_recon_m-2xxxxxxx", str(blocks[0]))
    _aT("content preserved",
         blocks[0]["content"] == "file1\nfile2", str(blocks[0]))


def test_reconstruct_thinking_entry():
    _step("reconstruct_entry: thinking content_type → thinking block")
    from repair import reconstruct_entry
    row = {
        "session_id": "sess-1", "idx": 103, "parent_uuid": "m-3",
        "message_uuid": "m-4", "role": "assistant",
        "ts": "2026-05-08T13:00:03.000000+00:00",
        "content": "Let me think about this carefully...",
        "content_type": "thinking",
        "model": "claude-opus-4-7",
    }
    entry = reconstruct_entry(row, cwd="/tmp/test")
    blocks = entry["message"]["content"]
    _aT("thinking block", blocks[0]["type"] == "thinking", str(blocks))
    _aT("thinking text preserved",
         blocks[0]["thinking"] == "Let me think about this carefully...",
         str(blocks[0]))
    _aT("top-level type=assistant", entry["type"] == "assistant")


def test_reconstruct_image_entry():
    _step("reconstruct_entry: image content_type → image block w/ text-stub")
    from repair import reconstruct_entry
    row = {
        "session_id": "sess-1", "idx": 104, "parent_uuid": "m-4",
        "message_uuid": "m-5", "role": "user",
        "ts": "2026-05-08T13:00:04.000000+00:00",
        "content": "(image bytes not recoverable: PNG 1024x768)",
        "content_type": "image",
        "model": None,
    }
    entry = reconstruct_entry(row, cwd="/tmp/test")
    blocks = entry["message"]["content"]
    _aT("image block", blocks[0]["type"] == "image", str(blocks))
    src = blocks[0].get("source", {})
    _aT("text-stub source type",
         src.get("type") == "text-stub", str(src))
    _aT("placeholder data preserved",
         "image bytes not recoverable" in (src.get("data") or ""), str(src))


def test_reconstruct_fallback_unknown_type():
    _step("reconstruct_entry: unrecognized content_type → text fallback")
    from repair import reconstruct_entry
    row = {
        "session_id": "sess-1", "idx": 105, "parent_uuid": "m-5",
        "message_uuid": "m-6", "role": "assistant",
        "ts": "2026-05-08T13:00:05.000000+00:00",
        "content": "raw content",
        "content_type": "some_future_type",
        "model": "claude",
    }
    entry = reconstruct_entry(row, cwd="/tmp/test")
    blocks = entry["message"]["content"]
    _aT("fallback → text block",
         blocks[0]["type"] == "text" and blocks[0]["text"] == "raw content",
         str(blocks))


def test_recon_id_length_guard():
    _step("_recon_msg_id / _recon_tool_id length guard for short uuids")
    from repair import _recon_msg_id, _recon_tool_id
    # 3-char uuid (synthetic test id) shouldn't crash.
    mid = _recon_msg_id("abc")
    tid = _recon_tool_id("abc")
    _aT("msg id prefix",
         mid.startswith("msg_recon_"), mid)
    _aT("msg id padded to >= 8-char suffix",
         len(mid) >= len("msg_recon_") + 8, mid)
    _aT("tool id prefix",
         tid.startswith("toolu_recon_"), tid)
    _aT("tool id padded to >= 8-char suffix",
         len(tid) >= len("toolu_recon_") + 8, tid)
    # Empty / None inputs shouldn't blow up
    mid2 = _recon_msg_id("")
    _aT("empty uuid handled", mid2.startswith("msg_recon_"), mid2)


# ── reconstruct_jsonl integration (with fake conn) ─────────────────────────

def test_reconstruct_jsonl_merges_live_and_db():
    _step("reconstruct_jsonl: live valid + DB rows merge in idx order")
    from repair import Diagnosis, reconstruct_jsonl
    # Live has uuid 'a' and 'b'. DB has rows at idx 1, 2, 3, 4 where 1+2
    # correspond to live a,b, and 3+4 are missing (reconstructed).
    rows = [
        (10, None, "a", "user", "2026-01-01T00:00:00Z", "hi", "text", None),
        (20, "a", "b", "assistant", "2026-01-01T00:00:01Z", "hello", "text",
         "claude"),
        (30, "b", "x", "user", "2026-01-01T00:00:02Z", "missing-1", "text",
         None),
        (40, "x", "y", "assistant", "2026-01-01T00:00:03Z", "missing-2",
         "text", "claude"),
    ]
    conn = _FakeConn({"rows": rows})
    d = Diagnosis(session_id="s1", live_path=FIXTURES / "clean.jsonl",
                  valid_lines=3, corrupted_lines=0, db_turn_count=4,
                  db_max_idx=40)
    out = reconstruct_jsonl(d, conn, project_path="/tmp/proj")
    # Live has a/b/c (3 lines); DB has a/b/x/y (4 rows). Merged output:
    # a + b kept from live, x + y reconstructed from DB, c preserved as
    # live-only (its uuid isn't in any DB row).
    uuids = set(e.get("uuid") for e in out)
    _aT("5 entries total (3 live + 2 DB-only)", len(out) == 5, f"got {len(out)}")
    _aT("union of all uuids present",
         uuids == {"a", "b", "c", "x", "y"}, str(sorted(uuids)))
    # The two reconstructed entries (x, y) should have cwd from project_path.
    cwd_by_uuid = {e["uuid"]: e.get("cwd") for e in out}
    _aT("reconstructed entries have project cwd",
         cwd_by_uuid["x"] == "/tmp/proj" and cwd_by_uuid["y"] == "/tmp/proj",
         f"cwds={cwd_by_uuid}")


def test_reconstruct_jsonl_preserves_uningested_live_entries():
    _step("reconstruct_jsonl: keeps live entries not yet in DB (regression: data loss)")
    from repair import Diagnosis, reconstruct_jsonl
    # Simulate the historical case: live JSONL has many valid lines (here 3),
    # DB has just 1 ingested turn. Without the live-only sweep, output would
    # be 1 entry (data loss). With the fix, output is 3.
    rows = [
        (10, None, "a", "user", "2026-01-01T00:00:00Z", "hi", "text", None),
    ]
    conn = _FakeConn({"rows": rows})
    # clean.jsonl has 3 valid lines (uuids "a", "b", "c"). DB only knows "a".
    d = Diagnosis(session_id="s1", live_path=FIXTURES / "clean.jsonl",
                  valid_lines=3, corrupted_lines=0, db_turn_count=1,
                  db_max_idx=10)
    out = reconstruct_jsonl(d, conn, project_path="/tmp/proj")
    uuids = [e.get("uuid") for e in out]
    _aT("3 entries kept (1 DB + 2 live-only)",
         len(out) == 3, f"got {len(out)}: {uuids}")
    _aT("contains a, b, c in original line order",
         uuids == ["a", "b", "c"], str(uuids))


def test_reconstruct_jsonl_preserves_no_uuid_metadata():
    _step("reconstruct_jsonl: keeps queue-operation / ai-title / file-history-snapshot lines")
    from repair import Diagnosis, reconstruct_jsonl
    # Build a temp JSONL with mixed message + no-uuid metadata lines.
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as tf:
        tf.write('{"type":"queue-operation","operation":"enqueue","sessionId":"s1"}\n')
        tf.write('{"parentUuid":null,"uuid":"a","type":"user","timestamp":"2026-01-01T00:00:00Z","message":{"role":"user","content":"hi"},"sessionId":"s1","cwd":"/t"}\n')
        tf.write('{"type":"ai-title","sessionId":"s1","aiTitle":"Test"}\n')
        tf.write('{"type":"file-history-snapshot","messageId":"m1","snapshot":{}}\n')
        tmp_path = Path(tf.name)
    try:
        rows = [
            (10, None, "a", "user", "2026-01-01T00:00:00Z", "hi", "text", None),
        ]
        conn = _FakeConn({"rows": rows})
        d = Diagnosis(session_id="s1", live_path=tmp_path,
                      valid_lines=4, corrupted_lines=0, db_turn_count=1,
                      db_max_idx=10)
        out = reconstruct_jsonl(d, conn, project_path="/tmp/proj")
        types = [e.get("type") for e in out]
        _aT("4 entries total (1 msg + 3 metadata)",
             len(out) == 4, f"got {len(out)}: {types}")
        _aT("queue-operation preserved",
             any(e.get("type") == "queue-operation" for e in out), str(types))
        _aT("ai-title preserved",
             any(e.get("type") == "ai-title" for e in out), str(types))
        _aT("file-history-snapshot preserved",
             any(e.get("type") == "file-history-snapshot" for e in out),
             str(types))
    finally:
        tmp_path.unlink(missing_ok=True)


def test_reconstruct_jsonl_tool_pairing_from_tool_calls():
    _step("reconstruct_jsonl: tool_result pairs with prev tool_use's recon id")
    from repair import Diagnosis, reconstruct_jsonl, _recon_tool_id
    rows = [
        # tool_use first, then tool_result — both reconstructed (no live).
        (10, None, "tu1", "assistant", "2026-01-01T00:00:00Z",
         '{"name": "Bash", "input": {"command": "ls"}}', "tool_use", "claude"),
        (20, "tu1", "tr1", "user", "2026-01-01T00:00:01Z",
         "file1\nfile2", "tool_result", None),
    ]
    conn = _FakeConn({"rows": rows})
    # live_path doesn't exist so live_by_uuid stays empty.
    d = Diagnosis(session_id="s1", live_path=Path("/tmp/nonexistent.jsonl"),
                  valid_lines=0, corrupted_lines=0, db_turn_count=2,
                  db_max_idx=20)
    out = reconstruct_jsonl(d, conn, project_path="/tmp/proj")
    _aT("2 entries", len(out) == 2)
    tool_use_block = out[0]["message"]["content"][0]
    tool_result_block = out[1]["message"]["content"][0]
    _aT("first is tool_use",
         tool_use_block["type"] == "tool_use", str(tool_use_block))
    _aT("second is tool_result",
         tool_result_block["type"] == "tool_result", str(tool_result_block))
    _aT("tool_result paired with tool_use's recon id",
         tool_result_block["tool_use_id"] == _recon_tool_id("tu1"),
         f"got {tool_result_block.get('tool_use_id')} "
         f"expected {_recon_tool_id('tu1')}")


# ── write_repaired_jsonl side effects ──────────────────────────────────────

def _make_simple_entries():
    return [
        {"parentUuid": None, "uuid": "a", "type": "user",
         "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "hi"},
         "sessionId": "s1", "cwd": "/t"},
        {"parentUuid": "a", "uuid": "b", "type": "assistant",
         "timestamp": "2026-01-01T00:00:01Z",
         "message": {"role": "assistant",
                      "content": [{"type": "text", "text": "hello"}]},
         "sessionId": "s1", "cwd": "/t"},
    ]


def test_write_repaired_writes_fingerprint():
    _step("write_repaired_jsonl: writes .dejavu-repair-fingerprint sidecar")
    from repair import Diagnosis, write_repaired_jsonl
    with tempfile.TemporaryDirectory() as tmp:
        live = Path(tmp) / "s1.jsonl"
        live.write_text("old content\n", encoding="utf-8")
        backup_root = Path(tmp) / "pre-repair-root"
        d = Diagnosis(session_id="s1", live_path=live,
                       valid_lines=1, corrupted_lines=0,
                       db_turn_count=2, db_max_idx=20)
        entries = _make_simple_entries()
        # v0.8.0: bypass the pre-recovery snapshot — this test only
        # exercises the post-snapshot side-effects.
        backup, n = write_repaired_jsonl(d, entries, backup_root=backup_root,
                                            no_pre_recovery_snapshot=True)
        fingerprint = live.parent / f"{live.stem}.dejavu-repair-fingerprint"
        _aT("fingerprint exists", fingerprint.is_file(),
             f"expected {fingerprint}")
        meta = json.loads(fingerprint.read_text(encoding="utf-8"))
        _aT("session_id in fingerprint",
             meta.get("session_id") == "s1", str(meta))
        _aT("repaired_at is int",
             isinstance(meta.get("repaired_at"), int), str(meta))
        _aT("count returned",
             n == 2, f"n={n}")
        _aT("backup exists", backup.is_file(), str(backup))
        # Critical: backup must live OUTSIDE the live JSONL's directory
        # (else Claude Code's history scanner sees ghost sessions).
        _aT("backup is NOT alongside live JSONL",
             backup.parent != live.parent,
             f"backup_dir={backup.parent} live_dir={live.parent}")
        _aT("backup is under backup_root/<session_id>",
             backup.parent == backup_root / "s1",
             f"expected {backup_root / 's1'} got {backup.parent}")


def test_write_repaired_writes_meta_sidecar():
    _step("write_repaired_jsonl: writes <ts>.meta.json sidecar in backup dir")
    from repair import Diagnosis, write_repaired_jsonl
    with tempfile.TemporaryDirectory() as tmp:
        live = Path(tmp) / "s2.jsonl"
        live.write_text("old\n", encoding="utf-8")
        backup_root = Path(tmp) / "pre-repair-root"
        d = Diagnosis(session_id="s2", live_path=live,
                       valid_lines=1, corrupted_lines=2,
                       db_turn_count=5, db_max_idx=50)
        entries = _make_simple_entries()
        write_repaired_jsonl(d, entries, backup_root=backup_root,
                                no_pre_recovery_snapshot=True)
        # find the meta.json sidecar — now lives in backup_root/<session>/
        metas = list((backup_root / "s2").glob("*.meta.json"))
        _aT("exactly one meta sidecar in per-session backup dir",
             len(metas) == 1, f"got {[m.name for m in metas]}")
        if metas:
            data = json.loads(metas[0].read_text(encoding="utf-8"))
            for key in ("session_id", "repaired_at", "db_turn_count",
                         "db_max_idx", "parse_fail_lines_before",
                         "reconstructed_count", "source"):
                _aT(f"meta contains '{key}'", key in data, str(data))
        # Confirm NO sidecar leaked into the live JSONL's directory
        leaked = list(Path(tmp).glob("s2.pre-repair.*.meta.json"))
        _aT("no meta sidecar leaked into live dir",
             len(leaked) == 0, f"leaked: {[l.name for l in leaked]}")


def test_write_repaired_default_backup_root_off_tree():
    _step("write_repaired_jsonl: default backup_root is OFF Claude Code's projects dir")
    from repair import _default_backup_root
    # Default must live under ~/.local/share/claude-dejavu (or $DATA_ROOT),
    # NEVER under ~/.claude/projects/. This is the v0.7.1 fix that
    # prevents ghost sessions in Claude Code's history UI.
    root = _default_backup_root()
    _aT("default root NOT under ~/.claude/projects",
         ".claude/projects" not in str(root.resolve()),
         f"got {root}")
    _aT("default root has 'pre-repair' segment",
         root.name == "pre-repair", f"got {root.name}")


def test_write_repaired_atomic_replace():
    _step("write_repaired_jsonl: atomic os.replace, no .repair.tmp leftover")
    from repair import Diagnosis, write_repaired_jsonl
    with tempfile.TemporaryDirectory() as tmp:
        live = Path(tmp) / "s3.jsonl"
        live.write_text("original\n", encoding="utf-8")
        backup_root = Path(tmp) / "pre-repair-root"
        d = Diagnosis(session_id="s3", live_path=live,
                       valid_lines=1, corrupted_lines=0,
                       db_turn_count=2, db_max_idx=20)
        entries = _make_simple_entries()
        write_repaired_jsonl(d, entries, backup_root=backup_root,
                                no_pre_recovery_snapshot=True)
        # Original now holds new content
        new = live.read_text(encoding="utf-8")
        _aT("original path has reconstructed content",
             '"uuid": "a"' in new and '"uuid": "b"' in new,
             new[:200])
        # No tmp leftover
        tmp_leftover = live.parent / "s3.jsonl.repair.tmp"
        _aT("no .repair.tmp leftover",
             not tmp_leftover.exists(),
             f"tmp still present: {tmp_leftover}")
        # All lines parse as JSON
        parse_ok = True
        for line in new.splitlines():
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError:
                parse_ok = False; break
        _aT("all written lines parse", parse_ok)


def main() -> int:
    print("claude-dejavu v0.7.1 repair primitive tests")
    print(f"  ROOT={ROOT}")
    tests = [
        test_count_parse_failures_clean_file,
        test_count_parse_failures_corrupted_file,
        test_count_parse_failures_mid_corrupt,
        test_count_parse_failures_skips_blank_lines,
        test_diagnose_thresholds,
        test_diagnose_corrupted_flag,
        test_diagnose_with_fake_conn,
        test_reconstruct_text_entry,
        test_reconstruct_tool_use_entry,
        test_reconstruct_tool_result_entry,
        test_reconstruct_thinking_entry,
        test_reconstruct_image_entry,
        test_reconstruct_fallback_unknown_type,
        test_recon_id_length_guard,
        test_reconstruct_jsonl_merges_live_and_db,
        test_reconstruct_jsonl_preserves_uningested_live_entries,
        test_reconstruct_jsonl_preserves_no_uuid_metadata,
        test_reconstruct_jsonl_tool_pairing_from_tool_calls,
        test_write_repaired_writes_fingerprint,
        test_write_repaired_writes_meta_sidecar,
        test_write_repaired_default_backup_root_off_tree,
        test_write_repaired_atomic_replace,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            import traceback
            traceback.print_exc()
            FAIL.append((t.__name__, str(e)))
    print()
    print("=" * 60)
    print(f"PASS: {len(PASS)}  FAIL: {len(FAIL)}")
    for m, d in FAIL:
        print(f"  ✗ {m}  {d[:80]}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
