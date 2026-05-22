"""Unit tests for code/repo_id.py — repo identity resolution."""
from __future__ import annotations
import os, sys, tempfile, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

PASS, FAIL = [], []
def _ok(m): PASS.append(m); print(f"  \033[32m✓\033[0m {m}", flush=True)
def _f(m, d=""): FAIL.append((m, d)); print(f"  \033[31m✗\033[0m {m}"); d and print(f"      {d}")
def _step(n): print(f"\n\033[36m── {n} ──\033[0m", flush=True)
def _aT(n, c, d=""): _ok(n) if c else _f(n, d)


def test_resolve_in_git_repo_writes_dejavu_id():
    _step("resolver writes .dejavu/id when cwd is in a git repo without one")
    import repo_id
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "myrepo"
        (root / ".git").mkdir(parents=True)
        (root / "src").mkdir()
        rid, rroot = repo_id.resolve(start=root / "src")
        _aT("repo_root equals git root", rroot == root.resolve(), f"got {rroot}")
        _aT("id is a valid uuid", uuid.UUID(rid) is not None)
        _aT(".dejavu/id file exists",
             (root / ".dejavu" / "id").is_file())
        _aT(".dejavu/id contains the same uuid",
             (root / ".dejavu" / "id").read_text().strip() == rid)
        _aT(".gitignore now contains .dejavu/",
             ".dejavu/" in (root / ".gitignore").read_text())


def test_resolve_reuses_existing_id():
    _step("resolver reads existing .dejavu/id without writing a new one")
    import repo_id
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "myrepo"
        (root / ".git").mkdir(parents=True)
        (root / ".dejavu").mkdir()
        existing = str(uuid.uuid4())
        (root / ".dejavu" / "id").write_text(existing + "\n")
        rid, _ = repo_id.resolve(start=root)
        _aT("returns existing uuid", rid == existing)


def test_resolve_no_git_uses_cwd_as_boundary():
    _step("resolver falls back to cwd as repo boundary when no .git is found")
    import repo_id
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "nogit"
        root.mkdir(parents=True)
        rid, rroot = repo_id.resolve(start=root)
        _aT("repo_root == start dir", rroot == root.resolve())
        _aT("id is a valid uuid", uuid.UUID(rid) is not None)
        _aT(".dejavu/id was written",
             (root / ".dejavu" / "id").is_file())


def test_resolve_malformed_id_raises_typed_error():
    _step("malformed .dejavu/id surfaces a typed error")
    import repo_id
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "broken"
        (root / ".git").mkdir(parents=True)
        (root / ".dejavu").mkdir()
        (root / ".dejavu" / "id").write_text("not-a-uuid\n")
        try:
            repo_id.resolve(start=root)
            _f("expected MalformedRepoIdError"); return
        except repo_id.MalformedRepoIdError:
            _aT("MalformedRepoIdError raised", True)


def test_walks_stop_at_home_boundary():
    _step("resolver never writes .dejavu/id above $HOME")
    import repo_id
    with tempfile.TemporaryDirectory() as tmp:
        # Simulate cwd outside any git root, but path doesn't ascend into home.
        weird = Path(tmp) / "deep" / "tree"
        weird.mkdir(parents=True)
        rid, rroot = repo_id.resolve(start=weird, home_override=Path(tmp))
        _aT("repo_root stays within tmp", str(rroot).startswith(str(Path(tmp).resolve())))


def test_repo_show_prints_id_root_label():
    _step("`claude-dejavu repo show` prints id + root + label")
    try:
        import psycopg2  # noqa: F401  recall.py imports it at module top
    except ImportError:
        print("  SKIP: psycopg2 not installed")
        return  # not _f — clean skip, no FAIL recorded
    import tempfile, subprocess, json as _json, os
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "showtest"
        (root / ".git").mkdir(parents=True)
        env = {**os.environ, "CLAUDE_DEJAVU_DATA_ROOT": tmp}
        r = subprocess.run(
            [sys.executable, str(ROOT / "code" / "recall.py"),
              "repo", "show", "--start", str(root), "--json"],
            env=env, capture_output=True, text=True,
        )
        if r.returncode != 0:
            _f("non-zero exit", r.stderr[:400]); return
        # Take the last non-empty stdout line as the JSON payload —
        # recall.py may print preamble lines that aren't part of the payload.
        lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
        if not lines:
            _f("empty stdout"); return
        try:
            payload = _json.loads(lines[-1])
        except Exception as e:
            _f(f"could not parse JSON: {e}", r.stdout); return
        _aT("payload has repo_id", "repo_id" in payload)
        _aT("payload has repo_root", "repo_root" in payload)
        _aT("payload has label", payload.get("label") == "showtest")


def main() -> int:
    tests = [test_resolve_in_git_repo_writes_dejavu_id,
              test_resolve_reuses_existing_id,
              test_resolve_no_git_uses_cwd_as_boundary,
              test_resolve_malformed_id_raises_typed_error,
              test_walks_stop_at_home_boundary,
              test_repo_show_prints_id_root_label]
    for t in tests:
        try: t()
        except Exception as e:
            import traceback; traceback.print_exc()
            FAIL.append((t.__name__, str(e)))
    print(f"\n{'='*60}\nPASS: {len(PASS)}  FAIL: {len(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
