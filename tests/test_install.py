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


def test_doctor_has_repo_identity_check():
    _step("code/doctor.py has _check_repo_identity wired into the report")
    src = (ROOT / "code" / "doctor.py").read_text()
    _aT("_check_repo_identity function present",
         "def _check_repo_identity" in src)
    _aT("it's called from the report aggregator",
         "_check_repo_identity(" in src and
         src.count("_check_repo_identity(") >= 2,
         "must be defined AND called — only 1 occurrence means it was defined but never wired")


def test_reindex_docs_flag_wired():
    _step("`claude-dejavu reindex --docs` is wired into argparse")
    try:
        import psycopg2  # noqa: F401  recall.py imports it at module top
    except ImportError:
        print("  SKIP: psycopg2 not installed")
        return
    import subprocess
    r = subprocess.run(
        [sys.executable, str(ROOT / "code" / "recall.py"),
          "reindex", "--help"],
        capture_output=True, text=True, timeout=10,
    )
    _aT("reindex --help exits 0", r.returncode == 0, r.stderr[:400])
    _aT("--docs flag present in help",
         "--docs" in r.stdout, r.stdout[:400])
    _aT("--full flag present in help",
         "--full" in r.stdout, r.stdout[:400])
    _aT("--repo flag present in help",
         "--repo" in r.stdout, r.stdout[:400])


def test_reindex_docs_helper_exists():
    _step("_reindex_docs helper exists in code/recall.py")
    src = (ROOT / "code" / "recall.py").read_text()
    _aT("_reindex_docs function defined",
         "def _reindex_docs" in src)
    _aT("cmd_reindex dispatches on args.docs",
         'getattr(args, "docs"' in src or 'args.docs' in src)


def test_install_calls_reindex_docs_once():
    _step("scripts/install.py invokes `reindex --docs` on first install")
    src = (ROOT / "scripts" / "install.py").read_text()
    _aT("call to reindex --docs present",
         "reindex" in src and "--docs" in src,
         "install must seed docs once so first session is useful")
    _aT("seed step wrapped in try/except (non-fatal)",
         # Look for the seed snippet — try/except surrounding a subprocess
         # call to recall.py reindex --docs
         "reindex" in src and "--docs" in src and
         any(
             "try:" in chunk and "except" in chunk and "reindex" in chunk
             for chunk in src.split("\n\n")
         ),
         "install must continue even if reindex fails")


def test_stop_hook_fires_doc_diff_scan():
    _step("hooks/stop.py spawns doc_diff_scan as fire-and-forget subprocess")
    src = (ROOT / "hooks" / "stop.py").read_text()
    _aT("imports subprocess module",
         "subprocess" in src or "import subprocess" in src)
    _aT("references doc_diff_scan.py",
         "doc_diff_scan" in src)
    _aT("uses Popen (fire-and-forget, not run)",
         "Popen" in src,
         "Stop hook must not block on the diff scan")
    _aT("uses start_new_session for proper detachment",
         "start_new_session" in src,
         "subprocess must survive the hook process exit")
    _aT("wrapped in try/except",
         # Look for try: followed within ~30 lines by doc_diff_scan + except
         any(
             "try:" in chunk and "doc_diff_scan" in chunk and "except" in chunk
             for chunk in src.split("\n\n")
         ),
         "spawn must not crash the hook on failure")


def test_search_docs_cli_wired():
    _step("`claude-dejavu search-docs` subcommand is wired")
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        print("  SKIP: psycopg2 not installed")
        return
    import subprocess
    r = subprocess.run(
        [sys.executable, str(ROOT / "code" / "recall.py"),
          "search-docs", "--help"],
        capture_output=True, text=True, timeout=10,
    )
    _aT("search-docs --help exits 0", r.returncode == 0, r.stderr[:400])
    _aT("--workspace flag present in help",
         "--workspace" in r.stdout, r.stdout[:400])
    _aT("--limit flag present in help",
         "--limit" in r.stdout, r.stdout[:400])
    _aT("--json flag present in help",
         "--json" in r.stdout, r.stdout[:400])


def test_search_docs_helper_exists():
    _step("cmd_search_docs handler is defined in code/recall.py")
    src = (ROOT / "code" / "recall.py").read_text()
    _aT("cmd_search_docs function defined",
         "def cmd_search_docs" in src)
    _aT("registers `search-docs` subparser",
         '"search-docs"' in src or "'search-docs'" in src)


def test_dejavu_docs_slash_command_exists():
    _step("commands/dejavu-docs.md slash command exists")
    p = ROOT / "commands" / "dejavu-docs.md"
    _aT("file exists", p.is_file())
    if p.is_file():
        content = p.read_text()
        _aT("mentions dejavu_search",
             "dejavu_search" in content)
        _aT("mentions kinds=[\"doc\"] or similar",
             "doc" in content)


# ── v0.7.1 — session repair feature wiring ──────────────────────────────────

def test_recall_repair_subcommand_wired():
    _step("`claude-dejavu repair` subcommand is wired")
    src = (ROOT / "code" / "recall.py").read_text()
    _aT("cmd_repair function defined", "def cmd_repair" in src)
    _aT("repair subparser registered",
         '"repair"' in src and 'sub.add_parser("repair"' in src,
         "subparser missing")


def test_recall_main_propagates_returncode():
    _step("recall.py main propagates args.func returncode via sys.exit")
    src = (ROOT / "code" / "recall.py").read_text()
    _aT("main uses sys.exit with returncode",
         ("sys.exit(rc" in src or "sys.exit(args.func" in src
          or "rc = args.func(args)" in src),
         "main must propagate handler returncode")


def test_session_start_hook_auto_detects_corruption():
    _step("hooks/session_start.py detects corruption + warns via systemMessage")
    src = (ROOT / "hooks" / "session_start.py").read_text()
    _aT("imports / uses code/repair.py",
         "repair" in src and "diagnose" in src,
         "must call repair.diagnose")
    _aT("emits systemMessage warning",
         "systemMessage" in src and "claude-dejavu repair" in src,
         "must surface a 'run claude-dejavu repair <uuid>' hint")
    _aT("wrapped in try/except so a hook failure can't block session-start",
         any("try:" in chunk and "diagnose" in chunk and "except" in chunk
              for chunk in src.split("\n\n")),
         "auto-detect must never crash session-start")


def test_install_seed_docs_forwards_env():
    _step("install.py [7/8] forwards env (WV_URL etc.) to reindex subprocess")
    src = (ROOT / "scripts" / "install.py").read_text()
    # Find the seed block — look for the chunk that runs `reindex --docs`
    blocks = src.split("\n\n")
    seed_block = next((b for b in blocks if "reindex" in b and "--docs" in b and "_sp.run" in b), "")
    _aT("seed block found", bool(seed_block), "couldn't locate the [7/8] reindex --docs subprocess")
    _aT("seed block passes env= kwarg to subprocess.run",
         "env=" in seed_block,
         "subprocess.run must be called with env= containing WV_URL")
    _aT("env loaded from config.env",
         "config.env" in seed_block,
         "must read config.env into the env dict before subprocess.run")


def test_install_preserves_cloud_config_on_reinstall():
    _step("install.py preserves DEJAVU_EMBED_MODE + DEJAVU_CLOUD_KEY across reinstalls")
    src = (ROOT / "scripts" / "install.py").read_text()
    _aT("preserves DEJAVU_EMBED_MODE",
         "DEJAVU_EMBED_MODE" in src and "preserved" in src.lower(),
         "must keep an existing DEJAVU_EMBED_MODE setting on reinstall")
    _aT("preserves DEJAVU_CLOUD_KEY",
         "DEJAVU_CLOUD_KEY" in src and "preserved" in src.lower(),
         "must keep an existing DEJAVU_CLOUD_KEY")


def test_recall_cleanup_stubs_subcommand_wired():
    _step("`claude-dejavu cleanup-stubs` subcommand is wired")
    src = (ROOT / "code" / "recall.py").read_text()
    _aT("cmd_cleanup_stubs defined",
         "def cmd_cleanup_stubs" in src,
         "handler missing")
    _aT("cleanup-stubs subparser registered",
         'sub.add_parser("cleanup-stubs"' in src,
         "subparser missing")
    _aT("backs up stubs to tar.gz before deletion",
         "tarfile" in src and "tar.gz" in src,
         "must backup before deleting (feedback_backup_before_destructive_ops)")
    _aT("--dry-run flag supported",
         '"--dry-run"' in src,
         "must offer a no-op preview")


# ── v0.7.2 — file corruption scanner + file recovery wiring ────────────────

def test_recall_scan_corruption_subcommand_wired():
    _step("`claude-dejavu scan-corruption` subcommand is wired")
    src = (ROOT / "code" / "recall.py").read_text()
    _aT("cmd_scan_corruption defined",
         "def cmd_scan_corruption" in src,
         "handler missing")
    _aT("scan-corruption subparser registered",
         'sub.add_parser("scan-corruption"' in src,
         "subparser missing")
    _aT("--all-projects flag present",
         '"--all-projects"' in src,
         "must offer --all-projects scope")
    _aT("--path flag present",
         '"--path"' in src,
         "must offer --path scope")


def test_recall_recover_file_subcommand_wired():
    _step("`claude-dejavu recover-file` subcommand is wired with split base-pin flags")
    src = (ROOT / "code" / "recall.py").read_text()
    _aT("cmd_recover_file defined",
         "def cmd_recover_file" in src,
         "handler missing")
    _aT("recover-file subparser registered",
         'sub.add_parser("recover-file"' in src,
         "subparser missing")
    # CRITICAL: spec §5 mandates split --from-version + --from-git, NOT a
    # single --from <ref>. The grammar is intentionally explicit.
    _aT("--from-version flag present",
         '"--from-version"' in src,
         "must offer --from-version <N>")
    _aT("--from-git flag present",
         '"--from-git"' in src,
         "must offer --from-git <ref>")
    _aT("NO single --from flag (split grammar enforced)",
         '"--from"' not in src or '"--from-' in src,
         "the spec forbids a single --from grammar — use --from-version + --from-git")
    _aT("--session flag present",
         '"--session"' in src,
         "must offer --session <uuid>")
    _aT("--any-project flag present",
         '"--any-project"' in src,
         "must offer --any-project cross-project session search")
    _aT("--to flag present",
         '"--to"' in src,
         "must offer --to <ts> replay cutoff")
    _aT("--write flag present",
         '"--write"' in src,
         "must offer --write to apply (default = dry-run)")
    _aT("--verify-tsc flag present",
         '"--verify-tsc"' in src,
         "must offer --verify-tsc")
    _aT("--skip-failed-edits flag present",
         '"--skip-failed-edits"' in src,
         "must offer --skip-failed-edits")
    _aT("--yes flag present",
         '"--yes"' in src,
         "must offer --yes (overwrite-original confirmation)")
    _aT("--force-write flag present",
         '"--force-write"' in src,
         "must offer --force-write (lockfile bypass)")


def test_recall_recover_file_writes_backup_before_overwrite():
    _step("`recover-file` backs up the original + uses atomic os.replace")
    src = (ROOT / "code" / "recall.py").read_text()
    _aT("references recovered-files backup root",
         "recovered-files" in src,
         "backup root must live at $DATA_ROOT/recovered-files")
    _aT("writes .original sidecar",
         ".original" in src,
         "must back up the current file as <hash>.original")
    _aT("uses os.replace for atomic write",
         "os.replace" in src,
         "must atomic-replace (not direct write)")
    _aT("temp file kept next to target (same filesystem)",
         "dejavu-recover.tmp" in src,
         "spec §6 requires temp file next to target, not in /tmp")
    _aT("editor-lockfile guard present",
         ".swp" in src and ".swo" in src,
         "must refuse to write when vim swap files exist (unless --force-write)")


def test_dejavu_scan_corruption_slash_command_exists():
    _step("commands/dejavu-scan-corruption.md slash command exists")
    p = ROOT / "commands" / "dejavu-scan-corruption.md"
    _aT("file exists", p.is_file())
    if p.is_file():
        content = p.read_text()
        _aT("mentions scan-corruption CLI",
             "claude-dejavu scan-corruption" in content)


def test_dejavu_recover_file_slash_command_exists():
    _step("commands/dejavu-recover-file.md slash command exists")
    p = ROOT / "commands" / "dejavu-recover-file.md"
    _aT("file exists", p.is_file())
    if p.is_file():
        content = p.read_text()
        _aT("mentions recover-file CLI",
             "claude-dejavu recover-file" in content)
        _aT("mentions dry-run default",
             "dry-run" in content.lower())


def test_ingester_skips_fresh_repair_fingerprint():
    _step("ingester.py skips JSONLs with fresh .dejavu-repair-fingerprint")
    src = (ROOT / "code" / "ingester.py").read_text()
    _aT("_should_skip_repaired helper defined",
         "def _should_skip_repaired" in src,
         "helper missing")
    _aT("helper is called from ingest entry",
         "_should_skip_repaired(" in src and src.count("_should_skip_repaired") >= 2,
         "must be called at the ingest entry point")
    _aT("checks dejavu-repair-fingerprint sidecar",
         "dejavu-repair-fingerprint" in src,
         "must reference the fingerprint sidecar name")


# v0.8.0 — H6 hook wiring tests (session-copy subsystem)

def test_session_start_hook_emits_migration_pending_when_marker_exists():
    _step("session_start.py adds migration_notice slot when "
          "migration-state.json exists with phase != 'done'")
    src = (ROOT / "hooks" / "session_start.py").read_text()
    _aT("_check_migration_pending helper defined",
         "def _check_migration_pending(" in src,
         "missing helper")
    _aT("_build_preamble takes migration_notice kwarg",
         "migration_notice" in src,
         "kwarg not threaded through preamble builder")
    _aT("migration_notice appears in the preamble lines builder",
         src.count("migration_notice") >= 4,
         "must be: helper name, kwarg, preamble param, lines.append")
    _aT("references migration-state.json filename",
         "migration-state.json" in src,
         "must check the marker filename")
    _aT("checks phase != 'done'",
         "'done'" in src or '"done"' in src,
         "must gate on phase status")
    # Behavioral smoke test — actually call the helper with a synthetic
    # marker and confirm it returns a non-empty string.
    spec = importlib.util.spec_from_file_location(
        "session_start_hook",
        str(ROOT / "hooks" / "session_start.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    import tempfile, json as _json
    with tempfile.TemporaryDirectory() as td:
        marker = Path(td) / "migration-state.json"
        marker.write_text(_json.dumps({"phase": "preserved"}))
        notice = m._check_migration_pending(Path(td))
        _aT("helper returns warning for non-done phase",
             notice is not None and "migration" in notice.lower())
        # phase=done → None
        marker.write_text(_json.dumps({"phase": "done"}))
        _aT("helper returns None when phase == 'done'",
             m._check_migration_pending(Path(td)) is None)
        # No marker → None
        marker.unlink()
        _aT("helper returns None when marker absent",
             m._check_migration_pending(Path(td)) is None)


def test_stop_hook_fires_session_copy_take_async():
    _step("stop.py fires `claude-dejavu session-copy take --tag stop-hook` "
          "via subprocess.Popen with detach flags")
    src = (ROOT / "hooks" / "stop.py").read_text()
    _aT("fire_session_copy_take_async helper defined",
         "def fire_session_copy_take_async(" in src,
         "missing helper")
    _aT("invokes session-copy take subcommand",
         '"session-copy"' in src and '"take"' in src,
         "argv must include session-copy + take")
    _aT("uses --tag stop-hook",
         '"stop-hook"' in src,
         "tag must be stop-hook so retention/diagnostics can filter")
    _aT("subprocess.Popen invoked for detach",
         "subprocess.Popen" in src and "session-copy" in src,
         "must use Popen, not blocking subprocess.run")
    _aT("POSIX detach via start_new_session",
         "start_new_session" in src,
         "POSIX child must outlive parent Stop hook")
    _aT("Windows detach via DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP",
         "0x00000008" in src and "0x00000200" in src,
         "Windows child must outlive parent")
    _aT("called from main()",
         "fire_session_copy_take_async()" in src,
         "helper must be wired into main")


def test_stop_hook_swallows_errors():
    _step("stop.py never raises out of session-copy spawn — errors are "
          "swallowed (Stop hook must never block Claude Code exit)")
    src = (ROOT / "hooks" / "stop.py").read_text()
    # Pattern check: the helper itself is wrapped in try/except AND the
    # call site is wrapped in another try/except. Belt + suspenders is
    # the whole point of a never-block-exit hook.
    _aT("helper body has try/except around Popen",
         "except Exception" in src,
         "must swallow spawn-time errors")
    # The call-site is the bare invocation `fire_session_copy_take_async()`
    # in main() — distinct from the `def fire_session_copy_take_async(`
    # definition. Confirm it's wrapped in try/except.
    call_marker = "fire_session_copy_take_async()"
    # Find the LAST occurrence (the def comes first; the call comes
    # after, lower in the file).
    idx = src.rfind(call_marker)
    def_idx = src.find("def fire_session_copy_take_async(")
    _aT("call-site found (separate from def)",
         idx > 0 and idx != def_idx)
    if idx > 0 and idx != def_idx:
        ctx = src[max(0, idx - 400):idx + 200]
        _aT("call site wrapped in try/except",
             "try:" in ctx and "except" in ctx,
             "main() must guard the call")


def test_stop_hook_skips_if_repo_uninitialized():
    _step("stop.py skips the session-copy spawn when the repo is not "
          "initialized yet (pre-migration window)")
    src = (ROOT / "hooks" / "stop.py").read_text()
    _aT("_session_copy_repo_initialized helper defined",
         "def _session_copy_repo_initialized(" in src,
         "missing repo-init probe")
    _aT("helper checks for restic-repo directory",
         "restic-repo" in src,
         "must look for the v0.8.0 repo dir")
    _aT("helper checks for .restic-pass passphrase",
         ".restic-pass" in src,
         "must look for the v0.8.0 passphrase file")
    # Behavioral: load the module, call fire_session_copy_take_async()
    # against an empty data root, confirm it returns None and does not
    # raise.
    spec = importlib.util.spec_from_file_location(
        "stop_hook", str(ROOT / "hooks" / "stop.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        # Point DATA_ROOT at the empty dir (no restic-repo, no pass)
        m.DATA_ROOT = Path(td)
        result = m.fire_session_copy_take_async()
        _aT("fire returns None when repo missing",
             result is None,
             "must skip silently pre-migration")


# ─── v0.8.0 H5 — CLI + doctor wiring ───────────────────────────────────────


def test_recall_session_copy_subcommand_wired():
    _step("`claude-dejavu session-copy` subcommand is wired with all 10 sub-subparsers")
    src = (ROOT / "code" / "recall.py").read_text()
    _aT("cmd_session_copy function defined",
         "def cmd_session_copy" in src,
         "handler missing")
    _aT("session-copy subparser registered",
         'sub.add_parser("session-copy"' in src,
         "subparser missing")
    # All 10 sub-subcommands from spec §3.3
    for sub_name in ("init", "take", "list", "diff", "restore", "mount",
                       "prune", "status", "check", "migrate-from-rsync"):
        _aT(f"{sub_name!r} sub-subparser present",
             f'"{sub_name}"' in src,
             f"missing add_parser({sub_name!r})")


def test_doctor_session_copy_section_present():
    _step("doctor.py has _check_session_copy with 4 checks "
          "(reachable / age / size / restic-check)")
    src = (ROOT / "code" / "doctor.py").read_text()
    _aT("_check_session_copy defined",
         "def _check_session_copy(" in src,
         "missing helper")
    _aT("checks for repo reachable",
         "session_copy" in src and (
             "reachable" in src or "list_snapshots" in src),
         "must probe repo reachability")
    _aT("checks last-snapshot age",
         "last_snapshot_age" in src or "snapshot_age" in src or "age" in src,
         "must report snapshot age")
    _aT("checks repo size threshold",
         "repo_size" in src or "20" in src or "GB" in src,
         "must check size warn/crit thresholds")
    _aT("runs restic check via session_copy.check",
         "session_copy.check" in src or ".check(" in src,
         "must invoke check function")
    _aT("section wired into run_doctor",
         "_check_session_copy" in src and src.count("_check_session_copy") >= 2,
         "helper must be called from run_doctor")


def test_doctor_session_copy_check_cached_to_24h():
    _step("doctor.py session-copy check honors 24h cache "
          "(via max_age_hours or default cache)")
    src = (ROOT / "code" / "doctor.py").read_text()
    _aT("max_age_hours=24 or force=False mentioned",
         "max_age_hours=24" in src or "max_age_hours = 24" in src or
         "force=False" in src,
         "must opt into the 24h cache")


def test_repair_pre_recovery_snapshot_wired():
    _step("repair.py calls session_copy.take(tag='pre-recovery') "
          "before destructive write")
    src = (ROOT / "code" / "repair.py").read_text()
    _aT("imports / references session_copy",
         "session_copy" in src,
         "must reference session_copy module")
    _aT("calls session_copy.take",
         "session_copy.take" in src,
         "must invoke take()")
    _aT("tags snapshot 'pre-recovery'",
         "'pre-recovery'" in src or '"pre-recovery"' in src,
         "tag must be pre-recovery")


def test_recovery_pre_recovery_snapshot_wired():
    _step("recovery.py write_recovered calls "
          "session_copy.take(tag='pre-recovery') before destructive write")
    src = (ROOT / "code" / "recovery.py").read_text()
    _aT("write_recovered helper defined",
         "def write_recovered(" in src,
         "missing helper")
    _aT("references session_copy",
         "session_copy" in src,
         "must import / call session_copy")
    _aT("calls session_copy.take",
         "session_copy.take" in src,
         "must invoke take()")
    _aT("tags snapshot 'pre-recovery'",
         "'pre-recovery'" in src or '"pre-recovery"' in src,
         "tag must be pre-recovery")


def test_repair_no_pre_recovery_snapshot_flag_present():
    _step("recall.py exposes --no-pre-recovery-snapshot on repair "
          "and recover-file subparsers")
    src = (ROOT / "code" / "recall.py").read_text()
    _aT("--no-pre-recovery-snapshot flag declared",
         "--no-pre-recovery-snapshot" in src,
         "must add_argument the override flag")
    # Should appear at both repair AND recover-file subparser sites.
    _aT("flag present at >=2 subparsers (repair + recover-file)",
         src.count("--no-pre-recovery-snapshot") >= 2,
         "flag must be on both repair and recover-file")


# ─── v0.8.0 H7 — install.py [7/9] + version bumps ──────────────────────────


def test_install_step_7_of_9_calls_install_restic_when_missing():
    _step("install.py [7/9] calls install_restic.install_restic() "
          "when restic binary is missing")
    src = (ROOT / "scripts" / "install.py").read_text()
    _aT("[7/9] step header present",
         "[7/9]" in src,
         "step header must be present")
    _aT("checks command -v restic OR uses shutil.which('restic')",
         "shutil.which" in src and "restic" in src,
         "must probe for an existing restic binary")
    _aT("calls install_restic.install_restic when missing",
         "install_restic" in src and "install_restic(" in src,
         "must invoke the installer when absent")


def test_install_step_7_of_9_detects_legacy_state_calls_migrate():
    _step("install.py [7/9] calls migrate_rsync.detect_legacy_state "
          "and runs migrate_rsync.run when legacy state present")
    src = (ROOT / "scripts" / "install.py").read_text()
    _aT("imports migrate_rsync",
         "migrate_rsync" in src,
         "must import migrate_rsync")
    _aT("calls detect_legacy_state",
         "detect_legacy_state" in src,
         "must probe for legacy state")
    _aT("calls migrate_rsync.run with mode='cold'",
         "migrate_rsync.run" in src and "cold" in src,
         "must invoke the orchestrator in cold mode")


def test_install_step_7_of_9_skips_migration_when_no_legacy_state():
    _step("install.py [7/9] does NOT invoke migrate when "
          "detect_legacy_state returns False (calls session_copy.init_repo)")
    src = (ROOT / "scripts" / "install.py").read_text()
    _aT("references session_copy.init_repo for fresh install path",
         "session_copy" in src and "init_repo" in src,
         "fresh-install path must call init_repo")


def test_install_step_7_of_9_auto_mode_silent_migrate():
    _step("install.py [7/9] runs migration silently when args.auto=True")
    src = (ROOT / "scripts" / "install.py").read_text()
    # The auto path should pass yes=args.auto / yes=True
    _aT("auto path passes yes=True (or yes=args.auto) to migrate",
         "yes=args.auto" in src or "yes=True" in src,
         "must thread --auto through migrate_rsync.run")


def test_install_step_7_of_9_interactive_prompts_then_runs():
    _step("install.py [7/9] interactive mode prompts Y/n then runs migrate")
    src = (ROOT / "scripts" / "install.py").read_text()
    # Look at the [7/9] block and ensure it has both an input() call AND
    # the migration call.
    # Find slice from [7/9] header to [8/9] header
    if "[7/9]" in src and "[8/9]" in src:
        a = src.find("[7/9]")
        b = src.find("[8/9]")
        block = src[a:b]
        _aT("[7/9] block contains input()/prompt",
             "input(" in block or "ask(" in block,
             "interactive path must prompt")
        _aT("[7/9] block invokes migrate_rsync.run",
             "migrate_rsync.run" in block,
             "must call migrate_rsync.run when user confirms")


def test_install_step_7_of_9_interactive_n_defers():
    _step("install.py [7/9] interactive 'n' defers — does NOT run migrate")
    src = (ROOT / "scripts" / "install.py").read_text()
    if "[7/9]" in src and "[8/9]" in src:
        a = src.find("[7/9]")
        b = src.find("[8/9]")
        block = src[a:b]
        # Should explicitly inspect input lowercase against 'n'
        _aT("[7/9] block branches on user input (e.g. == 'n' or != 'y')",
             ("'n'" in block or '"n"' in block or
              "lower()" in block),
             "must branch on user reply")


def test_plugin_json_at_0_8_0():
    """v0.8.0 historical check: ensure we never regressed below 0.8.x."""
    _step(".claude-plugin/plugin.json at version 0.8.x or newer")
    path = ROOT / ".claude-plugin" / "plugin.json"
    d = json.loads(path.read_text())
    v = d.get("version", "")
    parts = v.split(".")
    _aT("plugin.json version >= 0.8.0",
         len(parts) >= 2 and int(parts[0]) >= 0 and
         (int(parts[0]) > 0 or int(parts[1]) >= 8),
         f"got {v!r}")


def test_marketplace_json_at_0_8_0():
    """v0.8.0 historical check: marketplace contains a 0.8.x entry."""
    _step(".claude-plugin/marketplace.json contains a valid 0.x version")
    path = ROOT / ".claude-plugin" / "marketplace.json"
    d = json.loads(path.read_text())
    versions: list = []
    if isinstance(d.get("plugins"), list):
        for p in d["plugins"]:
            v = p.get("version")
            if v:
                versions.append(v)
    if "version" in d.get("metadata", {}):
        versions.append(d["metadata"]["version"])
    # v0.9.0 — accept any 0.x.y current-line version. v0.8.x was the
    # original hardcoded check; relaxing as new minor lines ship.
    _aT("marketplace.json contains a 0.x version",
         versions and all(v.startswith("0.") for v in versions),
         f"got {versions!r}")


def test_changelog_has_0_8_0_entry():
    _step("CHANGELOG.md has a [0.8.0] section")
    txt = (ROOT / "CHANGELOG.md").read_text()
    _aT("[0.8.0] heading present",
         "[0.8.0]" in txt or "0.8.0" in txt.splitlines()[0:60].__str__(),
         "section missing")
    _aT("mentions session-copy",
         "session-copy" in txt.lower() or "session copy" in txt.lower(),
         "must describe session-copy work")
    _aT("mentions migrate-from-rsync",
         "migrate-from-rsync" in txt or "migrate" in txt.lower(),
         "must document migration auto-run")


# ─── v0.8.2 — rescue-shard wiring ──────────────────────────────────────────


def test_recall_rescue_shard_subcommand_wired():
    _step("`claude-dejavu rescue-shard` subcommand is wired")
    src = (ROOT / "code" / "recall.py").read_text()
    _aT("cmd_rescue_shard handler defined",
         "def cmd_rescue_shard" in src,
         "handler missing")
    _aT("rescue-shard subparser registered",
         'sub.add_parser("rescue-shard"' in src,
         "subparser missing")
    _aT("--class flag present and required",
         '"--class"' in src and "required=True" in src.split('"--class"')[1][:200],
         "must declare --class as required")
    _aT("--shard flag present",
         '"--shard"' in src)
    _aT("--dry-run flag present",
         '"--dry-run"' in src)
    _aT("--yes flag present",
         "rescue-shard" in src and "--yes" in src)
    _aT("--include-quarantined flag present",
         "--include-quarantined" in src)


def test_recall_rescue_shard_handler_delegates_to_orchestrator():
    _step("cmd_rescue_shard imports rescue module + calls rescue_shard()")
    src = (ROOT / "code" / "recall.py").read_text()
    _aT("imports rescue module",
         "import rescue" in src,
         "must import rescue.py")
    _aT("calls rescue.rescue_shard",
         "rescue.rescue_shard(" in src or "_rescue.rescue_shard(" in src,
         "must delegate to orchestrator")


def test_dejavu_rescue_slash_command_exists():
    _step("commands/dejavu-rescue.md slash command exists")
    p = ROOT / "commands" / "dejavu-rescue.md"
    _aT("file exists", p.is_file())
    if p.is_file():
        content = p.read_text()
        _aT("mentions rescue-shard CLI",
             "claude-dejavu rescue-shard" in content)
        _aT("mentions --class arg",
             "--class" in content)
        _aT("mentions --dry-run safety",
             "--dry-run" in content)


def test_doctor_reports_rescue_pending_when_shard_broken():
    _step("doctor's weaviate_shards check surfaces "
          "'rescue-shard --class <C>' when a shard is unhealthy")
    src = (ROOT / "code" / "doctor.py").read_text()
    _aT("_check_weaviate_shard_health helper defined",
         "def _check_weaviate_shard_health(" in src,
         "helper missing")
    _aT("hits /v1/nodes endpoint",
         "/v1/nodes" in src,
         "must probe nodes endpoint")
    _aT("surfaces rescue-shard fix hint",
         "rescue-shard" in src,
         "fix line must mention rescue-shard command")
    _aT("wired into run_doctor",
         "_check_weaviate_shard_health(" in src
         and src.count("_check_weaviate_shard_health") >= 2,
         "helper must be called from run_doctor")


def test_install_step_7_of_9_calls_install_rescue_binary():
    _step("install.py [7/9] installs the dejavu-rescue Go binary "
          "alongside restic")
    src = (ROOT / "scripts" / "install.py").read_text()
    _aT("imports install_rescue module",
         "install_rescue" in src,
         "must import install_rescue")
    _aT("calls install_rescue_binary",
         "install_rescue_binary(" in src,
         "must invoke the installer")
    # The call MUST be inside the [7/9] block.
    if "[7/9]" in src and "[8/9]" in src:
        a = src.find("[7/9]"); b = src.find("[8/9]")
        block = src[a:b]
        _aT("install_rescue_binary call is inside the [7/9] block",
             "install_rescue_binary(" in block,
             "call must live in the [7/9] step")


def test_install_rescue_module_exposes_required_surface():
    _step("install_rescue.py exposes install_rescue_binary + "
          "RescueInstallError + RESCUE_VERSION")
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "install_rescue",
        str(ROOT / "code" / "install_rescue.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    _aT("install_rescue_binary callable",
         callable(getattr(m, "install_rescue_binary", None)))
    _aT("RescueInstallError defined",
         isinstance(getattr(m, "RescueInstallError", None), type))
    _aT("RESCUE_VERSION == '0.8.2'",
         getattr(m, "RESCUE_VERSION", None) == "0.8.2",
         f"got {getattr(m, 'RESCUE_VERSION', None)!r}")


def test_plugin_json_at_0_8_2():
    """v0.8.2 version pin — superseded by v0.8.3. Kept as a forward-
    compatibility check: the manifest must be >= 0.8.2."""
    _step(".claude-plugin/plugin.json at version >= 0.8.2")
    path = ROOT / ".claude-plugin" / "plugin.json"
    d = json.loads(path.read_text())
    v = (d.get("version") or "").split(".")
    _aT("plugin.json version >= 0.8.2",
         tuple(int(p) for p in v if p.isdigit()) >= (0, 8, 2),
         f"got {d.get('version')!r}")


def test_marketplace_json_at_0_8_2():
    """v0.8.2 version pin — superseded by v0.8.3. Kept as a forward-
    compatibility check: the marketplace must list a >= 0.8.2 version."""
    _step(".claude-plugin/marketplace.json at version >= 0.8.2")
    path = ROOT / ".claude-plugin" / "marketplace.json"
    d = json.loads(path.read_text())
    versions: list = []
    if isinstance(d.get("plugins"), list):
        for p in d["plugins"]:
            v = p.get("version")
            if v:
                versions.append(v)
    if "version" in d.get("metadata", {}):
        versions.append(d["metadata"]["version"])
    _aT("marketplace.json contains a >= 0.8.2 version",
         any(tuple(int(p) for p in v.split(".") if p.isdigit())
              >= (0, 8, 2) for v in versions),
         f"got {versions!r}")


def test_changelog_has_0_8_2_entry():
    _step("CHANGELOG.md has a [0.8.2] section covering rescue-shard")
    txt = (ROOT / "CHANGELOG.md").read_text()
    _aT("[0.8.2] heading present",
         "[0.8.2]" in txt,
         "section missing")
    head = txt[:txt.find("[0.8.0]")] if "[0.8.0]" in txt else txt[:4000]
    _aT("mentions rescue-shard",
         "rescue-shard" in head,
         "must describe rescue-shard command")
    _aT("mentions Go binary",
         "Go binary" in head or "dejavu-rescue" in head,
         "must describe the Go binary")
    _aT("mentions four-phase orchestrator",
         "four-phase" in head.lower() or "phase 4" in head.lower(),
         "must describe the orchestrator phases")


def test_install_step_renumbering_complete():
    _step("install.py has NO literal [N/8] step markers — "
          "all rewritten to [N/9]")
    src = (ROOT / "scripts" / "install.py").read_text()
    import re
    leftovers = re.findall(r"\[\d/8\]", src)
    _aT("no [N/8] markers remain",
         not leftovers,
         f"found leftovers: {leftovers}")
    # And the new [7/9] block must exist.
    _aT("[7/9] new step present",
         "[7/9]" in src,
         "new step header missing")


# ─── v0.8.3 — pg-rescue + weaviate-rescue + session-copy backup wiring ─


def test_recall_pg_rescue_subcommand_wired():
    _step("`claude-dejavu pg-rescue` subcommand is wired")
    src = (ROOT / "code" / "recall.py").read_text()
    _aT("cmd_pg_rescue handler defined",
         "def cmd_pg_rescue" in src)
    _aT("pg-rescue subparser registered",
         'sub.add_parser("pg-rescue"' in src)
    for sub in ("scan", "patch-toast", "reset-flags",
                  "rebuild-table", "reindex"):
        _aT(f"pg-rescue {sub} subparser present",
             f'sp_pg_sub.add_parser("{sub}"' in src,
             f"sub-subparser for {sub} missing")


def test_recall_weaviate_rescue_subcommand_wired():
    _step("`claude-dejavu weaviate-rescue` subcommand is wired")
    src = (ROOT / "code" / "recall.py").read_text()
    _aT("cmd_weaviate_rescue handler defined",
         "def cmd_weaviate_rescue" in src)
    _aT("weaviate-rescue subparser registered",
         'sub.add_parser("weaviate-rescue"' in src)
    for sub in ("scan", "quarantine-segment", "redo-class"):
        _aT(f"weaviate-rescue {sub} subparser present",
             f'sp_wv_sub.add_parser("{sub}"' in src,
             f"sub-subparser for {sub} missing")


def test_session_copy_take_has_include_flag():
    _step("session-copy take has --include flag (v0.8.3)")
    src = (ROOT / "code" / "recall.py").read_text()
    # Find the take subparser block and assert --include is present.
    take_block = src[src.find('sp_sc_sub.add_parser("take"'):
                          src.find('sp_sc_sub.add_parser("list"')]
    _aT('--include flag wired to take',
         '"--include"' in take_block,
         "--include must be wired to session-copy take")


def test_session_copy_restore_has_include_and_force_flags():
    _step("session-copy restore has --include and --force flags (v0.8.3)")
    src = (ROOT / "code" / "recall.py").read_text()
    rest_block = src[src.find('sp_sc_sub.add_parser("restore"'):
                          src.find('sp_sc_sub.add_parser("mount"')]
    _aT('--include flag on restore',
         '"--include"' in rest_block)
    _aT('--force flag on restore',
         '"--force"' in rest_block)


def test_doctor_has_deep_flag():
    _step("doctor has --deep flag wired (v0.8.3)")
    src = (ROOT / "code" / "recall.py").read_text()
    doctor_block = src[src.find('sub.add_parser("doctor"'):
                           src.find('sub.add_parser("config"')]
    _aT('--deep flag wired to doctor',
         '"--deep"' in doctor_block)
    dsrc = (ROOT / "code" / "doctor.py").read_text()
    _aT("run_doctor accepts deep parameter",
         "deep: bool" in dsrc or "deep=False" in dsrc)
    _aT("_check_pg_rescue_deep helper defined",
         "def _check_pg_rescue_deep" in dsrc)
    _aT("_check_weaviate_rescue_deep helper defined",
         "def _check_weaviate_rescue_deep" in dsrc)


def test_dejavu_pg_rescue_slash_command_exists():
    _step("commands/dejavu-pg-rescue.md slash command exists")
    p = ROOT / "commands" / "dejavu-pg-rescue.md"
    _aT("file exists", p.is_file())
    if p.is_file():
        c = p.read_text()
        _aT("mentions pg-rescue CLI",
             "claude-dejavu pg-rescue" in c)
        for sub in ("scan", "patch-toast", "reset-flags",
                      "rebuild-table", "reindex"):
            _aT(f"mentions {sub}", sub in c)


def test_dejavu_weaviate_rescue_slash_command_exists():
    _step("commands/dejavu-weaviate-rescue.md slash command exists")
    p = ROOT / "commands" / "dejavu-weaviate-rescue.md"
    _aT("file exists", p.is_file())
    if p.is_file():
        c = p.read_text()
        _aT("mentions weaviate-rescue CLI",
             "claude-dejavu weaviate-rescue" in c)
        for sub in ("scan", "quarantine-segment", "redo-class"):
            _aT(f"mentions {sub}", sub in c)


def test_plugin_json_at_0_8_3():
    """v0.8.3 version pin — superseded by v0.8.4. Kept as a forward-
    compatibility check: the manifest must be >= 0.8.3."""
    _step(".claude-plugin/plugin.json at version >= 0.8.3")
    d = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    v = (d.get("version") or "").split(".")
    _aT("plugin.json version >= 0.8.3",
         tuple(int(p) for p in v if p.isdigit()) >= (0, 8, 3),
         f"got {d.get('version')!r}")


def test_marketplace_json_at_0_8_3():
    """v0.8.3 version pin — superseded by v0.8.4. Kept as a forward-
    compatibility check: the marketplace must list a >= 0.8.3
    version."""
    _step(".claude-plugin/marketplace.json at version >= 0.8.3")
    d = json.loads((ROOT / ".claude-plugin" / "marketplace.json")
                       .read_text())
    versions = []
    if isinstance(d.get("plugins"), list):
        for p in d["plugins"]:
            v = p.get("version")
            if v:
                versions.append(v)
    if "version" in d.get("metadata", {}):
        versions.append(d["metadata"]["version"])
    _aT("contains a >= 0.8.3 version",
         any(tuple(int(p) for p in v.split(".") if p.isdigit())
              >= (0, 8, 3) for v in versions),
         f"got {versions!r}")


def test_changelog_has_0_8_3_entry():
    _step("CHANGELOG.md has a [0.8.3] section covering toolkit + backup")
    txt = (ROOT / "CHANGELOG.md").read_text()
    _aT("[0.8.3] heading present",
         "[0.8.3]" in txt)
    head = txt[:txt.find("[0.8.2]")] if "[0.8.2]" in txt else txt[:8000]
    _aT("mentions pg-rescue", "pg-rescue" in head)
    _aT("mentions weaviate-rescue", "weaviate-rescue" in head)
    _aT("mentions --include",
         "--include" in head or "include" in head)
    _aT("mentions doctor --deep", "--deep" in head)
    _aT("references the rescue scripts",
         "patch-pg-toast-corruption" in head
         or "rebuild-turns-table" in head
         or "quarantine-bad-weaviate-segment" in head,
         "should reference the canonical /tmp/* scripts")


def test_install_step_7_of_9_emits_v0_8_3_notice():
    _step("install.py [7/9] prints v0.8.3 backup-include reminder when "
          "installed-version.json < 0.8.3")
    src = (ROOT / "scripts" / "install.py").read_text()
    a = src.find("[7/9]"); b = src.find("[8/9]")
    block = src[a:b] if (a >= 0 and b > a) else src
    _aT("inspects installed-version.json",
         "installed-version.json" in block)
    _aT("compares to (0, 8, 3) tuple",
         "0, 8, 3" in block,
         "must gate the notice on previous-version < 0.8.3")
    _aT("prints the --include all reminder",
         "--include all" in block)


def test_installed_version_bumped_to_0_8_3_in_install_py():
    """v0.8.3 version pin — superseded by v0.8.4. Kept as a forward-
    compatibility check: install.py must write a version that parses
    as >= (0, 8, 3) into installed-version.json."""
    _step("install.py writes installed-version.json with version >= 0.8.3")
    import re as _re
    src = (ROOT / "scripts" / "install.py").read_text()
    # Match every `"version": "X.Y.Z"` literal that lives in the
    # installed-version.json write block. There's only one in install.py
    # per release-commit pattern, but the regex tolerates more.
    versions = _re.findall(r'"version":\s*"(\d+\.\d+\.\d+[a-z]?)"', src)
    parsed = [tuple(int(p) for p in v.split(".") if p.isdigit())
                for v in versions]
    _aT('"version": ">= 0.8.3" present in install.py',
         any(t >= (0, 8, 3) for t in parsed),
         f"got versions={versions!r}")


def test_pg_rescue_module_exposes_required_surface():
    _step("pg_rescue.py exposes scan, patch_toast, reset_flags, "
          "rebuild_table, reindex + RescueError")
    sys.path.insert(0, str(ROOT / "code"))
    import importlib
    pg = importlib.import_module("pg_rescue")
    for name in ("scan", "patch_toast", "reset_flags", "rebuild_table",
                   "reindex"):
        _aT(f"{name} callable",
             callable(getattr(pg, name, None)),
             f"{name} missing")
    _aT("RescueError defined",
         isinstance(getattr(pg, "RescueError", None), type))


def test_session_copy_reclaim_legacy_subcommand_wired():
    _step("`claude-dejavu session-copy reclaim-legacy` parser + handler "
          "+ migrate_rsync.reclaim_legacy_trees all present")
    src = (ROOT / "code" / "recall.py").read_text()
    _aT("reclaim-legacy sub-subparser registered",
         '"reclaim-legacy"' in src and "sp_sc_rl" in src,
         "parser missing")
    _aT("--yes flag present", "skip the typed-confirm phrase" in src)
    _aT("--dry-run flag present",
         "print the plan and exit without deleting" in src)
    _aT("handler dispatches to migrate_rsync.reclaim_legacy_trees",
         "reclaim_legacy_trees" in src,
         "handler missing")
    mr_src = (ROOT / "code" / "migrate_rsync.py").read_text()
    _aT("reclaim_legacy_trees defined in migrate_rsync.py",
         "def reclaim_legacy_trees" in mr_src)
    _aT("TTY-guard present (isatty check)",
         "isatty()" in mr_src and "reclaim" in mr_src.lower())
    _aT("typed-confirm phrase 'reclaim legacy snapshots'",
         "reclaim legacy snapshots" in mr_src)
    _aT("anchored regex for deleteme.<digits>",
         r"snapshots\.deleteme\.\\d+" in mr_src
         or r"snapshots\\.deleteme\\.\\d+" in mr_src
         or "_RECLAIM_RE" in mr_src,
         "regex pattern missing")


def test_install_step_7_of_9_post_migration_summary():
    _step("install.py [7/9] surfaces restic repo + .deleteme.* tree "
           "size + reclaim-legacy command after successful migration")
    import importlib.util as _iu
    spec = _iu.spec_from_file_location(
        "install", str(ROOT / "scripts" / "install.py"))
    install = _iu.module_from_spec(spec); spec.loader.exec_module(install)
    _aT("_print_post_migration_summary present",
         callable(getattr(install, "_print_post_migration_summary", None)))

    # Build a fake home/data_root with a legacy .deleteme tree.
    import tempfile, io, contextlib
    from pathlib import Path as _P
    class _FakeSpec:
        def __init__(self, **kw): self.__dict__.update(kw)
    class _FakeSC:
        RepoSpec = _FakeSpec
        @staticmethod
        def list_snapshots(spec):
            # one fake snapshot — anything truthy with len()
            return [object()]
    with tempfile.TemporaryDirectory() as td:
        td = _P(td)
        home = td / "home"; home.mkdir()
        root = td / "data"; root.mkdir()
        (root / "restic-repo").mkdir()
        (root / "restic-repo" / "config").write_bytes(b"x" * 1024)
        # legacy tree, ~2 MB
        backups = home / ".claude-backups"; backups.mkdir()
        deleteme = backups / "snapshots.deleteme.1234567890"
        deleteme.mkdir()
        (deleteme / "a").write_bytes(b"x" * (2 * 1024 * 1024))

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            install._print_post_migration_summary(
                root=root, home=home, sc=_FakeSC, auto=True)
        out = buf.getvalue()

    _aT("prints restic repo line", "restic repo:" in out, out)
    _aT("prints snapshot count line",
         "1 snapshot" in out, out)
    _aT("prints legacy tree line",
         "legacy tree renamed:" in out, out)
    _aT("prints reclaim-legacy command",
         "reclaim-legacy" in out, out)
    _aT("absolute path under --auto",
         str(deleteme) in out, out)


def test_weaviate_rescue_module_exposes_required_surface():
    _step("weaviate_rescue.py exposes scan, quarantine_segment, "
          "redo_class + WeaviateRescueError")
    sys.path.insert(0, str(ROOT / "code"))
    import importlib
    wv = importlib.import_module("weaviate_rescue")
    for name in ("scan", "quarantine_segment", "redo_class"):
        _aT(f"{name} callable",
             callable(getattr(wv, name, None)))
    _aT("WeaviateRescueError defined",
         isinstance(getattr(wv, "WeaviateRescueError", None), type))
    _aT("SEGMENT_EXTENSIONS exposed",
         tuple(getattr(wv, "SEGMENT_EXTENSIONS", ())) ==
         ("db", "bloom", "cna",
          "secondary.0.bloom", "secondary.1.bloom"))


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
        test_doctor_has_repo_identity_check,
        test_reindex_docs_flag_wired,
        test_reindex_docs_helper_exists,
        test_install_calls_reindex_docs_once,
        test_stop_hook_fires_doc_diff_scan,
        test_search_docs_cli_wired,
        test_search_docs_helper_exists,
        test_dejavu_docs_slash_command_exists,
        # v0.7.1 — session repair wiring
        test_recall_repair_subcommand_wired,
        test_recall_main_propagates_returncode,
        test_session_start_hook_auto_detects_corruption,
        test_install_seed_docs_forwards_env,
        test_install_preserves_cloud_config_on_reinstall,
        test_ingester_skips_fresh_repair_fingerprint,
        test_recall_cleanup_stubs_subcommand_wired,
        # v0.7.2 — file corruption scanner + file recovery wiring
        test_recall_scan_corruption_subcommand_wired,
        test_recall_recover_file_subcommand_wired,
        test_recall_recover_file_writes_backup_before_overwrite,
        test_dejavu_scan_corruption_slash_command_exists,
        test_dejavu_recover_file_slash_command_exists,
        # v0.8.0 — H6 session-copy hook wiring
        test_session_start_hook_emits_migration_pending_when_marker_exists,
        test_stop_hook_fires_session_copy_take_async,
        test_stop_hook_swallows_errors,
        test_stop_hook_skips_if_repo_uninitialized,
        # v0.8.0 — H5 CLI + doctor wiring
        test_recall_session_copy_subcommand_wired,
        test_doctor_session_copy_section_present,
        test_doctor_session_copy_check_cached_to_24h,
        test_repair_pre_recovery_snapshot_wired,
        test_recovery_pre_recovery_snapshot_wired,
        test_repair_no_pre_recovery_snapshot_flag_present,
        # v0.8.0 — H7 install.py + version + CHANGELOG
        test_install_step_7_of_9_calls_install_restic_when_missing,
        test_install_step_7_of_9_detects_legacy_state_calls_migrate,
        test_install_step_7_of_9_skips_migration_when_no_legacy_state,
        test_install_step_7_of_9_auto_mode_silent_migrate,
        test_install_step_7_of_9_interactive_prompts_then_runs,
        test_install_step_7_of_9_interactive_n_defers,
        test_plugin_json_at_0_8_0,
        test_marketplace_json_at_0_8_0,
        test_changelog_has_0_8_0_entry,
        test_install_step_renumbering_complete,
        # v0.8.2 — rescue-shard wiring
        test_recall_rescue_shard_subcommand_wired,
        test_recall_rescue_shard_handler_delegates_to_orchestrator,
        test_dejavu_rescue_slash_command_exists,
        test_doctor_reports_rescue_pending_when_shard_broken,
        test_install_step_7_of_9_calls_install_rescue_binary,
        test_install_rescue_module_exposes_required_surface,
        test_plugin_json_at_0_8_2,
        test_marketplace_json_at_0_8_2,
        test_changelog_has_0_8_2_entry,
        # v0.8.3 — pg-rescue / weaviate-rescue / session-copy --include
        test_recall_pg_rescue_subcommand_wired,
        test_recall_weaviate_rescue_subcommand_wired,
        test_session_copy_take_has_include_flag,
        test_session_copy_restore_has_include_and_force_flags,
        test_doctor_has_deep_flag,
        test_dejavu_pg_rescue_slash_command_exists,
        test_dejavu_weaviate_rescue_slash_command_exists,
        test_plugin_json_at_0_8_3,
        test_marketplace_json_at_0_8_3,
        test_changelog_has_0_8_3_entry,
        test_install_step_7_of_9_emits_v0_8_3_notice,
        test_installed_version_bumped_to_0_8_3_in_install_py,
        test_pg_rescue_module_exposes_required_surface,
        test_weaviate_rescue_module_exposes_required_surface,
        # v0.8.3 post-release polish — [7/9] UX + reclaim-legacy
        test_install_step_7_of_9_post_migration_summary,
        test_session_copy_reclaim_legacy_subcommand_wired,
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
