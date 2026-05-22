"""Smoke test for slash command docs added in v0.8.4 -> v0.8.6.

For each newly added doc:
  1. Open the file.
  2. Regex-extract every `claude-dejavu <sub>` invocation in the body
     (skip explanatory `claude-dejavu <verb>` mentions that aren't
     real invocation lines -- we only match command/code fences and
     `Use the bash command` blocks).
  3. Assert the subcommand actually exists in the live CLI by running
     `python3 code/recall.py <sub> --help` and checking exit==0.

Intentionally minimal: this catches the "doc references a flag /
subcommand that doesn't exist" class of bug, which is the most common
documentation drift mode. Deeper fuzz (every flag of every command)
is deferred -- the manual verify-against-`--help` pass in the
authoring PR is the canonical guard for now.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMANDS_DIR = ROOT / "commands"
RECALL = ROOT / "code" / "recall.py"

NEW_DOCS = [
    "dejavu-what-shipped.md",
    "dejavu-memory.md",
    "dejavu-jit-recall-preview.md",
    "dejavu-expand.md",
]

# Top-level subcommand names that should resolve under `recall.py <sub>
# --help`. Extracted from the bodies of the new docs.
INVOCATION_RE = re.compile(r"\bclaude-dejavu\s+([a-z][a-z0-9-]*)", re.MULTILINE)

# Subcommands we expect to be valid CLI verbs. Sourced once from the
# argparse choices list at the top of `recall.py --help`.
VALID_SUBS: set[str] = set()


def _load_valid_subs() -> set[str]:
    r = subprocess.run(
        [sys.executable, str(RECALL), "--help"],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        return set()
    # The argparse-generated help has `{a,b,c,...}` as the choices block.
    m = re.search(r"\{([a-z0-9,\-_]+)\}", r.stdout)
    if not m:
        return set()
    return {s.strip() for s in m.group(1).split(",") if s.strip()}


def test_each_new_doc_has_valid_invocations() -> int:
    failures: list[str] = []
    global VALID_SUBS
    VALID_SUBS = _load_valid_subs()
    if not VALID_SUBS:
        failures.append("could not load CLI subcommand choices from recall.py --help")
        return 1

    for name in NEW_DOCS:
        path = COMMANDS_DIR / name
        if not path.exists():
            failures.append(f"missing doc: {path}")
            continue
        body = path.read_text()
        found = set(INVOCATION_RE.findall(body))
        if not found:
            failures.append(f"{name}: no `claude-dejavu <sub>` invocations found")
            continue
        for sub in sorted(found):
            if sub not in VALID_SUBS:
                failures.append(
                    f"{name}: references unknown subcommand `{sub}` "
                    f"(not in {sorted(VALID_SUBS)[:5]}...)"
                )

    if failures:
        for f in failures:
            print(f"  FAIL: {f}")
        return 1
    print(f"  OK: {len(NEW_DOCS)} new docs reference only valid subcommands "
          f"(checked against {len(VALID_SUBS)} CLI verbs)")
    return 0


def main() -> int:
    return test_each_new_doc_has_valid_invocations()


if __name__ == "__main__":
    sys.exit(main())
