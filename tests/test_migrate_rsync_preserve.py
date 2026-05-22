#!/usr/bin/env python3
"""Unit tests for code/migrate_rsync.py — Step 3 preserve mechanics
(H4 of v0.8.0).

Style: homemade _step / _ok / _f / _aT. Ingester call is stubbed.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(m): PASS.append(m); print(f"  \033[32m✓\033[0m {m}", flush=True)
def _f(m, d=""): FAIL.append((m, d)); print(f"  \033[31m✗\033[0m {m}"); d and print(f"      {d}")
def _step(n): print(f"\n\033[36m── {n} ──\033[0m", flush=True)
def _aT(n, c, d=""): _ok(n) if c else _f(n, d)


UUID_A = "42bc895b-f449-4c5a-ac78-2a1d3fb28fd5"
UUID_B = "00000000-0000-0000-0000-000000000002"


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def _mk_at_risk(td: Path, uuid_: str, content: bytes) -> "AtRiskSession":
    """Build a synthetic source JSONL + return the AtRiskSession entry."""
    import migrate_rsync as M
    proj = "-fake-proj"
    src_dir = td / "snapshots" / "20260101-000000" / ".claude" / "projects" / proj
    src_dir.mkdir(parents=True, exist_ok=True)
    src = src_dir / f"{uuid_}.jsonl"
    src.write_bytes(content)
    return M.AtRiskSession(
        session_id=uuid_,
        project_encoded=proj,
        source_path=src,
        size=src.stat().st_size,
        sha256=_sha256(src),
    )


def test_preserve_writes_to_off_tree_only():
    _step("preserve_at_risk: writes ONLY to off-tree archive — never "
          "into ~/.claude/projects/")
    import migrate_rsync as M
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        archive = td / "data" / "preserved-sessions"
        # NB: live tree intentionally empty + watched
        live_proj = td / "home" / ".claude" / "projects"
        live_proj.mkdir(parents=True, exist_ok=True)
        item = _mk_at_risk(td, UUID_A, b'{"x":1}\n')
        called: list = []
        def fake_ingest(jsonl_path, session_uuid, project_encoded, conn):
            called.append((jsonl_path, session_uuid, project_encoded))
            return 1
        with mock.patch.dict(sys.modules, clear=False):
            with mock.patch.object(M, "_call_ingester", fake_ingest):
                with mock.patch.object(M, "_LIVE_ROOT_OVERRIDE",
                                          live_proj, create=True):
                    manifest = M.preserve_at_risk(
                        [item], archive, conn=None)
        # off-tree archive has the file
        archived = archive / "-fake-proj" / f"{UUID_A}.jsonl"
        _aT("off-tree copy exists", archived.is_file(),
             f"expected at {archived}")
        # live tree still empty
        live_entries = list(live_proj.rglob("*.jsonl"))
        _aT(f"live tree untouched (got {live_entries})",
             not live_entries)
        # manifest is well-formed
        _aT("manifest has 1 entry", len(manifest.entries) == 1)
        _aT("manifest entry has matching sha256",
             manifest.entries[0]["sha256"] == item.sha256)


def test_preserve_sha256_verified_on_both_copies():
    _step("preserve_at_risk: sha256 verified on source + destination")
    import migrate_rsync as M
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        archive = td / "data" / "preserved-sessions"
        item = _mk_at_risk(td, UUID_A, b"hello world\n" * 10)
        with mock.patch.object(M, "_call_ingester", lambda *a, **k: 1):
            manifest = M.preserve_at_risk([item], archive, conn=None)
        entry = manifest.entries[0]
        _aT("source sha256 in manifest equals entry sha256",
             entry["sha256"] == item.sha256)
        # confirm the destination file actually round-trips the sha
        dst = archive / "-fake-proj" / f"{UUID_A}.jsonl"
        h = hashlib.sha256(); h.update(dst.read_bytes())
        _aT("destination sha256 == source sha256",
             h.hexdigest() == item.sha256)


def test_preserve_calls_ingester_per_session():
    _step("preserve_at_risk: programmatic ingester.ingest_jsonl call "
          "happens for each preserved session (off-tree path, NOT live)")
    import migrate_rsync as M
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        archive = td / "data" / "preserved-sessions"
        item_a = _mk_at_risk(td, UUID_A, b'{"a":1}\n')
        item_b = _mk_at_risk(td, UUID_B, b'{"b":1}\n')
        seen: list = []
        def fake(jsonl_path, session_uuid, project_encoded, conn):
            seen.append((str(jsonl_path), session_uuid, project_encoded))
            return 1
        with mock.patch.object(M, "_call_ingester", fake):
            M.preserve_at_risk([item_a, item_b], archive, conn="STUB")
        _aT(f"ingester called twice (got {len(seen)})", len(seen) == 2)
        # all paths point INTO the archive, not the live tree
        all_paths = [p for (p, *_rest) in seen]
        _aT(f"all ingest paths point into off-tree archive "
             f"(got {all_paths})",
             all(str(archive) in p for p in all_paths))


def test_preserve_writes_manifest_file():
    _step("preserve_at_risk: writes manifest-<ts>.json to the archive root")
    import migrate_rsync as M
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        archive = td / "data" / "preserved-sessions"
        item = _mk_at_risk(td, UUID_A, b'{"x":1}\n')
        with mock.patch.object(M, "_call_ingester", lambda *a, **k: 1):
            manifest = M.preserve_at_risk([item], archive, conn=None)
        _aT("manifest_path is set on returned object",
             manifest.path is not None and manifest.path.is_file())
        data = json.loads(manifest.path.read_text(encoding="utf-8"))
        _aT("manifest JSON has entries list",
             isinstance(data.get("entries"), list)
             and len(data["entries"]) == 1)
        e = data["entries"][0]
        _aT("manifest entry has session_id",
             e.get("session_id") == UUID_A)
        _aT("manifest entry has sha256",
             e.get("sha256") == item.sha256)


def test_resume_mid_step_3_continues_from_unfinished_at_risk():
    _step("preserve_at_risk: re-run with partial manifest merges, "
          "skipping sessions already copied with matching sha256 "
          "(reviewer C4)")
    import migrate_rsync as M
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        archive = td / "data" / "preserved-sessions"
        item_a = _mk_at_risk(td, UUID_A, b'{"a":1}\n')
        item_b = _mk_at_risk(td, UUID_B, b'{"b":1}\n')
        seen: list = []
        def fake(jsonl_path, session_uuid, *a, **k):
            seen.append(session_uuid); return 1
        with mock.patch.object(M, "_call_ingester", fake):
            # first run: only item_a present in the inventory (simulate
            # a partial / interrupted run that wrote item_a + manifest
            # then crashed before item_b)
            M.preserve_at_risk([item_a], archive, conn=None)
        seen.clear()
        # second run: full inventory (both A + B). A is already preserved
        # with matching sha256; B is fresh.
        with mock.patch.object(M, "_call_ingester", fake):
            M.preserve_at_risk([item_a, item_b], archive, conn=None)
        _aT(f"only the unfinished session (B) was re-ingested "
             f"(got {seen})",
             seen == [UUID_B])
        # both end-state archive files exist
        _aT("session A still preserved",
             (archive / "-fake-proj" / f"{UUID_A}.jsonl").is_file())
        _aT("session B now preserved",
             (archive / "-fake-proj" / f"{UUID_B}.jsonl").is_file())


def main() -> int:
    print("migrate_rsync preserve tests")
    tests = [
        test_preserve_writes_to_off_tree_only,
        test_preserve_sha256_verified_on_both_copies,
        test_preserve_calls_ingester_per_session,
        test_preserve_writes_manifest_file,
        test_resume_mid_step_3_continues_from_unfinished_at_risk,
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
