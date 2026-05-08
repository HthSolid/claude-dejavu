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


def test_reclaim_stale_named_containers_safe_no_docker() -> None:
    _step("_reclaim_stale_named_containers no-op when docker missing")
    install = _import_install()
    import unittest.mock as mock_
    with mock_.patch("shutil.which") as m:
        m.return_value = None
        r = install._reclaim_stale_named_containers()
    _aT("checked is empty (no docker)", r["checked"] == [])
    _aT("reclaimed is empty", r["reclaimed"] == [])
    _aT("errors is empty", r["errors"] == [])


def test_reclaim_stale_named_containers_skips_owned() -> None:
    _step("_reclaim_stale_named_containers leaves our own "
            "containers alone")
    install = _import_install()
    import unittest.mock as mock_
    cp_inspect = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="claude-dejavu\n", stderr="")
    with mock_.patch("shutil.which", return_value="/usr/bin/docker"), \
            mock_.patch("subprocess.run", return_value=cp_inspect):
        r = install._reclaim_stale_named_containers()
    _aT("nothing reclaimed (all owned by us)",
         r["reclaimed"] == [])
    _aT("no errors", r["errors"] == [])
    _aT("checked all 3 canonical names",
         len(r["checked"]) == 3, f"got {r['checked']}")


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


def test_doctor_postgres_falls_back_to_docker_exec() -> None:
    # On Windows the host typically has neither psql nor pg_isready
    # on PATH but the bundled container is up. Doctor must use the
    # same `docker exec` fallback that install._run_psql does so it
    # can verify the schema. Without this, doctor shows a misleading
    # "no psql or PG_DB" warning even on a fully healthy install.
    _step("code/doctor.py uses docker-exec fallback for psql when "
            "host psql is missing")
    src = (ROOT / "code" / "doctor.py").read_text()
    _aT("_docker_exec_psql_available helper exists",
         "_docker_exec_psql_available" in src)
    _aT("_run_psql_dejavu helper exists",
         "_run_psql_dejavu" in src)
    _aT("uses use_docker_exec when host psql is missing",
         "use_docker_exec = (not psql)" in src)
    _aT("docker-exec branch probes pg_isready inside the container",
         '"docker", "exec", "claude-dejavu-postgres",' in src
         and '"pg_isready"' in src)
    _aT("no direct subprocess.run on `psql` outside helper",
         "subprocess.run(\n            [psql," not in src
         and "subprocess.run(\n        [psql," not in src)


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


def test_create_database_is_gated_on_select() -> None:
    # Earlier the SELECT-from-pg_database check was issued but its
    # result was discarded — install ran CREATE DATABASE
    # unconditionally. Postgres logged an ERROR-level "database
    # X already exists" line on every reinstall, which looked
    # alarming to anyone tailing `docker logs`.
    _step("install gates CREATE DATABASE on the SELECT result")
    src = (ROOT / "scripts" / "install.py").read_text()
    _aT("uses -tAc for parseable output",
         '"-tAc"' in src and "SELECT 1 FROM pg_database" in src)
    _aT("checks db_exists before issuing CREATE DATABASE",
         "db_exists" in src)
    _aT("only issues CREATE DATABASE in the else branch",
         "if db_exists:" in src and "CREATE DATABASE" in src)


def test_probe_pg_uses_container_host_port_mapping() -> None:
    # `docker exec pg_isready` works regardless of host port mapping
    # (it goes at the container's loopback). Without consulting
    # `docker port`, install.py would write the probed port (e.g.,
    # 5432) into config.env even when the container is actually
    # published on 5454 — every runtime client then times out.
    _step("probe_pg consults `docker port` when verifying via "
            "docker exec, and step_postgres honors the override")
    install = _import_install()
    _aT("_get_container_pg_host_port helper exists",
         hasattr(install, "_get_container_pg_host_port"))
    src = (ROOT / "scripts" / "install.py").read_text()
    _aT("probe_pg returns host_port=<n> when mapping mismatches",
         "host_port={mapped}" in src)
    _aT("probe_pg refuses success when no host port published",
         "docker_exec_ok_but_no_host_port_published" in src)
    _aT("step_postgres parses host_port= override from reason",
         '"host_port=" in detected_method' in src)


def test_bin_wrapper_is_windows_aware() -> None:
    # bin/claude-dejavu is invoked by Claude Code's slash commands via
    # Bash even on Windows (Git-Bash). The wrapper must (a) compute
    # the data root from %LOCALAPPDATA%, not $XDG_DATA_HOME; (b) look
    # for the venv at venv/Scripts/python.exe, not venv/bin/python3;
    # and (c) skip `python3` in the system fallback because that's
    # the Microsoft Store alias stub on most Windows boxes.
    _step("bin/claude-dejavu handles Windows-via-bash")
    src = (ROOT / "bin" / "claude-dejavu").read_text()
    _aT("detects MSYS/MINGW/CYGWIN via uname -s",
         "MINGW" in src and "MSYS" in src and "CYGWIN" in src)
    _aT("uses LOCALAPPDATA on Windows",
         "LOCALAPPDATA" in src)
    _aT("knows the Windows venv layout (Scripts/python.exe)",
         "Scripts/python.exe" in src)
    _aT("system fallback prefers `py` on Windows",
         "SYS_PY_CANDIDATES=(py" in src)
    _aT("filters out the MS Store python3 stub via --version probe",
         '"$sys_py" --version' in src)


def test_run_psql_forces_utf8_subprocess_encoding() -> None:
    # On Windows, subprocess.run(text=True) defaults to cp1252 — which
    # fails the moment schema.sql's em-dash / box-drawing chars hit
    # stdin. Pin the fix so we can't regress.
    _step("install._run_psql passes encoding='utf-8' to every "
            "subprocess.run call")
    import re
    src = (ROOT / "scripts" / "install.py").read_text()
    # Locate the function body.
    start = src.index("def _run_psql(")
    end = src.index("\ndef ", start + 1)
    body = src[start:end]
    run_calls = body.count("subprocess.run(")
    # For every subprocess.run(...) call, the matching ) must be
    # preceded by both encoding="utf-8" and errors="replace" within
    # the same call. Count balanced-paren windows.
    matches = list(re.finditer(r"subprocess\.run\(", body))
    paired = 0
    for m in matches:
        i = m.end()
        depth = 1
        while i < len(body) and depth > 0:
            if body[i] == "(":
                depth += 1
            elif body[i] == ")":
                depth -= 1
            i += 1
        call = body[m.end():i]
        if 'encoding="utf-8"' in call and 'errors="replace"' in call:
            paired += 1
    _aT(f"every subprocess.run paired with utf-8+replace "
         f"({paired}/{run_calls})",
         paired == run_calls and run_calls > 0)


def main() -> int:
    print("claude-dejavu v0.5.0e install.py + helpers tests")
    print(f"  ROOT={ROOT}")
    tests = [
        test_fail_writes_install_status,
        test_fail_no_marker_in_non_auto,
        test_normalize_mcp_json_skipped_in_git_clone,
        test_docker_compose_uses_project_name,
        test_install_py_uses_p_flag_in_compose_calls,
        test_reclaim_stale_named_containers_safe_no_docker,
        test_reclaim_stale_named_containers_skips_owned,
        test_dejavu_hook_resolves_venv_python,
        test_doctor_utf8_reconfigure_present,
        test_install_py_utf8_reconfigure_present,
        test_migrations_dir_layout,
        test_apply_schema_migrations_helper_exists,
        test_run_psql_falls_back_to_docker_exec,
        test_run_psql_forces_utf8_subprocess_encoding,
        test_bin_wrapper_is_windows_aware,
        test_probe_pg_uses_container_host_port_mapping,
        test_create_database_is_gated_on_select,
        test_doctor_postgres_falls_back_to_docker_exec,
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
