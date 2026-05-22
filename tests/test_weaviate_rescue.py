#!/usr/bin/env python3
"""Unit tests for code/weaviate_rescue.py — the v0.8.3 Weaviate rescue toolkit.

HTTP + docker subprocess are fully mocked. Tests run without live
Weaviate / docker.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))


PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(m): PASS.append(m); print(f"  \033[32m✓\033[0m {m}", flush=True)
def _f(m, d=""):
    FAIL.append((m, d)); print(f"  \033[31m✗\033[0m {m}")
    d and print(f"      {d}")
def _step(n): print(f"\n\033[36m── {n} ──\033[0m", flush=True)
def _aT(n, c, d=""): _ok(n) if c else _f(n, d)


# ─── scan ─────────────────────────────────────────────────────────────


def test_scan_detects_dim_mismatch():
    import weaviate_rescue as wv
    _step("scan: cloud emits 1024 but class stores 384 → dim_mismatches")
    nodes_resp = {"nodes": [{"shards": [
        {"class": "ClaudeDejavuTurn", "name": "shard1",
          "status": "READY"},
    ]}]}
    schema_resp = {"classes": [
        {"class": "ClaudeDejavuTurn"},
        {"class": "ClaudeDejavuDoc"},
    ]}
    graphql_resp = {"data": {"Get": {"ClaudeDejavuTurn": [
        {"_additional": {"vector": [0.0] * 384}}
    ], "ClaudeDejavuDoc": [
        {"_additional": {"vector": [0.0] * 384}}
    ]}}}

    def fake_http(method, url, **kw):
        if url.endswith("/v1/nodes"):
            return nodes_resp
        if url.endswith("/v1/schema"):
            return schema_resp
        if "graphql" in url:
            return graphql_resp
        return {}

    with mock.patch.object(wv, "_http", side_effect=fake_http):
        r = wv.scan(probe_embed_fn=lambda: 1024)

    _aT("reachable=True", r["reachable"])
    _aT("embed_dim=1024", r["embed_dim"] == 1024)
    classes = {dm["class"] for dm in r["dim_mismatches"]}
    _aT("Turn flagged", "ClaudeDejavuTurn" in classes)
    _aT("Doc flagged",  "ClaudeDejavuDoc" in classes)


def test_scan_flags_unhealthy_shard():
    import weaviate_rescue as wv
    _step("scan: shard status != READY → unhealthy_shards")
    nodes_resp = {"nodes": [{"shards": [
        {"class": "ClaudeDejavuTurn", "name": "shard1",
          "status": "INDEXING"},
        {"class": "ClaudeDejavuTurn", "name": "shard2",
          "status": "READY"},
    ]}]}
    schema_resp = {"classes": []}

    def fake_http(method, url, **kw):
        if url.endswith("/v1/nodes"): return nodes_resp
        if url.endswith("/v1/schema"): return schema_resp
        return {}

    with mock.patch.object(wv, "_http", side_effect=fake_http):
        r = wv.scan(probe_embed_fn=lambda: None)

    _aT("one unhealthy shard",
         len(r["unhealthy_shards"]) == 1
         and r["unhealthy_shards"][0]["name"] != "shard2"
         if r["unhealthy_shards"] and "name" in r["unhealthy_shards"][0]
         else True,
         str(r["unhealthy_shards"]))
    _aT("INDEXING flagged",
         len(r["unhealthy_shards"]) == 1
         and r["unhealthy_shards"][0]["status"] == "INDEXING")


def test_scan_reports_missing_classes():
    import weaviate_rescue as wv
    _step("scan: expected classes absent → missing_classes")

    def fake_http(method, url, **kw):
        if url.endswith("/v1/nodes"): return {"nodes": []}
        if url.endswith("/v1/schema"): return {"classes": []}
        return {}

    with mock.patch.object(wv, "_http", side_effect=fake_http):
        r = wv.scan(probe_embed_fn=lambda: None)

    _aT("ClaudeDejavuTurn reported missing",
         "ClaudeDejavuTurn" in r["missing_classes"])
    _aT("ClaudeDejavuDoc reported missing",
         "ClaudeDejavuDoc" in r["missing_classes"])


def test_scan_returns_unreachable_when_nodes_endpoint_fails():
    import weaviate_rescue as wv
    _step("scan: network error → reachable=False, no crash")

    def fake_http(method, url, **kw):
        raise RuntimeError("connection refused")

    with mock.patch.object(wv, "_http", side_effect=fake_http):
        r = wv.scan(probe_embed_fn=lambda: None)

    _aT("reachable=False", not r["reachable"])
    _aT("empty results",
         r["unhealthy_shards"] == [] and r["missing_classes"] == [])


# ─── quarantine_segment ───────────────────────────────────────────────


def test_quarantine_segment_identifies_5_files():
    import weaviate_rescue as wv
    _step("quarantine-segment: dry-run probe lists all 5 file extensions")
    calls: list[list[str]] = []

    def fake_runner(args, **kw):
        calls.append(args)
        if "ls" in args:
            ls = "\n".join(
                f"segment-99.{ext}" for ext in wv.SEGMENT_EXTENSIONS
            )
            return subprocess.CompletedProcess(args, 0, stdout=ls,
                                                  stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    r = wv.quarantine_segment(
        "99", class_name="ClaudeDejavuTurn",
        shard_id="shardA", dry_run=True, docker_run=fake_runner)

    _aT("mode == dry-run", r.mode == "dry-run")
    _aT("5 files identified",
         len(r.moved) == 5,
         f"got {r.moved}")
    _aT("all ext names listed",
         all(f"segment-99.{ext}" in r.moved
             for ext in wv.SEGMENT_EXTENSIONS))


def test_quarantine_segment_apply_stops_then_starts_container():
    import weaviate_rescue as wv
    _step("quarantine-segment apply: docker stop + start brackets the move")
    calls: list[list[str]] = []

    def fake_runner(args, **kw):
        calls.append(args)
        # `test -f` returns 0 for every segment file
        if args[:3] == ["docker", "exec", "claude-dejavu-weaviate"] \
                and "test" in args:
            return subprocess.CompletedProcess(args, 0, stdout="",
                                                  stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="",
                                              stderr="")

    with mock.patch("time.sleep", lambda *a, **kw: None):
        r = wv.quarantine_segment(
            "99", class_name="ClaudeDejavuTurn",
            shard_id="shardA", dry_run=False,
            data_root=Path("/tmp/dejavu-test-quar"),
            docker_run=fake_runner)

    # Look for the call signatures
    stops = [c for c in calls
              if c[:2] == ["docker", "stop"]]
    starts = [c for c in calls
                if c[:2] == ["docker", "start"]]
    rms = [c for c in calls
             if c[:2] == ["docker", "run"]]
    _aT("docker stop called", len(stops) >= 1)
    _aT("docker start called", len(starts) >= 1)
    _aT("alpine cleanup ran", any("alpine" in " ".join(c)
                                          for c in rms))
    _aT("container_restarted flag set", r.container_restarted)


def test_quarantine_segment_writes_to_data_root():
    import weaviate_rescue as wv
    _step("quarantine-segment: quarantine dir under $DATA_ROOT/weaviate-quarantine/<ts>/")

    def fake_runner(args, **kw):
        if "ls" in args:
            return subprocess.CompletedProcess(args, 0,
                stdout="\n".join(f"segment-99.{e}"
                                    for e in wv.SEGMENT_EXTENSIONS),
                stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="",
                                              stderr="")

    r = wv.quarantine_segment(
        "99", class_name="ClaudeDejavuTurn",
        shard_id="shardA", dry_run=True,
        data_root=Path("/tmp/dejavu-test-quar"),
        docker_run=fake_runner)
    s = str(r.quarantine_dir)
    _aT("path contains weaviate-quarantine",
         "weaviate-quarantine" in s, s)
    _aT("path contains shard id",
         "shardA" in s, s)


# ─── redo_class ───────────────────────────────────────────────────────


def test_redo_class_dry_run_returns_schema_without_calling_api():
    import weaviate_rescue as wv
    _step("redo-class --dry-run: returns the planned schema, no HTTP")
    calls: list[tuple[str, str]] = []

    def fake_http(method, url, **kw):
        calls.append((method, url))
        return {}

    with mock.patch.object(wv, "_http", side_effect=fake_http):
        r = wv.redo_class("ClaudeDejavuTurn", dry_run=True)

    _aT("no API calls", len(calls) == 0)
    _aT("schema returned", r["schema"] is not None)
    _aT("vectorizer set to none",
         r["schema"]["vectorizer"] == "none")


def test_redo_class_apply_deletes_then_creates():
    import weaviate_rescue as wv
    _step("redo-class apply: DELETE /v1/schema/<C> then POST /v1/schema")
    calls: list[tuple[str, str]] = []

    def fake_http(method, url, **kw):
        calls.append((method, url))
        return {}

    with mock.patch.object(wv, "_http", side_effect=fake_http):
        r = wv.redo_class("ClaudeDejavuDoc", reset_pg_flags=False,
                                dry_run=False)

    methods = [m for (m, _u) in calls]
    _aT("DELETE issued first", methods[0] == "DELETE")
    _aT("POST issued after", "POST" in methods)
    _aT("recreated=True", r["recreated"])


def test_redo_class_resets_pg_flags_for_turn():
    import weaviate_rescue as wv
    _step("redo-class ClaudeDejavuTurn --reset-pg-flags: also calls pg_rescue.reset_flags")
    pg_called = {"count": 0, "args": None}

    def fake_http(method, url, **kw):
        return {}

    class FakeMod:
        def reset_flags(self, *, db_name, dry_run, conn):
            pg_called["count"] += 1
            pg_called["args"] = {"db_name": db_name,
                                    "dry_run": dry_run}
            return {"reset": 42, "errored": 0, "total": 42,
                      "mode": "apply"}

    with mock.patch.object(wv, "_http", side_effect=fake_http), \
         mock.patch.dict(sys.modules, {"pg_rescue": FakeMod()}):
        r = wv.redo_class("ClaudeDejavuTurn",
                                 reset_pg_flags=True, dry_run=False)

    _aT("pg_rescue.reset_flags invoked", pg_called["count"] == 1)
    _aT("called in apply mode (dry_run=False)",
         pg_called["args"]["dry_run"] is False)
    _aT("pg_reset report bubbled up",
         r["pg_reset"] is not None
         and r["pg_reset"].get("reset") == 42)


def test_redo_class_unknown_class_raises():
    import weaviate_rescue as wv
    _step("redo-class: unknown class → WeaviateRescueError")
    raised = False
    try:
        wv.redo_class("UnknownClass", dry_run=True)
    except wv.WeaviateRescueError:
        raised = True
    _aT("WeaviateRescueError raised", raised)


def test_schema_for_doc_uses_vec_none():
    import weaviate_rescue as wv
    _step("_schema_for: Doc class with vectorizer:none has no transformers config")
    s = wv._schema_for("ClaudeDejavuDoc", "none")
    _aT("vectorizer:none", s["vectorizer"] == "none")
    _aT("no moduleConfig at class level",
         "moduleConfig" not in s)
    names = {p["name"] for p in s["properties"]}
    _aT("repo_id property present", "repo_id" in names)
    _aT("content_hash property present", "content_hash" in names)


def test_segment_extensions_are_canonical_five():
    import weaviate_rescue as wv
    _step("SEGMENT_EXTENSIONS lists the canonical 5 LSM file kinds")
    _aT(".db present", "db" in wv.SEGMENT_EXTENSIONS)
    _aT(".bloom present", "bloom" in wv.SEGMENT_EXTENSIONS)
    _aT(".cna present", "cna" in wv.SEGMENT_EXTENSIONS)
    _aT(".secondary.0.bloom present",
         "secondary.0.bloom" in wv.SEGMENT_EXTENSIONS)
    _aT(".secondary.1.bloom present",
         "secondary.1.bloom" in wv.SEGMENT_EXTENSIONS)
    _aT("exactly 5", len(wv.SEGMENT_EXTENSIONS) == 5)


def main() -> int:
    print("claude-dejavu v0.8.3 weaviate_rescue tests")
    print(f"  ROOT={ROOT}")
    tests = [
        test_scan_detects_dim_mismatch,
        test_scan_flags_unhealthy_shard,
        test_scan_reports_missing_classes,
        test_scan_returns_unreachable_when_nodes_endpoint_fails,
        test_quarantine_segment_identifies_5_files,
        test_quarantine_segment_apply_stops_then_starts_container,
        test_quarantine_segment_writes_to_data_root,
        test_redo_class_dry_run_returns_schema_without_calling_api,
        test_redo_class_apply_deletes_then_creates,
        test_redo_class_resets_pg_flags_for_turn,
        test_redo_class_unknown_class_raises,
        test_schema_for_doc_uses_vec_none,
        test_segment_extensions_are_canonical_five,
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
    print(f"PASS: {len(PASS)}     FAIL: {len(FAIL)}")
    for m, d in FAIL:
        print(f"  ✗ {m}  {d[:120]}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
