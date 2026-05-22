"""v0.8.17 unit tests — PreCompact hook output contract.

PreCompact is the Claude Code hook event that fires JUST BEFORE the
working context gets compacted. Our hook (hooks/pre_compact.py) emits
a systemMessage telling the assistant how to recover detail via
dejavu_session_export / dejavu_global_search.

Infra-free. Drives the hook via subprocess with mocked stdin payloads
and asserts on the JSON envelope it prints to stdout. PG connection
is skipped when unreachable — the hook is designed to degrade
gracefully there.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "pre_compact.py"


PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(msg: str) -> None:
    PASS.append(msg)
    print(f"  \033[32m✓\033[0m {msg}", flush=True)


def _fail(msg: str, detail: str = "") -> None:
    FAIL.append((msg, detail))
    print(f"  \033[31m✗\033[0m {msg}")
    if detail:
        print(f"      {detail[:300]}")


def _aT(name: str, cond: bool, detail: str = "") -> None:
    _ok(name) if cond else _fail(name, detail)


def _step(name: str) -> None:
    print(f"\n\033[36m── {name} ──\033[0m", flush=True)


def _run(stdin: str, timeout: float = 5.0) -> tuple[int, str, str]:
    r = subprocess.run(
        [sys.executable, str(HOOK)],
        input=stdin, capture_output=True, text=True, timeout=timeout,
    )
    return r.returncode, r.stdout, r.stderr


def test_valid_payload_emits_recovery_systemMessage() -> None:
    _step("PreCompact: valid {session_id, transcript_path} payload")
    sid = "f4358269-1234-4abc-8def-0123456789ab"
    rc, out, err = _run(json.dumps(
        {"session_id": sid, "transcript_path": "/tmp/x"}))
    _aT("hook exits 0", rc == 0, f"rc={rc} stderr={err[:200]}")
    try:
        envelope = json.loads(out)
    except json.JSONDecodeError as e:
        _fail("stdout is valid JSON", f"{e!r}: {out[:200]}")
        return
    _aT("envelope.continue == True", envelope.get("continue") is True,
        str(envelope))
    msg = envelope.get("systemMessage") or ""
    _aT("systemMessage mentions compaction", "compact" in msg.lower(),
        msg[:200])
    _aT("systemMessage names dejavu_session_export",
        "dejavu_session_export" in msg, msg[:200])
    _aT("systemMessage embeds the 8-char session prefix",
        sid[:8] in msg, msg[:200])
    _aT("systemMessage mentions dejavu_global_search "
        "(cross-session recall path)",
        "dejavu_global_search" in msg, msg[:200])


def test_empty_stdin_emits_noop_envelope() -> None:
    _step("PreCompact: empty stdin → no-op JSON envelope, never breaks")
    rc, out, err = _run("")
    _aT("hook exits 0 on empty stdin", rc == 0,
        f"rc={rc} stderr={err[:200]}")
    try:
        envelope = json.loads(out)
    except json.JSONDecodeError:
        _fail("stdout is valid JSON on empty stdin", out[:200])
        return
    _aT("envelope has continue=True", envelope.get("continue") is True,
        str(envelope))
    _aT("envelope has NO systemMessage on empty input",
        "systemMessage" not in envelope, str(envelope))


def test_missing_session_id_emits_noop_envelope() -> None:
    _step("PreCompact: payload without session_id → no-op")
    rc, out, err = _run("{}")
    _aT("hook exits 0 on {} payload", rc == 0,
        f"rc={rc} stderr={err[:200]}")
    try:
        envelope = json.loads(out)
    except json.JSONDecodeError:
        _fail("stdout is valid JSON on {} payload", out[:200])
        return
    _aT("no systemMessage emitted when session_id absent",
        "systemMessage" not in envelope, str(envelope))


def test_malformed_json_emits_noop_envelope() -> None:
    _step("PreCompact: malformed JSON stdin → no-op, no crash")
    rc, out, err = _run("not-json-at-all")
    _aT("hook exits 0 on malformed input", rc == 0,
        f"rc={rc} stderr={err[:200]}")
    try:
        envelope = json.loads(out)
    except json.JSONDecodeError:
        _fail("stdout is valid JSON on malformed input", out[:200])
        return
    _aT("no systemMessage emitted on malformed input",
        "systemMessage" not in envelope, str(envelope))


def test_hook_is_fast_enough_for_3s_timeout() -> None:
    _step("PreCompact: hook completes well under 3s claude-code budget")
    import time
    sid = "f4358269-1234-4abc-8def-0123456789ab"
    t0 = time.perf_counter()
    rc, _, _ = _run(json.dumps(
        {"session_id": sid, "transcript_path": "/tmp/x"}))
    dt = time.perf_counter() - t0
    _aT(f"hook completes in <2s (got {dt:.2f}s)", dt < 2.0,
        f"dt={dt:.2f}s rc={rc}")


def test_hook_is_registered_in_hooks_json() -> None:
    _step("PreCompact: hook is wired in hooks.json + dispatcher")
    hooks_json = json.loads((ROOT / "hooks" / "hooks.json").read_text(
        encoding="utf-8"))
    pre_compact_entries = hooks_json.get("hooks", {}).get("PreCompact")
    _aT("hooks.json has a PreCompact entry",
        bool(pre_compact_entries), str(pre_compact_entries)[:200])
    # The dispatcher recognises the 'pre-compact' subcommand.
    dispatcher = (ROOT / "bin" / "dejavu-hook.py").read_text(
        encoding="utf-8")
    _aT("dispatcher maps 'pre-compact' → hooks/pre_compact.py",
        "\"pre-compact\":" in dispatcher
         and "pre_compact.py" in dispatcher,
        "see bin/dejavu-hook.py HOOK_TARGETS")


def main() -> int:
    print("v0.8.17 PreCompact hook tests")
    tests = [
        test_valid_payload_emits_recovery_systemMessage,
        test_empty_stdin_emits_noop_envelope,
        test_missing_session_id_emits_noop_envelope,
        test_malformed_json_emits_noop_envelope,
        test_hook_is_fast_enough_for_3s_timeout,
        test_hook_is_registered_in_hooks_json,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            _fail(f"{t.__name__} raised", repr(e))
    print()
    print("=" * 60)
    print(f"PASS: {len(PASS)}     FAIL: {len(FAIL)}")
    if FAIL:
        for n, d in FAIL:
            print(f"  ✗ {n}  {d[:120]}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
