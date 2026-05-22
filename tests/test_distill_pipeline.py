"""Unit tests for code/distill_pipeline.py (v0.8.6 Context Economy
phase 3).

These tests pin the load-bearing-token contract: anchors that
matter for keyword recall (paths, ports, env-var names, error
class names, version strings, flags) MUST survive distillation,
or the pipeline MUST return None so the caller stores NULL and
falls back to raw content.

Runs against the stub bridge by default (the suite is infra-free)
and exercises the same code path the real wheel will use — the
splice + round-trip + skip gates are claude-dejavu-side logic.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

import distill_pipeline as dp  # noqa: E402


PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(msg: str) -> None:
    PASS.append(msg); print(f"  \033[32m✓\033[0m {msg}", flush=True)


def _fail(msg: str, d: str = "") -> None:
    FAIL.append((msg, d)); print(f"  \033[31m✗\033[0m {msg}")
    if d:
        print(f"      {d}")


def _aT(name: str, cond: bool, d: str = "") -> None:
    _ok(name) if cond else _fail(name, d)


def _step(name: str) -> None:
    print(f"\n\033[36m── {name} ──\033[0m", flush=True)


# ─── anchor extraction ───────────────────────────────────────────────────────

def test_extracts_paths() -> None:
    _step("extract_anchors: filesystem paths")
    text = ("Edit /home/x/projects/foo/src/bar.ts and then "
            "scripts/install.py — also tests/run_all.py.")
    anchors = dp.extract_anchors(text)
    _aT("absolute path captured",
         "/home/x/projects/foo/src/bar.ts" in anchors, repr(anchors))
    _aT("relative path captured",
         any(a.endswith("install.py") for a in anchors), repr(anchors))


def test_extracts_urls_and_ports() -> None:
    _step("extract_anchors: URLs + ports")
    text = ("Hitting http://localhost:8889/v1/batch/objects against "
            "Weaviate on port 8889; PG is on 5450.")
    anchors = dp.extract_anchors(text)
    _aT("URL captured",
         any("8889/v1/batch/objects" in a for a in anchors), repr(anchors))
    _aT("port 8889 captured",
         "8889" in anchors or any("8889" in a for a in anchors),
         repr(anchors))
    _aT("port 5450 captured",
         "5450" in anchors, repr(anchors))


def test_extracts_env_vars() -> None:
    _step("extract_anchors: env-var names")
    text = ("Set WV_URL=http://localhost:8888 and PG_DB=foo plus "
            "DEJAVU_BRIDGE_INDEX_URL pointing at the wheel.")
    anchors = dp.extract_anchors(text)
    _aT("WV_URL captured", "WV_URL" in anchors, repr(anchors))
    _aT("PG_DB captured", "PG_DB" in anchors, repr(anchors))
    _aT("DEJAVU_BRIDGE_INDEX_URL captured",
         "DEJAVU_BRIDGE_INDEX_URL" in anchors, repr(anchors))


def test_extracts_version_strings() -> None:
    _step("extract_anchors: version strings")
    text = "Upgrading from v0.8.3 to 0.8.6 (was 1.10.2-rc2 in dev)."
    anchors = dp.extract_anchors(text)
    _aT("v0.8.3 captured", "v0.8.3" in anchors, repr(anchors))
    _aT("0.8.6 captured", "0.8.6" in anchors, repr(anchors))


def test_extracts_command_flags() -> None:
    _step("extract_anchors: command flags")
    text = ("Run `claude-dejavu memory backfill-gists --dry-run "
            "--workers 4 --limit 100`.")
    anchors = dp.extract_anchors(text)
    _aT("--dry-run captured", "--dry-run" in anchors, repr(anchors))
    _aT("--workers captured",
         any(a.startswith("--workers") for a in anchors), repr(anchors))


def test_extracts_error_class_names() -> None:
    _step("extract_anchors: error class names")
    text = ("psycopg2 raised UndefinedColumn; before that we saw "
            "ImportError and a RuntimeException.")
    anchors = dp.extract_anchors(text)
    _aT("UndefinedColumn captured",
         "UndefinedColumn" in anchors, repr(anchors))
    _aT("ImportError captured", "ImportError" in anchors, repr(anchors))


def test_extracts_backtick_identifiers() -> None:
    _step("extract_anchors: backtick identifiers")
    text = ("The fix is to call `claude-dejavu memory "
            "backfill-gists` after `pip install dejavu_bridge`.")
    anchors = dp.extract_anchors(text)
    _aT("backtick command captured",
         any("backfill-gists" in a for a in anchors), repr(anchors))


# ─── skip gates ──────────────────────────────────────────────────────────────

def test_skip_code_blocks() -> None:
    _step("should_skip_distill: code blocks")
    text = "Here's the fix:\n```python\nprint('hi')\n```\nDone."
    reason = dp.should_skip_distill(text, "text")
    _aT("code block skipped",
         reason == "contains-code-block", repr(reason))
    gist, method = dp.make_gist(text, content_type="text")
    _aT("make_gist returns None on code block",
         gist is None and method is None, f"{gist!r}, {method!r}")


def test_skip_tracebacks() -> None:
    _step("should_skip_distill: tracebacks")
    text = ("Traceback (most recent call last):\n"
            "  File \"a.py\", line 1, in <module>\n"
            "    raise RuntimeError('bad')\n"
            "RuntimeError: bad")
    reason = dp.should_skip_distill(text, "text")
    _aT("traceback skipped",
         reason == "contains-traceback", repr(reason))
    gist, method = dp.make_gist(text, content_type="text")
    _aT("make_gist returns None on traceback",
         gist is None and method is None, f"{gist!r}, {method!r}")


def test_skip_structured_json() -> None:
    _step("should_skip_distill: structured JSON")
    text = '{"foo": 1, "bar": [1, 2, 3], "baz": {"x": "y"}}'
    reason = dp.should_skip_distill(text, "text")
    _aT("JSON skipped", reason == "structured-json", repr(reason))


def test_skip_diffs() -> None:
    _step("should_skip_distill: diffs")
    text = ("Here is the patch:\n"
            "+ added one\n"
            "+ added two\n"
            "- removed three\n"
            "+ added four\n"
            "- removed five\n"
            "+ added six\n")
    reason = dp.should_skip_distill(text, "text")
    _aT("diff skipped", reason == "contains-diff", repr(reason))


def test_skip_tool_use_payloads() -> None:
    _step("should_skip_distill: tool_use / tool_result")
    text = "Anything at all here, doesn't matter."
    _aT("tool_use skipped",
         dp.should_skip_distill(text, "tool_use") == "content_type=tool_use",
         "")
    _aT("tool_result skipped",
         dp.should_skip_distill(text, "tool_result")
         == "content_type=tool_result", "")


# ─── round-trip + splice ─────────────────────────────────────────────────────

def test_round_trip_preserves_canonical_anchors() -> None:
    _step("make_gist: canonical anchor survival")
    text = ("We hit port 8889 on /v1/batch/objects, env var WV_URL "
            "wasn't set, got UndefinedColumn on v0.8.3 — ran with "
            "`--workers 4`.")
    gist, method = dp.make_gist(text, content_type="text")
    _aT("gist returned",
         gist is not None and method == dp.GIST_METHOD, f"{gist!r}")
    if gist is None:
        return
    _aT("8889 survives", "8889" in gist, gist)
    _aT("/v1/batch/objects survives",
         "/v1/batch/objects" in gist, gist)
    _aT("WV_URL survives", "WV_URL" in gist, gist)
    _aT("v0.8.3 survives", "v0.8.3" in gist, gist)
    _aT("UndefinedColumn survives",
         "UndefinedColumn" in gist, gist)
    _aT("--workers survives", "--workers" in gist, gist)


def test_method_tag_is_set() -> None:
    _step("make_gist: gist_method tag")
    text = ("Quick note about WV_URL and the v0.8.6 release on "
            "port 8889.")
    gist, method = dp.make_gist(text, content_type="text")
    _aT("method tag == claudedj-distill-v1",
         method == "claudedj-distill-v1", repr(method))
    _aT("gist not empty", bool(gist), repr(gist))


def test_lossy_bridge_still_preserves_anchors_via_splice() -> None:
    """When the bridge's distill output drops anchors (it's a length
    truncator, so this is the common case), the splice re-injects them
    verbatim into the [anchors: ...] prefix. The round-trip check runs
    on the FINAL spliced output so a lossy body alone is not a failure.

    Regression: earlier `make_gist` ran the round-trip on body-only.
    That over-rejected anything dejavu's LinePrefixCompressor truncated,
    which is every gist longer than its tier char cap. Fix kept the
    anchor-preservation guarantee (gold) while letting compression
    actually compress (the whole point).
    """
    _step("make_gist: lossy body OK as long as splice re-injects anchors")
    text = ("port 8889 plus WV_URL plus error UndefinedColumn at "
            "/x/y.ts is what we hit.")

    real_bridge = dp._bridge

    class _LossyBridge:
        # noinspection PyMethodMayBeStatic
        def distill(self, _text, **_kwargs):  # type: ignore[no-untyped-def]
            return "all the anchors gone"

    dp._bridge = _LossyBridge()  # type: ignore[assignment]
    try:
        gist, method = dp.make_gist(text, content_type="text")
        _aT("gist returned (not None) when splice re-injects",
             gist is not None and method == "claudedj-distill-v1",
             f"{gist!r}, {method!r}")
        for anchor in ("8889", "WV_URL", "UndefinedColumn", "/x/y.ts"):
            _aT(f"anchor {anchor!r} survives in final gist",
                 gist is not None and anchor in gist,
                 f"got: {gist!r}")
    finally:
        dp._bridge = real_bridge  # type: ignore[assignment]


def test_bridge_exception_returns_none() -> None:
    _step("make_gist: bridge raises → None")
    text = "port 5450, env WV_URL, fine plain prose."

    class _BoomBridge:
        # noinspection PyMethodMayBeStatic
        def distill(self, _text, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("bridge ICE")

    real = dp._bridge
    dp._bridge = _BoomBridge()  # type: ignore[assignment]
    try:
        gist, method = dp.make_gist(text, content_type="text")
        _aT("None returned on bridge exception",
             gist is None and method is None, f"{gist!r}, {method!r}")
    finally:
        dp._bridge = real  # type: ignore[assignment]


def test_empty_input_returns_none() -> None:
    _step("make_gist: empty input")
    gist, method = dp.make_gist("", content_type="text")
    _aT("empty → None",
         gist is None and method is None, f"{gist!r}, {method!r}")


def test_anchor_prefix_format() -> None:
    _step("make_gist: anchor prefix format")
    text = "Set WV_URL to whatever and we're done."
    gist, _ = dp.make_gist(text, content_type="text")
    _aT("gist starts with [anchors:",
         gist is not None and gist.startswith("[anchors:"), repr(gist))


def test_bridge_version_exposed() -> None:
    _step("module: BRIDGE_VERSION + BRIDGE_IS_STUB exposed")
    _aT("BRIDGE_VERSION present",
         hasattr(dp, "BRIDGE_VERSION") and dp.BRIDGE_VERSION,
         f"BRIDGE_VERSION={dp.BRIDGE_VERSION!r}")
    _aT("BRIDGE_IS_STUB present",
         isinstance(dp.BRIDGE_IS_STUB, bool),
         f"BRIDGE_IS_STUB={dp.BRIDGE_IS_STUB!r}")


# ─── runner ──────────────────────────────────────────────────────────────────

def main() -> int:
    tests = [
        test_extracts_paths,
        test_extracts_urls_and_ports,
        test_extracts_env_vars,
        test_extracts_version_strings,
        test_extracts_command_flags,
        test_extracts_error_class_names,
        test_extracts_backtick_identifiers,
        test_skip_code_blocks,
        test_skip_tracebacks,
        test_skip_structured_json,
        test_skip_diffs,
        test_skip_tool_use_payloads,
        test_round_trip_preserves_canonical_anchors,
        test_method_tag_is_set,
        test_lossy_bridge_still_preserves_anchors_via_splice,
        test_bridge_exception_returns_none,
        test_empty_input_returns_none,
        test_anchor_prefix_format,
        test_bridge_version_exposed,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAIL.append((t.__name__, str(e)))
    print()
    print("=" * 60)
    print(f"PASS: {len(PASS)}     FAIL: {len(FAIL)}")
    for m, d in FAIL:
        print(f"  ✗ {m}  {d[:80]}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
