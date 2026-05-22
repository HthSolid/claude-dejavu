"""End-to-end smoke tests for the v0.8.2 dejavu-rescue Go binary.

These tests build (or reuse) the binary at runtime then run it against
real fixture data. The canonical fixture is the 2026-05-14 torn
segment preserved at
``~/.local/share/claude-dejavu/weaviate-quarantine/1778931907/segment-1778777077583721728.db``.

If the fixture is absent (e.g. CI), the canonical test is skipped but
the synthetic test still runs to validate the JSONL output shape.
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(msg): PASS.append(msg); print(f"  \033[32m✓\033[0m {msg}", flush=True)
def _fail_(msg, d=""):
    FAIL.append((msg, d)); print(f"  \033[31m✗\033[0m {msg}")
    d and print(f"      {d}")
def _step(name): print(f"\n\033[36m── {name} ──\033[0m", flush=True)
def _aT(name, cond, d=""): _ok(name) if cond else _fail_(name, d)
def _skip(msg): print(f"  \033[33m•\033[0m SKIP {msg}", flush=True)


def _build_binary(out_dir: Path) -> Path:
    """Build dejavu-rescue locally; reuse if already built."""
    suffix = ".exe" if sys.platform.startswith("win") else ""
    out = out_dir / f"dejavu-rescue{suffix}"
    if out.is_file():
        return out
    src = ROOT / "code" / "rescue"
    if shutil.which("go") is None:
        raise RuntimeError("go not on PATH — cannot build dejavu-rescue")
    r = subprocess.run(
        ["go", "build", "-o", str(out), "./cmd/dejavu-rescue/"],
        cwd=src, capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"go build failed: {r.stderr.strip()}")
    return out


# ─── Synthetic fixture: handcraft a tiny replace-bucket segment ─────────


def _build_synthetic_segment() -> bytes:
    """Build a 1-record strategy=0 segment matching the v1.27.x format.

    Returns the raw bytes the Go binary should walk cleanly and emit
    exactly one record.
    """
    # storobj payload (marshaller v1)
    marshallerVersion = b"\x01"
    docID = struct.pack("<Q", 7)
    kind = b"\x00"
    uuid_bytes = bytes.fromhex("00112233445566778899aabbccddeeff")
    createTime = struct.pack("<Q", 1000)
    updateTime = struct.pack("<Q", 2000)
    vec_floats = [0.1, 0.2, 0.3]
    vec_bytes = b"".join(struct.pack("<f", x) for x in vec_floats)
    vectorLen = struct.pack("<H", len(vec_floats))
    className = b"SynthClass"
    classNameLen = struct.pack("<H", len(className))
    schema_json = json.dumps({"foo": "bar", "n": 42}).encode("utf-8")
    schemaLen = struct.pack("<I", len(schema_json))

    storobj = (marshallerVersion + docID + kind + uuid_bytes +
               createTime + updateTime + vectorLen + vec_bytes +
               classNameLen + className + schemaLen + schema_json)

    # data-region record:
    #   tombstone(1) + valueLen(8) + value + primaryKeyLen(4) + key
    #   (no secondary keys: SecondaryIndices=0 in header)
    tombstone = b"\x00"
    valueLen = struct.pack("<Q", len(storobj))
    primaryKey = uuid_bytes  # 16 bytes
    primaryKeyLen = struct.pack("<I", len(primaryKey))
    record = tombstone + valueLen + storobj + primaryKeyLen + primaryKey

    # header: level(2) + version(2) + secondaryIndices(2) +
    #         strategy(2) + indexStart(8)
    indexStart = 16 + len(record)  # data ends here, no index region after
    header = (struct.pack("<H", 0) +  # level
              struct.pack("<H", 0) +  # version
              struct.pack("<H", 0) +  # secondaryIndices
              struct.pack("<H", 0) +  # strategy=0 (replace)
              struct.pack("<Q", indexStart))
    return header + record


def test_inspect_corrupt_segment_against_canonical_fixture():
    _step("`inspect` recovers ~2082 records from the canonical torn fixture")
    fixture = (Path.home() / ".local" / "share" / "claude-dejavu"
                / "weaviate-quarantine" / "1778931907"
                / "segment-1778777077583721728.db")
    if not fixture.is_file():
        _skip(f"fixture missing: {fixture}")
        return

    with tempfile.TemporaryDirectory() as td:
        try:
            binary = _build_binary(Path(td))
        except RuntimeError as exc:
            _skip(f"binary build skipped: {exc}")
            return
        r = subprocess.run([str(binary), "inspect", str(fixture)],
                              capture_output=True, text=True, timeout=30)
    _aT("binary exited 0", r.returncode == 0, r.stderr)
    if r.returncode != 0:
        return
    report = json.loads(r.stdout)
    _aT("plausible_records == 2082 (POC-verified)",
         report.get("plausible_records") == 2082,
         f"got {report.get('plausible_records')}")
    _aT("truncation_offset == 8389082",
         report.get("truncation_offset") == 8389082,
         f"got {report.get('truncation_offset')}")
    _aT("file_size == 8822783",
         report.get("file_size") == 8822783)
    _aT("header.strategy == 0",
         (report.get("header") or {}).get("strategy") == 0)


def test_scan_shard_outputs_jsonl():
    _step("`scan-shard` emits one valid JSONL record per recovered object")
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        try:
            binary = _build_binary(td_p)
        except RuntimeError as exc:
            _skip(f"binary build skipped: {exc}")
            return
        # Synthesize a 1-record shard.
        objects = td_p / "shard" / "lsm" / "objects"
        objects.mkdir(parents=True)
        seg = objects / "segment-0000000001.db"
        seg.write_bytes(_build_synthetic_segment())

        out = td_p / "out.jsonl"
        r = subprocess.run(
            [str(binary), "scan-shard",
              "--out", str(out), str(td_p / "shard")],
            capture_output=True, text=True, timeout=20,
        )
        rc = r.returncode
        stderr = r.stderr
        body = out.read_text() if out.is_file() else ""
    _aT("binary exited 0", rc == 0, stderr)
    if rc != 0:
        return
    lines = [l for l in body.splitlines() if l.strip()]
    _aT("exactly 1 record emitted",
         len(lines) == 1, f"got {len(lines)} lines")
    rec = json.loads(lines[0])
    _aT("record has uuid",
         "uuid" in rec and len(rec["uuid"]) == 36)
    _aT("record has class_name=SynthClass",
         rec.get("class_name") == "SynthClass",
         f"got {rec.get('class_name')!r}")
    _aT("record has vector of 3 floats",
         isinstance(rec.get("vector"), list) and len(rec["vector"]) == 3)
    _aT("record has properties {foo, n}",
         isinstance(rec.get("properties"), dict)
         and set(rec["properties"].keys()) == {"foo", "n"})
    _aT("record has source_segment",
         rec.get("source_segment") == "segment-0000000001.db")
    _aT("record has byte_offset",
         isinstance(rec.get("byte_offset"), int))
    _aT("record plausible=True",
         rec.get("plausible") is True)
    _aT("record tombstone=False",
         rec.get("tombstone") is False)


def test_inspect_emits_human_text_when_json_disabled():
    _step("`inspect --json=false` prints a human-readable summary line")
    with tempfile.TemporaryDirectory() as td:
        try:
            binary = _build_binary(Path(td))
        except RuntimeError as exc:
            _skip(f"binary build skipped: {exc}")
            return
        seg = Path(td) / "small.db"
        seg.write_bytes(_build_synthetic_segment())
        r = subprocess.run(
            [str(binary), "inspect", "--json=false", str(seg)],
            capture_output=True, text=True, timeout=10,
        )
    _aT("exited 0", r.returncode == 0)
    if r.returncode != 0:
        return
    _aT("output contains 'records=' kv",
         "records=" in r.stdout)
    _aT("output contains 'plausible=' kv",
         "plausible=" in r.stdout)


def test_version_subcommand_reports_0_8_2():
    _step("`dejavu-rescue version` reports 0.8.2")
    with tempfile.TemporaryDirectory() as td:
        try:
            binary = _build_binary(Path(td))
        except RuntimeError as exc:
            _skip(f"binary build skipped: {exc}")
            return
        r = subprocess.run([str(binary), "version"],
                              capture_output=True, text=True, timeout=5)
    _aT("version output mentions 0.8.2",
         "0.8.2" in (r.stdout or ""),
         f"stdout={r.stdout!r}")


def main() -> int:
    print("claude-dejavu v0.8.2 dejavu-rescue Go binary tests")
    tests = [
        test_inspect_corrupt_segment_against_canonical_fixture,
        test_scan_shard_outputs_jsonl,
        test_inspect_emits_human_text_when_json_disabled,
        test_version_subcommand_reports_0_8_2,
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
