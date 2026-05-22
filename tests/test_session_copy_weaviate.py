#!/usr/bin/env python3
"""Unit tests for code/session_copy_weaviate.py — v0.8.3 Weaviate volume
backup wrapper. subprocess is mocked."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

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


def test_tar_argv_uses_throwaway_alpine_container():
    import session_copy_weaviate as scwv
    _step("_tar_argv runs alpine container with read-only volume mount")
    argv = scwv._tar_argv("my-vol", host_out="/tmp/out/wv.tgz")
    _aT("docker run --rm leading",
         argv[:3] == ["docker", "run", "--rm"])
    _aT("source volume mount read-only",
         any("my-vol:/data:ro" in a for a in argv))
    _aT("destination dir mount",
         any("/tmp/out:/out" in a for a in argv))
    _aT("alpine image",
         "alpine" in argv)
    _aT("tar invocation",
         any("tar -czf /out/wv.tgz" in a for a in argv))


def test_untar_argv_clears_then_extracts():
    import session_copy_weaviate as scwv
    _step("_untar_argv does rm -rf /data/* THEN extract")
    argv = scwv._untar_argv("my-vol", host_in="/tmp/in/wv.tgz")
    sh_cmd = next((a for a in argv if "tar -xzf" in a), "")
    _aT("clears /data/* first",
         "rm -rf /data/*" in sh_cmd)
    _aT("then extracts tar",
         "tar -xzf /in/wv.tgz" in sh_cmd)
    _aT("source dir mounted read-only",
         any("/tmp/in:/in:ro" in a for a in argv))


# ─── stage ────────────────────────────────────────────────────────────


def test_stage_brackets_with_docker_stop_and_start():
    import session_copy_weaviate as scwv
    _step("stage_weaviate_volume calls docker stop BEFORE and docker start AFTER tar")
    calls: list[list] = []

    def fake_runner(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="",
                                              stderr="")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        r = scwv.stage_weaviate_volume(td, runner=fake_runner)

    # Ordering: stop appears before tar appears before start.
    stop_idx = next((i for i, c in enumerate(calls)
                       if c[:2] == ["docker", "stop"]), -1)
    tar_idx = next((i for i, c in enumerate(calls)
                       if c[:3] == ["docker", "run", "--rm"]), -1)
    start_idx = next((i for i, c in enumerate(calls)
                        if c[:2] == ["docker", "start"]), -1)
    _aT("stop called", stop_idx >= 0)
    _aT("tar called", tar_idx >= 0)
    _aT("start called", start_idx >= 0)
    _aT("stop precedes tar", stop_idx < tar_idx)
    _aT("tar precedes start", tar_idx < start_idx)
    _aT("container_restarted true", r["container_restarted"])


def test_stage_restarts_weaviate_even_when_tar_fails():
    import session_copy_weaviate as scwv
    _step("stage_weaviate_volume restarts WV in finally{} on tar failure")
    started_after_failure = {"yes": False}
    tar_seen = {"yes": False}

    def fake_runner(args, **kw):
        if args[:3] == ["docker", "run", "--rm"]:
            tar_seen["yes"] = True
            return subprocess.CompletedProcess(args, 1, stdout="",
                                                  stderr="disk full")
        if args[:2] == ["docker", "start"] and tar_seen["yes"]:
            started_after_failure["yes"] = True
        return subprocess.CompletedProcess(args, 0, stdout="",
                                              stderr="")

    with tempfile.TemporaryDirectory() as td:
        r = scwv.stage_weaviate_volume(Path(td), runner=fake_runner)
    _aT("errored=True", r["errored"])
    _aT("container_restarted=True (finally{})",
         r["container_restarted"]
         and started_after_failure["yes"])


def test_stage_records_file_size_on_success():
    import session_copy_weaviate as scwv
    _step("stage_weaviate_volume records the tarball size after success")

    def fake_runner(args, **kw):
        if args[:3] == ["docker", "run", "--rm"]:
            # Simulate the alpine container writing the tarball
            # Output path is parsed from the -v /out mount + the tar cmd
            for a in args:
                if a.startswith("tar -czf "):
                    parts = a.split(" ")
                    out_in_container = parts[2]  # /out/<basename>
                    basename = Path(out_in_container).name
            # We need the host out_dir — extract from the -v mount
            mount = next(a for a in args if ":/out" in a)
            host_out = mount.split(":")[0]
            (Path(host_out) / basename).write_bytes(b"X" * 4096)
        return subprocess.CompletedProcess(args, 0, stdout="",
                                              stderr="")

    with tempfile.TemporaryDirectory() as td:
        r = scwv.stage_weaviate_volume(Path(td), runner=fake_runner)
    _aT("size > 0", r["size"] >= 4096, str(r))
    _aT("not errored", not r["errored"])


# ─── restore ──────────────────────────────────────────────────────────


def test_restore_refuses_without_force():
    import session_copy_weaviate as scwv
    _step("restore_weaviate_volume refuses to clobber live volume without force=True")
    with tempfile.TemporaryDirectory() as td:
        tar = Path(td) / "wv.tgz"
        tar.write_bytes(b"dummy")
        out = scwv.restore_weaviate_volume(tar)
    _aT("restored=False", not out["restored"])
    _aT("error mentions force",
         "force=True" in (out.get("error") or ""))


def test_restore_with_force_runs_untar():
    import session_copy_weaviate as scwv
    _step("restore_weaviate_volume with force=True invokes the untar argv")
    calls: list[list] = []

    def fake_runner(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="",
                                              stderr="")

    with tempfile.TemporaryDirectory() as td:
        tar = Path(td) / "wv.tgz"
        tar.write_bytes(b"dummy")
        out = scwv.restore_weaviate_volume(tar, force=True,
                                                  runner=fake_runner)

    untar_calls = [c for c in calls if c[:3] == ["docker", "run", "--rm"]]
    _aT("untar invoked", len(untar_calls) == 1)
    _aT("restored=True", out["restored"])
    _aT("container_restarted=True", out["container_restarted"])


def test_restore_raises_when_tarball_missing():
    import session_copy_weaviate as scwv
    _step("restore_weaviate_volume raises WeaviateBackupError when tarball is gone")
    raised = False
    try:
        scwv.restore_weaviate_volume(Path("/tmp/missing.tgz"),
                                            force=True)
    except scwv.WeaviateBackupError:
        raised = True
    _aT("raised", raised)


def test_wv_config_defaults():
    import session_copy_weaviate as scwv
    _step("WvConfig defaults match dejavu canonical names")
    c = scwv.WvConfig()
    _aT("container default",
         c.container == "claude-dejavu-weaviate")
    _aT("volume default",
         c.volume == "claude-dejavu_claude-dejavu-weaviate")


def test_restore_restarts_weaviate_on_failure():
    import session_copy_weaviate as scwv
    _step("restore_weaviate_volume restarts WV in finally{} on untar failure")
    started = {"yes": False}
    saw_untar_fail = {"yes": False}

    def fake_runner(args, **kw):
        if args[:3] == ["docker", "run", "--rm"]:
            saw_untar_fail["yes"] = True
            return subprocess.CompletedProcess(args, 1, stdout="",
                                                  stderr="corrupted")
        if args[:2] == ["docker", "start"] and saw_untar_fail["yes"]:
            started["yes"] = True
        return subprocess.CompletedProcess(args, 0, stdout="",
                                              stderr="")

    with tempfile.TemporaryDirectory() as td:
        tar = Path(td) / "wv.tgz"
        tar.write_bytes(b"X")
        out = scwv.restore_weaviate_volume(tar, force=True,
                                                  runner=fake_runner)
    _aT("restored=False", not out["restored"])
    _aT("container_restarted in finally{}",
         out["container_restarted"] and started["yes"])


def main() -> int:
    print("claude-dejavu v0.8.3 session_copy_weaviate tests")
    print(f"  ROOT={ROOT}")
    tests = [
        test_tar_argv_uses_throwaway_alpine_container,
        test_untar_argv_clears_then_extracts,
        test_stage_brackets_with_docker_stop_and_start,
        test_stage_restarts_weaviate_even_when_tar_fails,
        test_stage_records_file_size_on_success,
        test_restore_refuses_without_force,
        test_restore_with_force_runs_untar,
        test_restore_raises_when_tarball_missing,
        test_wv_config_defaults,
        test_restore_restarts_weaviate_on_failure,
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
