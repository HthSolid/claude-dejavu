"""v0.8.12 — verify the ingester honors CLAUDE_DEJAVU_WV_CLASS / WV_CLASS.

The default ingest class is ``ClaudeDejavuTurn``. v0.8.12 needs to point
the ingester at ``ClaudeDejavuTurn_bench`` for bench-fixture rebuilds
without polluting the live class. The override matches the parity rule
already in ``code/recall.py:59``: env wins, config.env second, default
last.

Tests reload the ``ingester`` module after each env tweak so the
module-level ``WEAVIATE_CLASS`` constant picks up the new value — that
mirrors how ``tests/_ingest_bench_corpus_0812.py`` arranges the import
order for the bench rebuild.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))


PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(msg: str) -> None:
    PASS.append(msg)
    print(f"  \033[32m✓\033[0m {msg}", flush=True)


def _fail(msg: str, d: str = "") -> None:
    FAIL.append((msg, d))
    print(f"  \033[31m✗\033[0m {msg}")
    if d:
        print(f"      {d}")


def _aT(name: str, cond: bool, d: str = "") -> None:
    _ok(name) if cond else _fail(name, d)


def _step(name: str) -> None:
    print(f"\n\033[36m── {name} ──\033[0m", flush=True)


def _reload_ingester():
    """Drop the module cache so module-level config reads pick up env."""
    if "ingester" in sys.modules:
        importlib.reload(sys.modules["ingester"])
    else:
        import ingester  # noqa: F401
    return sys.modules["ingester"]


def _save_env() -> dict[str, str | None]:
    return {
        "CLAUDE_DEJAVU_WV_CLASS": os.environ.get("CLAUDE_DEJAVU_WV_CLASS"),
        "CLAUDE_DEJAVU_DATA_ROOT": os.environ.get("CLAUDE_DEJAVU_DATA_ROOT"),
    }


def _restore_env(saved: dict[str, str | None]) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_default_unchanged() -> None:
    _step("default ingester.WEAVIATE_CLASS is ClaudeDejavuTurn")
    saved = _save_env()
    try:
        os.environ.pop("CLAUDE_DEJAVU_WV_CLASS", None)
        # Point DATA_ROOT at a tmp dir with NO config.env so the only
        # source of WV class is the in-tree default.
        with tempfile.TemporaryDirectory() as td:
            os.environ["CLAUDE_DEJAVU_DATA_ROOT"] = td
            ing = _reload_ingester()
            _aT("WEAVIATE_CLASS == 'ClaudeDejavuTurn'",
                 ing.WEAVIATE_CLASS == "ClaudeDejavuTurn",
                 f"got {ing.WEAVIATE_CLASS!r}")
    finally:
        _restore_env(saved)


def test_env_override_wins() -> None:
    _step("CLAUDE_DEJAVU_WV_CLASS env var overrides default")
    saved = _save_env()
    try:
        with tempfile.TemporaryDirectory() as td:
            os.environ["CLAUDE_DEJAVU_DATA_ROOT"] = td
            os.environ["CLAUDE_DEJAVU_WV_CLASS"] = "ClaudeDejavuTurn_bench"
            ing = _reload_ingester()
            _aT("WEAVIATE_CLASS == 'ClaudeDejavuTurn_bench'",
                 ing.WEAVIATE_CLASS == "ClaudeDejavuTurn_bench",
                 f"got {ing.WEAVIATE_CLASS!r}")
    finally:
        _restore_env(saved)


def test_config_env_fallback() -> None:
    _step("WV_CLASS in config.env honored when env var unset")
    saved = _save_env()
    try:
        os.environ.pop("CLAUDE_DEJAVU_WV_CLASS", None)
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config.env"
            cfg.write_text("WV_CLASS=ClaudeDejavuTurn_bench\n")
            os.environ["CLAUDE_DEJAVU_DATA_ROOT"] = td
            ing = _reload_ingester()
            _aT("WEAVIATE_CLASS reads from config.env",
                 ing.WEAVIATE_CLASS == "ClaudeDejavuTurn_bench",
                 f"got {ing.WEAVIATE_CLASS!r}")
    finally:
        _restore_env(saved)


def test_env_wins_over_config_env() -> None:
    _step("env var wins over config.env (precedence order)")
    saved = _save_env()
    try:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config.env"
            cfg.write_text("WV_CLASS=FromConfigFile\n")
            os.environ["CLAUDE_DEJAVU_DATA_ROOT"] = td
            os.environ["CLAUDE_DEJAVU_WV_CLASS"] = "FromEnvVar"
            ing = _reload_ingester()
            _aT("env var wins",
                 ing.WEAVIATE_CLASS == "FromEnvVar",
                 f"got {ing.WEAVIATE_CLASS!r}")
    finally:
        _restore_env(saved)


def test_class_constant_used_by_batch_upsert() -> None:
    """Sanity check: the override actually flows into the batch_upsert
    code path. We don't talk to Weaviate; we just verify that
    weaviate_batch_upsert builds objects with the overridden class
    name in the {'class': ...} envelope."""
    _step("weaviate_batch_upsert builds objects with overridden class")
    saved = _save_env()
    try:
        with tempfile.TemporaryDirectory() as td:
            os.environ["CLAUDE_DEJAVU_DATA_ROOT"] = td
            os.environ["CLAUDE_DEJAVU_WV_CLASS"] = "ClaudeDejavuTurn_bench"
            ing = _reload_ingester()
            # Replace urllib so no real HTTP. Capture the request body.
            captured: dict = {}

            class _FakeResp:
                def __enter__(self): return self
                def __exit__(self, *_a): return False
                def read(self):
                    # Mimic a single-item success envelope per item.
                    import json as _json
                    return _json.dumps(
                        [{"result": {"status": "SUCCESS"}}]
                    ).encode()

            def _fake_urlopen(req, timeout=0):
                import json as _json
                captured["body"] = _json.loads(req.data.decode())
                return _FakeResp()

            ing.urllib.request.urlopen = _fake_urlopen  # type: ignore[attr-defined]
            ok = ing.weaviate_batch_upsert([
                (1, {"session_id": "s", "turn_id": 1, "project_slug": "x",
                     "role": "user", "content": "hi", "ts": None,
                     "model": "", "ai_title": ""}),
            ])
            _aT("upsert returned an id for the single item",
                 1 in ok,
                 f"ok={ok!r}")
            objs = (captured.get("body") or {}).get("objects") or []
            _aT("one object built",
                 len(objs) == 1,
                 f"got {len(objs)}")
            cls = (objs[0] or {}).get("class") if objs else None
            _aT("object[0].class == 'ClaudeDejavuTurn_bench'",
                 cls == "ClaudeDejavuTurn_bench",
                 f"got {cls!r}")
    finally:
        _restore_env(saved)


def main() -> int:
    print("\033[1m=== ingester WV_CLASS override (v0.8.12) ===\033[0m")
    test_default_unchanged()
    test_env_override_wins()
    test_config_env_fallback()
    test_env_wins_over_config_env()
    test_class_constant_used_by_batch_upsert()
    print()
    print(f"PASS: {len(PASS)}     FAIL: {len(FAIL)}")
    for name, d in FAIL:
        print(f"  ✗ {name}  {d[:80]}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
