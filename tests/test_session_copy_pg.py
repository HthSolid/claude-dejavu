#!/usr/bin/env python3
"""Unit tests for code/session_copy_pg.py — the v0.8.3 pg_dump / pg_restore
staging modules for session-copy. subprocess is fully mocked."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(m): PASS.append(m); print(f"  \033[32m✓\033[0m {m}", flush=True)
def _f(m, d=""):
    FAIL.append((m, d)); print(f"  \033[31m✗\033[0m {m}")
    d and print(f"      {d}")
def _step(n): print(f"\n\033[36m── {n} ──\033[0m", flush=True)
def _aT(n, c, d=""): _ok(n) if c else _f(n, d)


# ─── argv shape ────────────────────────────────────────────────────────


def test_docker_pg_dump_argv_shape():
    import session_copy_pg as scpg
    _step("_docker_pg_dump_argv has --format=custom + --file + db_name")
    conn = scpg.PgConnInfo()
    argv = scpg._docker_pg_dump_argv(conn, "claude_dejavu_test")
    _aT("docker exec leading",
         argv[:2] == ["docker", "exec"])
    _aT("PGPASSWORD env",
         any("PGPASSWORD=postgres" in a for a in argv))
    _aT("pg_dump invoked",
         "pg_dump" in argv)
    _aT("--format=custom",
         "--format=custom" in argv)
    _aT("--no-owner present", "--no-owner" in argv)
    _aT("db name terminates argv",
         argv[-1] == "claude_dejavu_test")


def test_host_pg_dump_argv_shape():
    import session_copy_pg as scpg
    _step("_host_pg_dump_argv has --file + db name without docker prefix")
    conn = scpg.PgConnInfo()
    argv = scpg._host_pg_dump_argv(conn, "claude_dejavu_x",
                                          file_out="/tmp/x.dump")
    _aT("does NOT start with docker", argv[0] != "docker")
    _aT("pg_dump first", argv[0] == "pg_dump")
    _aT("--file then path",
         "--file" in argv
         and argv[argv.index("--file") + 1] == "/tmp/x.dump")
    _aT("db name last",
         argv[-1] == "claude_dejavu_x")


def test_docker_pg_restore_argv_has_clean_and_if_exists():
    import session_copy_pg as scpg
    _step("_docker_pg_restore_argv has --clean --if-exists")
    conn = scpg.PgConnInfo()
    argv = scpg._docker_pg_restore_argv(conn, "claude_dejavu_x")
    _aT("--clean present", "--clean" in argv)
    _aT("--if-exists present", "--if-exists" in argv)
    _aT("--dbname matches",
         argv[argv.index("--dbname") + 1] == "claude_dejavu_x")


# ─── docker vs host fallback ──────────────────────────────────────────


def test_stage_pg_dump_prefers_docker_path():
    import session_copy_pg as scpg
    _step("stage_pg_dump uses docker-exec when pg_dump lives in container")
    captured: list[list] = []

    def fake_runner(args, **kw):
        captured.append(args)
        # docker-availability probe
        if "which" in args and "pg_dump" in args:
            return subprocess.CompletedProcess(args, 0,
                                                  stdout="/usr/bin/pg_dump\n",
                                                  stderr="")
        # pg_dump invocation
        if "pg_dump" in args:
            return subprocess.CompletedProcess(args, 0,
                                                  stdout="", stderr="")
        # docker cp
        if args[:2] == ["docker", "cp"]:
            # Side-effect: write a placeholder file so .stat().st_size works
            dst = args[3]
            Path(dst).write_bytes(b"X" * 32)
            return subprocess.CompletedProcess(args, 0, stdout="",
                                                  stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="",
                                              stderr="")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        r = scpg.stage_pg_dump(td, db_names=["mydb"], runner=fake_runner)

    _aT("mode == docker", r["mode"] == "docker",
         str(r))
    _aT("one dump produced",
         len(r["dumped"]) == 1 and r["errored"] == [],
         str(r))
    _aT("dump size > 0",
         r["dumped"][0]["size"] > 0,
         str(r))
    _aT("docker cp invoked",
         any(a[:2] == ["docker", "cp"] for a in captured))


def test_stage_pg_dump_falls_back_to_host_binary():
    import session_copy_pg as scpg
    _step("stage_pg_dump falls back to host pg_dump when docker-exec fails")
    captured: list[list] = []

    def fake_runner(args, **kw):
        captured.append(args)
        # docker probe FAILS (no pg_dump in container)
        if args[:2] == ["docker", "exec"] and "which" in args:
            return subprocess.CompletedProcess(args, 1, stdout="",
                                                  stderr="not found")
        # host pg_dump invocation
        if args[0] == "pg_dump":
            # touch the output file so size check passes
            f_idx = args.index("--file") + 1
            Path(args[f_idx]).write_bytes(b"H" * 64)
            return subprocess.CompletedProcess(args, 0, stdout="",
                                                  stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="",
                                              stderr="")

    with mock.patch("shutil.which", return_value="/usr/bin/pg_dump"), \
         tempfile.TemporaryDirectory() as td:
        td = Path(td)
        r = scpg.stage_pg_dump(td, db_names=["mydb"],
                                   runner=fake_runner)

    _aT("mode == host", r["mode"] == "host", str(r))
    _aT("dump produced",
         len(r["dumped"]) == 1 and r["dumped"][0]["size"] > 0,
         str(r))
    _aT("no docker cp called",
         not any(a[:2] == ["docker", "cp"] for a in captured))


def test_stage_pg_dump_raises_when_neither_available():
    import session_copy_pg as scpg
    _step("stage_pg_dump raises PgBackupError when no pg_dump anywhere")

    def fake_runner(args, **kw):
        return subprocess.CompletedProcess(args, 1, stdout="",
                                              stderr="not found")

    raised = False
    with mock.patch("shutil.which", return_value=None), \
         tempfile.TemporaryDirectory() as td:
        try:
            scpg.stage_pg_dump(Path(td), db_names=["mydb"],
                                 runner=fake_runner)
        except scpg.PgBackupError:
            raised = True
    _aT("PgBackupError raised", raised)


# ─── restore guards ───────────────────────────────────────────────────


def test_restore_refuses_non_empty_db_without_force():
    import session_copy_pg as scpg
    _step("restore_pg_dump: refuses to clobber non-empty target without force=True")

    def fake_runner(args, **kw):
        if "which" in args and "pg_dump" in args:
            return subprocess.CompletedProcess(args, 0,
                                                  stdout="/usr/bin/pg_dump",
                                                  stderr="")
        # psql probe — return non-zero count → "non-empty"
        if "psql" in args:
            return subprocess.CompletedProcess(args, 0, stdout="42\n",
                                                  stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="",
                                              stderr="")

    with tempfile.TemporaryDirectory() as td:
        dump = Path(td) / "x.dump"
        dump.write_bytes(b"dummy")
        out = scpg.restore_pg_dump(dump, db_name="mydb",
                                          force=False, runner=fake_runner)

    _aT("restored=False", not out["restored"])
    _aT("error mentions clobber",
         "non-empty" in (out.get("error") or "")
         or "clobber" in (out.get("error") or ""),
         str(out))


def test_restore_runs_pg_restore_when_force_and_empty():
    import session_copy_pg as scpg
    _step("restore_pg_dump with force=True runs pg_restore --clean --if-exists")
    captured: list[list] = []

    def fake_runner(args, **kw):
        captured.append(args)
        if "which" in args and "pg_dump" in args:
            return subprocess.CompletedProcess(args, 0,
                                                  stdout="/usr/bin/pg_dump",
                                                  stderr="")
        # psql probe — empty DB
        if "psql" in args:
            return subprocess.CompletedProcess(args, 0, stdout="0\n",
                                                  stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="",
                                              stderr="")

    with tempfile.TemporaryDirectory() as td:
        dump = Path(td) / "x.dump"
        dump.write_bytes(b"dummy")
        out = scpg.restore_pg_dump(dump, db_name="mydb",
                                          force=False, runner=fake_runner)

    # Empty DB → should restore even without force
    _aT("restored=True (empty DB is OK)",
         out["restored"], str(out))
    # The pg_restore argv must include --clean --if-exists
    restore_calls = [a for a in captured if "pg_restore" in a]
    _aT("pg_restore invoked", len(restore_calls) >= 1)
    one = restore_calls[0]
    _aT("--clean in pg_restore", "--clean" in one)
    _aT("--if-exists in pg_restore", "--if-exists" in one)


def test_restore_raises_when_dump_file_missing():
    import session_copy_pg as scpg
    _step("restore_pg_dump raises PgBackupError when the dump file is gone")
    raised = False
    try:
        scpg.restore_pg_dump(Path("/tmp/does-not-exist.dump"),
                                  db_name="mydb")
    except scpg.PgBackupError:
        raised = True
    _aT("PgBackupError raised", raised)


def test_default_db_names_uses_pg_db_cfg_when_set():
    import session_copy_pg as scpg
    _step("_default_db_names honors PG_DB env / cfg override")
    with mock.patch.object(scpg, "_load_cfg",
                            return_value={"PG_DB": "my_special"}):
        names = scpg._default_db_names()
    _aT("returns the override", names == ["my_special"])


def test_pg_conn_info_defaults_match_dejavu():
    import session_copy_pg as scpg
    _step("PgConnInfo defaults match dejavu canonical (5450 / postgres / postgres)")
    c = scpg.PgConnInfo()
    _aT("port 5450", c.port == "5450")
    _aT("user postgres", c.user == "postgres")
    _aT("container claude-dejavu-postgres",
         c.container == "claude-dejavu-postgres")


def main() -> int:
    print("claude-dejavu v0.8.3 session_copy_pg tests")
    print(f"  ROOT={ROOT}")
    tests = [
        test_docker_pg_dump_argv_shape,
        test_host_pg_dump_argv_shape,
        test_docker_pg_restore_argv_has_clean_and_if_exists,
        test_stage_pg_dump_prefers_docker_path,
        test_stage_pg_dump_falls_back_to_host_binary,
        test_stage_pg_dump_raises_when_neither_available,
        test_restore_refuses_non_empty_db_without_force,
        test_restore_runs_pg_restore_when_force_and_empty,
        test_restore_raises_when_dump_file_missing,
        test_default_db_names_uses_pg_db_cfg_when_set,
        test_pg_conn_info_defaults_match_dejavu,
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
        print(f"  ✗ {m}  {d[:120]}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
