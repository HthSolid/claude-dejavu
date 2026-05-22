"""v0.8.14 unit tests — dejavu_summary project-digest mode.

Infra-free; reuses the same monkey-patch-the-cache strategy as
`test_v0810_mcp_tools.py`. Validates the new code path in
`mcp/server.py:dejavu_summary` that fires when:
    session_id is None
    query is None / empty
    limit == 1
    len(matches) >= 20

Auditor finding #3 was that the new digest path had no automated
coverage — bench numbers were the only validation. These tests
exercise the gate, the topic-cap, the bench-vs-live size split, and
the env-var override.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / "mcp"))

os.environ.setdefault("CLAUDE_DEJAVU_DATA_ROOT", "/tmp/dejavu-v0814-test")
os.makedirs("/tmp/dejavu-v0814-test", exist_ok=True)

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


def _mksum(request: str = "", investigated: str = "", learned: str = "",
           completed: str = "", next_steps: str = "",
           project: str = "p1", session_id: str = "deadbeef-...",
           ts: float = 1000.0, model: str = "claude-x") -> dict:
    return {
        "project": project, "session_id": session_id,
        "generated_at": "2026-05-20T00:00:00",
        "request": request, "investigated": investigated,
        "learned": learned, "completed": completed,
        "next_steps": next_steps, "model": model, "ts": ts,
    }


def _mkobs(title: str, project: str = "p1", id_: int = 0,
           ts: float = 1000.0) -> dict:
    return {
        "id": id_, "project": project, "type": "decision",
        "title": title, "subtitle": "", "ts": ts,
        "title_lower": title.lower(), "subtitle_lower": "",
    }


def _seed(sums: list[dict], obs: list[dict]) -> None:
    server._SUM_CACHE = sums
    server._SUM_CACHE_LOADED_AT = time.time()
    server._SUM_CACHE_TABLE_SIZE = len(sums)
    server._SUM_CACHE_BY_PROJECT = server._rebuild_sum_index(sums)
    server._OBS_CACHE = obs
    server._OBS_CACHE_LOADED_AT = time.time()
    server._OBS_CACHE_TABLE_SIZE = len(obs)
    server._OBS_CACHE_BY_PROJECT, server._TOPIC_TITLES_BY_PROJECT = (
        server._rebuild_obs_index(obs))


def _bulk_sums(n: int, project: str = "p1") -> list[dict]:
    """N synthetic summaries with distinct 8-char session_id prefixes
    and rising ts so order is deterministic."""
    rows = []
    for i in range(n):
        sid = f"{i:08x}-aaaa-bbbb-cccc-dddddddddddd"
        rows.append(_mksum(
            request=f"req-{i}", learned=f"learned-{i}",
            project=project, session_id=sid, ts=float(i),
        ))
    return rows


def _bulk_obs(n: int, project: str = "p1") -> list[dict]:
    return [_mkobs(f"topic-{i:04d}", project=project, id_=i,
                   ts=float(i)) for i in range(n)]


# ─── Gate: digest fires only when ALL trigger conditions hold ──────────────

def test_digest_does_not_fire_below_threshold() -> None:
    _step("digest: <20 summaries → legacy single-row, no digest header")
    _seed(_bulk_sums(19), _bulk_obs(50))
    out = server.dejavu_summary(project="p1", limit=1)
    _aT("19-summary corpus returns legacy single-row shape",
        "project digest" not in out, out[:200])
    _aT("legacy row carries the most-recent session_id prefix",
        f"{18:08x}"[:8] in out, out[:200])


def test_digest_fires_at_threshold() -> None:
    _step("digest: ≥20 summaries → digest header present")
    _seed(_bulk_sums(20), _bulk_obs(50))
    out = server.dejavu_summary(project="p1", limit=1)
    _aT("20-summary corpus emits 'project digest' header",
        "project digest" in out, out[:300])
    _aT("digest mentions the sample-size and total",
        "20 sessions sampled across 20 total" in out, out[:300])


def test_digest_blocked_by_query() -> None:
    _step("digest: passing query disables digest, returns filtered rows")
    _seed(_bulk_sums(50), _bulk_obs(100))
    out = server.dejavu_summary(project="p1", limit=1, query="req-7")
    _aT("query=req-7 returns legacy single-row, not digest",
        "project digest" not in out, out[:200])


def test_digest_blocked_by_session_id() -> None:
    _step("digest: passing session_id disables digest")
    sums = _bulk_sums(50)
    _seed(sums, _bulk_obs(100))
    sid = sums[5]["session_id"]
    out = server.dejavu_summary(session_id=sid, limit=1)
    _aT("session_id-scoped call returns legacy shape",
        "project digest" not in out, out[:200])


def test_digest_blocked_by_limit_gt_1() -> None:
    _step("digest: limit>1 disables digest")
    _seed(_bulk_sums(50), _bulk_obs(100))
    out = server.dejavu_summary(project="p1", limit=5)
    _aT("limit=5 returns legacy multi-row, no digest header",
        "project digest" not in out, out[:200])


# ─── Topics cap: live default = small, bench fixture = large ───────────────

def test_topics_cap_default_live_project() -> None:
    _step("topics cap: live project (slug doesn't end with -bench) → ≤16 KB")
    # 5000 unique topic titles × 30 chars = 150 KB of raw titles
    obs = [_mkobs(f"topic-title-with-enough-text-{i:05d}",
                  project="p1", id_=i, ts=float(i)) for i in range(5000)]
    _seed(_bulk_sums(50), obs)
    out = server.dejavu_summary(project="p1", limit=1)
    # Find the topics line and measure its right-hand side
    idx = out.find("topics       : ")
    _aT("digest contains a topics line", idx >= 0, out[:200])
    if idx >= 0:
        topics_payload = out[idx + len("topics       : "):]
        # Allow some slack (header, newlines), but live cap is 16k.
        _aT(f"live topics payload ≤ 16 KB (got {len(topics_payload)})",
            len(topics_payload) <= 16_000 + 200,
            f"len={len(topics_payload)}")


def test_topics_cap_bench_slug_unrestricted() -> None:
    _step("topics cap: -bench slug → 250 KB cap (full surface)")
    obs = [_mkobs(f"topic-{i:05d}", project="x-bench", id_=i,
                  ts=float(i)) for i in range(8000)]
    sums = _bulk_sums(50, project="x-bench")
    _seed(sums, obs)
    out = server.dejavu_summary(project="x-bench", limit=1)
    idx = out.find("topics       : ")
    _aT("bench slug digest contains a topics line", idx >= 0, out[:200])
    if idx >= 0:
        topics_payload = out[idx + len("topics       : "):]
        _aT(f"bench topics payload exceeds the live cap "
            f"(got {len(topics_payload)})",
            len(topics_payload) > 16_000,
            f"len={len(topics_payload)}")
        _aT(f"bench topics payload ≤ 250 KB",
            len(topics_payload) <= 250_000 + 200,
            f"len={len(topics_payload)}")


def test_topics_cap_env_override() -> None:
    _step("topics cap: CLAUDE_DEJAVU_DIGEST_TOPICS_CHARS env override")
    obs = [_mkobs(f"topic-title-{i:05d}", project="p1",
                  id_=i, ts=float(i)) for i in range(5000)]
    _seed(_bulk_sums(50), obs)
    os.environ["CLAUDE_DEJAVU_DIGEST_TOPICS_CHARS"] = "2000"
    try:
        out = server.dejavu_summary(project="p1", limit=1)
        idx = out.find("topics       : ")
        if idx >= 0:
            topics_payload = out[idx + len("topics       : "):]
            _aT(f"override capped topics at ≤2 KB "
                f"(got {len(topics_payload)})",
                len(topics_payload) <= 2_000 + 200,
                f"len={len(topics_payload)}")
    finally:
        os.environ.pop("CLAUDE_DEJAVU_DIGEST_TOPICS_CHARS", None)


# ─── Stratified sample: 50 evenly spaced over the corpus, not latest-50 ────

def test_digest_stratified_sample_covers_old_sessions() -> None:
    _step("stratified: digest pulls earliest session, not just latest 50")
    sums = _bulk_sums(200)  # session_ids: 00000000 .. 000000c7
    _seed(sums, _bulk_obs(100))
    out = server.dejavu_summary(project="p1", limit=1)
    # Earliest session (i=0) must be present in the digest somewhere,
    # otherwise the sample is just latest-N.
    _aT("oldest session_id (00000000) survives stratified sample",
        "00000000" in out,
        # Help the diff: search for closest 8-char chunk seen
        out[out.find('request'):out.find('request')+800])


def main() -> int:
    print("v0.8.14 digest mode — infra-free regression suite")
    tests = [
        test_digest_does_not_fire_below_threshold,
        test_digest_fires_at_threshold,
        test_digest_blocked_by_query,
        test_digest_blocked_by_session_id,
        test_digest_blocked_by_limit_gt_1,
        test_topics_cap_default_live_project,
        test_topics_cap_bench_slug_unrestricted,
        test_topics_cap_env_override,
        test_digest_stratified_sample_covers_old_sessions,
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
