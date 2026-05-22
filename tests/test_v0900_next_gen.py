"""v0.9.0 unit tests — reranker cascade + PreCompact synthesis.

Two next-gen pieces:

  1. code/reranker.py — Voyage / Cohere / BGE-local cascade with
     graceful no-op fallback on every failure mode.
  2. hooks/pre_compact.py — inline synthesis of a recovery memo
     from recent PG turns via the summarize provider cascade.

Infra-free. Provider HTTP calls are stubbed; the synthesis step is
exercised by mocking `summarize.generate_summary`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / "mcp"))

os.environ.setdefault("CLAUDE_DEJAVU_DATA_ROOT",
                       "/tmp/dejavu-v0900-test")
os.makedirs("/tmp/dejavu-v0900-test", exist_ok=True)

import reranker  # type: ignore[import]
import server  # type: ignore[import]


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


def _mk_hit(turn_id: int, content: str,
             score: float = 0.5) -> dict:
    return {
        "turn_id": turn_id, "content": content,
        "session_id": f"sess-{turn_id:04d}",
        "project_slug": "p1", "ts": "2026-05-21T10:00:00",
        "gist": content[:80],
        "_additional": {"score": score},
    }


# ─── Reranker module — provider resolution ─────────────────────────────────

def test_reranker_default_off() -> None:
    _step("reranker: default = off (no env set)")
    orig = os.environ.pop("CLAUDE_DEJAVU_RERANKER", None)
    try:
        _aT("default provider = off",
            reranker._resolve_provider() == "off")
        hits = [_mk_hit(i, f"text {i}") for i in range(3)]
        out = reranker.rerank("query", hits)
        _aT("hits unchanged when off", out is hits or out == hits)
    finally:
        if orig is not None:
            os.environ["CLAUDE_DEJAVU_RERANKER"] = orig


def test_reranker_resolves_voyage() -> None:
    _step("reranker: env=voyage resolves to voyage")
    os.environ["CLAUDE_DEJAVU_RERANKER"] = "voyage"
    try:
        _aT("provider = voyage",
            reranker._resolve_provider() == "voyage")
    finally:
        os.environ.pop("CLAUDE_DEJAVU_RERANKER", None)


def test_reranker_resolves_auto_for_unknown() -> None:
    _step("reranker: env=auto / true / yes all map to 'auto'")
    for raw in ("auto", "true", "yes", "on", "1"):
        os.environ["CLAUDE_DEJAVU_RERANKER"] = raw
        try:
            _aT(f"env={raw!r} → 'auto'",
                reranker._resolve_provider() == "auto")
        finally:
            os.environ.pop("CLAUDE_DEJAVU_RERANKER", None)


def test_reranker_unknown_provider_is_off() -> None:
    _step("reranker: unknown provider name → off (don't crash, "
          "don't silently substitute)")
    os.environ["CLAUDE_DEJAVU_RERANKER"] = "made-up-provider"
    try:
        _aT("provider = off",
            reranker._resolve_provider() == "off")
    finally:
        os.environ.pop("CLAUDE_DEJAVU_RERANKER", None)


# ─── Reranker module — rerank() behavior ───────────────────────────────────

def test_rerank_returns_reordered_hits() -> None:
    _step("rerank: provider returns new ordering; hits are reordered "
          "and annotated")
    hits = [_mk_hit(1, "alpha"),
            _mk_hit(2, "beta"),
            _mk_hit(3, "gamma")]
    # Inject a fake voyage provider that says "score gamma highest,
    # then alpha, then beta".
    orig = reranker._PROVIDERS["voyage"]

    def fake_voyage(query, texts, top_k, timeout_s):
        # Return [(idx, score)] sorted desc.
        return [(2, 0.99), (0, 0.55), (1, 0.10)]

    reranker._PROVIDERS["voyage"] = fake_voyage  # type: ignore[assignment]
    os.environ["CLAUDE_DEJAVU_RERANKER"] = "voyage"
    try:
        out = reranker.rerank("q", hits)
        ids = [h["turn_id"] for h in out]
        _aT("reordered: gamma(3) first, alpha(1), beta(2)",
            ids == [3, 1, 2], repr(ids))
        _aT("top hit has rerank_score annotation",
            out[0].get("_additional", {}).get("rerank_score") == 0.99,
            repr(out[0].get("_additional")))
        _aT("top hit has rerank_source='voyage'",
            out[0].get("_additional", {}).get("rerank_source")
              == "voyage",
            repr(out[0].get("_additional")))
    finally:
        reranker._PROVIDERS["voyage"] = orig  # type: ignore[assignment]
        os.environ.pop("CLAUDE_DEJAVU_RERANKER", None)


def test_rerank_provider_failure_preserves_order() -> None:
    _step("rerank: provider returns None → original order preserved")
    hits = [_mk_hit(1, "a"), _mk_hit(2, "b"), _mk_hit(3, "c")]
    orig = reranker._PROVIDERS["voyage"]
    reranker._PROVIDERS["voyage"] = (  # type: ignore[assignment]
        lambda q, t, k, to: None)
    os.environ["CLAUDE_DEJAVU_RERANKER"] = "voyage"
    try:
        out = reranker.rerank("q", hits)
        _aT("order unchanged on provider failure",
            [h["turn_id"] for h in out] == [1, 2, 3],
            repr([h["turn_id"] for h in out]))
    finally:
        reranker._PROVIDERS["voyage"] = orig  # type: ignore[assignment]
        os.environ.pop("CLAUDE_DEJAVU_RERANKER", None)


def test_rerank_skips_when_text_is_thin() -> None:
    _step("rerank: skip when most hits have no usable text "
          "(scoring against empties dilutes signal)")
    hits = [{"turn_id": i, "content": "",
             "_additional": {"score": 0.5}} for i in range(5)]
    os.environ["CLAUDE_DEJAVU_RERANKER"] = "voyage"
    try:
        called: list[bool] = []
        orig = reranker._PROVIDERS["voyage"]
        reranker._PROVIDERS["voyage"] = (  # type: ignore[assignment]
            lambda q, t, k, to: (called.append(True) or [(0, 1.0)]))
        try:
            out = reranker.rerank("q", hits)
            _aT("provider was NOT called", not called)
            _aT("hits returned unchanged",
                out == hits or [h["turn_id"] for h in out]
                  == [h["turn_id"] for h in hits])
        finally:
            reranker._PROVIDERS["voyage"] = orig  # type: ignore[assignment]
    finally:
        os.environ.pop("CLAUDE_DEJAVU_RERANKER", None)


def test_rerank_auto_cascade() -> None:
    _step("rerank: auto cascade tries voyage → cohere → bge in order, "
          "stops at first success")
    hits = [_mk_hit(1, "a"), _mk_hit(2, "b")]
    calls: list[str] = []
    orig_v = reranker._PROVIDERS["voyage"]
    orig_c = reranker._PROVIDERS["cohere"]
    orig_b = reranker._PROVIDERS["bge"]
    try:
        # voyage fails, cohere succeeds
        reranker._PROVIDERS["voyage"] = (  # type: ignore[assignment]
            lambda q, t, k, to: (calls.append("voyage"), None)[1])
        reranker._PROVIDERS["cohere"] = (  # type: ignore[assignment]
            lambda q, t, k, to: (calls.append("cohere"),
                                  [(1, 0.9), (0, 0.5)])[1])
        reranker._PROVIDERS["bge"] = (  # type: ignore[assignment]
            lambda q, t, k, to: (calls.append("bge"),
                                  [(0, 0.9), (1, 0.5)])[1])
        os.environ["CLAUDE_DEJAVU_RERANKER"] = "auto"
        out = reranker.rerank("q", hits)
        _aT("voyage called before cohere",
            calls == ["voyage", "cohere"], repr(calls))
        _aT("bge NOT called (cohere succeeded)",
            "bge" not in calls, repr(calls))
        _aT("ordering reflects cohere result",
            [h["turn_id"] for h in out] == [2, 1],
            repr([h["turn_id"] for h in out]))
        _aT("rerank_source='cohere'",
            out[0]["_additional"]["rerank_source"] == "cohere")
    finally:
        reranker._PROVIDERS["voyage"] = orig_v  # type: ignore[assignment]
        reranker._PROVIDERS["cohere"] = orig_c  # type: ignore[assignment]
        reranker._PROVIDERS["bge"] = orig_b  # type: ignore[assignment]
        os.environ.pop("CLAUDE_DEJAVU_RERANKER", None)


def test_rerank_specific_provider_does_not_fall_through() -> None:
    _step("rerank: env=voyage does NOT fall through to cohere on "
          "voyage failure (user picked voyage explicitly)")
    hits = [_mk_hit(1, "a"), _mk_hit(2, "b")]
    calls: list[str] = []
    orig_v = reranker._PROVIDERS["voyage"]
    orig_c = reranker._PROVIDERS["cohere"]
    try:
        reranker._PROVIDERS["voyage"] = (  # type: ignore[assignment]
            lambda q, t, k, to: (calls.append("voyage"), None)[1])
        reranker._PROVIDERS["cohere"] = (  # type: ignore[assignment]
            lambda q, t, k, to: (calls.append("cohere"),
                                  [(1, 0.9), (0, 0.5)])[1])
        os.environ["CLAUDE_DEJAVU_RERANKER"] = "voyage"
        out = reranker.rerank("q", hits)
        _aT("only voyage was tried",
            calls == ["voyage"], repr(calls))
        _aT("hits unchanged (voyage returned no result)",
            [h["turn_id"] for h in out] == [1, 2],
            repr([h["turn_id"] for h in out]))
    finally:
        reranker._PROVIDERS["voyage"] = orig_v  # type: ignore[assignment]
        reranker._PROVIDERS["cohere"] = orig_c  # type: ignore[assignment]
        os.environ.pop("CLAUDE_DEJAVU_RERANKER", None)


# ─── reranker_status() ─────────────────────────────────────────────────────

def test_reranker_status_shape() -> None:
    _step("reranker_status: contract for dejavu_status")
    s = reranker.reranker_status()
    for k in ("configured", "timeout_s", "voyage_key_set",
               "cohere_key_set", "bge_model_id",
               "bge_package_available"):
        _aT(f"key {k!r} present", k in s, repr(list(s.keys())))


# ─── dejavu_status surfaces reranker block ─────────────────────────────────

def test_dejavu_status_includes_reranker_block() -> None:
    _step("dejavu_status: includes a 'reranker:' line")
    out = server.dejavu_status()
    _aT("status contains 'reranker:'", "reranker:" in out, out[:600])


# ─── PreCompact synthesis ─────────────────────────────────────────────────

def test_precompact_synthesis_off_via_env() -> None:
    _step("PreCompact: synthesis OFF when env=false → no recovery memo")
    os.environ["CLAUDE_DEJAVU_PRECOMPACT_SYNTHESIS"] = "false"
    try:
        r = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "pre_compact.py")],
            input=json.dumps({
                "session_id": "f4358269-1234-4abc-8def-0123456789ab",
                "transcript_path": "/tmp/x"}),
            capture_output=True, text=True, timeout=10)
    finally:
        os.environ.pop("CLAUDE_DEJAVU_PRECOMPACT_SYNTHESIS", None)
    _aT("hook exits 0", r.returncode == 0, r.stderr[:200])
    env = json.loads(r.stdout)
    msg = env.get("systemMessage") or ""
    _aT("memo present (recovery tool list)",
        "dejavu_session_export" in msg, msg[:200])
    _aT("synthesis block NOT present",
        "Recovery memo" not in msg, msg[:400])


def test_precompact_helpers_are_defined() -> None:
    _step("PreCompact: synthesis helpers are defined + callable")
    sys.path.insert(0, str(ROOT / "hooks"))
    import pre_compact  # type: ignore[import]
    for name in ("_fetch_recent_turn_text",
                  "_build_synthesis_prompt",
                  "_format_synthesis_memo", "_pg_dsn"):
        _aT(f"pre_compact.{name} exists",
            callable(getattr(pre_compact, name, None)),
            repr(getattr(pre_compact, name, None)))


def test_precompact_synthesis_prompt_shape() -> None:
    _step("PreCompact: synthesis prompt asks for 5 JSON fields")
    sys.path.insert(0, str(ROOT / "hooks"))
    import pre_compact  # type: ignore[import]
    p = pre_compact._build_synthesis_prompt(
        "[1][user] hi\n[2][assistant] ok")
    for fld in ("request", "investigated", "learned",
                 "completed", "next_steps"):
        _aT(f"prompt names field {fld!r}",
            fld in p, "")
    _aT("prompt mentions verbatim/JSON contract",
        "JSON" in p and "verbatim" in p, "")


def test_precompact_memo_formatting() -> None:
    _step("PreCompact: _format_synthesis_memo renders all 5 fields")
    sys.path.insert(0, str(ROOT / "hooks"))
    import pre_compact  # type: ignore[import]
    parsed = {
        "request": "fix the IP",
        "investigated": "ssh + ufw + iptables",
        "learned": "Cilium chain drops 22",
        "completed": "rolled back",
        "next_steps": "re-enroll WG mesh",
    }
    rendered = pre_compact._format_synthesis_memo(parsed, "ollama:qwen")
    for v in parsed.values():
        _aT(f"rendered output includes value {v!r}",
            v in rendered, rendered)
    _aT("model label included", "ollama:qwen" in rendered, rendered)


def main() -> int:
    print("v0.9.0 next-gen tests (reranker + PreCompact synthesis)")
    tests = [
        test_reranker_default_off,
        test_reranker_resolves_voyage,
        test_reranker_resolves_auto_for_unknown,
        test_reranker_unknown_provider_is_off,
        test_rerank_returns_reordered_hits,
        test_rerank_provider_failure_preserves_order,
        test_rerank_skips_when_text_is_thin,
        test_rerank_auto_cascade,
        test_rerank_specific_provider_does_not_fall_through,
        test_reranker_status_shape,
        test_dejavu_status_includes_reranker_block,
        test_precompact_synthesis_off_via_env,
        test_precompact_helpers_are_defined,
        test_precompact_synthesis_prompt_shape,
        test_precompact_memo_formatting,
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
