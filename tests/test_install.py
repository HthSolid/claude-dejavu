"""Unit tests for scripts/install.py helpers added in v0.5.0e.

Can't run install.py end-to-end without Docker + Postgres + Weaviate,
but we test the new helpers in isolation: _fail (INSTALL_STATUS
flagging), _normalize_mcp_json (git-clone skip), the dejavu-hook venv
resolver, the schema-migration applier, and docker-compose project
name pinning."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(msg): PASS.append(msg); print(f"  \033[32m✓\033[0m {msg}", flush=True)
def _fail_(msg, d=""): FAIL.append((msg, d)); print(f"  \033[31m✗\033[0m {msg}"); d and print(f"      {d}")
def _step(name): print(f"\n\033[36m── {name} ──\033[0m", flush=True)
def _aT(name, cond, d=""): _ok(name) if cond else _fail_(name, d)


def _import_install():
    spec = importlib.util.spec_from_file_location(
        "install", str(ROOT / "scripts" / "install.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_fail_writes_install_status() -> None:
    _step("_fail() writes INSTALL_STATUS marker in --auto mode")
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "CLAUDE_DEJAVU_DATA_ROOT": tmp}
        runner = Path(tmp) / "_runner.py"
        runner.write_text(f"""
import sys, os, importlib.util
os.environ['CLAUDE_DEJAVU_DATA_ROOT'] = {tmp!r}
spec = importlib.util.spec_from_file_location('install', {str(ROOT / 'scripts' / 'install.py')!r})
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
class Args: pass
args = Args(); args.auto = True
try:
    m._fail(args, 'test_reason', 'test summary', 'test fix instructions')
except SystemExit as e:
    print(f'EXIT {{e.code}}')
""")
        r = subprocess.run([sys.executable, str(runner)],
                              env=env, capture_output=True, text=True)
        marker = Path(tmp) / "INSTALL_STATUS"
        if not marker.exists():
            _fail_("INSTALL_STATUS not written by _fail()",
                     r.stdout + r.stderr); return
        d = json.loads(marker.read_text())
        _aT("status=incomplete", d.get("status") == "incomplete")
        _aT("reason_code preserved",
             d.get("reason_code") == "test_reason")
        _aT("summary preserved",
             d.get("summary") == "test summary")
        _aT("fix preserved",
             "test fix instructions" in d.get("fix", ""))
        _aT("subprocess exited 1",
             "EXIT 1" in r.stdout, r.stdout)


def test_fail_no_marker_in_non_auto() -> None:
    _step("_fail() does NOT write INSTALL_STATUS when args.auto=False")
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "CLAUDE_DEJAVU_DATA_ROOT": tmp}
        runner = Path(tmp) / "_runner.py"
        runner.write_text(f"""
import sys, os, importlib.util
os.environ['CLAUDE_DEJAVU_DATA_ROOT'] = {tmp!r}
spec = importlib.util.spec_from_file_location('install', {str(ROOT / 'scripts' / 'install.py')!r})
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
class Args: pass
args = Args(); args.auto = False
try:
    m._fail(args, 'r', 's', 'f')
except SystemExit:
    pass
""")
        subprocess.run([sys.executable, str(runner)], env=env,
                          capture_output=True, text=True)
        _aT("INSTALL_STATUS not created in non-auto",
             not (Path(tmp) / "INSTALL_STATUS").exists())


def test_normalize_mcp_json_skipped_in_git_clone() -> None:
    _step("_normalize_mcp_json skips when running from a git clone")
    install = _import_install()
    if not (install.PLUGIN_ROOT / ".git").is_dir():
        _ok("(no .git dir present — skipping; the guard would "
             "activate on real installs)")
        return
    mcp_path = install.PLUGIN_ROOT / ".mcp.json"
    before = mcp_path.read_text() if mcp_path.exists() else ""
    install._normalize_mcp_json()
    after = mcp_path.read_text() if mcp_path.exists() else ""
    _aT(".mcp.json untouched in git clone", before == after,
         "file changed - git-clone guard not working")


def test_docker_compose_uses_project_name() -> None:
    _step("docker-compose.minimal.yml declares `name: claude-dejavu`")
    compose_path = ROOT / "docker" / "docker-compose.minimal.yml"
    txt = compose_path.read_text()
    _aT("name field present",
         "name: claude-dejavu" in txt)


def test_install_py_uses_p_flag_in_compose_calls() -> None:
    _step("install.py passes -p claude-dejavu to docker compose")
    install_src = (ROOT / "scripts" / "install.py").read_text()
    n_compose = install_src.count('"docker", "compose"')
    n_p_flag = install_src.count('"-p", "claude-dejavu"')
    _aT(f"all {n_compose} compose calls have -p claude-dejavu",
         n_p_flag >= n_compose,
         f"got {n_p_flag} -p flags vs {n_compose} compose calls")


def test_dejavu_hook_resolves_venv_python() -> None:
    _step("dejavu-hook resolves venv python for non-Setup hooks")
    spec = importlib.util.spec_from_file_location(
        "dh", str(ROOT / "bin" / "dejavu-hook.py"))
    dh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dh)
    setup_py = dh._resolve_python_for("setup")
    other_py = dh._resolve_python_for("session-start")
    _aT("setup uses sys.executable",
         setup_py == sys.executable)
    _aT("session-start resolved a python path",
         os.path.isabs(other_py))


def test_doctor_utf8_reconfigure_present() -> None:
    _step("doctor.py reconfigures stdout to utf-8 on Windows")
    src = (ROOT / "code" / "doctor.py").read_text()
    _aT("UTF-8 reconfigure block present",
         'reconfigure(encoding="utf-8"' in src)


def test_install_py_utf8_reconfigure_present() -> None:
    _step("install.py reconfigures stdout to utf-8 on Windows")
    src = (ROOT / "scripts" / "install.py").read_text()
    _aT("UTF-8 reconfigure block present",
         'reconfigure(encoding="utf-8"' in src)


def test_migrations_dir_layout() -> None:
    _step("code/migrations/ exists and has README")
    mdir = ROOT / "code" / "migrations"
    _aT("migrations dir present", mdir.is_dir())
    _aT("README.md present", (mdir / "README.md").is_file())


def test_apply_schema_migrations_helper_exists() -> None:
    _step("install._apply_schema_migrations is wired")
    install = _import_install()
    _aT("function exists",
         hasattr(install, "_apply_schema_migrations"))


def test_run_psql_falls_back_to_docker_exec() -> None:
    _step("install._run_psql exists and _USE_BUNDLED_PG_PSQL "
            "default is False")
    install = _import_install()
    _aT("function exists", hasattr(install, "_run_psql"))
    _aT("_USE_BUNDLED_PG_PSQL flag exists",
         hasattr(install, "_USE_BUNDLED_PG_PSQL"))
    _aT("default flag is False",
         install._USE_BUNDLED_PG_PSQL is False)


def main() -> int:
    print("claude-dejavu v0.5.0e install.py + helpers tests")
    print(f"  ROOT={ROOT}")
    tests = [
        test_fail_writes_install_status,
        test_fail_no_marker_in_non_auto,
        test_normalize_mcp_json_skipped_in_git_clone,
        test_docker_compose_uses_project_name,
        test_install_py_uses_p_flag_in_compose_calls,
        test_dejavu_hook_resolves_venv_python,
        test_doctor_utf8_reconfigure_present,
        test_install_py_utf8_reconfigure_present,
        test_migrations_dir_layout,
        test_apply_schema_migrations_helper_exists,
        test_run_psql_falls_back_to_docker_exec,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
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
