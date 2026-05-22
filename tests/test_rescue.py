"""Unit tests for the v0.8.2 rescue-shard Python orchestrator.

We mock the Go binary, Weaviate REST endpoints, and the PG query path,
so the tests run without docker / weaviate / postgres / Go available.
The Go binary itself has its own end-to-end smoke test in
``test_rescue_go_binary.py``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

import rescue  # type: ignore[import]


PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(msg): PASS.append(msg); print(f"  \033[32m✓\033[0m {msg}", flush=True)
def _fail_(msg, d=""):
    FAIL.append((msg, d)); print(f"  \033[31m✗\033[0m {msg}")
    d and print(f"      {d}")
def _step(name): print(f"\n\033[36m── {name} ──\033[0m", flush=True)
def _aT(name, cond, d=""): _ok(name) if cond else _fail_(name, d)


def _make_record(uuid: str, *, plausible: bool = True,
                  tombstone: bool = False,
                  class_name: str = "ClaudeDejavuTurn") -> dict:
    return {
        "uuid": uuid,
        "doc_id": 42,
        "class_name": class_name,
        "vector_len": 4 if plausible else 0,
        "vector": [0.1, 0.2, 0.3, 0.4] if plausible else None,
        "properties": ({"session_id": "s1", "turn_id": 1,
                            "content": "hi"} if plausible else None),
        "tombstone": tombstone,
        "primary_key_hex": "deadbeef",
        "source_segment": "segment-1.db",
        "byte_offset": 16,
        "plausible": plausible,
    }


# ─── Phase 1: dry-run guards ───────────────────────────────────────────


def test_rescue_dry_run_no_changes():
    _step("--dry-run skips DELETE, batch-insert, and gap-reindex")
    fake_walk = rescue.WalkResult(
        records=[_make_record("aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"),
                 _make_record("bbbbbbbb-2222-2222-2222-bbbbbbbbbbbb")],
        summary={"segments": 1, "records": 2, "plausible": 2,
                  "tombstones": 0},
    )
    deleted_called = {"count": 0}
    recreated_called = {"count": 0}
    restored_called = {"count": 0}
    gap_called = {"count": 0}

    def _stage(*a, **kw):
        return Path("/tmp/fake-stage"), None

    def _walk(*a, **kw):
        return fake_walk

    def _delete(*a, **kw):
        deleted_called["count"] += 1
        return True

    def _recreate(*a, **kw):
        recreated_called["count"] += 1
        return {"ok": True}

    def _restore(*a, **kw):
        restored_called["count"] += 1
        return {"sent": 0, "succeeded": 0, "failed": []}

    def _gap(*a, **kw):
        gap_called["count"] += 1
        return {}

    with patch.object(rescue, "stage_shard", _stage), \
            patch.object(rescue, "walk_shard", _walk), \
            patch.object(rescue, "discover_shards", lambda *a, **kw: ["shardA"]), \
            patch.object(rescue, "delete_class", _delete), \
            patch.object(rescue, "recreate_class", _recreate), \
            patch.object(rescue, "restore_records", _restore), \
            patch.object(rescue, "gap_reindex", _gap):
        plan = rescue.rescue_shard("ClaudeDejavuTurn", dry_run=True,
                                          docker_container="X")

    _aT("delete_class not called in dry-run",
         deleted_called["count"] == 0,
         f"called {deleted_called['count']} times")
    _aT("recreate_class not called",
         recreated_called["count"] == 0)
    _aT("restore_records not called",
         restored_called["count"] == 0)
    _aT("gap_reindex not called",
         gap_called["count"] == 0)
    _aT("dry-run reports records",
         plan.walk["shardA"]["record_count"] == 2)
    _aT("dry-run sets restore.skipped",
         plan.restore.get("skipped") == "dry-run")
    _aT("dry-run reports would-send count",
         plan.restore.get("would_send") == 2)


# ─── Phase 4: gap-reindex against PG canonical ─────────────────────────


def test_rescue_gap_reindex_uses_pg_canonical():
    _step("gap_reindex finds UUIDs in PG `turns` that weren't restored")
    import uuid as _uuid

    sid = "11111111-1111-1111-1111-111111111111"

    def _wv_id(s, t):
        return str(_uuid.uuid5(_uuid.NAMESPACE_URL,
                                f"claudeturn:{s}:{t}"))

    expected_uuids = [_wv_id(sid, 1), _wv_id(sid, 2), _wv_id(sid, 3)]

    fake_cursor = MagicMock()
    fake_cursor.__enter__ = MagicMock(return_value=fake_cursor)
    fake_cursor.__exit__ = MagicMock(return_value=False)
    fake_cursor.fetchall.return_value = [(sid, 1), (sid, 2), (sid, 3)]
    # Track execute() calls so we can assert the SQL uses real columns.
    # Regression: 2026-05-16 ff24f9f shipped with SELECT session_id, turn_id
    # — there is no `turn_id` column on `turns` (the per-row PK is `id`).
    # The live rescue against the canary caught it in Phase 4.
    executed_sql: list = []
    _orig_execute = fake_cursor.execute
    def _spy_execute(sql, *args, **kwargs):
        executed_sql.append(sql)
        return _orig_execute(sql, *args, **kwargs)
    fake_cursor.execute = _spy_execute
    fake_conn = MagicMock()
    fake_conn.cursor.return_value = fake_cursor

    # Restored only the first two — third should be flagged as missing.
    restored = set(expected_uuids[:2])

    ingester_calls = {"args": None}

    class _CR:
        returncode = 0
        stderr = ""
        stdout = ""

    def _fake_run(cmd, **kw):
        ingester_calls["args"] = cmd
        return _CR()

    with patch("subprocess.run", _fake_run):
        out = rescue.gap_reindex(restored, class_name="ClaudeDejavuTurn",
                                       pg_conn=fake_conn)

    _aT("expected_in_pg == 3",
         out["expected_in_pg"] == 3,
         f"got {out['expected_in_pg']}")
    _aT("missing list contains 1 uuid",
         len(out["missing"]) == 1,
         f"got {out['missing']!r}")
    _aT("missing uuid is the third one",
         expected_uuids[2] in out["missing"])
    _aT("ingester invoked when missing > 0",
         ingester_calls["args"] is not None
         and any("ingester.py" in str(a)
                  for a in ingester_calls["args"]))
    # Regression assertions on the SQL itself — column name must exist
    # on `turns` (per ingester.py:215-216 + schema). Catches the
    # 2026-05-16 ff24f9f bug.
    _aT("PG SELECT actually executed",
         len(executed_sql) >= 1, f"got {executed_sql!r}")
    sql_blob = " ".join(s.lower() for s in executed_sql)
    _aT("SQL does NOT reference non-existent column turn_id",
         "turn_id" not in sql_blob,
         f"SQL leaked turn_id: {executed_sql!r}")
    _aT("SQL selects (session_id, id) from turns",
         "select session_id" in sql_blob and "from turns" in sql_blob
         and ", id " in sql_blob.replace("select ", "select  "),
         f"SQL didn't match expected shape: {executed_sql!r}")


def test_rescue_gap_reindex_no_op_when_all_restored():
    _step("gap_reindex is a no-op when nothing is missing")
    import uuid as _uuid
    sid = "22222222-2222-2222-2222-222222222222"

    def _wv_id(s, t):
        return str(_uuid.uuid5(_uuid.NAMESPACE_URL,
                                f"claudeturn:{s}:{t}"))

    uuids = [_wv_id(sid, 1), _wv_id(sid, 2)]
    fake_cursor = MagicMock()
    fake_cursor.__enter__ = MagicMock(return_value=fake_cursor)
    fake_cursor.__exit__ = MagicMock(return_value=False)
    fake_cursor.fetchall.return_value = [(sid, 1), (sid, 2)]
    fake_conn = MagicMock()
    fake_conn.cursor.return_value = fake_cursor

    called = {"n": 0}

    def _fake_run(cmd, **kw):
        called["n"] += 1
        class CR:
            returncode = 0
            stderr = ""
            stdout = ""
        return CR()

    with patch("subprocess.run", _fake_run):
        out = rescue.gap_reindex(set(uuids),
                                       class_name="ClaudeDejavuTurn",
                                       pg_conn=fake_conn)
    _aT("missing is empty",
         out["missing"] == [])
    _aT("ingester NOT invoked",
         called["n"] == 0)


# ─── Phase 3: tombstones must NOT be re-inserted ───────────────────────


def test_rescue_honors_tombstones():
    _step("Records flagged tombstone are skipped during restore")
    sent_payloads = []

    def _fake_http(method, url, *, body=None, timeout=None):
        if method == "POST" and "/v1/batch/objects" in url:
            sent_payloads.append(json.loads(body.decode()))
            n = len(sent_payloads[-1]["objects"])
            return [{"result": {"status": "SUCCESS"}} for _ in range(n)]
        return {}

    records = [
        _make_record("11111111-aaaa-aaaa-aaaa-111111111111",
                       tombstone=False),
        _make_record("22222222-bbbb-bbbb-bbbb-222222222222",
                       tombstone=True),  # MUST be skipped
        _make_record("33333333-cccc-cccc-cccc-333333333333",
                       tombstone=False),
    ]

    with patch.object(rescue, "_http", _fake_http):
        out = rescue.restore_records("http://x", "C", records,
                                            batch_size=10)

    _aT("only 2 records sent (tombstone skipped)",
         out["sent"] == 2,
         f"sent={out['sent']}")
    _aT("succeeded == 2",
         out["succeeded"] == 2)
    # Inspect the batch payload to confirm uuids
    sent_uuids = {o["id"] for p in sent_payloads for o in p["objects"]}
    _aT("tombstone uuid NOT in sent payload",
         "22222222-bbbb-bbbb-bbbb-222222222222" not in sent_uuids)
    _aT("non-tombstone uuids both sent",
         "11111111-aaaa-aaaa-aaaa-111111111111" in sent_uuids
         and "33333333-cccc-cccc-cccc-333333333333" in sent_uuids)


def test_rescue_skips_implausible_records():
    _step("Records marked plausible=False are skipped during restore")
    sent = []

    def _fake_http(method, url, *, body=None, timeout=None):
        if method == "POST" and "/v1/batch/objects" in url:
            sent.append(json.loads(body.decode()))
            n = len(sent[-1]["objects"])
            return [{"result": {"status": "SUCCESS"}} for _ in range(n)]
        return {}

    records = [
        _make_record("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                       plausible=False),
        _make_record("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                       plausible=True),
    ]
    with patch.object(rescue, "_http", _fake_http):
        out = rescue.restore_records("http://x", "C", records,
                                            batch_size=10)
    _aT("only the plausible record was sent",
         out["sent"] == 1, f"sent={out['sent']}")


# ─── Abort on class recreate failure ───────────────────────────────────


def test_rescue_aborts_on_class_recreate_failure():
    _step("Phase 3 (restore) is NOT attempted when Phase 2 (reset) fails")
    fake_walk = rescue.WalkResult(
        records=[_make_record("cccccccc-1111-1111-1111-cccccccccccc")],
        summary={"plausible": 1, "tombstones": 0},
    )
    restore_called = {"n": 0}

    def _stage(*a, **kw):
        return Path("/tmp/fake"), None

    def _walk(*a, **kw):
        return fake_walk

    def _delete(*a, **kw):
        return True

    def _recreate(*a, **kw):
        raise rescue.RescueError("simulated recreate failure")

    def _restore(*a, **kw):
        restore_called["n"] += 1
        return {"sent": 0, "succeeded": 0, "failed": []}

    with patch.object(rescue, "stage_shard", _stage), \
            patch.object(rescue, "walk_shard", _walk), \
            patch.object(rescue, "discover_shards",
                          lambda *a, **kw: ["shardA"]), \
            patch.object(rescue, "delete_class", _delete), \
            patch.object(rescue, "recreate_class", _recreate), \
            patch.object(rescue, "restore_records", _restore), \
            patch.object(rescue, "gap_reindex", lambda *a, **kw: {}):
        plan = rescue.rescue_shard("ClaudeDejavuTurn", dry_run=False,
                                          docker_container="X")
    _aT("reset.failed set with the error",
         "simulated recreate failure" in plan.reset.get("failed", ""))
    _aT("restore NOT attempted",
         restore_called["n"] == 0)


def test_rescue_walks_with_quarantined_extra_segment():
    _step("walk_shard threads --include-quarantined through to the binary")
    captured: dict = {}

    class _CR:
        returncode = 0
        stdout = '{"uuid":"x","plausible":true,"properties":{"a":1},"vector":[0.1],"tombstone":false}\n'
        stderr = '{"segments":1,"records":1,"plausible":1}'

    def _fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _CR()

    with patch("subprocess.run", _fake_run):
        res = rescue.walk_shard(Path("/tmp/x"),
                                       include_quarantined=Path("/tmp/q.db"),
                                       binary=Path("/usr/local/bin/dejavu-rescue"))
    _aT("binary called with --include-quarantined",
         "--include-quarantined" in captured["cmd"]
         and "/tmp/q.db" in captured["cmd"])
    _aT("walk returned the one record",
         len(res.records) == 1 and res.records[0]["uuid"] == "x")
    _aT("walk parsed the stderr summary",
         res.summary.get("plausible") == 1)


# ─── Schema bootstrap reads canonical class definition ──────────────────


def test_rescue_recreate_class_uses_canonical_schema():
    _step("recreate_class POSTs the file at code/weaviate-class.json")
    captured: dict = {}

    def _fake_http(method, url, *, body=None, timeout=None):
        captured["method"] = method
        captured["url"] = url
        captured["body"] = body
        return {"class": "ClaudeDejavuTurn"}

    with patch.object(rescue, "_http", _fake_http):
        result = rescue.recreate_class("http://wv")

    _aT("POST'd to /v1/schema",
         captured.get("method") == "POST" and "/v1/schema" in captured.get("url", ""))
    body = json.loads(captured["body"].decode())
    _aT("body has class=ClaudeDejavuTurn",
         body.get("class") == "ClaudeDejavuTurn")
    _aT("body has vectorizer set",
         "vectorizer" in body)


def main() -> int:
    print("claude-dejavu v0.8.2 rescue.py orchestrator tests")
    tests = [
        test_rescue_dry_run_no_changes,
        test_rescue_gap_reindex_uses_pg_canonical,
        test_rescue_gap_reindex_no_op_when_all_restored,
        test_rescue_honors_tombstones,
        test_rescue_skips_implausible_records,
        test_rescue_aborts_on_class_recreate_failure,
        test_rescue_walks_with_quarantined_extra_segment,
        test_rescue_recreate_class_uses_canonical_schema,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            import traceback; traceback.print_exc()
            FAIL.append((t.__name__, str(e)))
    print()
    print("=" * 60)
    print(f"PASS: {len(PASS)}     FAIL: {len(FAIL)}")
    for m, d in FAIL:
        print(f"  ✗ {m}  {d[:80]}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
