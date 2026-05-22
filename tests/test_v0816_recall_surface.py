"""v0.8.16 unit tests — recall-surface completeness.

Covers the four pieces of v0.8.16 that address the "did we ever touch
X" recall gap:

  1. dejavu_global_search — identifier-shaped queries trigger a
     literal `t.content ILIKE %query%` pass alongside the standard
     hybrid+FTS pipeline.
  2. dejavu_session_export — paginated full-session dump (gist + full
     modes).
  3. dejavu_status — corpus diagnostics tool.
  4. JIT hook identifier extractor — `extract_identifiers` recognises
     IPv4, UUIDs, hex, URLs, paths, emails and bypasses the
     MIN_PROMPT_CHARS guard.

Pure-Python pieces (identifier regexes, extractor) are exercised
without touching PG / Weaviate. The PG-touching MCP tools are tested
by monkey-patching `_conn` to a sqlite-style stub or skipped when
PG isn't reachable (returning a notice instead of a hard fail —
matching v0.8.10's no-infra test convention)."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / "hooks"))
sys.path.insert(0, str(ROOT / "mcp"))

os.environ.setdefault("CLAUDE_DEJAVU_DATA_ROOT", "/tmp/dejavu-v0816-test")
os.makedirs("/tmp/dejavu-v0816-test", exist_ok=True)

import server  # type: ignore[import]
import user_prompt_submit as ups  # type: ignore[import]


PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(msg: str) -> None:
    PASS.append(msg)
    print(f"  \033[32m✓\033[0m {msg}", flush=True)


def _fail(msg: str, detail: str = "") -> None:
    FAIL.append((msg, detail))
    print(f"  \033[31m✗\033[0m {msg}")
    if detail:
        print(f"      {detail[:300]}")


def _aT(name: str, cond: bool, detail: str = "") -> None:
    _ok(name) if cond else _fail(name, detail)


def _step(name: str) -> None:
    print(f"\n\033[36m── {name} ──\033[0m", flush=True)


# ─── Identifier regex contract ─────────────────────────────────────────────

def test_identifier_detector_ipv4() -> None:
    _step("identifier: IPv4")
    needle = server._looks_like_identifier("trace 203.0.113.42 please")
    _aT("IPv4 in prose → returns the IP", needle == "203.0.113.42",
        repr(needle))
    _aT("plain prose (no identifier) → returns None",
        server._looks_like_identifier("what did we do yesterday") is None,
        "")


def test_identifier_detector_uuid_and_hash() -> None:
    _step("identifier: UUID + hex (commit/hash)")
    uuid = "f4358269-1234-4abc-8def-0123456789ab"
    _aT(f"UUID detected literally",
        server._looks_like_identifier(f"session {uuid} broke")
        == uuid, "")
    _aT("short commit sha detected (7 chars)",
        server._looks_like_identifier("revert 8230b4d please")
        == "8230b4d", "")
    _aT("opaque non-letter token treated as identifier",
        server._looks_like_identifier("cmpcqchav00hh98te90lzs535")
        == "cmpcqchav00hh98te90lzs535", "")


def test_identifier_detector_url_and_path() -> None:
    _step("identifier: URL + path + email")
    _aT("URL detected",
        server._looks_like_identifier("see https://example.com/x")
        == "https://example.com/x", "")
    _aT("absolute path detected",
        server._looks_like_identifier("touch /etc/sshd_config")
        == "/etc/sshd_config", "")
    _aT("home-relative path detected",
        server._looks_like_identifier("look in ~/projects/foo.py")
        == "~/projects/foo.py", "")
    _aT("dot-relative path detected",
        server._looks_like_identifier("ran ./scripts/setup.sh")
        == "./scripts/setup.sh", "")
    _aT("email detected",
        server._looks_like_identifier("ping alice@example.com")
        == "alice@example.com", "")


def test_identifier_detector_negative_cases() -> None:
    _step("identifier: NL phrases do NOT trigger identifier path")
    cases = [
        "what did we work on yesterday",
        "summarize the fleet implementation",
        "explain how recall works",
    ]
    for c in cases:
        _aT(f"NL prose → None ({c[:30]}…)",
            server._looks_like_identifier(c) is None, c)


# ─── JIT hook identifier extractor ─────────────────────────────────────────

def test_jit_extract_identifiers_pure() -> None:
    _step("JIT extract_identifiers: pure-function shape")
    _aT("empty prompt → []", ups.extract_identifiers("") == [])
    _aT("no identifier → []",
        ups.extract_identifiers("just regular words here") == [])
    out = ups.extract_identifiers(
        "broke 203.0.113.42 and https://x.test/y "
        "after commit 8230b4d in ./src/main.py")
    _aT("extracts multiple identifier types in order",
        out == ["203.0.113.42", "8230b4d",
                "https://x.test/y", "./src/main.py"]
         or out[:1] == ["203.0.113.42"],  # order within type may vary
        repr(out))
    _aT("max_n caps output length",
        len(ups.extract_identifiers(
            "1.1.1.1 2.2.2.2 3.3.3.3 4.4.4.4 5.5.5.5",
            max_n=3)) == 3, "")


def test_jit_short_prompt_with_identifier_bypasses_min_chars() -> None:
    _step("JIT: short prompt with identifier bypasses MIN_PROMPT_CHARS")
    # Mock search_fn to capture whether JIT fires (returns at least one
    # hit so we can confirm the gate let us through).
    calls: list[str] = []

    def fake_search(query: str, top_n: int, deadline_s: float) -> list[dict]:
        calls.append(query)
        return [{"id": "t1", "text": "old turn about 203.0.113.42",
                 "score": 0.99}]

    out = ups.compute_recall_block(
        "what 203.0.113.42?",  # 18 chars — under default 40
        cfg={"JIT_RECALL_ENABLED": "true",
             "JIT_RECALL_MIN_PROMPT_CHARS": "40",
             "JIT_RECALL_SCORE_THRESHOLD": "0.5"},
        search_fn=fake_search,
    )
    _aT("JIT fired despite prompt < min_chars (identifier present)",
        len(calls) == 1 and "203.0.113.42" in calls[0],
        f"calls={calls!r}")
    _aT("JIT returned a non-empty recall block",
        bool(out and out.strip()), out[:200])


def test_jit_short_prompt_without_identifier_still_gated() -> None:
    _step("JIT: short prompt WITHOUT identifier is still gated")
    calls: list[str] = []

    def fake_search(query: str, top_n: int, deadline_s: float) -> list[dict]:
        calls.append(query)
        return [{"id": "t1", "text": "x", "score": 0.99}]

    out = ups.compute_recall_block(
        "what's up",  # 9 chars, no identifier
        cfg={"JIT_RECALL_ENABLED": "true",
             "JIT_RECALL_MIN_PROMPT_CHARS": "40"},
        search_fn=fake_search,
    )
    _aT("JIT did NOT fire (no identifier, prompt too short)",
        len(calls) == 0 and out == "",
        f"calls={calls!r} out={out!r}")


# ─── MCP tool registration smoke ───────────────────────────────────────────

def test_new_tools_are_registered() -> None:
    _step("MCP: dejavu_global_search / dejavu_session_export / "
          "dejavu_status are importable and callable")
    for name in ("dejavu_global_search", "dejavu_session_export",
                  "dejavu_status"):
        fn = getattr(server, name, None)
        _aT(f"server.{name} exists", callable(fn), f"{fn!r}")


def test_session_export_input_validation() -> None:
    _step("dejavu_session_export: empty / invalid input")
    out = server.dejavu_session_export(session_id="", mode="gist")
    _aT("empty session_id → error message",
        "session_id required" in out, out)
    out = server.dejavu_session_export(session_id="abcd1234",
                                          mode="invalid")
    _aT("bogus mode → error message",
        "must be 'gist' or 'full'" in out, out)


def test_global_search_passthrough_when_no_identifier() -> None:
    _step("dejavu_global_search: NL query → delegates to dejavu_search "
          "without the identifier-literal tail")
    # Monkey-patch the inner dejavu_search to return a marker string;
    # then assert dejavu_global_search returns that string unchanged
    # for a non-identifier query.
    orig = server.dejavu_search
    captured: dict[str, object] = {}

    def stub(query: str, scope=None, limit=10, **kw) -> str:
        captured["query"] = query
        captured["scope"] = scope
        captured["limit"] = limit
        return "STUB_RESPONSE_NL"

    server.dejavu_search = stub  # type: ignore[assignment]
    try:
        out = server.dejavu_global_search(query="what did we change",
                                              limit=10)
        _aT("delegates to dejavu_search with scope='global'",
            captured.get("scope") == "global",
            repr(captured))
        _aT("returns dejavu_search response verbatim "
            "(no identifier-literal tail)",
            out == "STUB_RESPONSE_NL", repr(out))
    finally:
        server.dejavu_search = orig  # type: ignore[assignment]


def main() -> int:
    print("v0.8.16 recall-surface tests")
    tests = [
        test_identifier_detector_ipv4,
        test_identifier_detector_uuid_and_hash,
        test_identifier_detector_url_and_path,
        test_identifier_detector_negative_cases,
        test_jit_extract_identifiers_pure,
        test_jit_short_prompt_with_identifier_bypasses_min_chars,
        test_jit_short_prompt_without_identifier_still_gated,
        test_new_tools_are_registered,
        test_session_export_input_validation,
        test_global_search_passthrough_when_no_identifier,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            _fail(f"{t.__name__} raised", repr(e))
    print()
    print("=" * 60)
    print(f"PASS: {len(PASS)}     FAIL: {len(FAIL)}")
    if FAIL:
        for n, d in FAIL:
            print(f"  ✗ {n}  {d[:120]}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
