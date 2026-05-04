"""Unit tests for code/error_log.py.

Exercises: log_error / read_recent / install_excepthook / format_for_human
/ log_path_str / rotation."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(msg): PASS.append(msg); print(f"  \033[32m✓\033[0m {msg}", flush=True)
def _fail(msg, d=""): FAIL.append((msg, d)); print(f"  \033[31m✗\033[0m {msg}"); d and print(f"      {d}")
def _step(name): print(f"\n\033[36m── {name} ──\033[0m", flush=True)
def _aT(name, cond, d=""): _ok(name) if cond else _fail(name, d)


def _isolated_env():
    tmp = tempfile.mkdtemp(prefix="dejavu-elog-")
    return tmp, {**os.environ, "XDG_DATA_HOME": tmp}


def test_log_error_basic() -> None:
    _step("log_error: basic record + read_recent")
    tmp, env = _isolated_env()
    script = f"""
import sys, json
sys.path.insert(0, {str(ROOT / 'code')!r})
from error_log import log_error, read_recent
log_error('test.basic', 'msg one')
log_error('test.basic', 'msg two', context={{'k': 1}})
recs = read_recent(limit=5)
print(json.dumps([r['message'] for r in recs]))
"""
    r = subprocess.run([sys.executable, "-c", script], env=env,
                          capture_output=True, text=True)
    if r.returncode != 0:
        _fail("crashed", r.stderr[-300:]); return
    msgs = json.loads(r.stdout.strip())
    _aT("two records, newest first",
         msgs == ["msg two", "msg one"],
         f"got {msgs}")


def test_log_error_traceback() -> None:
    _step("log_error: exc_info captures traceback")
    tmp, env = _isolated_env()
    script = f"""
import sys, json
sys.path.insert(0, {str(ROOT / 'code')!r})
from error_log import log_error, read_recent
try:
    raise ValueError('boom')
except Exception:
    log_error('test.tb', 'failed', exc_info=True)
recs = read_recent(limit=1)
r = recs[0]
print(json.dumps({{
    'has_tb': 'traceback' in r,
    'exc_type': r.get('exception_type'),
    'exc_msg': r.get('exception'),
}}))
"""
    r = subprocess.run([sys.executable, "-c", script], env=env,
                          capture_output=True, text=True)
    if r.returncode != 0:
        _fail("crashed", r.stderr[-300:]); return
    info = json.loads(r.stdout.strip())
    _aT("traceback captured", info["has_tb"])
    _aT("exception_type set", info["exc_type"] == "ValueError")
    _aT("exception message preserved", info["exc_msg"] == "boom")


def test_filtering() -> None:
    _step("read_recent filtering: component / level / since_ts")
    tmp, env = _isolated_env()
    script = f"""
import sys, json
sys.path.insert(0, {str(ROOT / 'code')!r})
from error_log import log_error, read_recent
log_error('indexer.env', 'env failed', level='error')
log_error('indexer.db', 'db failed', level='error')
log_error('mcp.server', 'tool slow', level='warning')

# component prefix
by_idx = [r['component'] for r in read_recent(limit=10, component='indexer')]
# level
by_lvl = [r['component'] for r in read_recent(limit=10, level='warning')]
print(json.dumps({{'by_component': by_idx, 'by_level': by_lvl}}))
"""
    r = subprocess.run([sys.executable, "-c", script], env=env,
                          capture_output=True, text=True)
    if r.returncode != 0:
        _fail("crashed", r.stderr[-300:]); return
    info = json.loads(r.stdout.strip())
    _aT("component prefix filter",
         set(info["by_component"]) == {"indexer.env", "indexer.db"})
    _aT("level filter",
         info["by_level"] == ["mcp.server"])


def test_rotation() -> None:
    _step("rotation: large writes spill into .1")
    tmp, env = _isolated_env()
    # Force a tiny MAX_BYTES so we don't have to write 5 MB.
    # Patch via env var the module reads at import? error_log.MAX_BYTES
    # is a module constant — easiest: monkey-patch it post-import.
    script = f"""
import sys, json
sys.path.insert(0, {str(ROOT / 'code')!r})
import error_log
error_log.MAX_BYTES = 200  # tiny
for i in range(20):
    error_log.log_error('rot.test', f'record-{{i:02d}}' + 'X'*30)
from pathlib import Path
base = error_log._log_path()
rotated = base.with_suffix(base.suffix + '.1')
print(json.dumps({{
    'main_exists': base.exists(),
    'rotated_exists': rotated.exists(),
    'main_size': base.stat().st_size if base.exists() else 0,
}}))
"""
    r = subprocess.run([sys.executable, "-c", script], env=env,
                          capture_output=True, text=True)
    if r.returncode != 0:
        _fail("crashed", r.stderr[-300:]); return
    info = json.loads(r.stdout.strip())
    _aT("active log present", info["main_exists"])
    _aT("rotated .1 file created", info["rotated_exists"])


def test_log_path_str() -> None:
    _step("log_path_str + format_for_human")
    tmp, env = _isolated_env()
    script = f"""
import sys, json
sys.path.insert(0, {str(ROOT / 'code')!r})
from error_log import log_error, read_recent, format_for_human, log_path_str
log_error('test.fmt', 'a message', context={{'foo': 'bar'}})
out = format_for_human(read_recent(limit=1))
print(json.dumps({{'path_ends_with': log_path_str().endswith('errors.log'),
                    'has_message': 'a message' in out,
                    'has_context': 'foo: bar' in out}}))
"""
    r = subprocess.run([sys.executable, "-c", script], env=env,
                          capture_output=True, text=True)
    if r.returncode != 0:
        _fail("crashed", r.stderr[-300:]); return
    info = json.loads(r.stdout.strip())
    _aT("log_path_str ends with errors.log", info["path_ends_with"])
    _aT("formatter renders message", info["has_message"])
    _aT("formatter renders context", info["has_context"])


def test_install_excepthook() -> None:
    _step("install_excepthook captures uncaught exceptions")
    tmp, env = _isolated_env()
    script = f"""
import sys
sys.path.insert(0, {str(ROOT / 'code')!r})
from error_log import install_excepthook
install_excepthook(component='test.uncaught')
raise RuntimeError('escaped')
"""
    r = subprocess.run([sys.executable, "-c", script], env=env,
                          capture_output=True, text=True)
    # Process should crash (returncode != 0) but the error_log file
    # should now contain the record.
    if r.returncode == 0:
        _fail("excepthook didn't propagate the crash"); return
    # Read the log
    logfile = Path(tmp) / "claude-dejavu" / "errors.log"
    if not logfile.exists():
        _fail(f"log not created at {logfile}"); return
    content = logfile.read_text()
    _aT("traceback captured by excepthook",
         "RuntimeError" in content and "escaped" in content,
         content[:300])


def test_safe_on_disk_failure() -> None:
    _step("log_error never raises on disk failure")
    # Point XDG to a non-writable path
    script = f"""
import sys, os
sys.path.insert(0, {str(ROOT / 'code')!r})
os.environ['XDG_DATA_HOME'] = '/proc/this-cannot-be-written'
from error_log import log_error
# Should swallow the OSError and not propagate
log_error('test.disk', 'oops')
print('OK')
"""
    r = subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True)
    _aT("returncode 0 even with bad XDG",
         r.returncode == 0,
         r.stderr[-200:])
    _aT("'OK' marker reached",
         "OK" in r.stdout)


def main() -> int:
    print("claude-dejavu error_log tests")
    print(f"  ROOT={ROOT}")
    tests = [
        test_log_error_basic,
        test_log_error_traceback,
        test_filtering,
        test_rotation,
        test_log_path_str,
        test_install_excepthook,
        test_safe_on_disk_failure,
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
