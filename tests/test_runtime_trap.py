"""Unit tests for code/runtime_trap.py.

Validates the renderers (TS + Python), the generate() entry point,
and end-to-end functional behavior of the generated Python module."""
from __future__ import annotations

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


def test_render_typescript() -> None:
    _step("render_typescript: structure + content")
    from runtime_trap import render_typescript
    out = render_typescript(["DATABASE_URL", "STRIPE_SECRET_KEY",
                                  "LAUNCHDARKLY_SDK_KEY"])
    for need in (
        'import { z } from "zod";',
        "DeclaredEnvKey",
        "DATABASE_URL",
        "STRIPE_SECRET_KEY",
        "LAUNCHDARKLY_SDK_KEY",
        "new Proxy",
        "throw new Error",
        "claude-dejavu env propose",  # error message hints at fix
        "AUTO-GENERATED",
    ):
        _aT(f"contains `{need[:30]}`", need in out, out[:200])
    # Sorted output (declared keys appear sorted)
    db_at = out.index("DATABASE_URL")
    ld_at = out.index("LAUNCHDARKLY_SDK_KEY")
    st_at = out.index("STRIPE_SECRET_KEY")
    _aT("declared keys are sorted",
         db_at < ld_at < st_at,
         f"positions: db={db_at}, ld={ld_at}, st={st_at}")


def test_render_python() -> None:
    _step("render_python: structure + content")
    from runtime_trap import render_python
    out = render_python(["DATABASE_URL", "REDIS_URL"])
    for need in (
        "_DECLARED_ENV_KEYS",
        "DejavuUndeclaredEnvError",
        "_EnvProxy",
        "import os",
        "DATABASE_URL",
        "REDIS_URL",
        "claude-dejavu env propose",
        "AUTO-GENERATED",
    ):
        _aT(f"contains `{need}`", need in out, out[:200])


def test_generate_writes_file() -> None:
    _step("generate(): writes file at expected path")
    from runtime_trap import generate, DEFAULT_OUTPUTS
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Default output path for TS
        path, content = generate(["DATABASE_URL"],
                                       language="typescript",
                                       project_root=root)
        _aT("default TS path",
             path == root / DEFAULT_OUTPUTS["typescript"])
        _aT("file exists", path.is_file())
        _aT("content has DATABASE_URL", "DATABASE_URL" in content)

        # Custom output
        custom = root / "custom" / "env.ts"
        path2, _ = generate(["X"], language="typescript",
                                  output_path=custom)
        _aT("custom path used", path2 == custom and custom.is_file())

        # Default Python path
        path3, _ = generate(["A", "B"], language="python",
                                  project_root=root)
        _aT("default Python path",
             path3 == root / DEFAULT_OUTPUTS["python"])

        # Unknown language
        try:
            generate(["X"], language="cobol", project_root=root)
            _fail("unknown language should have raised")
        except ValueError:
            _ok("ValueError on unknown language")


def test_python_trap_functional() -> None:
    _step("generated Python trap: declared returns, undeclared raises")
    from runtime_trap import generate
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        out = root / "dejavu_env.py"
        generate(["DATABASE_URL", "STRIPE_SECRET_KEY"],
                   language="python", output_path=out)

        check = f"""
import sys
sys.path.insert(0, {str(root)!r})
from dejavu_env import env, DejavuUndeclaredEnvError, _DECLARED_ENV_KEYS

# Declared set
assert _DECLARED_ENV_KEYS == frozenset({{'DATABASE_URL', 'STRIPE_SECRET_KEY'}}), \\
    f'wrong declared set: {{_DECLARED_ENV_KEYS}}'

# `in env` works
assert 'DATABASE_URL' in env
assert 'NONEXISTENT' not in env

# Attribute access — declared returns env value (we set DATABASE_URL=hello below)
assert env.DATABASE_URL == 'hello', f'got {{env.DATABASE_URL!r}}'

# Bracket access works the same way
assert env['DATABASE_URL'] == 'hello'

# Undeclared via attribute → raises
try:
    _ = env.MADE_UP_KEY
    raise SystemExit('did not raise on attribute access')
except DejavuUndeclaredEnvError as e:
    assert 'MADE_UP_KEY' in str(e)
    assert 'claude-dejavu env propose' in str(e)

# Undeclared via bracket → raises (same)
try:
    _ = env['ANOTHER_FAKE']
    raise SystemExit('did not raise on bracket access')
except DejavuUndeclaredEnvError:
    pass

# DejavuUndeclaredEnvError must be a KeyError subclass for
# graceful catch in generic code paths
try:
    _ = env.X
except KeyError:
    pass

# Internal attrs are still inaccessible (the .startswith('_') guard)
try:
    _ = env._private_attr
    raise SystemExit('private attr should have raised AttributeError')
except AttributeError:
    pass

print('OK')
"""
        r = subprocess.run([sys.executable, "-c", check],
                              env={**os.environ,
                                    "DATABASE_URL": "hello"},
                              capture_output=True, text=True)
        if r.returncode != 0 or "OK" not in r.stdout:
            _fail("functional check failed",
                   r.stderr[-300:] or r.stdout[-300:])
            return
        _ok("declared/undeclared/in/bracket/attribute/private all "
             "behave correctly; DejavuUndeclaredEnvError <: KeyError")


def test_python_trap_returns_none_for_unset_declared() -> None:
    _step("generated Python trap: declared-but-unset env returns None")
    from runtime_trap import generate
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        out = root / "dejavu_env.py"
        generate(["UNSET_BUT_DECLARED"], language="python",
                   output_path=out)
        check = f"""
import sys, os
os.environ.pop('UNSET_BUT_DECLARED', None)
sys.path.insert(0, {str(root)!r})
from dejavu_env import env
assert env.UNSET_BUT_DECLARED is None
print('OK')
"""
        r = subprocess.run([sys.executable, "-c", check],
                              capture_output=True, text=True)
        _aT("declared-but-unset returns None",
             r.returncode == 0 and "OK" in r.stdout,
             r.stderr[-200:])


def main() -> int:
    print("claude-dejavu runtime_trap tests")
    print(f"  ROOT={ROOT}")
    tests = [
        test_render_typescript,
        test_render_python,
        test_generate_writes_file,
        test_python_trap_functional,
        test_python_trap_returns_none_for_unset_declared,
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
