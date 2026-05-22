"""Unit tests for the v0.8.6 search response shape +
`_parse_anchors` helper.

We don't reach Postgres/Weaviate. Instead, we exercise
`_parse_anchors` directly (pure function) and assert the v0.8.6
search shape contract:

  - `[anchors: a, b, c] body` parses back to (body, anchors)
  - legacy `rule`/`llm` gists pass through unchanged
  - empty / None gracefully handled

We also do a smoke-test against `cmd_search` via a stub _hybrid +
fake conn so we can verify the new --full flag changes the output
shape (full content vs default first-200).
"""
from __future__ import annotations

import io
import contextlib
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

import recall  # noqa: E402  type: ignore[import]


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


# ─── _parse_anchors pure-function tests ──────────────────────────────────────

def test_parse_anchors_basic() -> None:
    _step("_parse_anchors: basic [anchors: …] body parses out")
    gist = ("[anchors: WV_URL, 8889, /v1/batch/objects] We hit "
            "port 8889 on /v1/batch/objects via WV_URL.")
    body, anchors = recall._parse_anchors(gist)
    _aT("body strips prefix",
         body == "We hit port 8889 on /v1/batch/objects via WV_URL.",
         repr(body))
    _aT("3 anchors parsed",
         anchors == ["WV_URL", "8889", "/v1/batch/objects"],
         repr(anchors))


def test_parse_anchors_legacy_passes_through() -> None:
    _step("_parse_anchors: legacy gist (no anchor prefix) passes "
          "through unchanged")
    gist = "The Weaviate URL env var was unset."
    body, anchors = recall._parse_anchors(gist)
    _aT("body equals input", body == gist, repr(body))
    _aT("no anchors extracted", anchors == [], repr(anchors))


def test_parse_anchors_empty() -> None:
    _step("_parse_anchors: empty + None handled")
    _aT("None → (None, [])",
         recall._parse_anchors(None) == (None, []),
         repr(recall._parse_anchors(None)))
    _aT("empty → (\"\", [])",
         recall._parse_anchors("") == ("", []),
         repr(recall._parse_anchors("")))


def test_parse_anchors_malformed_bracket() -> None:
    _step("_parse_anchors: missing close bracket passes through")
    gist = "[anchors: WV_URL, 8889 with no close"
    body, anchors = recall._parse_anchors(gist)
    _aT("malformed body unchanged", body == gist, repr(body))
    _aT("no anchors extracted", anchors == [], repr(anchors))


def test_parse_anchors_empty_anchor_list() -> None:
    _step("_parse_anchors: [anchors:] empty list")
    gist = "[anchors: ] body here"
    body, anchors = recall._parse_anchors(gist)
    _aT("body strips prefix", body == "body here", repr(body))
    _aT("empty anchor list", anchors == [], repr(anchors))


# ─── cmd_search smoke test against stubbed _hybrid + fake conn ──────────────

class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def execute(self, sql, params=()):
        # Pull the requested ids and synthesize fixture rows.
        ids = params[0] if params and isinstance(params[0], (list, tuple)) else []
        # We always have id=1 in the fixture, with a v0.8.6 gist +
        # a long content body.
        if "substring(content" in sql:
            self._cur = [
                (1, "[anchors: WV_URL, 8889] v0.8.6 anchor-prefixed gist.",
                 "first 200 chars of the full content body…"),
            ]
        else:
            self._cur = [
                (1, "[anchors: WV_URL, 8889] v0.8.6 anchor-prefixed gist.",
                 "<<< FULL CONTENT — every byte of the historical turn, "
                 "all 32 megabytes of it, well, just enough to verify "
                 "--full reaches here rather than 200 chars. >>>"),
            ]

    def fetchall(self):
        return list(self._cur)

    def fetchone(self):
        return self._cur[0] if self._cur else None


class _FakeConn:
    def __init__(self):
        self._cur = _FakeCursor([])

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def cursor(self, **_kw):
        return _FakeCursor([])


def _make_args(**kw):
    defaults = dict(
        query="dummy", limit=5, mode="grunt", full=False, scope=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_search_default_shape_is_gist_or_truncated() -> None:
    _step("cmd_search: default shape uses gist (with anchors footer)")
    real_hybrid = recall._hybrid
    real_conn = recall._conn

    def _stub_hybrid(q, lim, sc):
        return [{"turn_id": 1, "session_id": "deadbeef-…",
                 "project_slug": "foo",
                 "ts": "2026-05-18T00:00:00+00:00",
                 "_additional": {"score": 3.5},
                 "ai_title": "fallback"}]

    recall._hybrid = _stub_hybrid
    recall._conn = lambda: _FakeConn()
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            recall.cmd_search(_make_args(full=False))
        out = buf.getvalue()
    finally:
        recall._hybrid = real_hybrid
        recall._conn = real_conn

    _aT("body (without anchor prefix) is shown",
         "v0.8.6 anchor-prefixed gist" in out, out[-300:])
    _aT("anchor footer present",
         "anchors: WV_URL, 8889" in out, out[-300:])
    _aT("full content NOT included",
         "FULL CONTENT" not in out, out[-300:])


def test_search_full_flag_returns_content() -> None:
    _step("cmd_search --full: full content is included")
    real_hybrid = recall._hybrid
    real_conn = recall._conn

    def _stub_hybrid(q, lim, sc):
        return [{"turn_id": 1, "session_id": "deadbeef-…",
                 "project_slug": "foo",
                 "ts": "2026-05-18T00:00:00+00:00",
                 "_additional": {"score": 3.5},
                 "ai_title": "fallback"}]

    recall._hybrid = _stub_hybrid
    recall._conn = lambda: _FakeConn()
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            recall.cmd_search(_make_args(full=True))
        out = buf.getvalue()
    finally:
        recall._hybrid = real_hybrid
        recall._conn = real_conn

    _aT("full content shown",
         "FULL CONTENT" in out, out[-400:])


# ─── runner ──────────────────────────────────────────────────────────────────

def test_hybrid_uses_bm25_not_hybrid_graphql() -> None:
    """Regression (2026-05-18): `_hybrid` builds a Weaviate GraphQL
    query that must use `bm25:`, not `hybrid:`. ClaudeDejavuTurn ships
    with vectorizer:none — `hybrid:` 404s against it. The function name
    stays `_hybrid` for back-compat but the underlying query is BM25.
    The same root cause was fixed in the JIT recall hook in 0208d8d;
    this test prevents either surface from drifting back.

    v0.8.7 update: `_hybrid` may also fire a nearVector branch when the
    caller-side cloud embed succeeds. The test pins down the BM25
    body separately from any embed/nearVector traffic by capturing
    bodies on a list and asserting that AT LEAST ONE captured body
    uses bm25: and NONE uses hybrid:."""
    _step("_hybrid: GraphQL query uses bm25: (not hybrid:)")
    captured: list[str] = []

    class _FakeResp:
        def __init__(self, body: bytes) -> None: self._body = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self) -> bytes: return self._body

    real_urlopen = recall.urllib.request.urlopen

    def fake_urlopen(req, timeout=30):  # type: ignore[no-untyped-def]
        try:
            data = req.data.decode("utf-8") if req.data else ""
            captured.append(data)
        except Exception:
            captured.append("")
        url = getattr(req, "full_url", "") or ""
        # /v1/embed gets a vector response; /v1/graphql gets an empty
        # hit set. We don't care about the response content past the
        # captured-body assertions.
        if "/v1/embed" in url:
            return _FakeResp(b'{"embeddings":[[0.1,0.2,0.3,0.4]]}')
        return _FakeResp(b'{"data":{"Get":{"ClaudeDejavuTurn":[]}}}')

    # v0.8.7: also stub query_embed.urllib.urlopen since embed_query
    # uses its own import. We patch BOTH so the test is stable
    # regardless of whether the user has a cloud key set locally.
    import query_embed as qe  # type: ignore[import]
    real_qe_urlopen = qe.urllib.request.urlopen

    recall.urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]
    qe.urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]
    qe._cache_reset()
    try:
        recall._hybrid("rsync to restic", limit=3,
                         scope=recall._get_scope(SimpleNamespace(scope=None)))
        graphql_bodies = [b for b in captured if "bm25:" in b or "hybrid:" in b]
        _aT("at least one GraphQL body captured",
            len(graphql_bodies) >= 1, repr(captured)[:200])
        _aT("uses bm25: keyword search",
             any("bm25:" in b for b in graphql_bodies),
             repr(graphql_bodies)[:300])
        _aT("does NOT use hybrid: (would 404 on vectorizer:none class)",
             all("hybrid:" not in b for b in graphql_bodies),
             repr(graphql_bodies)[:300])
    finally:
        recall.urllib.request.urlopen = real_urlopen  # type: ignore[assignment]
        qe.urllib.request.urlopen = real_qe_urlopen  # type: ignore[assignment]


def main() -> int:
    tests = [
        test_parse_anchors_basic,
        test_parse_anchors_legacy_passes_through,
        test_parse_anchors_empty,
        test_parse_anchors_malformed_bracket,
        test_parse_anchors_empty_anchor_list,
        test_search_default_shape_is_gist_or_truncated,
        test_search_full_flag_returns_content,
        test_hybrid_uses_bm25_not_hybrid_graphql,
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
