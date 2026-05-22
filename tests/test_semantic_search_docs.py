"""Verifies the ClaudeDejavuDoc class definition is well-formed.
No live Weaviate required."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

PASS, FAIL = [], []
def _ok(m): PASS.append(m); print(f"  \033[32m✓\033[0m {m}", flush=True)
def _f(m, d=""): FAIL.append((m, d)); print(f"  \033[31m✗\033[0m {m}"); d and print(f"      {d}")
def _step(n): print(f"\n\033[36m── {n} ──\033[0m", flush=True)
def _aT(n, c, d=""): _ok(n) if c else _f(n, d)


def test_doc_class_definition_well_formed():
    _step("ClaudeDejavuDoc class definition exists and has required properties")
    import semantic_search
    _aT("DOC_CLASS_NAME defined",
         hasattr(semantic_search, "DOC_CLASS_NAME") and
         semantic_search.DOC_CLASS_NAME == "ClaudeDejavuDoc")
    schema = semantic_search._doc_class_schema()
    _aT("class name matches", schema["class"] == "ClaudeDejavuDoc")
    _aT("vectorizer is text2vec-transformers",
         schema["vectorizer"] == "text2vec-transformers")
    props = {p["name"] for p in schema["properties"]}
    expected = {"repo_id", "file_path", "heading_path", "chunk_idx",
                "one_line_summary", "content", "mtime", "content_hash"}
    _aT(f"properties == {sorted(expected)}",
         props == expected, str(sorted(props)))


def test_index_docs_callable_signature():
    _step("index_docs is callable with (repo_id, file_path, chunks, *, mtime_iso)")
    import inspect, semantic_search
    sig = inspect.signature(semantic_search.index_docs)
    _aT("has parameters: repo_id, file_path, chunks",
         {"repo_id", "file_path", "chunks"} <= set(sig.parameters.keys()),
         str(sig))


def test_search_signature_accepts_kinds_and_repo_id():
    _step("semantic_search.search accepts kinds + repo_id params")
    import inspect, semantic_search
    sig = inspect.signature(semantic_search.search)
    _aT("has 'kinds' parameter", "kinds" in sig.parameters, str(sig))
    _aT("has 'repo_id' parameter", "repo_id" in sig.parameters, str(sig))
    # back-compat: kinds default keeps old behavior (turn-only)
    _aT("'kinds' default preserves back-compat",
         sig.parameters["kinds"].default in (None, ["turn"], ("turn",)),
         f"default={sig.parameters['kinds'].default}")


def test_rrf_fuse_helper_exists():
    _step("semantic_search._rrf_fuse exists for cross-kind fusion")
    import semantic_search
    _aT("_rrf_fuse callable",
         hasattr(semantic_search, "_rrf_fuse") and
         callable(semantic_search._rrf_fuse))
    fused = semantic_search._rrf_fuse([
        [{"result_id": "a"}, {"result_id": "b"}],
        [{"result_id": "a"}, {"result_id": "c"}],
    ])
    _aT("a ranked first (appears in both lists)",
         fused[0]["result_id"] == "a", str(fused))


def test_expand_dispatches_by_prefix():
    _step("dejavu_expand dispatches int → turn, doc| → doc")
    sys.path.insert(0, str(ROOT / "mcp"))
    import server
    _aT("server has _expand_dispatch helper",
         hasattr(server, "_expand_dispatch"))
    handler = server._expand_dispatch("doc|abc-uuid|docs/foo.md|0")
    _aT("doc| prefix → doc handler", handler == server._expand_doc)
    handler = server._expand_dispatch(42)
    _aT("int → turn handler", handler == server._expand_turn)
    handler = server._expand_dispatch("turn|42")
    _aT("turn| prefix → turn handler", handler == server._expand_turn)


def test_expand_doc_section_returns_content():
    _step("dejavu_expand(doc-id, level='section') returns chunk content")
    sys.path.insert(0, str(ROOT / "mcp"))
    import server
    # Inject a fake Weaviate fetcher.
    original = server._fetch_doc_chunk
    server._fetch_doc_chunk = lambda rid, fp, idx: {
        "content": "FAKE SECTION CONTENT",
        "heading_path": "Top > A",
    }
    try:
        out = server._expand_doc("doc|abc|docs/x.md|0", level="section")
        _aT("output contains the chunk content",
             "FAKE SECTION CONTENT" in out, out[:200])
        _aT("output includes heading_path",
             "Top > A" in out, out[:200])
    finally:
        server._fetch_doc_chunk = original


def test_expand_doc_file_stale_path_fallback():
    _step("level='file' returns section + warning when path is stale")
    sys.path.insert(0, str(ROOT / "mcp"))
    import server
    orig_fetch = server._fetch_doc_chunk
    orig_resolve = server._resolve_doc_file_path
    server._fetch_doc_chunk = lambda rid, fp, idx: {
        "content": "FAKE CHUNK", "heading_path": "Top"}
    server._resolve_doc_file_path = lambda rid, fp: None  # always stale
    try:
        out = server._expand_doc("doc|abc|nope.md|0", level="file")
        _aT("output contains stale-path warning",
             "no longer at last-known path" in out, out[:200])
        _aT("output still returns the chunk content",
             "FAKE CHUNK" in out, out[:200])
    finally:
        server._fetch_doc_chunk = orig_fetch
        server._resolve_doc_file_path = orig_resolve


def test_backfill_repo_ids_into_code_exists():
    _step("backfill_repo_ids_into_code is callable")
    import semantic_search
    _aT("function exists",
         hasattr(semantic_search, "backfill_repo_ids_into_code") and
         callable(semantic_search.backfill_repo_ids_into_code))
    # Inspect signature: should take no required args
    import inspect
    sig = inspect.signature(semantic_search.backfill_repo_ids_into_code)
    required = [p for p in sig.parameters.values()
                  if p.default is inspect.Parameter.empty]
    _aT("no required positional params", required == [],
         f"got required={required}")


def test_backfill_no_weaviate_returns_zero():
    _step("backfill returns {tagged: 0, orphan_slugs: []} when Weaviate down")
    import semantic_search
    # Monkey-patch is_available to False to short-circuit cleanly.
    original = semantic_search.is_available
    semantic_search.is_available = lambda: False
    try:
        result = semantic_search.backfill_repo_ids_into_code()
        _aT("tagged == 0", result.get("tagged") == 0, str(result))
        _aT("orphan_slugs is list", isinstance(result.get("orphan_slugs"), list),
             str(result))
    finally:
        semantic_search.is_available = original


def test_code_class_schema_can_carry_repo_id():
    _step("ensure_class wires repo_id as an addProperty call for older installs")
    src = (ROOT / "code" / "semantic_search.py").read_text()
    _aT("ensure_class checks for repo_id property",
         '"repo_id"' in src and "addProperty" in src.lower() or
         "/properties" in src,
         "must defensively addProperty repo_id for upgraded installs")


def main() -> int:
    tests = [test_doc_class_definition_well_formed,
              test_index_docs_callable_signature,
              test_search_signature_accepts_kinds_and_repo_id,
              test_rrf_fuse_helper_exists,
              test_expand_dispatches_by_prefix,
              test_expand_doc_section_returns_content,
              test_expand_doc_file_stale_path_fallback,
              test_backfill_repo_ids_into_code_exists,
              test_backfill_no_weaviate_returns_zero,
              test_code_class_schema_can_carry_repo_id,
              test_wv_url_reads_config_env_when_env_missing,
              test_wv_url_env_overrides_config_env]
    for t in tests:
        try: t()
        except Exception as e:
            import traceback; traceback.print_exc()
            FAIL.append((t.__name__, str(e)))
    print(f"\n{'='*60}\nPASS: {len(PASS)}  FAIL: {len(FAIL)}")
    return 0 if not FAIL else 1


def test_wv_url_reads_config_env_when_env_missing():
    """Regression (2026-05-17): semantic_search._wv_url must fall back
    to config.env's WV_URL when the env var is unset — otherwise every
    reindex --docs / search-docs / ensure_class hits the default
    localhost:8080 and connect-refuses against the installer's actual
    port (commonly 8889 in dejavu's Docker-Compose stack)."""
    import os, tempfile, importlib
    _step("_wv_url falls back to config.env WV_URL when env unset")
    import semantic_search
    with tempfile.TemporaryDirectory() as td:
        cfg = os.path.join(td, "config.env")
        with open(cfg, "w") as f:
            f.write("# comment line\n")
            f.write("WV_URL=http://fixture-host:1234\n")
            f.write("OTHER_KEY=ignored\n")
        old_env = os.environ.pop("WV_URL", None)
        old_xdg = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = td
        # The loader looks for <XDG_DATA_HOME>/claude-dejavu/config.env
        os.makedirs(os.path.join(td, "claude-dejavu"), exist_ok=True)
        import shutil; shutil.move(cfg, os.path.join(td, "claude-dejavu", "config.env"))
        try:
            url = semantic_search._wv_url()
            _aT("returns config.env value", url == "http://fixture-host:1234",
                 f"got {url!r}")
        finally:
            if old_env is not None: os.environ["WV_URL"] = old_env
            if old_xdg is None: os.environ.pop("XDG_DATA_HOME", None)
            else: os.environ["XDG_DATA_HOME"] = old_xdg


def test_wv_url_env_overrides_config_env():
    """env var beats config.env — keeps tests + ad-hoc overrides
    deterministic."""
    import os
    _step("_wv_url uses env value when set, not config.env")
    import semantic_search
    old_env = os.environ.get("WV_URL")
    os.environ["WV_URL"] = "http://env-wins:9999"
    try:
        url = semantic_search._wv_url()
        _aT("returns env value", url == "http://env-wins:9999",
             f"got {url!r}")
    finally:
        if old_env is None: os.environ.pop("WV_URL", None)
        else: os.environ["WV_URL"] = old_env


if __name__ == "__main__":
    sys.exit(main())
