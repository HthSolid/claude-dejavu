"""Ingester robustness tests — covers the defensive shims that prevent
silent ingest gaps.

The bug these tests guard against (fixed 2026-05-05):
  - A turn whose text contained `\\x00` (NUL byte) caused
    `psycopg2.cur.execute()` to raise `ValueError: A string literal
    cannot contain NUL (0x00) characters.`
  - The exception aborted the whole session ingest before the cursor
    was written, so the SessionEnd hook re-failed on every subsequent
    invocation, leaving entire sessions silently un-indexed.
  - On the user's machine, ~10 of 15 sessions had zero turns indexed
    in PG despite the .jsonl files being present on disk.

The fix has four prongs:
  1. `_strip_nul()` recursively scrubs NUL from any string in dicts/lists.
  2. `extract_text_blocks()` sanitizes at the boundary so all downstream
     callers get NUL-free strings.
  3. The per-line ingest loop wraps work in try/except and rolls back +
     advances the cursor on failure (so one bad turn no longer kills
     a whole session).
  4. The file is opened with `errors='replace'` so non-UTF-8 bytes
     decode rather than raising.

These tests are infra-free — no PG, no Weaviate, no Docker required.
They run as part of `tests/run_all.py`.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(msg: str) -> None:
    PASS.append(msg)
    print(f"  \033[32m✓\033[0m {msg}", flush=True)


def _fail(msg: str, detail: str = "") -> None:
    FAIL.append((msg, detail))
    print(f"  \033[31m✗\033[0m {msg}")
    if detail:
        print(f"      {detail}")


def _step(name: str) -> None:
    print(f"\n\033[36m── {name} ──\033[0m", flush=True)


def _assert_true(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        _ok(name)
    else:
        _fail(name, detail)


def _assert_eq(name: str, got, want) -> None:
    if got == want:
        _ok(name)
    else:
        _fail(name, f"got={got!r}  want={want!r}")


# ─── _strip_nul: the core helper ────────────────────────────────────────

def test_strip_nul_string() -> None:
    _step("_strip_nul on plain strings")
    from ingester import _strip_nul
    _assert_eq("clean string passes through", _strip_nul("hello"), "hello")
    _assert_eq("string with NUL gets stripped",
               _strip_nul("hel\x00lo"), "hello")
    _assert_eq("string of only NULs becomes empty",
               _strip_nul("\x00\x00\x00"), "")
    _assert_eq("NUL at start", _strip_nul("\x00abc"), "abc")
    _assert_eq("NUL at end", _strip_nul("abc\x00"), "abc")
    _assert_eq("multiple NULs interspersed",
               _strip_nul("a\x00b\x00c\x00d"), "abcd")


def test_strip_nul_non_string() -> None:
    _step("_strip_nul preserves non-string types")
    from ingester import _strip_nul
    _assert_eq("None passes through", _strip_nul(None), None)
    _assert_eq("int passes through", _strip_nul(42), 42)
    _assert_eq("float passes through", _strip_nul(3.14), 3.14)
    _assert_eq("bool passes through", _strip_nul(True), True)


def test_strip_nul_dict() -> None:
    _step("_strip_nul on dicts")
    from ingester import _strip_nul
    out = _strip_nul({"a": "ok", "b": "bad\x00val", "c": 1})
    _assert_eq("clean key untouched", out["a"], "ok")
    _assert_eq("dirty value scrubbed", out["b"], "badval")
    _assert_eq("int value preserved", out["c"], 1)


def test_strip_nul_list() -> None:
    _step("_strip_nul on lists")
    from ingester import _strip_nul
    out = _strip_nul(["clean", "dirty\x00here", 42, None])
    _assert_eq("list element 0", out[0], "clean")
    _assert_eq("list element 1 scrubbed", out[1], "dirtyhere")
    _assert_eq("list element 2 (int)", out[2], 42)
    _assert_eq("list element 3 (None)", out[3], None)


def test_strip_nul_nested() -> None:
    _step("_strip_nul recurses into nested structures")
    from ingester import _strip_nul
    nested = {
        "outer": {
            "inner_str": "x\x00y",
            "inner_list": ["a\x00b", {"deep": "z\x00z"}],
        },
        "siblings": ["plain", "with\x00nul"],
    }
    out = _strip_nul(nested)
    _assert_eq("inner string scrubbed", out["outer"]["inner_str"], "xy")
    _assert_eq("inner list element scrubbed",
               out["outer"]["inner_list"][0], "ab")
    _assert_eq("deeply nested string scrubbed",
               out["outer"]["inner_list"][1]["deep"], "zz")
    _assert_eq("sibling list element scrubbed",
               out["siblings"][1], "withnul")


# ─── extract_text_blocks: boundary sanitization ─────────────────────────

def test_extract_text_blocks_string_content() -> None:
    _step("extract_text_blocks: plain string content")
    from ingester import extract_text_blocks
    blocks = extract_text_blocks("hello world")
    _assert_eq("returns one block", len(blocks), 1)
    _assert_eq("type is text", blocks[0][0], "text")
    _assert_eq("text preserved", blocks[0][1], "hello world")


def test_extract_text_blocks_strips_nul_from_text() -> None:
    _step("extract_text_blocks: NUL byte in text content gets stripped")
    from ingester import extract_text_blocks
    blocks = extract_text_blocks("hel\x00lo")
    _assert_eq("returns one block", len(blocks), 1)
    _assert_true("no NUL in output", "\x00" not in blocks[0][1],
                 detail=repr(blocks[0][1]))
    _assert_eq("expected NUL-free text", blocks[0][1], "hello")


def test_extract_text_blocks_strips_nul_from_list_content() -> None:
    _step("extract_text_blocks: NUL byte inside a content-list block")
    from ingester import extract_text_blocks
    content = [
        {"type": "text", "text": "clean text"},
        {"type": "text", "text": "with\x00nul"},
        {"type": "thinking", "thinking": "thought\x00with\x00nul"},
    ]
    blocks = extract_text_blocks(content)
    _assert_eq("returns three blocks", len(blocks), 3)
    _assert_eq("first block clean", blocks[0][1], "clean text")
    _assert_eq("second block stripped", blocks[1][1], "withnul")
    _assert_eq("thinking block stripped",
               blocks[2][1], "thoughtwithnul")
    for ct, txt in blocks:
        _assert_true(f"no NUL in {ct} block", "\x00" not in txt,
                     detail=repr(txt))


def test_extract_text_blocks_strips_nul_from_tool_use_input() -> None:
    _step("extract_text_blocks: NUL byte deep in a tool_use input dict")
    from ingester import extract_text_blocks
    import json as _json
    content = [{
        "type": "tool_use",
        "name": "Bash",
        "id": "tu_123",
        "input": {"command": "echo bad\x00val", "args": ["ok", "x\x00y"]},
    }]
    blocks = extract_text_blocks(content)
    _assert_eq("returns one block", len(blocks), 1)
    _assert_eq("type is tool_use", blocks[0][0], "tool_use")
    _assert_true("no NUL in serialized tool_use",
                 "\x00" not in blocks[0][1], detail=repr(blocks[0][1]))
    parsed = _json.loads(blocks[0][1])
    _assert_eq("command scrubbed",
               parsed["input"]["command"], "echo badval")
    _assert_eq("nested arg scrubbed",
               parsed["input"]["args"][1], "xy")


def test_extract_text_blocks_strips_nul_from_tool_result() -> None:
    _step("extract_text_blocks: NUL byte in a tool_result block")
    from ingester import extract_text_blocks
    content = [{
        "type": "tool_result",
        "content": [{"type": "text", "text": "out\x00put"}],
    }]
    blocks = extract_text_blocks(content)
    _assert_eq("returns one block", len(blocks), 1)
    _assert_eq("type is tool_result", blocks[0][0], "tool_result")
    _assert_true("no NUL in tool_result text",
                 "\x00" not in blocks[0][1], detail=repr(blocks[0][1]))


def test_extract_text_blocks_unknown_type_skipped() -> None:
    _step("extract_text_blocks: unknown content types are skipped (no crash)")
    from ingester import extract_text_blocks
    content = [
        {"type": "image", "source": {"type": "base64", "data": "abc"}},
        {"type": "text", "text": "the only one we keep"},
    ]
    blocks = extract_text_blocks(content)
    _assert_eq("only the text block", len(blocks), 1)
    _assert_eq("text content preserved",
               blocks[0][1], "the only one we keep")


def test_extract_text_blocks_handles_empty_and_malformed() -> None:
    _step("extract_text_blocks: empty / malformed inputs don't crash")
    from ingester import extract_text_blocks
    _assert_eq("None content → empty list", extract_text_blocks(None), [])
    _assert_eq("empty list → empty list", extract_text_blocks([]), [])
    # Non-dict items inside the content list are silently ignored.
    blocks = extract_text_blocks(["not a dict", {"type": "text", "text": "ok"}])
    _assert_eq("only the dict survives", len(blocks), 1)
    _assert_eq("its text preserved", blocks[0][1], "ok")


# ─── Per-session lock — guards against concurrent stop-hooks racing ─────

def test_ingest_lock_acquire_release() -> None:
    _step("per-session lock: simple acquire + release")
    import os, tempfile
    # Override the data root so the lock dir is in tmp.
    os.environ["CLAUDE_DEJAVU_DATA_ROOT"] = tempfile.mkdtemp(prefix="dejavu-lock-test-")
    # Force re-import so the new env var is picked up by _ingest_lock_path.
    import importlib, ingester
    importlib.reload(ingester)
    sid = "11111111-aaaa-bbbb-cccc-deadbeefdead0"
    _assert_true("first acquire succeeds", ingester._acquire_ingest_lock(sid))
    _assert_true("second acquire while held fails",
                 not ingester._acquire_ingest_lock(sid))
    ingester._release_ingest_lock(sid)
    _assert_true("after release, acquire succeeds again",
                 ingester._acquire_ingest_lock(sid))
    ingester._release_ingest_lock(sid)


def test_ingest_lock_different_sessions_independent() -> None:
    _step("per-session lock: different sessions don't block each other")
    import os, tempfile, importlib
    os.environ["CLAUDE_DEJAVU_DATA_ROOT"] = tempfile.mkdtemp(prefix="dejavu-lock-test-")
    import ingester
    importlib.reload(ingester)
    sid_a = "aaaaaaaa-1111-1111-1111-111111111111"
    sid_b = "bbbbbbbb-2222-2222-2222-222222222222"
    _assert_true("acquire A", ingester._acquire_ingest_lock(sid_a))
    _assert_true("acquire B (different session) — independent",
                 ingester._acquire_ingest_lock(sid_b))
    ingester._release_ingest_lock(sid_a)
    ingester._release_ingest_lock(sid_b)


def test_ingest_lock_stale_reaped() -> None:
    _step("per-session lock: stale lockfile (>TTL) is reaped on acquire")
    import os, time, tempfile, importlib
    os.environ["CLAUDE_DEJAVU_DATA_ROOT"] = tempfile.mkdtemp(prefix="dejavu-lock-test-")
    import ingester
    importlib.reload(ingester)
    sid = "ccccccc1-3333-3333-3333-333333333333"
    lock = ingester._ingest_lock_path(sid)
    # Plant a stale lockfile (mtime older than TTL).
    lock.write_text("99999\n")
    old_time = time.time() - (ingester.INGEST_LOCK_TTL_S + 60)
    os.utime(str(lock), (old_time, old_time))
    _assert_true("acquire succeeds (stale reaped)",
                 ingester._acquire_ingest_lock(sid))
    ingester._release_ingest_lock(sid)


# ─── Progress emit — format check (no PG/Weaviate dependency) ───────────

def test_progress_emit_format() -> None:
    _step("progress heartbeat: format is parseable + has expected fields")
    import io
    import sys as _sys
    from ingester import _progress_emit
    captured = io.StringIO()
    real_stderr = _sys.stderr
    _sys.stderr = captured
    try:
        _progress_emit("aabbccdd-1111-2222-3333-444455556666",
                       line_no=500, file_size=10_000_000, bytes_read=1_500_000,
                       new_turns=120, started=__import__("time").time() - 5.0)
    finally:
        _sys.stderr = real_stderr
    out = captured.getvalue()
    _assert_true("starts with [ingest]", out.startswith("[ingest] "), detail=out)
    _assert_true("contains short session id", "aabbccdd" in out, detail=out)
    _assert_true("contains line=500", "line=500" in out, detail=out)
    _assert_true("contains bytes ratio",
                 "1500000/10000000" in out, detail=out)
    _assert_true("contains turn delta", "+120t" in out, detail=out)
    _assert_true("ends with newline (line-buffered consumers)",
                 out.endswith("\n"), detail=repr(out))


# ─── Main ───────────────────────────────────────────────────────────────

def main() -> int:
    print("claude-dejavu ingester-robustness tests")
    print(f"  ROOT={ROOT}")

    tests = [
        test_strip_nul_string,
        test_strip_nul_non_string,
        test_strip_nul_dict,
        test_strip_nul_list,
        test_strip_nul_nested,
        test_extract_text_blocks_string_content,
        test_extract_text_blocks_strips_nul_from_text,
        test_extract_text_blocks_strips_nul_from_list_content,
        test_extract_text_blocks_strips_nul_from_tool_use_input,
        test_extract_text_blocks_strips_nul_from_tool_result,
        test_extract_text_blocks_unknown_type_skipped,
        test_extract_text_blocks_handles_empty_and_malformed,
        test_ingest_lock_acquire_release,
        test_ingest_lock_different_sessions_independent,
        test_ingest_lock_stale_reaped,
        test_progress_emit_format,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            import traceback
            traceback.print_exc()
            FAIL.append((t.__name__, f"{type(e).__name__}: {e}"))

    print()
    print("=" * 60)
    print(f"PASS: {len(PASS)}     FAIL: {len(FAIL)}")
    for name, detail in FAIL:
        print(f"  ✗ {name}  {detail[:80]}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
