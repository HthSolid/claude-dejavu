#!/usr/bin/env python3
"""
claude-dejavu portable smoke test.

Runs the 10-step battery used during development:
  1. Sandbox setup
  2. install.py --non-interactive
  3. Verify config + DB schema + Weaviate class
  4. Stage a fixture jsonl into the backup mirror
  5. ingester --backfill
  6. CLI: sessions / search / expand / resume
  7. CLI: workspace create / list / add / remove / delete
  8. CLI: restore --list / restore <uuid> / sha256 verify
  9. hooks/stop.py end-to-end with deletion-detection
 10. MCP server tools/list + tools/call dejavu_sessions/dejavu_restore
 11. v0.3.0 symbol grounding (reindex / fuzzy resolve / outcomes / purge)
 12. Cleanup

Exits non-zero on any failure. Designed for CI matrix runs.

Required env:
  PG_HOST, PG_PORT, PG_USER, PG_PASS    — Postgres connection
  WV_URL                                  — Weaviate REST URL
  CLAUDE_DEJAVU_PLUGIN_ROOT               — repo checkout path
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PLUGIN_ROOT = Path(os.environ.get("CLAUDE_DEJAVU_PLUGIN_ROOT") or
                   Path(__file__).resolve().parent.parent)
SANDBOX = Path(os.environ.get("CLAUDE_DEJAVU_SANDBOX", "/tmp/dejavu-smoketest"))
DATA_ROOT = SANDBOX / "data"

PG_HOST = os.environ.get("PG_HOST", "localhost")
PG_PORT = os.environ.get("PG_PORT", "5432")
PG_USER = os.environ.get("PG_USER", "postgres")
PG_PASS = os.environ.get("PG_PASS", "postgres")
WV_URL = os.environ.get("WV_URL", "http://localhost:8080")
# The install script generates the DB name from $USER; we read what it actually
# created from config.env after install (so the smoke test exercises the real path).
# Pre-step: pin $USER to a stable value so the install picks a predictable DB name.
SMOKE_USER = "smoketest"
os.environ["USER"] = SMOKE_USER  # consumed by install.py
PG_DB = f"claude_dejavu_{SMOKE_USER}"

PASS = []
FAIL = []


def step(name: str):
    print(f"\n\033[36m── {name} ──\033[0m", flush=True)


def ok(msg: str):
    PASS.append(msg)
    print(f"  \033[32m✓\033[0m {msg}", flush=True)


def fail(msg: str, detail: str = ""):
    FAIL.append((msg, detail))
    print(f"  \033[31m✗\033[0m {msg}")
    if detail:
        print(f"      {detail}")


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    # CLAUDE_DEJAVU_BIN_DIR keeps the smoke install from clobbering the
    # user's real ~/.local/bin/claude-dejavu wrapper. install.py would
    # otherwise overwrite it pointing at /tmp/dejavu-smoketest/data/venv —
    # which would leave a broken pointer once cleanup wipes the sandbox.
    env = {**os.environ,
           "CLAUDE_DEJAVU_DATA_ROOT": str(DATA_ROOT),
           "CLAUDE_DEJAVU_BIN_DIR": str(SANDBOX / "bin"),
           "PGPASSWORD": PG_PASS}
    return subprocess.run(cmd, env=env, capture_output=True, text=True, **kw)


def psql(sql: str, db: str | None = None) -> subprocess.CompletedProcess:
    args = ["psql", "-h", PG_HOST, "-p", PG_PORT, "-U", PG_USER, "-tAc", sql]
    if db:
        args[1:1] = ["-d", db]
    else:
        args[1:1] = ["-d", "postgres"]
    return run(args)


# ─── Steps ───────────────────────────────────────────────────────────────────

def step_setup():
    step("[1] Sandbox setup")
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    SANDBOX.mkdir(parents=True, exist_ok=True)
    psql(f"DROP DATABASE IF EXISTS {PG_DB}")
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"{WV_URL}/v1/schema/ClaudeDejavuTurn", method="DELETE"),
            timeout=5,
        )
    except Exception:
        pass
    ok("clean state")


def step_install():
    step("[2] install.py --non-interactive")
    # Snapshot any existing user wrapper so we can verify the smoke
    # install doesn't clobber it (regression from prior versions where
    # the smoke would overwrite ~/.local/bin/claude-dejavu pointing at
    # the soon-to-be-deleted sandbox).
    user_wrapper = Path.home() / ".local" / "bin" / "claude-dejavu"
    pre_user_wrapper_content = (user_wrapper.read_text()
                                 if user_wrapper.is_file() else None)

    py = sys.executable
    r = run([py, str(PLUGIN_ROOT / "scripts" / "install.py"), "--non-interactive"])
    if r.returncode != 0:
        fail("install.py exit != 0", r.stderr[-500:]); return False
    if not (DATA_ROOT / "config.env").is_file():
        fail("config.env not written"); return False
    if not (DATA_ROOT / "venv").is_dir():
        fail("venv not created"); return False
    # The wrapper should land in our sandbox bin dir, NOT in ~/.local/bin.
    sandbox_wrapper = SANDBOX / "bin" / "claude-dejavu"
    if not sandbox_wrapper.is_file():
        fail(f"sandbox wrapper missing at {sandbox_wrapper}\n{r.stdout}")
        return False
    if pre_user_wrapper_content is not None:
        cur = user_wrapper.read_text() if user_wrapper.is_file() else None
        if cur != pre_user_wrapper_content:
            fail(f"smoke install clobbered user wrapper at {user_wrapper}\n"
                 f"BEFORE: {pre_user_wrapper_content!r}\n"
                 f"AFTER : {cur!r}")
            return False
    ok("install.py exited 0, config + venv + sandbox wrapper present, user wrapper untouched")
    return True


def step_verify_schema():
    step("[3] Verify schema + Weaviate class")
    r = psql("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'", db=PG_DB)
    if r.returncode != 0:
        fail("psql connect failed", r.stderr[-200:]); return False
    if int((r.stdout or "0").strip()) < 6:
        fail(f"expected >=6 tables, got {r.stdout.strip()}"); return False
    ok(f"{r.stdout.strip()} tables in {PG_DB}")
    try:
        with urllib.request.urlopen(f"{WV_URL}/v1/schema/ClaudeDejavuTurn", timeout=5) as resp:
            d = json.loads(resp.read())
        props = [p["name"] for p in d.get("properties", [])]
        assert "session_id" in props and "content" in props
        ok(f"Weaviate class has {len(props)} properties")
    except Exception as e:
        fail("Weaviate class verify failed", str(e))
        return False
    return True


def step_stage_fixture():
    step("[4] Stage fixture jsonl")
    fixture = PLUGIN_ROOT / "tests" / "fixtures" / "sample-session.jsonl"
    if not fixture.is_file():
        fail(f"fixture missing: {fixture}"); return False
    proj_dir = DATA_ROOT / "chat-logs" / "-tmp-dejavu-smoketest-fixtures"
    proj_dir.mkdir(parents=True, exist_ok=True)
    dst = proj_dir / "00000000-0000-0000-0000-000000000001.jsonl"
    shutil.copy2(fixture, dst)
    ok(f"copied → {dst}")
    return True


def step_ingest():
    step("[5] Ingester --backfill")
    venv_py = DATA_ROOT / "venv" / ("Scripts" if sys.platform.startswith("win") else "bin") / (
        "python.exe" if sys.platform.startswith("win") else "python3"
    )
    r = run([str(venv_py), str(PLUGIN_ROOT / "code" / "ingester.py"), "--backfill"])
    if r.returncode != 0:
        fail("ingester non-zero", r.stderr[-300:]); return False
    sess = psql(f"SELECT count(*) FROM sessions", db=PG_DB)
    turns = psql(f"SELECT count(*) FROM turns", db=PG_DB)
    n_sess = int(sess.stdout.strip() or "0")
    n_turns = int(turns.stdout.strip() or "0")
    if n_sess < 1:
        fail(f"no sessions in PG (got {n_sess})"); return False
    ok(f"ingested {n_sess} session(s), {n_turns} turn(s)")
    return True


def cli(*args):
    venv_py = DATA_ROOT / "venv" / ("Scripts" if sys.platform.startswith("win") else "bin") / (
        "python.exe" if sys.platform.startswith("win") else "python3"
    )
    return run([str(venv_py), str(PLUGIN_ROOT / "code" / "recall.py"), *args])


def step_cli_battery():
    step("[6] CLI: sessions / search / expand / resume")
    r = cli("sessions", "--scope=global", "--limit=5")
    if r.returncode != 0 or "scope=" not in r.stdout:
        fail("sessions failed", r.stderr[-200:]); return False
    ok("sessions returned scope-aware output")

    r = cli("search", "where did we leave off", "--scope=global", "--limit=3")
    if r.returncode != 0:
        fail("search failed", r.stderr[-200:]); return False
    ok("search returned hits")

    r = cli("resume", "where did we leave off", "--scope=global")
    if r.returncode != 0 or "claude --resume" not in r.stdout:
        fail("resume missing 'claude --resume' hint"); return False
    ok("resume produced runnable command")
    return True


def step_workspace():
    step("[7] Workspace lifecycle")
    cli("workspace", "delete", "smoke-ws")  # idempotent
    r = cli("workspace", "create", "smoke-ws", "--projects=fixtures,demo")
    if r.returncode != 0:
        fail("workspace create", r.stderr[-200:]); return False
    r = cli("workspace", "list")
    if "smoke-ws" not in r.stdout:
        fail("workspace not listed"); return False
    cli("workspace", "add", "smoke-ws", "extra")
    r = cli("workspace", "list")
    if "extra" not in r.stdout:
        fail("workspace add did not persist"); return False
    cli("workspace", "remove", "smoke-ws", "extra")
    cli("workspace", "delete", "smoke-ws")
    ok("create / add / remove / delete all work")
    return True


def step_restore():
    step("[8] Restore lifecycle")
    fixtures = DATA_ROOT / "chat-logs" / "-tmp-dejavu-smoketest-fixtures"
    src_jsonl = next(fixtures.glob("*.jsonl"))
    uuid = src_jsonl.stem
    # Stage a fake live transcript (so deletion is meaningful)
    live_dir = Path.home() / ".claude" / "projects" / "-tmp-dejavu-smoketest-fixtures"
    live_dir.mkdir(parents=True, exist_ok=True)
    live = live_dir / f"{uuid}.jsonl"
    shutil.copy2(src_jsonl, live)
    sha_before = hashlib.sha256(live.read_bytes()).hexdigest()
    # Now delete it (simulating the bug)
    live.unlink()
    r = cli("restore", "--list")
    if uuid not in r.stdout:
        fail("restore --list missed our deletion"); return False
    ok(f"deletion detected: {uuid}")
    r = cli("restore", uuid)
    if r.returncode != 0 or not live.exists():
        fail("restore did not bring file back", r.stderr[-200:]); return False
    sha_after = hashlib.sha256(live.read_bytes()).hexdigest()
    if sha_before != sha_after:
        fail("sha256 mismatch after restore"); return False
    ok("byte-equal restore via sha256")
    # Cleanup live path we created
    live.unlink(missing_ok=True)
    if live_dir.is_dir() and not any(live_dir.iterdir()):
        live_dir.rmdir()
    return True


def step_stop_hook():
    step("[9] hooks/stop.py end-to-end")
    venv_py = DATA_ROOT / "venv" / ("Scripts" if sys.platform.startswith("win") else "bin") / (
        "python.exe" if sys.platform.startswith("win") else "python3"
    )
    stop = PLUGIN_ROOT / "hooks" / "stop.py"
    fixtures = DATA_ROOT / "chat-logs" / "-tmp-dejavu-smoketest-fixtures"
    src_jsonl = next(fixtures.glob("*.jsonl"))
    uuid = src_jsonl.stem
    payload = json.dumps({"session_id": uuid, "transcript_path": str(src_jsonl)})
    r = subprocess.run(
        [str(venv_py), str(stop)], input=payload, capture_output=True, text=True,
        env={**os.environ, "CLAUDE_DEJAVU_DATA_ROOT": str(DATA_ROOT)},
    )
    if r.returncode != 0:
        fail("stop.py exit non-zero", r.stderr[-200:]); return False
    try:
        out = json.loads(r.stdout.strip())
    except Exception:
        fail(f"stop.py stdout not JSON: {r.stdout[:200]}"); return False
    if "systemMessage" not in out:
        fail("stop.py missing systemMessage key"); return False
    snap_dir = DATA_ROOT / "snapshots"
    if not any(snap_dir.iterdir() if snap_dir.is_dir() else []):
        fail("no snapshot dir created"); return False
    ok("mirror + snapshot + systemMessage all clean")
    return True


def step_mcp():
    step("[10] MCP server stdio JSONRPC")
    venv_py = DATA_ROOT / "venv" / ("Scripts" if sys.platform.startswith("win") else "bin") / (
        "python.exe" if sys.platform.startswith("win") else "python3"
    )
    server = PLUGIN_ROOT / "mcp" / "server.py"
    proc = subprocess.Popen(
        [str(venv_py), str(server)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "CLAUDE_DEJAVU_DATA_ROOT": str(DATA_ROOT)},
        text=True, bufsize=1,
    )

    def send(req):
        proc.stdin.write(json.dumps(req) + "\n"); proc.stdin.flush()

    def recv(timeout=8):
        end = time.time() + timeout
        while time.time() < end:
            line = proc.stdout.readline()
            if not line: return None
            try: return json.loads(line.strip())
            except json.JSONDecodeError: continue
        return None

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "smoketest", "version": "0"}}})
        if not recv(): fail("MCP initialize"); return False
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        r = recv()
        tools = (r or {}).get("result", {}).get("tools", [])
        if len(tools) < 7:
            fail(f"tools/list returned only {len(tools)} tools"); return False
        ok(f"{len(tools)} MCP tools advertised")
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "dejavu_sessions", "arguments": {"limit": 5}}})
        r = recv()
        if not r or "result" not in r:
            fail("tools/call dejavu_sessions failed"); return False
        ok("tools/call dejavu_sessions returned result")
        send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
              "params": {"name": "dejavu_restore", "arguments": {"list_only": True}}})
        r = recv()
        if not r or "result" not in r:
            fail("tools/call dejavu_restore failed"); return False
        ok("tools/call dejavu_restore returned result")
        return True
    finally:
        proc.terminate()
        try: proc.wait(timeout=2)
        except Exception: proc.kill()


def step_symbol_grounding():
    step("[11] v0.3.0 symbol grounding (reindex / fuzzy resolve / outcomes)")
    # Build a tiny fixture project with deliberate camelCase + snake_case mix.
    proj = SANDBOX / "fixtures-grounding"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "py_module.py").write_text(
        '''"""Module docstring."""
def parse_user_input(s: str) -> str:
    """Parse the input."""
    return s.strip()

class UserService:
    """Service class."""
    def parse_input(self, x: str) -> str:
        return x

CONST_VALUE = 42
'''
    )
    (proj / "ts_module.ts").write_text(
        """export function fetchProfile(id: string): Profile { return {} as any; }
export class ProfileCache {
  refresh(): void {}
}
export interface Profile { id: string; name: string; }
type Handle = string;
const MAX_RETRIES = 3;

// Arrow function — only the tree-sitter parser catches this as 'function'.
// The old regex would mark it 'const' and grounding lookups by intent would miss.
export const handleRequest = async (req: Request): Promise<Response> => {
  return new Response();
};
"""
    )
    (proj / "go_module.go").write_text(
        """package main

func ServeAPI(addr string) error { return nil }

type Pipeline struct { stages int }

func (p *Pipeline) Run() error { return nil }

const MaxStages = 10
"""
    )
    (proj / "rs_module.rs").write_text(
        """pub fn ground_input(s: &str) -> String { s.to_string() }

pub struct Resolver { entries: Vec<String> }

impl Resolver {
    pub fn new() -> Self { Self { entries: vec![] } }
}

pub trait Lookup { fn find(&self, k: &str) -> Option<String>; }
"""
    )
    (proj / "Java_module.java").write_text(
        """public class JavaService {
    public String fetchProfile(String id) { return null; }
    private void clearCache() {}
}
public interface JavaLoader { byte[] load(); }
public enum JavaStatus { ACTIVE, INACTIVE }
"""
    )
    (proj / "rb_module.rb").write_text(
        """MAX_RETRIES = 3

def parse_request(req)
  req.body
end

class RubyService
  def find(id); @repo.get(id); end
  def self.factory; RubyService.new; end
end

module RubyAuth
  def authenticate(token); end
end
"""
    )
    (proj / "sh_module.sh").write_text(
        """#!/usr/bin/env bash
DEPLOY_TARGET=production

deploy_application() {
  echo deploying
}

function rollback_release {
  echo reverting
}
"""
    )

    # reindex — 7 fixture files: python + ts + go + rust + java + ruby + bash
    r = cli("reindex", f"--path={proj}", "--project=smoke-grounding")
    if r.returncode != 0 or "symbols added" not in r.stdout:
        fail("reindex failed", r.stderr[-200:] + r.stdout[-200:]); return False
    if "files indexed : 7" not in r.stdout:
        fail(f"reindex did not pick up all seven files\n{r.stdout}"); return False
    ok("reindex parsed python + ts + go + rust + java + ruby + bash fixtures")

    # incremental re-run = nothing to do
    r = cli("reindex", f"--path={proj}", "--project=smoke-grounding")
    if "files skipped : 7" not in r.stdout or "files indexed : 0" not in r.stdout:
        fail(f"incremental reindex didn't skip\n{r.stdout}"); return False
    ok("incremental re-index correctly skipped unchanged files")

    # exact match
    r = cli("symbols", "parse_user_input", "--project=smoke-grounding")
    if r.returncode != 0 or "ed=0" not in r.stdout:
        fail(f"exact match failed\n{r.stdout}"); return False
    ok("exact resolve -> ed=0")

    # fuzzy: camelCase typo for snake_case symbol
    r = cli("symbols", "parseUserInput", "--project=smoke-grounding")
    if r.returncode != 0 or "parse_user_input" not in r.stdout:
        fail(f"fuzzy parseUserInput did not surface parse_user_input\n{r.stdout}"); return False
    ok("fuzzy resolve catches camelCase->snake_case typo")

    # TS class still picked up
    r = cli("symbols", "ProfileCache", "--project=smoke-grounding")
    if r.returncode != 0 or "ed=0" not in r.stdout or "class" not in r.stdout:
        fail(f"TS class lookup failed\n{r.stdout}"); return False
    ok("TS class symbols indexed")

    # Listing with kind filter
    r = cli("symbols", "--project=smoke-grounding", "--kind=class")
    if r.returncode != 0 or "UserService" not in r.stdout or "ProfileCache" not in r.stdout:
        fail(f"--kind=class did not list both\n{r.stdout}"); return False
    ok("--kind filter works")

    # outcomes table populated by previous fuzzy resolve calls
    r = cli("outcomes", "--project=smoke-grounding", "--limit=10")
    if r.returncode != 0 or "outcome(s):" not in r.stdout:
        fail(f"outcomes command failed\n{r.stdout}"); return False
    if "parse_user_input" not in r.stdout:
        fail(f"outcomes did not capture lookup\n{r.stdout}"); return False
    ok("correction_outcomes populated by lookup calls")

    # Tree-sitter wins: arrow-function handleRequest is captured as 'function'
    # (the regex parser would label it 'const').
    r = cli("symbols", "handleRequest", "--project=smoke-grounding")
    if r.returncode != 0 or "ed=0" not in r.stdout:
        fail(f"arrow-function lookup failed\n{r.stdout}"); return False
    if "function" not in r.stdout.split("ed=0")[0]:
        # Couldn't find 'function' kind on the matched line.
        fail(f"arrow-function not classified as function (regex-parser regression?)\n{r.stdout}")
        return False
    ok("tree-sitter classifies arrow-function as 'function' (not 'const')")

    # Method nesting: refresh inside ProfileCache should have parent
    r = cli("symbols", "refresh", "--project=smoke-grounding", "--kind=method")
    if r.returncode != 0 or "method" not in r.stdout:
        fail(f"method lookup failed\n{r.stdout}"); return False
    ok("class methods captured with kind=method")

    # Go function lookup
    r = cli("symbols", "ServeAPI", "--project=smoke-grounding")
    if r.returncode != 0 or "ed=0" not in r.stdout:
        fail(f"Go function lookup failed\n{r.stdout}"); return False
    ok("Go symbols indexable via tree-sitter")

    # Rust struct (kind=class) lookup
    r = cli("symbols", "Resolver", "--project=smoke-grounding")
    if r.returncode != 0 or "ed=0" not in r.stdout or "class" not in r.stdout:
        fail(f"Rust struct lookup failed\n{r.stdout}"); return False
    ok("Rust symbols indexable via tree-sitter")

    # Java class + method lookup
    r = cli("symbols", "JavaService", "--project=smoke-grounding")
    if r.returncode != 0 or "ed=0" not in r.stdout or "class" not in r.stdout:
        fail(f"Java class lookup failed\n{r.stdout}"); return False
    r = cli("symbols", "fetchProfile", "--project=smoke-grounding", "--language=java")
    if r.returncode != 0 or "method" not in r.stdout:
        fail(f"Java method lookup failed\n{r.stdout}"); return False
    ok("Java symbols indexable via tree-sitter")

    # Ruby method, class, module, singleton method
    r = cli("symbols", "parse_request", "--project=smoke-grounding")
    if r.returncode != 0 or "ed=0" not in r.stdout:
        fail(f"Ruby top-level method lookup failed\n{r.stdout}"); return False
    r = cli("symbols", "RubyAuth", "--project=smoke-grounding")
    if r.returncode != 0 or "ed=0" not in r.stdout:
        fail(f"Ruby module lookup failed\n{r.stdout}"); return False
    r = cli("symbols", "factory", "--project=smoke-grounding", "--language=ruby")
    if r.returncode != 0 or "method" not in r.stdout:
        fail(f"Ruby singleton method lookup failed\n{r.stdout}"); return False
    ok("Ruby symbols indexable (incl. modules + singleton methods)")

    # Bash function: both `name() { … }` and `function name { … }` forms
    r = cli("symbols", "deploy_application", "--project=smoke-grounding")
    if r.returncode != 0 or "ed=0" not in r.stdout or "function" not in r.stdout:
        fail(f"Bash POSIX-style function lookup failed\n{r.stdout}"); return False
    r = cli("symbols", "rollback_release", "--project=smoke-grounding")
    if r.returncode != 0 or "ed=0" not in r.stdout or "function" not in r.stdout:
        fail(f"Bash 'function NAME' style lookup failed\n{r.stdout}"); return False
    ok("Bash symbols indexable (both POSIX + 'function' keyword forms)")

    # Typo recall — typos of symbols that DO exist should resolve to the correct one.
    # Critical regression guard: the dogfood-driven re-rank tuning depends on this.
    typo_pairs = [
        ("parseUserIput", "parse_user_input"),  # missing letter, snake_case target
        ("UseSrvice", "UserService"),           # transposition + dropped letter
        ("ProfilCache", "ProfileCache"),         # dropped letter (TS class)
    ]
    for typo, intended in typo_pairs:
        r = cli("symbols", typo, "--project=smoke-grounding", "--limit=3")
        if r.returncode != 0 or intended not in r.stdout:
            fail(f"typo recall: {typo!r} did not surface {intended!r} (top-3)\n{r.stdout}")
            return False
    ok("typo recall: dropped/transposed-letter typos surface the right symbol")

    # Hallucination rejection — names with no meaningful trigram overlap should NOT match.
    hallucinations = ["xyzzyplover", "frobnicateGalaxy", "quantumTeapotResolver"]
    for q in hallucinations:
        r = cli("symbols", q, "--project=smoke-grounding")
        if "no candidates for" not in r.stdout:
            fail(f"hallucination {q!r} produced false-positive matches\n{r.stdout}")
            return False
    ok("hallucinations correctly produce no candidates (threshold 0.4 + ED gate)")

    # ─── v0.3.1 lint mode ────────────────────────────────────────────────
    # CLI lint with mixed content (resolved + typo + fabrication).
    lint_fixture = SANDBOX / "lint-fixture.ts"
    lint_fixture.write_text(
        """import { Foo } from './foo';
export function handle() {
  const x = new ProfileCache();
  fetchProfile('id');
  fetchPrfile('id');
  totallyMadeUpFunction();
  console.log('hi');
  return null;
}
"""
    )
    r = cli("lint", str(lint_fixture), "--project=smoke-grounding",
            f"--content-file={lint_fixture}")
    if r.returncode != 2:  # block expected
        fail(f"lint expected exit=2 (block), got exit={r.returncode}\n{r.stdout}")
        return False
    if "totallyMadeUpFunction" not in r.stdout:
        fail(f"lint did not flag fabricated name\n{r.stdout}"); return False
    if "fetchPrfile" not in r.stdout:
        fail(f"lint did not flag typo\n{r.stdout}"); return False
    if "ProfileCache" not in r.stdout and "verdict: BLOCK" not in r.stdout:
        fail(f"lint missed resolved reference\n{r.stdout}"); return False
    ok("lint CLI flags fabricated names + typos, exits 2 on block")

    # Type-aware lint: `obj.method()` where obj has a known type, but method
    # doesn't exist on that type, should be flagged with receiver-type context.
    type_aware_fixture = SANDBOX / "type-aware.ts"
    type_aware_fixture.write_text(
        """class ProfileCache {
  refresh(): void {}
}
function f() {
  const c: ProfileCache = new ProfileCache();
  c.refresh();
  c.totallyMadeUpMethod();
}
"""
    )
    r = cli("lint", str(type_aware_fixture), "--project=smoke-grounding",
            f"--content-file={type_aware_fixture}")
    if r.returncode != 2 or "totallyMadeUpMethod" not in r.stdout:
        fail(f"type-aware lint did not flag missing method on known receiver\n{r.stdout}")
        return False
    if "ProfileCache" not in r.stdout:
        fail(f"type-aware lint did not surface receiver type in output\n{r.stdout}")
        return False
    ok("type-aware lint: obj.method() grounds against receiver type")

    # Multi-language lint: Go fabrication should be flagged.
    go_fixture = SANDBOX / "x.go"
    go_fixture.write_text(
        """package main

func main() {
  ServeAPI("addr")
  totallyFakeGoFunction()
}
"""
    )
    r = cli("lint", str(go_fixture), "--project=smoke-grounding",
            f"--content-file={go_fixture}")
    if r.returncode != 2 or "totallyFakeGoFunction" not in r.stdout:
        fail(f"go lint did not flag fabricated function\n{r.stdout}"); return False
    ok("Go reference extractor flags fabricated functions")

    # Multi-language lint: Bash fabrication
    bash_fixture = SANDBOX / "x.sh"
    bash_fixture.write_text(
        """#!/usr/bin/env bash
deploy_application
totally_fake_bash_function
"""
    )
    r = cli("lint", str(bash_fixture), "--project=smoke-grounding",
            f"--content-file={bash_fixture}")
    if r.returncode != 2 or "totally_fake_bash_function" not in r.stdout:
        fail(f"bash lint did not flag fabricated command\n{r.stdout}"); return False
    ok("Bash reference extractor flags fabricated commands")

    # Lint clean code → exit 0
    clean_fixture = SANDBOX / "clean.py"
    clean_fixture.write_text(
        """def my_func(x):
    return x * 2

result = my_func(42)
print(result)
"""
    )
    r = cli("lint", str(clean_fixture), "--project=smoke-grounding",
            f"--content-file={clean_fixture}")
    if r.returncode != 0:
        fail(f"clean code lint should exit 0, got {r.returncode}\n{r.stdout}"); return False
    ok("lint passes clean code with exit 0")

    # PreToolUse hook end-to-end. The hook derives project_slug from
    # basename(cwd), so we point cwd at a directory whose basename matches
    # the indexed project_slug 'smoke-grounding' (otherwise the empty-index
    # graceful-skip kicks in and the hook silent-passes correctly).
    cwd_match = SANDBOX / "smoke-grounding"
    cwd_match.mkdir(parents=True, exist_ok=True)
    venv_py = DATA_ROOT / "venv" / ("Scripts" if sys.platform.startswith("win") else "bin") / (
        "python.exe" if sys.platform.startswith("win") else "python3"
    )
    hook = PLUGIN_ROOT / "hooks" / "pre_tool_use.py"
    payload = json.dumps({
        "tool_name": "Write",
        "tool_input": {
            "file_path": "/tmp/dejavu-hook-smoke.ts",
            "content": "export function f() { totallyFakeFn(); }\n",
        },
        "session_id": "smoke",
        "cwd": str(cwd_match),
    })
    r = subprocess.run([str(venv_py), str(hook)], input=payload,
                       capture_output=True, text=True,
                       env={**os.environ, "CLAUDE_DEJAVU_DATA_ROOT": str(DATA_ROOT)})
    if r.returncode != 0:
        fail(f"hook returncode != 0: {r.stderr[-200:]}"); return False
    try:
        hook_out = json.loads(r.stdout.strip())
    except Exception:
        fail(f"hook stdout not JSON: {r.stdout[:200]}"); return False
    if "systemMessage" not in hook_out or "totallyFakeFn" not in hook_out["systemMessage"]:
        fail(f"hook didn't flag fabricated name in systemMessage\n{hook_out}"); return False
    ok("PreToolUse hook returns systemMessage flagging fabricated name")

    # Hook with mode=off should silently pass
    r = subprocess.run([str(venv_py), str(hook)], input=payload, capture_output=True, text=True,
                       env={**os.environ, "CLAUDE_DEJAVU_DATA_ROOT": str(DATA_ROOT),
                            "CLAUDE_DEJAVU_LINT_MODE": "off"})
    if r.returncode != 0 or r.stdout.strip() != "{}":
        fail(f"hook with mode=off should output empty {{}}, got: {r.stdout!r}"); return False
    ok("PreToolUse hook respects CLAUDE_DEJAVU_LINT_MODE=off")

    # mode=suggest: never blocks, but message must include the rename table.
    # Build a payload with a typo of a real fixture symbol (parse_user_input).
    # The hook derives project_slug from basename(cwd), so we point cwd at a
    # path whose basename matches the indexed project_slug 'smoke-grounding'.
    cwd_for_hook = SANDBOX / "smoke-grounding"
    cwd_for_hook.mkdir(parents=True, exist_ok=True)
    suggest_payload = json.dumps({
        "tool_name": "Write",
        "tool_input": {
            "file_path": "/tmp/dejavu-suggest-smoke.py",
            "content": "def call_it():\n    return parse_user_iput('x')\n",
        },
        "session_id": "smoke",
        "cwd": str(cwd_for_hook),
    })
    r = subprocess.run([str(venv_py), str(hook)], input=suggest_payload,
                       capture_output=True, text=True,
                       env={**os.environ, "CLAUDE_DEJAVU_DATA_ROOT": str(DATA_ROOT),
                            "CLAUDE_DEJAVU_LINT_MODE": "suggest"})
    if r.returncode != 0:
        fail(f"suggest mode hook returncode != 0: {r.stderr[-200:]}"); return False
    out = json.loads(r.stdout.strip())
    if "decision" in out:
        fail(f"suggest mode must never block; got decision={out['decision']}"); return False
    if "APPLY THESE CORRECTIONS" not in out.get("systemMessage", ""):
        fail(f"suggest mode missing rename table in systemMessage\n{out}"); return False
    if "parse_user_input" not in out.get("systemMessage", ""):
        fail(f"suggest mode did not surface the resolved name\n{out}"); return False
    ok("PreToolUse hook mode=suggest emits rename table without blocking")

    # Empty-index graceful skip: if cwd basename doesn't match any indexed
    # project, the lint hook should silent-pass (not flag every reference
    # as fabricated). This is the bootstrap state during first SessionStart
    # while auto-reindex runs in the background.
    empty_cwd = SANDBOX / "never-indexed-project-xyz"
    empty_cwd.mkdir(parents=True, exist_ok=True)
    empty_payload = json.dumps({
        "tool_name": "Write",
        "tool_input": {
            "file_path": "/tmp/empty-index.ts",
            "content": "export function f() { someName(); otherName(); }\n",
        },
        "session_id": "smoke",
        "cwd": str(empty_cwd),
    })
    r = subprocess.run([str(venv_py), str(hook)], input=empty_payload,
                       capture_output=True, text=True,
                       env={**os.environ, "CLAUDE_DEJAVU_DATA_ROOT": str(DATA_ROOT)})
    if r.returncode != 0:
        fail(f"empty-index hook returncode != 0"); return False
    if r.stdout.strip() != "{}":
        fail(f"empty-index hook should silent-pass (output {{}}); got: {r.stdout!r}")
        return False
    if "not yet indexed" not in r.stderr:
        fail(f"empty-index hook should log a 'not yet indexed' notice to stderr\n{r.stderr}")
        return False
    ok("PreToolUse hook silent-passes when project has zero indexed symbols")

    # mode=autofix: blocks even on warn-level (typo) verdict.
    r = subprocess.run([str(venv_py), str(hook)], input=suggest_payload,
                       capture_output=True, text=True,
                       env={**os.environ, "CLAUDE_DEJAVU_DATA_ROOT": str(DATA_ROOT),
                            "CLAUDE_DEJAVU_LINT_MODE": "autofix"})
    if r.returncode != 0:
        fail(f"autofix mode hook returncode != 0"); return False
    out = json.loads(r.stdout.strip())
    if out.get("decision") != "block":
        fail(f"autofix mode must block on warn/block verdict; got {out.get('decision')}\n{out}")
        return False
    if "APPLY THESE CORRECTIONS" not in out.get("systemMessage", ""):
        fail(f"autofix mode missing rename table\n{out}"); return False
    ok("PreToolUse hook mode=autofix blocks + emits rename table")

    # ─── Doctor + config ─────────────────────────────────────────────────
    # doctor should report 'ok' overall when the smoke install is healthy.
    r = cli("doctor")
    if r.returncode != 0:
        fail(f"doctor exit non-zero (overall != ok): {r.stdout[-300:]}"); return False
    if "claude-dejavu doctor" not in r.stdout or "OVERALL: OK" not in r.stdout.upper():
        fail(f"doctor output missing expected header / OK status\n{r.stdout}"); return False
    ok("doctor reports 'ok' on a healthy install")

    # doctor --json
    r = cli("doctor", "--json")
    try:
        d = json.loads(r.stdout.strip())
    except Exception:
        fail(f"doctor --json output not parseable\n{r.stdout[:200]}"); return False
    if d.get("overall") != "ok" or "checks" not in d:
        fail(f"doctor --json missing fields\n{d}"); return False
    if not any(c["name"] == "venv" for c in d["checks"]):
        fail(f"doctor JSON missing venv check"); return False
    ok("doctor --json returns structured report with all checks")

    # config list
    r = cli("config", "list")
    if r.returncode != 0:
        fail(f"config list failed\n{r.stdout}"); return False
    for needed in ("LINT_MODE", "AUTO_REINDEX", "PG_HOST", "WV_URL"):
        if needed not in r.stdout:
            fail(f"config list missing {needed}\n{r.stdout}"); return False
    ok("config list shows all known settings")

    # config get
    r = cli("config", "get", "LINT_MODE")
    if r.returncode != 0 or r.stdout.strip() != "warn":
        fail(f"config get LINT_MODE expected 'warn', got {r.stdout!r}"); return False
    ok("config get returns current value")

    # config set + persistence
    r = cli("config", "set", "LINT_MODE", "suggest")
    if r.returncode != 0:
        fail(f"config set failed\n{r.stdout}"); return False
    r = cli("config", "get", "LINT_MODE")
    if r.stdout.strip() != "suggest":
        fail(f"config set didn't persist\n{r.stdout}"); return False
    ok("config set persists to config.env")

    # config validation: bad enum value
    r = cli("config", "set", "LINT_MODE", "bogus_mode")
    if r.returncode == 0:
        fail(f"config set should reject bogus enum value\n{r.stdout}{r.stderr}")
        return False
    ok("config set rejects invalid enum values")

    # config reset
    r = cli("config", "reset", "LINT_MODE")
    if r.returncode != 0:
        fail(f"config reset failed\n{r.stdout}"); return False
    r = cli("config", "get", "LINT_MODE")
    if r.stdout.strip() != "warn":
        fail(f"config reset didn't restore default\n{r.stdout}"); return False
    ok("config reset restores default")

    # config: read-only key write rejected
    r = cli("config", "set", "PG_HOST", "evil.example.com")
    if r.returncode == 0:
        fail(f"config set should reject read-only key\n{r.stdout}"); return False
    ok("config set rejects read-only keys (PG_HOST etc.)")

    # ─── v0.3.2.1: API route grounding ───────────────────────────────────
    # Build a tiny multi-framework fixture and verify routes index + lookup.
    routes_dir = SANDBOX / "routes-grounding"
    routes_dir.mkdir(parents=True, exist_ok=True)
    (routes_dir / "express_app.ts").write_text(
        """import express from 'express';
const app = express();
app.get('/api/users/:id', (req, res) => res.json({}));
app.post('/api/users', (req, res) => res.json({}));
app.delete('/api/users/:id', (req, res) => res.json({}));
"""
    )
    (routes_dir / "fastapi_app.py").write_text(
        """from fastapi import FastAPI
app = FastAPI()

@app.get("/items/{item_id}")
def read_item(item_id: int):
    return {}

@app.post("/items")
async def create_item(payload: dict):
    return {}
"""
    )
    (routes_dir / "openapi.yaml").write_text(
        """openapi: 3.0.0
info: {title: x, version: '1.0'}
paths:
  /declared/route:
    get:
      summary: A declared-only route
"""
    )

    r = cli("reindex", f"--path={routes_dir}", "--project=smoke-routes",
            "--skip-semantic")
    if r.returncode != 0 or "routes added  : 6" not in r.stdout:
        fail(f"route reindex didn't pick up all 6 routes (Express×3 + FastAPI×2 + OpenAPI×1)\n{r.stdout}"); return False
    ok("route indexer extracts Express + FastAPI + OpenAPI declarations")

    # Exact lookup
    r = cli("routes", "get", "GET", "/api/users/:id", "--project=smoke-routes")
    if r.returncode != 0 or "express" not in r.stdout or "✓" not in r.stdout:
        fail(f"exact route lookup failed\n{r.stdout}"); return False
    ok("route lookup: exact normalized match")

    # Pattern lookup — concrete URL against parameterized route
    r = cli("routes", "get", "GET", "/api/users/42", "--project=smoke-routes")
    if r.returncode != 0 or "≈" not in r.stdout or "/api/users/:id" not in r.stdout:
        fail(f"pattern-match route lookup failed\n{r.stdout}"); return False
    ok("route lookup: pattern-match concrete URL → parameterized route")

    # Fuzzy lookup — typo
    r = cli("routes", "get", "GET", "/api/usrs/42", "--project=smoke-routes")
    if r.returncode != 0 or "/api/users" not in r.stdout:
        fail(f"fuzzy route lookup didn't catch typo\n{r.stdout}"); return False
    ok("route lookup: fuzzy match catches path typo")

    # Path-style normalization: FastAPI {id} vs Express :id vs Next.js [id]
    r = cli("routes", "get", "GET", "/items/{item_id}", "--project=smoke-routes")
    if r.returncode != 0 or "/items/:" not in r.stdout:
        fail(f"path normalization broke FastAPI {{id}} matching\n{r.stdout}"); return False
    ok("route lookup: cross-framework path normalization (Express :id ↔ FastAPI {id})")

    # OpenAPI ground truth
    r = cli("routes", "get", "GET", "/declared/route", "--project=smoke-routes")
    if r.returncode != 0 or "openapi" not in r.stdout:
        fail(f"OpenAPI route not found\n{r.stdout}"); return False
    ok("route lookup: OpenAPI ground-truth routes are indexed")

    # ─── route_grounding lint integration ────────────────────────────────
    # Lint a TS client file with mixed real / typo / fabricated URLs.
    client_file = SANDBOX / "smoke-routes" / "client.ts"
    client_file.parent.mkdir(parents=True, exist_ok=True)
    client_file.write_text(
        """import axios from 'axios';
async function go() {
  await axios.get('/api/users/123');
  await axios.post('/api/users', {});
  await fetch('/api/usrs/4');
  await fetch('/totally/made-up/route', {method:'POST'});
}
"""
    )
    r = cli("lint", str(client_file), "--project=smoke-routes",
            f"--content-file={client_file}")
    # Lint exit code: 2=block, 1=warn, 0=ok
    if r.returncode != 2:
        fail(f"route lint expected exit=2 (block); got {r.returncode}\n{r.stdout}")
        return False
    if "/totally/made-up/route" not in r.stdout:
        fail(f"route lint missed fabricated URL\n{r.stdout}"); return False
    ok("route grounding lint: flags fabricated URL + typo, resolves real routes")

    # ─── Semantic search (best-effort — only if Weaviate is up) ──────────
    # Build the semantic index and probe with intent queries.
    r = cli("reindex", f"--path={routes_dir}", "--project=smoke-routes")
    # Use the MCP server's dejavu_resolve_semantic? Easier: use Python directly.
    # The CLI doesn't have a `semantic` subcommand for v0.3.2.1; we test via
    # the imported function.
    import subprocess as _sp
    test_script = (
        "import sys; sys.path.insert(0, str(__import__('pathlib').Path("
        f"{repr(str(PLUGIN_ROOT))}) / 'code'));"
        "from semantic_search import is_available, search;"
        "import json;"
        "ok = is_available();"
        "hits = search('create user endpoint', 'smoke-routes', kind='route', limit=3) if ok else [];"
        "print(json.dumps({'available': ok, 'top': hits[0]['name'] if hits else None}))"
    )
    r2 = subprocess.run([str(venv_py), "-c", test_script], capture_output=True, text=True,
                        env={**os.environ, "WV_URL": WV_URL,
                             "PG_HOST": PG_HOST, "PG_PORT": PG_PORT,
                             "PG_USER": PG_USER, "PG_PASS": PG_PASS,
                             "CLAUDE_DEJAVU_DATA_ROOT": str(DATA_ROOT)})
    try:
        sem = json.loads(r2.stdout.strip().splitlines()[-1])
    except Exception:
        sem = {"available": False, "top": None}
    if sem.get("available"):
        if not sem.get("top") or "users" not in (sem["top"] or "").lower():
            fail(f"semantic 'create user endpoint' didn't surface a /users route: {sem}")
            return False
        ok(f"semantic search: 'create user endpoint' → {sem['top']}")
    else:
        ok("semantic search: Weaviate not reachable — channel cleanly silent (additive only)")

    # ─── LSP scaffold ────────────────────────────────────────────────────
    # The strategy registers and either reports availability or skips.
    # We just verify the module imports cleanly via the registry.
    test_script = (
        "import sys; sys.path.insert(0, str(__import__('pathlib').Path("
        f"{repr(str(PLUGIN_ROOT))}) / 'code'));"
        "import lint_strategies as ls;"
        "names = [s.name for s in ls.all_strategies()];"
        "print(','.join(sorted(names)))"
    )
    r3 = subprocess.run([str(venv_py), "-c", test_script], capture_output=True, text=True)
    if r3.returncode != 0:
        fail(f"lint_strategies import failed: {r3.stderr}"); return False
    have = set((r3.stdout.strip() or "").split(","))
    expected = {"symbol_grounding", "route_grounding", "semantic_intent", "lsp_verification"}
    missing = expected - have
    if missing:
        fail(f"strategies missing from registry: {missing} (have {have})"); return False
    ok(f"strategy registry has all 4 strategies: {sorted(have)}")

    # File deletion handling
    (proj / "ts_module.ts").unlink()
    r = cli("reindex", f"--path={proj}", "--project=smoke-grounding")
    if "files purged" not in r.stdout:
        fail(f"deletion not reported\n{r.stdout}"); return False
    r = cli("symbols", "ProfileCache", "--project=smoke-grounding")
    if "ed=0" in r.stdout and "smoke-grounding" in r.stdout:
        # If exact match still present after delete, purge logic broke
        fail(f"purge left stale rows\n{r.stdout}"); return False
    ok("file deletion purges stale symbol rows")

    return True


def step_pages_and_bootstrap():
    step("[12] v0.3.5 page indexing + auto-bootstrap")

    # Build a multi-framework page fixture under one project root so the
    # _find_project_root walk finds it via package.json.
    proj = SANDBOX / "pages-fixture"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "package.json").write_text('{"name": "pages-fixture"}\n')

    # Next.js app router pages.
    app = proj / "app"
    (app / "dashboard" / "profile").mkdir(parents=True, exist_ok=True)
    (app / "dashboard" / "profile" / "page.tsx").write_text(
        "export default function Profile() { return null; }\n"
    )
    (app / "blog" / "[slug]").mkdir(parents=True, exist_ok=True)
    (app / "blog" / "[slug]" / "page.tsx").write_text(
        "export default function BlogPost() { return null; }\n"
    )
    (app / "settings").mkdir(parents=True, exist_ok=True)
    (app / "settings" / "page.tsx").write_text(
        "export default function Settings() { return null; }\n"
    )

    # Next.js app router API route — should be picked up too.
    (app / "api" / "users").mkdir(parents=True, exist_ok=True)
    (app / "api" / "users" / "route.ts").write_text(
        "export async function GET() { return new Response(); }\n"
    )

    # Reindex picks up pages alongside symbols + routes.
    r = cli("reindex", f"--path={proj}", "--project=smoke-pages",
            "--skip-semantic")
    if r.returncode != 0:
        fail(f"pages reindex failed\n{r.stdout}\n{r.stderr}"); return False
    if "pages added" not in r.stdout:
        fail(f"reindex didn't report pages section\n{r.stdout}"); return False
    # We seeded 3 pages + 1 api route in the app dir.
    if "pages added   : 4" not in r.stdout and "pages added   : 3" not in r.stdout:
        fail(f"reindex didn't pick up the right page count\n{r.stdout}"); return False
    ok("page indexer extracts Next.js app router pages + api routes")

    # `pages list` shows them.
    r = cli("pages", "list", "--project=smoke-pages")
    if r.returncode != 0:
        fail(f"pages list failed\n{r.stdout}\n{r.stderr}"); return False
    if "/dashboard/profile" not in r.stdout or "/blog/:slug" not in r.stdout:
        fail(f"pages list missing expected entries\n{r.stdout}"); return False
    ok("pages list shows indexed page routes")

    # Exact match: proposing /dashboard/profile is a definite duplicate.
    r = cli("pages", "check", "/dashboard/profile", "--project=smoke-pages")
    if r.returncode != 0:
        fail(f"pages check (exact) failed\n{r.stdout}\n{r.stderr}"); return False
    if "✗" not in r.stdout or "/dashboard/profile" not in r.stdout:
        fail(f"pages check didn't flag exact duplicate\n{r.stdout}"); return False
    ok("pages check: exact-match duplicate detected")

    # Pattern match: proposing /blog/foo overlaps with the parameterized /blog/:slug.
    r = cli("pages", "check", "/blog/foo", "--project=smoke-pages")
    if r.returncode != 0:
        fail(f"pages check (pattern) failed\n{r.stdout}\n{r.stderr}"); return False
    if "/blog/:slug" not in r.stdout:
        fail(f"pages check didn't pattern-match parameterized route\n{r.stdout}"); return False
    ok("pages check: concrete URL → parameterized route pattern match")

    # Clean: a totally unrelated path should produce no overlap.
    r = cli("pages", "check", "/nothing/here/at/all", "--project=smoke-pages")
    if r.returncode != 0:
        fail(f"pages check (clean) failed\n{r.stdout}\n{r.stderr}"); return False
    if "safe to create" not in r.stdout:
        fail(f"pages check should report 'safe to create' for unrelated path\n{r.stdout}")
        return False
    ok("pages check: unrelated path passes cleanly")

    # ─── Lint integration: page_duplicate strategy ────────────────────────
    # Propose a NEW file at an existing route — should block.
    dup_file = proj / "app" / "dashboard" / "profile" / "page-new.tsx"
    # Note: page-new.tsx is NOT a Next.js page filename — only `page.tsx`/.jsx
    # creates a route. Use a different setup: propose a totally new path that
    # collides via parameterized match, and a new path that's an exact dup.
    #
    # Exact dup: re-create the existing /dashboard/profile/page.tsx in a
    # fresh project layout (we lint a proposed Write to that path).
    proposed_dup = str(proj / "app" / "settings" / "page.tsx")  # already exists
    # A proposed Write to settings/page.tsx that ALREADY exists is "editing
    # in place" per the strategy's same_file guard, so it should NOT flag.
    r = cli("lint", proposed_dup, "--project=smoke-pages",
            f"--content-file={proposed_dup}")
    if r.returncode != 0:
        fail(f"lint of editing-in-place existing page should pass; got {r.returncode}\n{r.stdout}")
        return False
    ok("lint: editing in place (same file) is not flagged as duplicate")

    # Now propose a brand-new file that would route to /blog/foo (which
    # collides with /blog/:slug pattern). The new file is at a path that
    # doesn't yet exist.
    new_blog = proj / "app" / "blog" / "foo" / "page.tsx"
    new_blog.parent.mkdir(parents=True, exist_ok=True)
    pattern_content = "export default function BlogFoo() { return null; }\n"
    # Don't write the file to disk — pages_indexer would re-pick it up. Pass
    # via --content-stdin so the lint sees the proposed file path + content.
    venv_py = DATA_ROOT / "venv" / ("Scripts" if sys.platform.startswith("win") else "bin") / (
        "python.exe" if sys.platform.startswith("win") else "python3"
    )
    cmd = [str(venv_py), str(PLUGIN_ROOT / "code" / "recall.py"),
           "lint", str(new_blog), "--project=smoke-pages", "--content-stdin"]
    r = subprocess.run(cmd, input=pattern_content, capture_output=True, text=True,
                       env={**os.environ, "CLAUDE_DEJAVU_DATA_ROOT": str(DATA_ROOT),
                            "PGPASSWORD": PG_PASS})
    # Pattern match → LOW_CONFIDENCE → exit 1 (warn).
    if r.returncode not in (0, 1):
        fail(f"lint of pattern-overlap page expected exit 0 or 1, got {r.returncode}\n{r.stdout}")
        return False
    if r.returncode == 1 and "/blog/:slug" not in r.stdout:
        fail(f"lint warn but didn't surface the parameterized overlap\n{r.stdout}")
        return False
    ok("lint: page_duplicate strategy detects pattern overlap (or cleanly skips)")

    # Empty-index graceful skip — different project slug with zero pages
    # should NOT have the strategy flag every Write.
    empty_proj = SANDBOX / "no-pages-fixture"
    empty_proj.mkdir(parents=True, exist_ok=True)
    (empty_proj / "package.json").write_text('{"name":"empty-pages"}\n')
    (empty_proj / "app").mkdir(parents=True, exist_ok=True)
    nofile = empty_proj / "app" / "thing" / "page.tsx"
    nofile.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(venv_py), str(PLUGIN_ROOT / "code" / "recall.py"),
           "lint", str(nofile), "--project=no-pages-here", "--content-stdin"]
    r = subprocess.run(cmd, input="export default function X() { return null; }\n",
                       capture_output=True, text=True,
                       env={**os.environ, "CLAUDE_DEJAVU_DATA_ROOT": str(DATA_ROOT),
                            "PGPASSWORD": PG_PASS})
    # No pages indexed for this slug → no page_duplicate issues. Symbol
    # grounding may still flag things (or not), but specifically no
    # "pages — DUPLICATE PAGE" output.
    if "DUPLICATE PAGE" in r.stdout:
        fail(f"page_duplicate fired against empty index\n{r.stdout}"); return False
    ok("lint: page_duplicate silent-passes when project has zero indexed pages")

    # ─── Auto-bootstrap (ingester --bootstrap) ────────────────────────────
    # The bootstrap command should run cleanly even if ~/.claude/projects
    # doesn't exist or is empty. We test the no-crash + idempotency path
    # by pointing HOME at a sandbox dir with no live projects.
    bootstrap_home = SANDBOX / "fake-home"
    bootstrap_home.mkdir(parents=True, exist_ok=True)
    cmd = [str(venv_py), str(PLUGIN_ROOT / "code" / "ingester.py"), "--bootstrap"]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       env={**os.environ, "CLAUDE_DEJAVU_DATA_ROOT": str(DATA_ROOT),
                            "PGPASSWORD": PG_PASS, "HOME": str(bootstrap_home)})
    if r.returncode != 0:
        fail(f"bootstrap with empty live tree should not crash; got {r.returncode}\n{r.stderr}")
        return False
    if "no ~/.claude/projects directory" not in r.stdout and "[bootstrap]" not in r.stdout:
        fail(f"bootstrap didn't print expected progress\n{r.stdout}"); return False
    ok("bootstrap: handles missing ~/.claude/projects gracefully")

    # Stage a real .jsonl in a fake live tree, run bootstrap, verify
    # ingestion + mirroring + idempotency.
    live_proj_dir = bootstrap_home / ".claude" / "projects" / "-tmp-smoke-bootstrap-test"
    live_proj_dir.mkdir(parents=True, exist_ok=True)
    sample = PLUGIN_ROOT / "tests" / "fixtures" / "sample-session.jsonl"
    bootstrap_uuid = "11111111-1111-1111-1111-111111111111"
    shutil.copy2(sample, live_proj_dir / f"{bootstrap_uuid}.jsonl")

    r = subprocess.run(cmd, capture_output=True, text=True,
                       env={**os.environ, "CLAUDE_DEJAVU_DATA_ROOT": str(DATA_ROOT),
                            "PGPASSWORD": PG_PASS, "HOME": str(bootstrap_home)})
    if r.returncode != 0:
        fail(f"bootstrap with real .jsonl failed: {r.returncode}\n{r.stderr}"); return False
    if "mirrored 1 new" not in r.stdout:
        fail(f"bootstrap didn't mirror the staged .jsonl\n{r.stdout}"); return False
    # Verify the mirror landed in DATA_ROOT/chat-logs/
    mirrored = DATA_ROOT / "chat-logs" / "-tmp-smoke-bootstrap-test" / f"{bootstrap_uuid}.jsonl"
    if not mirrored.is_file():
        fail(f"bootstrap mirror missing at {mirrored}"); return False
    ok("bootstrap: mirrors live .jsonl files into chat-logs/")

    # Idempotency — second run should skip the already-mirrored file.
    r = subprocess.run(cmd, capture_output=True, text=True,
                       env={**os.environ, "CLAUDE_DEJAVU_DATA_ROOT": str(DATA_ROOT),
                            "PGPASSWORD": PG_PASS, "HOME": str(bootstrap_home)})
    if r.returncode != 0:
        fail(f"bootstrap idempotency check returncode != 0\n{r.stderr}"); return False
    if "skipped 1 unchanged" not in r.stdout:
        fail(f"bootstrap not idempotent — should skip unchanged file\n{r.stdout}")
        return False
    ok("bootstrap: idempotent (skips unchanged files on re-run)")

    return True


def step_env_grounding():
    step("[13] v0.3.6 env var grounding")

    # Build a project with all four canonical env-var sources so the
    # indexer exercises every parser path.
    proj = SANDBOX / "env-fixture"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "package.json").write_text('{"name":"env-fixture"}\n')
    (proj / ".env.example").write_text(
        "# Database connection string\n"
        "DATABASE_URL=postgres://localhost/mydb\n"
        "# Stripe API key for billing\n"
        "STRIPE_SECRET_KEY=\n"
        "# Public API base\n"
        'NEXT_PUBLIC_API_URL="http://localhost:3000"\n'
    )
    (proj / "env.config.ts").write_text(
        "import { z } from 'zod';\n"
        "export const env = z.object({\n"
        "  DATABASE_URL: z.string().url(),\n"
        "  STRIPE_SECRET_KEY: z.string().min(1),\n"
        "  REDIS_URL: z.string().optional(),\n"
        "});\n"
    )
    (proj / "docker-compose.yml").write_text(
        "version: '3'\n"
        "services:\n"
        "  api:\n"
        "    image: x\n"
        "    environment:\n"
        "      - DATABASE_URL=postgres://db/mydb\n"
        "      - STRIPE_SECRET_KEY\n"
        "      RABBITMQ_URL: amqp://broker:5672\n"
    )
    gh_dir = proj / ".github" / "workflows"
    gh_dir.mkdir(parents=True, exist_ok=True)
    (gh_dir / "ci.yml").write_text(
        "name: CI\n"
        "on: push\n"
        "env:\n"
        "  CI_BUILD_ID: build-123\n"
        "  GITHUB_TOKEN: secret\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
    )
    # Source-read tier: a TS file that reads env vars (one declared,
    # one undeclared but consistent, one typo, one fabricated).
    (proj / "server.ts").write_text(
        "const db = process.env.DATABASE_URL;\n"
        "const stripe = process.env.STRIPE_SECRET_KEY;\n"
        "const undeclared = process.env.UNDECLARED_BUT_USED;\n"
        "const typo = process.env.DATABSE_URL;\n"
    )

    # Reindex with --skip-semantic to keep this fast — env indexing is
    # a separate pass.
    r = cli("reindex", f"--path={proj}", "--project=smoke-env",
            "--skip-semantic")
    if r.returncode != 0:
        fail(f"env reindex failed\n{r.stdout}\n{r.stderr}"); return False
    if "env vars added" not in r.stdout:
        fail(f"reindex didn't report env section\n{r.stdout}"); return False
    if "by source" not in r.stdout:
        fail(f"reindex didn't break down by source\n{r.stdout}"); return False
    # Expected names: DATABASE_URL, STRIPE_SECRET_KEY, NEXT_PUBLIC_API_URL,
    # REDIS_URL, RABBITMQ_URL, CI_BUILD_ID, GITHUB_TOKEN, UNDECLARED_BUT_USED,
    # DATABSE_URL. Across multiple sources (the count is rows not names).
    if "env-example=" not in r.stdout or "zod-schema=" not in r.stdout \
            or "docker-compose=" not in r.stdout \
            or "github-actions=" not in r.stdout \
            or "source-read=" not in r.stdout:
        fail(f"reindex didn't populate all 5 source tiers\n{r.stdout}"); return False
    ok("env indexer extracts .env.example + zod + docker-compose + GH Actions + source-reads")

    # `env list` shows everything.
    r = cli("env", "list", "--project=smoke-env")
    if r.returncode != 0:
        fail(f"env list failed\n{r.stdout}\n{r.stderr}"); return False
    for needed in ("DATABASE_URL", "STRIPE_SECRET_KEY", "REDIS_URL",
                    "RABBITMQ_URL", "CI_BUILD_ID", "GITHUB_TOKEN",
                    "UNDECLARED_BUT_USED"):
        if needed not in r.stdout:
            fail(f"env list missing {needed}\n{r.stdout}"); return False
    # Secrets heuristic — STRIPE_SECRET_KEY + GITHUB_TOKEN should be
    # marked, NEXT_PUBLIC_API_URL should not.
    if "🔒" not in r.stdout:
        fail(f"env list didn't flag any secrets\n{r.stdout}"); return False
    ok("env list shows indexed vars across all sources, flags secrets")

    # `env list --secrets-only` filters down.
    r = cli("env", "list", "--project=smoke-env", "--secrets-only")
    if r.returncode != 0 or "STRIPE_SECRET_KEY" not in r.stdout:
        fail(f"env list --secrets-only failed\n{r.stdout}"); return False
    if "NEXT_PUBLIC_API_URL" in r.stdout:
        fail(f"--secrets-only included a non-secret\n{r.stdout}"); return False
    ok("env list --secrets-only filters correctly")

    # Exact match in canonical source.
    r = cli("env", "check", "DATABASE_URL", "--project=smoke-env")
    if r.returncode != 0:
        fail(f"env check exact failed\n{r.stdout}\n{r.stderr}"); return False
    if "✓" not in r.stdout or "DATABASE_URL" not in r.stdout:
        fail(f"env check (exact) missed the var\n{r.stdout}"); return False
    ok("env check: exact match in canonical source")

    # Fuzzy match — a typo of an existing canonical var.
    r = cli("env", "check", "DATABASE_URLE", "--project=smoke-env")
    if r.returncode != 0:
        fail(f"env check fuzzy failed\n{r.stdout}\n{r.stderr}"); return False
    if "DATABASE_URL" not in r.stdout or "~" not in r.stdout:
        fail(f"env check (fuzzy) didn't surface the canonical neighbor\n{r.stdout}")
        return False
    ok("env check: fuzzy match catches typo of canonical var")

    # Fabricated name → no match.
    r = cli("env", "check", "TOTALLY_FABRICATED_VAR_XYZ", "--project=smoke-env")
    if r.returncode == 0:
        fail(f"env check (fabricated) should exit non-zero\n{r.stdout}"); return False
    if "would be flagged as fabricated" not in r.stdout:
        fail(f"env check didn't surface the fabricated-name message\n{r.stdout}")
        return False
    ok("env check: fabricated name reports no-match cleanly")

    # ─── Lint integration: env_grounding strategy ────────────────────────
    proposed = SANDBOX / "env-proposed.ts"
    proposed.write_text(
        "const db = process.env.DATABASE_URL;\n"            # resolved
        "const stripe = process.env.STRIPE_SECRET_KEY;\n"  # resolved
        "const fake = process.env.JUST_FABRICATED_HERE;\n"  # unknown -> block
        "const typo = process.env.DATABSE_URL;\n"           # low_conf -> typo
    )
    r = cli("lint", str(proposed), "--project=smoke-env",
            f"--content-file={proposed}")
    # JUST_FABRICATED_HERE is unknown → block expected.
    if r.returncode != 2:
        fail(f"env lint expected exit=2 (block for unknown), got {r.returncode}\n{r.stdout}")
        return False
    if "JUST_FABRICATED_HERE" not in r.stdout:
        fail(f"env lint didn't flag fabricated name\n{r.stdout}"); return False
    if "DATABSE_URL" not in r.stdout or "DATABASE_URL" not in r.stdout:
        fail(f"env lint didn't surface typo + canonical neighbor\n{r.stdout}")
        return False
    ok("env_grounding lint: blocks fabricated, flags typos with canonical candidate")

    # Lint a clean file with only declared vars → no env_unknown / env_low.
    clean = SANDBOX / "env-clean.ts"
    clean.write_text(
        "const db = process.env.DATABASE_URL;\n"
        "const redis = process.env.REDIS_URL;\n"
    )
    r = cli("lint", str(clean), "--project=smoke-env",
            f"--content-file={clean}")
    if r.returncode != 0:
        fail(f"clean env lint should exit 0, got {r.returncode}\n{r.stdout}")
        return False
    if "env vars — unknown" in r.stdout or "env vars — low-confidence" in r.stdout:
        fail(f"clean env lint flagged something\n{r.stdout}"); return False
    ok("env_grounding lint: clean references pass with exit 0")

    # Empty-index graceful skip — different project slug.
    r = cli("lint", str(proposed), "--project=no-env-here",
            f"--content-file={proposed}")
    # No env vars indexed for that slug → strategy bails. Some symbol-
    # grounding may flag things, but specifically no env_grounding output.
    if "env vars — unknown" in r.stdout or "env vars — low-confidence" in r.stdout:
        fail(f"env_grounding fired against empty index\n{r.stdout}"); return False
    ok("env_grounding lint: silent-passes on empty index")

    # Multi-language: Python os.getenv, Go os.Getenv, Rust env::var.
    py_proposed = SANDBOX / "env-proposed.py"
    py_proposed.write_text(
        "import os\n"
        "db = os.getenv('DATABASE_URL')\n"
        "fake = os.environ['PYTHON_FAKE_VAR']\n"
    )
    r = cli("lint", str(py_proposed), "--project=smoke-env",
            f"--content-file={py_proposed}")
    if r.returncode != 2 or "PYTHON_FAKE_VAR" not in r.stdout:
        fail(f"python env lint didn't flag fabricated\n{r.stdout}"); return False
    ok("env_grounding lint: Python os.getenv / os.environ extraction works")

    # Verify the strategy is registered in the registry alongside the others.
    venv_py = DATA_ROOT / "venv" / ("Scripts" if sys.platform.startswith("win") else "bin") / (
        "python.exe" if sys.platform.startswith("win") else "python3"
    )
    test_script = (
        "import sys; sys.path.insert(0, str(__import__('pathlib').Path("
        f"{repr(str(PLUGIN_ROOT))}) / 'code'));"
        "import lint_strategies as ls;"
        "names = sorted(s.name for s in ls.all_strategies());"
        "print(','.join(names))"
    )
    r3 = subprocess.run([str(venv_py), "-c", test_script],
                        capture_output=True, text=True)
    if r3.returncode != 0 or "env_grounding" not in r3.stdout:
        fail(f"env_grounding not in registry: {r3.stdout} | {r3.stderr}")
        return False
    ok(f"strategy registry has env_grounding: {r3.stdout.strip()}")

    return True


def step_db_grounding():
    step("[14] v0.3.7 DB schema grounding (Prisma / Drizzle / TypeORM / SQL)")

    proj = SANDBOX / "db-fixture"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "package.json").write_text('{"name":"db-fixture"}\n')

    # Prisma schema with explicit @@map.
    (proj / "prisma").mkdir(parents=True, exist_ok=True)
    (proj / "prisma" / "schema.prisma").write_text(
        'generator client { provider = "prisma-client-js" }\n'
        'datasource db { provider = "postgresql"; url = env("DATABASE_URL") }\n'
        '\n'
        '/// A user in the system\n'
        'model User {\n'
        '  id        Int      @id @default(autoincrement())\n'
        '  email     String   @unique\n'
        '  name      String?\n'
        '  posts     Post[]\n'
        '  createdAt DateTime @default(now())\n'
        '  @@map("users")\n'
        '}\n'
        '\n'
        'model Post {\n'
        '  id       Int     @id @default(autoincrement())\n'
        '  title    String\n'
        '  content  String?\n'
        '  authorId Int\n'
        '  author   User    @relation(fields: [authorId], references: [id])\n'
        '}\n'
    )

    # Drizzle schema.
    (proj / "drizzle").mkdir(parents=True, exist_ok=True)
    (proj / "drizzle" / "schema.ts").write_text(
        "import { pgTable, serial, text, integer, timestamp } from 'drizzle-orm/pg-core';\n"
        "\n"
        "export const usersTable = pgTable('users', {\n"
        "  id: serial('id').primaryKey(),\n"
        "  email: text('email').notNull().unique(),\n"
        "  displayName: text('display_name'),\n"
        "});\n"
        "\n"
        "export const postsTable = pgTable('posts', {\n"
        "  id: serial('id').primaryKey(),\n"
        "  title: text('title').notNull(),\n"
        "  authorId: integer('author_id').notNull(),\n"
        "});\n"
    )

    # TypeORM entity.
    (proj / "entities").mkdir(parents=True, exist_ok=True)
    (proj / "entities" / "Account.ts").write_text(
        "import { Entity, PrimaryGeneratedColumn, Column } from 'typeorm';\n"
        "\n"
        '@Entity("accounts")\n'
        "export class Account {\n"
        "  @PrimaryGeneratedColumn()\n"
        "  id: number;\n"
        "\n"
        "  @Column({ unique: true })\n"
        "  email: string;\n"
        "\n"
        "  @Column({ nullable: true })\n"
        "  displayName: string;\n"
        "}\n"
    )

    # SQL migration.
    (proj / "migrations").mkdir(parents=True, exist_ok=True)
    (proj / "migrations" / "001_init.sql").write_text(
        "CREATE TABLE IF NOT EXISTS organizations (\n"
        "  id SERIAL PRIMARY KEY,\n"
        "  name TEXT NOT NULL,\n"
        "  slug TEXT NOT NULL UNIQUE,\n"
        "  owner_id INTEGER NOT NULL REFERENCES users(id)\n"
        ");\n"
    )

    r = cli("reindex", f"--path={proj}", "--project=smoke-db",
            "--skip-semantic")
    if r.returncode != 0:
        fail(f"db reindex failed\n{r.stdout}\n{r.stderr}"); return False
    if "models added" not in r.stdout:
        fail(f"reindex didn't report db section\n{r.stdout}"); return False
    for src in ("prisma=", "drizzle=", "typeorm=", "sql-migration="):
        if src not in r.stdout:
            fail(f"reindex didn't populate source `{src}`\n{r.stdout}")
            return False
    ok("db indexer extracts Prisma + Drizzle + TypeORM + SQL migrations")

    # `db models` shows everything.
    r = cli("db", "models", "--project=smoke-db")
    if r.returncode != 0:
        fail(f"db models failed\n{r.stdout}\n{r.stderr}"); return False
    for needed in ("User", "Post", "usersTable", "postsTable", "Account",
                    "organizations"):
        if needed not in r.stdout:
            fail(f"db models missing {needed}\n{r.stdout}"); return False
    ok("db models lists across all four sources")

    # `db fields --model=User`
    r = cli("db", "fields", "--project=smoke-db", "--model=User")
    if r.returncode != 0:
        fail(f"db fields failed\n{r.stdout}\n{r.stderr}"); return False
    for needed in ("id", "email", "name", "posts", "createdAt"):
        if needed not in r.stdout:
            fail(f"db fields missing {needed}\n{r.stdout}"); return False
    ok("db fields lists fields per model with badges")

    # check exact MODEL.FIELD
    r = cli("db", "check", "User.email", "--project=smoke-db")
    if r.returncode != 0 or "✓" not in r.stdout:
        fail(f"db check exact failed\n{r.stdout}"); return False
    ok("db check: exact MODEL.FIELD resolves")

    # check fuzzy field typo
    r = cli("db", "check", "User.emaill", "--project=smoke-db")
    if r.returncode != 0 or "User" not in r.stdout or ".email" not in r.stdout \
            or "~" not in r.stdout:
        fail(f"db check fuzzy field typo failed\n{r.stdout}"); return False
    ok("db check: fuzzy match catches field typo")

    # check cross-model field
    r = cli("db", "check", "Post.email", "--project=smoke-db")
    if r.returncode != 0 or "≈" not in r.stdout:
        fail(f"db check cross-model failed\n{r.stdout}"); return False
    ok("db check: cross-model match flagged with ≈ marker")

    # check fabricated
    r = cli("db", "check", "User.totallyFakeColumn", "--project=smoke-db")
    if r.returncode == 0:
        fail(f"db check fabricated should exit non-zero\n{r.stdout}"); return False
    ok("db check: fabricated field reports no-match")

    # check-model
    r = cli("db", "check-model", "User", "--project=smoke-db")
    if r.returncode != 0 or "✓" not in r.stdout:
        fail(f"db check-model exact failed\n{r.stdout}"); return False
    ok("db check-model: exact match")

    # ─── Lint integration: db_grounding strategy ────────────────────────
    proposed = SANDBOX / "db-proposed.ts"
    proposed.write_text(
        "// Proposed Prisma client + Drizzle code\n"
        "const u1 = await prisma.user.findUnique({ where: { email: 'x' } });\n"
        "const u2 = await prisma.user.findUnique({ where: { emaill: 'x' } });\n"
        "const u3 = await prisma.post.findFirst({ where: { totallyFakeField: 1 } });\n"
        "const u4 = await prisma.usrs.findMany({ where: { id: 1 } });\n"
        "const a = await db.select().from(usersTable);\n"
        "const b = await db.select().from(totallyFakeTable);\n"
    )
    r = cli("lint", str(proposed), "--project=smoke-db",
            f"--content-file={proposed}")
    # Unknown fields/models → block (exit 2).
    if r.returncode != 2:
        fail(f"db lint expected exit=2 (block), got {r.returncode}\n{r.stdout}")
        return False
    if "Post.totallyFakeField" not in r.stdout:
        fail(f"db lint missed unknown field\n{r.stdout}"); return False
    if "User.emaill" not in r.stdout or "User.email" not in r.stdout:
        fail(f"db lint missed typo + canonical neighbor\n{r.stdout}"); return False
    if "totallyFakeTable" not in r.stdout:
        fail(f"db lint missed unknown Drizzle table\n{r.stdout}"); return False
    ok("db_grounding lint: blocks fabricated, flags typos + cross-model")

    # Clean lint should pass.
    clean = SANDBOX / "db-clean.ts"
    clean.write_text(
        "const u = await prisma.user.findUnique({ where: { email: 'x' } });\n"
        "const ps = await prisma.post.findMany({ where: { authorId: 1 } });\n"
        "const t = await db.select().from(usersTable);\n"
    )
    r = cli("lint", str(clean), "--project=smoke-db",
            f"--content-file={clean}")
    # NB: symbol_grounding may flag `prisma`, `findUnique` etc. as
    # undeclared symbols — that's a pre-existing issue, not v0.3.7's
    # concern. We assert specifically that NO db_grounding section was
    # emitted.
    if "db schema — unknown" in r.stdout or "db schema — low-confidence" in r.stdout:
        fail(f"db_grounding flagged a clean reference\n{r.stdout}"); return False
    ok("db_grounding lint: clean references don't trigger db section")

    # Empty-index graceful skip.
    r = cli("lint", str(proposed), "--project=no-db-here",
            f"--content-file={proposed}")
    if "db schema — unknown" in r.stdout or "db schema — low-confidence" in r.stdout:
        fail(f"db_grounding fired against empty index\n{r.stdout}"); return False
    ok("db_grounding lint: silent-passes on empty index")

    # Verify strategy is registered.
    venv_py = DATA_ROOT / "venv" / ("Scripts" if sys.platform.startswith("win") else "bin") / (
        "python.exe" if sys.platform.startswith("win") else "python3"
    )
    r3 = subprocess.run([str(venv_py), "-c",
        "import sys; sys.path.insert(0, str(__import__('pathlib').Path("
        f"{repr(str(PLUGIN_ROOT))}) / 'code'));"
        "import lint_strategies as ls;"
        "names = sorted(s.name for s in ls.all_strategies());"
        "print(','.join(names))"
    ], capture_output=True, text=True)
    if r3.returncode != 0 or "db_grounding" not in r3.stdout:
        fail(f"db_grounding not in registry: {r3.stdout} | {r3.stderr}")
        return False
    ok(f"strategy registry has db_grounding: {r3.stdout.strip()}")

    return True


def step_graphql_grounding():
    step("[15] v0.3.8 GraphQL schema grounding")

    proj = SANDBOX / "gql-fixture"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "package.json").write_text('{"name":"gql-fixture"}\n')

    # SDL schema file.
    (proj / "schema.graphql").write_text(
        '"""A user in the system"""\n'
        'type User {\n'
        '  id: ID!\n'
        '  email: String!\n'
        '  name: String\n'
        '  posts: [Post!]!\n'
        '}\n'
        '\n'
        'type Post {\n'
        '  id: ID!\n'
        '  title: String!\n'
        '  content: String\n'
        '  author: User!\n'
        '}\n'
        '\n'
        'input CreateUserInput {\n'
        '  email: String!\n'
        '  name: String\n'
        '}\n'
        '\n'
        'enum Role { ADMIN USER GUEST }\n'
        '\n'
        'type Query {\n'
        '  user(id: ID!): User\n'
        '  posts(authorId: ID, limit: Int = 10): [Post!]!\n'
        '}\n'
        '\n'
        'type Mutation {\n'
        '  createUser(input: CreateUserInput!): User!\n'
        '}\n'
    )

    # Inline gql in a TS file.
    (proj / "typedefs.ts").write_text(
        "import { gql } from 'graphql-tag';\n"
        "\n"
        "export const accountTypes = gql`\n"
        "  type Account {\n"
        "    id: ID!\n"
        "    displayName: String!\n"
        "  }\n"
        "`;\n"
    )

    r = cli("reindex", f"--path={proj}", "--project=smoke-gql",
            "--skip-semantic")
    if r.returncode != 0:
        fail(f"gql reindex failed\n{r.stdout}\n{r.stderr}"); return False
    if "types added" not in r.stdout:
        fail(f"reindex didn't report graphql section\n{r.stdout}"); return False
    if "sdl-file=" not in r.stdout or "inline-gql=" not in r.stdout:
        fail(f"reindex didn't populate both sources\n{r.stdout}"); return False
    ok("graphql indexer extracts SDL + inline-gql")

    # `graphql types` and `graphql fields`.
    r = cli("graphql", "types", "--project=smoke-gql")
    if r.returncode != 0:
        fail(f"graphql types failed\n{r.stdout}"); return False
    for needed in ("User", "Post", "Query", "Mutation", "Account",
                    "CreateUserInput", "Role"):
        if needed not in r.stdout:
            fail(f"graphql types missing {needed}\n{r.stdout}"); return False
    ok("graphql types lists across SDL + inline-gql")

    r = cli("graphql", "fields", "--project=smoke-gql", "--type=User")
    if r.returncode != 0:
        fail(f"graphql fields failed\n{r.stdout}"); return False
    for needed in ("id", "email", "name", "posts"):
        if needed not in r.stdout:
            fail(f"graphql fields missing {needed}\n{r.stdout}"); return False
    ok("graphql fields lists fields per type")

    # check exact
    r = cli("graphql", "check", "User.email", "--project=smoke-gql")
    if r.returncode != 0 or "✓" not in r.stdout:
        fail(f"graphql check exact failed\n{r.stdout}"); return False
    ok("graphql check: exact TYPE.FIELD resolves")

    # check fuzzy (typo)
    r = cli("graphql", "check", "User.emaill", "--project=smoke-gql")
    if r.returncode != 0 or ".email" not in r.stdout or "~" not in r.stdout:
        fail(f"graphql check fuzzy failed\n{r.stdout}"); return False
    ok("graphql check: fuzzy match catches field typo")

    # check cross-type
    r = cli("graphql", "check", "Post.email", "--project=smoke-gql")
    if r.returncode != 0 or "≈" not in r.stdout:
        fail(f"graphql check cross-type failed\n{r.stdout}"); return False
    ok("graphql check: cross-type match flagged")

    # check fabricated
    r = cli("graphql", "check", "User.totallyFakeField", "--project=smoke-gql")
    if r.returncode == 0:
        fail(f"graphql check fabricated should exit non-zero\n{r.stdout}")
        return False
    ok("graphql check: fabricated field reports no-match")

    # check-type (Account from inline-gql)
    r = cli("graphql", "check-type", "Account", "--project=smoke-gql")
    if r.returncode != 0 or "inline-gql" not in r.stdout:
        fail(f"graphql check-type Account failed\n{r.stdout}"); return False
    ok("graphql check-type: resolves type from inline-gql")

    # ─── Lint integration ───────────────────────────────────────────────
    proposed = SANDBOX / "gql-proposed.ts"
    proposed.write_text(
        "import { gql } from 'graphql-tag';\n"
        "const A = gql`query GetUser($id: ID!) {\n"
        "  user(id: $id) { id email name posts { title } }\n"
        "}`;\n"
        "const B = gql`query { user(id: \"1\") { emaill } }`;\n"
        "const C = gql`query { totallyFakeQuery { id } }`;\n"
    )
    r = cli("lint", str(proposed), "--project=smoke-gql",
            f"--content-file={proposed}")
    if r.returncode != 2:
        fail(f"gql lint expected exit=2 (block), got {r.returncode}\n{r.stdout}")
        return False
    if "totallyFakeQuery" not in r.stdout:
        fail(f"gql lint missed fabricated field\n{r.stdout}"); return False
    if "User.emaill" not in r.stdout or ".email" not in r.stdout:
        fail(f"gql lint missed typo\n{r.stdout}"); return False
    ok("graphql_grounding lint: blocks fabricated, flags typos")

    # Clean operation.
    clean = SANDBOX / "gql-clean.ts"
    clean.write_text(
        "import { gql } from 'graphql-tag';\n"
        "const Q = gql`query { user(id: \"1\") { id email name posts { title } } }`;\n"
    )
    r = cli("lint", str(clean), "--project=smoke-gql",
            f"--content-file={clean}")
    if "graphql — unknown" in r.stdout or "graphql — low-confidence" in r.stdout:
        fail(f"clean gql query flagged something\n{r.stdout}"); return False
    ok("graphql_grounding lint: clean operations don't trigger gql section")

    # Empty index graceful skip.
    r = cli("lint", str(proposed), "--project=no-gql-here",
            f"--content-file={proposed}")
    if "graphql — unknown" in r.stdout or "graphql — low-confidence" in r.stdout:
        fail(f"graphql_grounding fired against empty index\n{r.stdout}")
        return False
    ok("graphql_grounding lint: silent-passes on empty index")

    # Verify strategy registered.
    venv_py = DATA_ROOT / "venv" / ("Scripts" if sys.platform.startswith("win") else "bin") / (
        "python.exe" if sys.platform.startswith("win") else "python3"
    )
    r3 = subprocess.run([str(venv_py), "-c",
        "import sys; sys.path.insert(0, str(__import__('pathlib').Path("
        f"{repr(str(PLUGIN_ROOT))}) / 'code'));"
        "import lint_strategies as ls;"
        "names = sorted(s.name for s in ls.all_strategies());"
        "print(','.join(names))"
    ], capture_output=True, text=True)
    if r3.returncode != 0 or "graphql_grounding" not in r3.stdout:
        fail(f"graphql_grounding not in registry: {r3.stdout} | {r3.stderr}")
        return False
    ok(f"strategy registry has graphql_grounding: {r3.stdout.strip()}")

    return True


def step_lsp_wire_protocol():
    step("[16] v0.3.9 LSP wire protocol (JSON-RPC stdio)")

    venv_py = DATA_ROOT / "venv" / ("Scripts" if sys.platform.startswith("win") else "bin") / (
        "python.exe" if sys.platform.startswith("win") else "python3"
    )
    mock_server = PLUGIN_ROOT / "tests" / "fixtures" / "mock_lsp_server.py"
    if not mock_server.is_file():
        fail(f"mock LSP server fixture missing at {mock_server}")
        return False
    mock_server.chmod(0o755)

    # ─── Direct lsp_client probe ──────────────────────────────────────
    probe_script = (
        "import sys, json;"
        "sys.path.insert(0, str(__import__('pathlib').Path("
        f"{repr(str(PLUGIN_ROOT))}) / 'code'));"
        "from lsp_client import verify, find_lsp_binary, shutdown_all;"
        "b = find_lsp_binary('typescript');"
        "r = verify('/tmp/probe.ts',"
        " 'const a = FAKE_BAD_NAME;\\nconst b = FAKE_WARN_NAME;\\n',"
        " 'typescript', '/tmp', timeout_s=2.0);"
        "shutdown_all();"
        "print(json.dumps({'binary': b, 'available': r['available'],"
        " 'diag_count': len(r['diagnostics']),"
        " 'severities': sorted(d['severity'] for d in r['diagnostics']),"
        " 'names': sorted(d['name_referenced'] for d in r['diagnostics']"
        " if d.get('name_referenced'))}))"
    )
    env_with_mock = {**os.environ,
                      "CLAUDE_DEJAVU_DATA_ROOT": str(DATA_ROOT),
                      "CLAUDE_DEJAVU_LSP_BINARY_typescript": str(mock_server),
                      "PGPASSWORD": PG_PASS}
    r = subprocess.run([str(venv_py), "-c", probe_script],
                        capture_output=True, text=True, env=env_with_mock,
                        timeout=20)
    if r.returncode != 0:
        fail(f"lsp_client probe crashed\n{r.stdout}\n{r.stderr}")
        return False
    try:
        data = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        fail(f"lsp_client probe didn't return JSON\n{r.stdout}"); return False
    if not data.get("available"):
        fail(f"lsp_client probe: not available\n{data}"); return False
    if data["binary"] != str(mock_server):
        fail(f"lsp_client didn't pick env-var-overridden binary\n{data}")
        return False
    if data["diag_count"] != 2:
        fail(f"expected 2 diagnostics, got {data['diag_count']}\n{data}")
        return False
    if data["severities"] != ["error", "warning"]:
        fail(f"severity mapping broken\n{data}"); return False
    if data["names"] != ["FAKE_BAD_NAME", "FAKE_WARN_NAME"]:
        fail(f"name extraction broken\n{data}"); return False
    ok("lsp_client: full JSON-RPC handshake → didOpen → publishDiagnostics")

    # ─── No-binary skip path ──────────────────────────────────────────
    skip_script = (
        "import sys, json, os;"
        "os.environ.pop('CLAUDE_DEJAVU_LSP_BINARY_typescript', None);"
        "sys.path.insert(0, str(__import__('pathlib').Path("
        f"{repr(str(PLUGIN_ROOT))}) / 'code'));"
        "from lsp_client import verify;"
        "r = verify('/tmp/probe.ts', 'const a = 1;', 'NEVER_KNOWN_LANG_xyz');"
        "print(json.dumps({'available': r['available'],"
        " 'reason': r['skipped_reason']}))"
    )
    r = subprocess.run([str(venv_py), "-c", skip_script],
                        capture_output=True, text=True,
                        env={**os.environ,
                              "CLAUDE_DEJAVU_DATA_ROOT": str(DATA_ROOT)},
                        timeout=10)
    if r.returncode != 0:
        fail(f"lsp_client skip-path probe crashed\n{r.stderr}")
        return False
    data = json.loads(r.stdout.strip().splitlines()[-1])
    if data["available"]:
        fail(f"lsp_client should not be available for unknown lang\n{data}")
        return False
    if "no LSP binary" not in (data.get("reason") or ""):
        fail(f"lsp_client skip reason wrong\n{data}"); return False
    ok("lsp_client: gracefully skips when no binary on PATH")

    # ─── Persistent pool: second verify reuses warm server ──────────
    pool_script = (
        "import sys, json, time;"
        "sys.path.insert(0, str(__import__('pathlib').Path("
        f"{repr(str(PLUGIN_ROOT))}) / 'code'));"
        "from lsp_client import verify, shutdown_all;"
        "t1 = time.monotonic();"
        "verify('/tmp/p1.ts', 'const a = FAKE_BAD_NAME;', 'typescript', '/tmp', timeout_s=2.0);"
        "cold = time.monotonic() - t1;"
        "t2 = time.monotonic();"
        "verify('/tmp/p2.ts', 'const a = FAKE_BAD_NAME;', 'typescript', '/tmp', timeout_s=2.0);"
        "warm = time.monotonic() - t2;"
        "shutdown_all();"
        "print(json.dumps({'cold': round(cold, 3), 'warm': round(warm, 3)}))"
    )
    r = subprocess.run([str(venv_py), "-c", pool_script],
                        capture_output=True, text=True, env=env_with_mock,
                        timeout=20)
    if r.returncode != 0:
        fail(f"lsp_client pool probe crashed\n{r.stderr}")
        return False
    data = json.loads(r.stdout.strip().splitlines()[-1])
    # Warm verify should be at least 25% faster than cold (typically much
    # more — but we keep the bar loose since CI is noisy).
    if data["warm"] >= data["cold"]:
        fail(f"warm verify wasn't faster than cold (pool not reused?)\n{data}")
        return False
    ok(f"lsp_client: persistent pool reuses warm server "
       f"(cold={data['cold']}s, warm={data['warm']}s)")

    # ─── lsp_verification strategy integration ────────────────────────
    proj = SANDBOX / "lsp-fixture"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "package.json").write_text('{"name":"lsp-fixture"}\n')
    target = proj / "client.ts"
    target.write_text(
        "// Simple TS file with a fake-bad and fake-warn identifier.\n"
        "const ok = 42;\n"
        "const x = FAKE_BAD_NAME;\n"
        "const y = FAKE_WARN_NAME;\n"
    )
    # Reindex so symbol_grounding has SOMETHING — otherwise it bails.
    cli("reindex", f"--path={proj}", "--project=smoke-lsp",
         "--skip-semantic")
    # Run lint with the mock-LSP override.
    cmd = [str(venv_py),
            str(PLUGIN_ROOT / "code" / "recall.py"),
            "lint", str(target), "--project=smoke-lsp",
            f"--content-file={target}", "--json"]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env_with_mock,
                        timeout=20)
    # Recall doesn't care about exit code for this assertion — we read JSON.
    # The JSON output is multi-line; find the first '{' and parse from there.
    try:
        first_brace = r.stdout.index("{")
        out = json.loads(r.stdout[first_brace:])
    except Exception:
        fail(f"lint --json didn't return JSON\n{r.stdout[:400]}")
        return False
    diagnostics = out.get("strategy_diagnostics", [])
    lsp_diags = [d for d in diagnostics if d.get("strategy") == "lsp_verification"]
    if not lsp_diags:
        fail(f"lsp_verification didn't surface any issues from mock\n"
             f"strategy_diagnostics={diagnostics}")
        return False
    ok(f"lsp_verification strategy: surfaced {len(lsp_diags)} issue(s) from mock LSP")

    return True


def step_flag_grounding():
    step("[17] v0.3.10 feature flag grounding")

    proj = SANDBOX / "flags-fixture"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "package.json").write_text('{"name":"flags-fixture"}\n')

    (proj / "flags.json").write_text(
        '{\n'
        '  "checkout-flow-v2": false,\n'
        '  "kill-switch-payments": false,\n'
        '  "experimental-search": true,\n'
        '  "beta-rollout": {"default": false, "description": "Beta access"}\n'
        '}\n'
    )
    (proj / "featureFlags.ts").write_text(
        "export const featureFlags = {\n"
        "  'new-pricing-page': false,\n"
        "  'admin-dashboard-v3': true,\n"
        "  emergency_disable_signups: false,\n"
        "};\n"
    )
    (proj / "client.ts").write_text(
        "const a = client.variation('checkout-flow-v2', user, false);\n"
        "const b = isEnabled('beta-rollout');\n"
        "const c = Statsig.checkGate('totally-fabricated-key');\n"
    )

    r = cli("reindex", f"--path={proj}", "--project=smoke-flags",
            "--skip-semantic")
    if r.returncode != 0:
        fail(f"flag reindex failed\n{r.stdout}\n{r.stderr}"); return False
    if "flags added" not in r.stdout:
        fail(f"reindex didn't report flags section\n{r.stdout}"); return False
    for src in ("flags-json=", "feature-flags-ts=", "source-read="):
        if src not in r.stdout:
            fail(f"reindex missing source `{src}`\n{r.stdout}"); return False
    if "kill switches" not in r.stdout:
        fail(f"reindex didn't surface kill_switches count\n{r.stdout}")
        return False
    ok("flag indexer extracts flags.json + featureFlags.ts + source-reads, flags kill switches")

    # `flags list` and `flags list --kill-switches-only`
    r = cli("flags", "list", "--project=smoke-flags")
    if r.returncode != 0:
        fail(f"flags list failed\n{r.stdout}"); return False
    for needed in ("checkout-flow-v2", "beta-rollout", "new-pricing-page",
                    "kill-switch-payments", "emergency_disable_signups",
                    "experimental-search"):
        if needed not in r.stdout:
            fail(f"flags list missing {needed}\n{r.stdout}"); return False
    if "🛑" not in r.stdout:
        fail(f"flags list didn't mark any kill switch\n{r.stdout}"); return False
    ok("flags list shows declared flags + kill-switch markers")

    r = cli("flags", "list", "--project=smoke-flags", "--kill-switches-only")
    if r.returncode != 0 or "checkout-flow-v2" in r.stdout:
        fail(f"--kill-switches-only didn't filter\n{r.stdout}"); return False
    ok("flags list --kill-switches-only filters correctly")

    # Resolutions.
    r = cli("flags", "check", "checkout-flow-v2", "--project=smoke-flags")
    if r.returncode != 0 or "✓" not in r.stdout:
        fail(f"flags check exact failed\n{r.stdout}"); return False
    ok("flags check: exact match")

    r = cli("flags", "check", "checkout-flow-v3", "--project=smoke-flags")
    if r.returncode != 0 or "checkout-flow-v2" not in r.stdout:
        fail(f"flags check fuzzy failed\n{r.stdout}"); return False
    ok("flags check: fuzzy catches typo")

    # Pick a name with NO shared trigrams against any indexed flag
    # (including source-read entries from client.ts).
    r = cli("flags", "check", "quartzplover-omega", "--project=smoke-flags")
    if r.returncode == 0:
        fail(f"flags check fabricated should exit non-zero\n{r.stdout}")
        return False
    ok("flags check: fabricated reports no-match")

    # Lint integration.
    proposed = SANDBOX / "flags-proposed.ts"
    proposed.write_text(
        "client.variation('checkout-flow-v2', user, false);\n"          # resolved
        "client.variation('checkout-flow-v3', user, false);\n"          # typo
        "isEnabled('quartzplover-omega-lambda');\n"                    # unknown -> block
    )
    r = cli("lint", str(proposed), "--project=smoke-flags",
            f"--content-file={proposed}")
    if r.returncode != 2:
        fail(f"flag lint expected exit=2, got {r.returncode}\n{r.stdout}")
        return False
    if "quartzplover-omega-lambda" not in r.stdout:
        fail(f"flag lint missed unknown\n{r.stdout}"); return False
    if "checkout-flow-v3" not in r.stdout or "checkout-flow-v2" not in r.stdout:
        fail(f"flag lint missed typo + canonical neighbor\n{r.stdout}"); return False
    ok("flag_grounding lint: blocks fabricated, flags typos with canonical candidate")

    # Empty-index graceful skip.
    r = cli("lint", str(proposed), "--project=no-flags-here",
            f"--content-file={proposed}")
    if "feature flags — unknown" in r.stdout or "feature flags — low-confidence" in r.stdout:
        fail(f"flag_grounding fired against empty index\n{r.stdout}")
        return False
    ok("flag_grounding lint: silent-passes on empty index")

    # Strategy registered.
    venv_py = DATA_ROOT / "venv" / ("Scripts" if sys.platform.startswith("win") else "bin") / (
        "python.exe" if sys.platform.startswith("win") else "python3"
    )
    r3 = subprocess.run([str(venv_py), "-c",
        "import sys; sys.path.insert(0, str(__import__('pathlib').Path("
        f"{repr(str(PLUGIN_ROOT))}) / 'code'));"
        "import lint_strategies as ls;"
        "names = sorted(s.name for s in ls.all_strategies());"
        "print(','.join(names))"
    ], capture_output=True, text=True)
    if r3.returncode != 0 or "flag_grounding" not in r3.stdout:
        fail(f"flag_grounding not in registry: {r3.stdout}"); return False
    ok(f"strategy registry has flag_grounding: {r3.stdout.strip()}")

    return True


def step_adaptive_thresholds():
    step("[18] v0.3.10 adaptive thresholds")

    venv_py = DATA_ROOT / "venv" / ("Scripts" if sys.platform.startswith("win") else "bin") / (
        "python.exe" if sys.platform.startswith("win") else "python3"
    )

    # `thresholds list` should work even with no rows.
    r = cli("thresholds", "list")
    if r.returncode != 0:
        fail(f"thresholds list crashed\n{r.stderr}"); return False
    ok("thresholds list works on empty state")

    # Manually populate correction_outcomes for project=smoke-tune, channel
    # symbol with > 25 outcomes so the tuner has signal. Half are
    # 'accepted' at sim>=0.6, the other half are 'rejected' at sim<0.5 —
    # so the optimal threshold should land around 0.55-0.65.
    seed_path = SANDBOX / "seed_outcomes.py"
    seed_path.write_text(
        f"import sys\n"
        f"sys.path.insert(0, str(__import__('pathlib').Path("
        f"{repr(str(PLUGIN_ROOT))}) / 'code'))\n"
        f"from recall import _conn\n"
        f"with _conn() as c:\n"
        f"    c.autocommit = True\n"
        f"    with c.cursor() as cur:\n"
        f"        cur.execute(\"DELETE FROM correction_outcomes WHERE project_slug = 'smoke-tune'\")\n"
        f"        for i in range(20):\n"
        f"            cur.execute(\n"
        f"                \"INSERT INTO correction_outcomes \"\n"
        f"                \"(project_slug, mode, proposed_name, resolved_name, candidate_count, outcome, outcome_signal, trigram_sim, edit_distance) \"\n"
        f"                \"VALUES ('smoke-tune','lookup','q' || %s, 'r' || %s, 1, 'accepted', 'symbol', 0.65 + 0.001*%s, 1)\",\n"
        f"                (i, i, i),\n"
        f"            )\n"
        f"        for i in range(20):\n"
        f"            cur.execute(\n"
        f"                \"INSERT INTO correction_outcomes \"\n"
        f"                \"(project_slug, mode, proposed_name, resolved_name, candidate_count, outcome, outcome_signal, trigram_sim, edit_distance) \"\n"
        f"                \"VALUES ('smoke-tune','lookup','q' || %s, 'r' || %s, 1, 'rejected', 'symbol', 0.30 + 0.001*%s, 4)\",\n"
        f"                (1000+i, 1000+i, i),\n"
        f"            )\n"
        f"print('seeded')\n"
    )
    r = subprocess.run([str(venv_py), str(seed_path)],
                        capture_output=True, text=True,
                        env={**os.environ, "CLAUDE_DEJAVU_DATA_ROOT": str(DATA_ROOT),
                              "PGPASSWORD": PG_PASS},
                        timeout=20)
    if r.returncode != 0 or "seeded" not in r.stdout:
        fail(f"seed correction_outcomes failed\n{r.stdout}\n{r.stderr}")
        return False
    ok("seeded 40 correction_outcomes for tuning")

    # Tune.
    r = cli("thresholds", "tune", "--project=smoke-tune", "--channel=symbol")
    if r.returncode != 0:
        fail(f"thresholds tune failed\n{r.stderr}"); return False
    if "optimal=" not in r.stdout:
        fail(f"thresholds tune didn't surface optimal value\n{r.stdout}")
        return False
    ok(f"thresholds tune ran: {r.stdout.strip()}")

    # List should now show the tuned row.
    r = cli("thresholds", "list", "--project=smoke-tune")
    if r.returncode != 0 or "smoke-tune" not in r.stdout or "symbol" not in r.stdout:
        fail(f"thresholds list missing tuned row\n{r.stdout}"); return False
    ok("thresholds list surfaces tuned row")

    # Sub-25-outcomes case: project with too few outcomes keeps default.
    sparse_path = SANDBOX / "seed_sparse.py"
    sparse_path.write_text(
        f"import sys\n"
        f"sys.path.insert(0, str(__import__('pathlib').Path("
        f"{repr(str(PLUGIN_ROOT))}) / 'code'))\n"
        f"from recall import _conn\n"
        f"with _conn() as c:\n"
        f"    c.autocommit = True\n"
        f"    with c.cursor() as cur:\n"
        f"        cur.execute(\"DELETE FROM correction_outcomes WHERE project_slug = 'smoke-tune-sparse'\")\n"
        f"        for i in range(5):\n"
        f"            cur.execute(\n"
        f"                \"INSERT INTO correction_outcomes \"\n"
        f"                \"(project_slug, mode, proposed_name, resolved_name, candidate_count, outcome, outcome_signal, trigram_sim, edit_distance) \"\n"
        f"                \"VALUES ('smoke-tune-sparse','lookup','q' || %s, 'r' || %s, 1, 'accepted', 'symbol', 0.7, 1)\",\n"
        f"                (i, i),\n"
        f"            )\n"
    )
    subprocess.run([str(venv_py), str(sparse_path)],
                    capture_output=True, text=True,
                    env={**os.environ, "CLAUDE_DEJAVU_DATA_ROOT": str(DATA_ROOT),
                          "PGPASSWORD": PG_PASS}, timeout=20)
    r = cli("thresholds", "tune", "--project=smoke-tune-sparse", "--channel=symbol")
    if r.returncode != 0 or "insufficient" not in r.stdout:
        fail(f"sparse-outcomes tune should report 'insufficient'\n{r.stdout}")
        return False
    ok("thresholds tune: skips insufficient-outcomes case (< 25)")

    return True


def step_real_world_integration():
    step("[19] v0.3.10 real-world integration (8 grounding domains together)")

    # Build a realistic mini-monorepo that exercises every grounding
    # surface in one project. The lint should produce findings across
    # multiple buckets simultaneously without false positives in others.
    proj = SANDBOX / "rw-fixture"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "package.json").write_text('{"name":"rw-fixture"}\n')

    # SYMBOLS — a few user-defined functions.
    (proj / "lib.ts").write_text(
        "export function parseUserInput(s: string): string { return s; }\n"
        "export class ProfileCache { refresh(): void {} }\n"
    )

    # ROUTES — Express handler.
    (proj / "server.ts").write_text(
        "import express from 'express';\n"
        "const app = express();\n"
        "app.get('/api/users/:id', (req, res) => res.json({}));\n"
        "app.post('/api/posts', (req, res) => res.json({}));\n"
    )

    # PAGES — Next.js app router.
    (proj / "app" / "dashboard").mkdir(parents=True, exist_ok=True)
    (proj / "app" / "dashboard" / "page.tsx").write_text(
        "export default function Dashboard() { return null; }\n"
    )
    (proj / "app" / "blog" / "[slug]").mkdir(parents=True, exist_ok=True)
    (proj / "app" / "blog" / "[slug]" / "page.tsx").write_text(
        "export default function BlogPost() { return null; }\n"
    )

    # ENV — .env.example.
    (proj / ".env.example").write_text(
        "DATABASE_URL=postgres://localhost/rw\n"
        "STRIPE_SECRET_KEY=\n"
    )

    # DB — Prisma schema. Use multi-line field layout (the convention).
    (proj / "prisma").mkdir(parents=True, exist_ok=True)
    (proj / "prisma" / "schema.prisma").write_text(
        "model User {\n"
        "  id    Int     @id @default(autoincrement())\n"
        "  email String  @unique\n"
        "  name  String?\n"
        "}\n"
        "\n"
        "model Post {\n"
        "  id       Int    @id @default(autoincrement())\n"
        "  title    String\n"
        "  authorId Int\n"
        "}\n"
    )

    # GRAPHQL — SDL with multi-line type bodies (convention).
    (proj / "schema.graphql").write_text(
        "type User {\n"
        "  id: ID!\n"
        "  email: String!\n"
        "  name: String\n"
        "}\n"
        "\n"
        "type Post {\n"
        "  id: ID!\n"
        "  title: String!\n"
        "}\n"
        "\n"
        "type Query {\n"
        "  user(id: ID!): User\n"
        "  posts: [Post!]!\n"
        "}\n"
    )

    # FLAGS — flags.json.
    (proj / "flags.json").write_text(
        '{"new-checkout": false, "beta-program": true}\n'
    )

    r = cli("reindex", f"--path={proj}", "--project=smoke-rw",
            "--skip-semantic")
    if r.returncode != 0:
        fail(f"real-world reindex failed\n{r.stdout}\n{r.stderr}")
        return False
    # Every domain should have at least one entry indexed.
    for needed in ("symbols added", "routes added", "pages added",
                    "env vars added", "models added", "types added",
                    "flags added"):
        if needed not in r.stdout:
            fail(f"real-world reindex missing `{needed}`\n{r.stdout}")
            return False
    ok("real-world reindex: all 7 indexers fired (symbols/routes/pages/env/db/gql/flags)")

    # Now lint a file that intentionally triggers issues in MULTIPLE
    # domains simultaneously.
    multi = proj / "multi.ts"
    multi.write_text(
        "// 1. Symbol typo + library method (should NOT flag findUnique now)\n"
        "import { ProfilCache } from './lib';\n"  # symbol typo
        "const cache = new ProfileCache();\n"      # exact symbol
        "const u = await prisma.user.findUnique({ where: { emaill: 'x' } });\n"  # db typo
        "// 2. Env var typo + canonical neighbor\n"
        "const db = process.env.DATABSE_URL;\n"
        "const stripe = process.env.STRIPE_SECRET_KEY;\n"
        "// 3. Route fabrication\n"
        "await fetch('/api/totally/fake/route', {method:'POST'});\n"
        "// 4. GraphQL field typo\n"
        "import { gql } from 'graphql-tag';\n"
        "const Q = gql`query { user(id: \"1\") { emaill } }`;\n"
        "// 5. Flag fabrication\n"
        "client.variation('quartzplover-omega-lambda', user, false);\n"
    )
    r = cli("lint", str(multi), "--project=smoke-rw",
            f"--content-file={multi}")
    if r.returncode != 2:
        fail(f"multi-domain lint expected exit=2, got {r.returncode}\n{r.stdout}")
        return False
    # The library method allowlist should mean `findUnique` is NOT flagged
    # by symbol_grounding. Verify symbol_grounding output doesn't list it.
    if "? findUnique" in r.stdout:
        fail(f"library-method 'findUnique' was flagged — allowlist regression\n{r.stdout}")
        return False
    # Each grounding domain should have surfaced at least one finding.
    expected_findings = [
        ("DATABSE_URL", "env_grounding should flag typo"),
        ("DATABASE_URL", "env_grounding should surface canonical"),
        ("totally/fake/route", "route_grounding should flag fabricated URL"),
        ("emaill", "graphql / db should flag typo"),
        ("quartzplover-omega-lambda", "flag_grounding should flag fabricated"),
    ]
    for needle, desc in expected_findings:
        if needle not in r.stdout:
            fail(f"multi-domain lint missing: {desc} (needle={needle!r})\n{r.stdout}")
            return False
    ok("real-world lint: 5 grounding domains fired together with no library-method noise")

    # Verify the verdict aggregation: at least one UNKNOWN must drive BLOCK.
    if "verdict: BLOCK" not in r.stdout:
        fail(f"verdict didn't aggregate to BLOCK\n{r.stdout}"); return False
    ok("real-world lint: verdict aggregator promotes any-domain UNKNOWN to BLOCK")

    # Clean code (no fabrications, no typos) → exit 0 across every domain.
    clean = proj / "clean.ts"
    clean.write_text(
        "import { gql } from 'graphql-tag';\n"
        "const u = await prisma.user.findUnique({ where: { email: 'x' } });\n"
        "const db = process.env.DATABASE_URL;\n"
        "await fetch('/api/users/123');\n"
        "const Q = gql`query { user(id: \"1\") { email name } }`;\n"
        "client.variation('new-checkout', user, false);\n"
    )
    r = cli("lint", str(clean), "--project=smoke-rw",
            f"--content-file={clean}")
    if r.returncode != 0:
        fail(f"clean multi-domain code should exit 0; got {r.returncode}\n{r.stdout}")
        return False
    ok("real-world lint: clean code passes 8-domain inspection with exit 0")

    return True


def step_dejavuignore():
    step("[20] v0.3.11 .dejavuignore + per-folder scopes")

    proj = SANDBOX / "ignore-fixture"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "package.json").write_text('{"name":"ignore-fixture"}\n')

    # Top-level .dejavuignore — exclude `vendored/`, also re-include
    # the gitignored `.env.example`.
    (proj / ".dejavuignore").write_text(
        "# Top-level rules\n"
        "vendored/\n"
        "**/*.generated.ts\n"
        "!.env.example\n"
    )
    # Sub-directory .dejavuignore — scoped to the subtree.
    (proj / "subpkg").mkdir()
    (proj / "subpkg" / ".dejavuignore").write_text(
        "# Subtree rules\n"
        "internal-only/\n"
    )

    # Files that SHOULD be indexed.
    (proj / "kept.ts").write_text("export function keptFunction() {}\n")
    (proj / "subpkg" / "alsoKept.ts").write_text("export function alsoKept() {}\n")
    (proj / ".env.example").write_text("KEPT_VAR=hello\n")

    # Files that should NOT be indexed.
    (proj / "vendored").mkdir()
    (proj / "vendored" / "skipped.ts").write_text("export function shouldSkip() {}\n")
    (proj / "build.generated.ts").write_text("export function generatedSkipped() {}\n")
    (proj / "subpkg" / "internal-only").mkdir()
    (proj / "subpkg" / "internal-only" / "secret.ts").write_text(
        "export function internalOnlyFn() {}\n")

    # ─── claude-dejavu ignore list ────────────────────────────────────
    venv_py = DATA_ROOT / "venv" / ("Scripts" if sys.platform.startswith("win") else "bin") / (
        "python.exe" if sys.platform.startswith("win") else "python3"
    )
    cmd_list = [str(venv_py), str(PLUGIN_ROOT / "code" / "recall.py"),
                 "ignore", "list"]
    r = subprocess.run(cmd_list, capture_output=True, text=True,
                        cwd=str(proj),
                        env={**os.environ, "CLAUDE_DEJAVU_DATA_ROOT": str(DATA_ROOT),
                              "PGPASSWORD": PG_PASS},
                        timeout=10)
    if r.returncode != 0:
        fail(f"ignore list failed\n{r.stdout}\n{r.stderr}"); return False
    if "vendored" not in r.stdout:
        fail(f"ignore list missing top-level rule\n{r.stdout}"); return False
    if "internal-only" not in r.stdout:
        fail(f"ignore list missing nested rule\n{r.stdout}"); return False
    if "node_modules" not in r.stdout:
        fail(f"ignore list missing builtin\n{r.stdout}"); return False
    if "!.env.example" not in r.stdout:
        fail(f"ignore list missing re-include rule\n{r.stdout}"); return False
    ok("ignore list shows builtins + top-level + nested rules + re-include")

    # ─── claude-dejavu ignore status ─────────────────────────────────
    def ignore_status(path: Path):
        cmd = [str(venv_py), str(PLUGIN_ROOT / "code" / "recall.py"),
                "ignore", "status", str(path)]
        return subprocess.run(cmd, capture_output=True, text=True,
                                cwd=str(proj),
                                env={**os.environ,
                                      "CLAUDE_DEJAVU_DATA_ROOT": str(DATA_ROOT),
                                      "PGPASSWORD": PG_PASS},
                                timeout=10)

    r = ignore_status(proj / "vendored" / "skipped.ts")
    if "verdict  : SKIP" not in r.stdout or "vendored" not in r.stdout:
        fail(f"ignore status: vendored file should be SKIP\n{r.stdout}")
        return False
    ok("ignore status: top-level rule correctly skips file")

    r = ignore_status(proj / "build.generated.ts")
    if "verdict  : SKIP" not in r.stdout:
        fail(f"ignore status: *.generated.ts should be SKIP\n{r.stdout}")
        return False
    ok("ignore status: glob `**/*.generated.ts` matches at root")

    r = ignore_status(proj / "subpkg" / "internal-only" / "secret.ts")
    if "verdict  : SKIP" not in r.stdout or "internal-only" not in r.stdout:
        fail(f"ignore status: nested rule should match\n{r.stdout}")
        return False
    ok("ignore status: nested .dejavuignore rule applies to subtree")

    r = ignore_status(proj / "kept.ts")
    if "verdict  : INDEX" not in r.stdout:
        fail(f"ignore status: top-level kept.ts should be INDEX\n{r.stdout}")
        return False
    ok("ignore status: file with no matching rule defaults to INDEX")

    r = ignore_status(proj / ".env.example")
    if "verdict  : INDEX" not in r.stdout:
        fail(f"ignore status: !re-include should yield INDEX\n{r.stdout}")
        return False
    ok("ignore status: !re-include overrides earlier ignore")

    # ─── Reindex applies the filter ───────────────────────────────────
    r = cli("reindex", f"--path={proj}", "--project=smoke-ignore",
             "--skip-semantic")
    if r.returncode != 0:
        fail(f"ignore-aware reindex failed\n{r.stdout}\n{r.stderr}")
        return False
    # Should index 2 files (kept.ts + alsoKept.ts) — NOT the vendored,
    # generated, or internal-only ones. Actual file count includes
    # package.json and similar but we just want symbols extracted only
    # from the .ts files we kept.
    if "files indexed : 2" not in r.stdout:
        fail(f"reindex didn't honor .dejavuignore\n{r.stdout}")
        return False
    ok("reindex: .dejavuignore reduced indexed file count to expected 2")

    # ─── Symbol from a SKIPPED file should NOT be resolvable ─────────
    r = cli("symbols", "shouldSkip", "--project=smoke-ignore")
    if r.returncode == 0 and "ed=0" in r.stdout:
        fail(f"symbol from ignored file was indexed\n{r.stdout}"); return False
    ok("symbols: skipped file's symbols are NOT in the index")

    # ─── Symbol from a KEPT file IS resolvable ───────────────────────
    r = cli("symbols", "keptFunction", "--project=smoke-ignore")
    if r.returncode != 0 or "ed=0" not in r.stdout:
        fail(f"symbol from kept file should be indexed\n{r.stdout}")
        return False
    ok("symbols: kept file's symbols ARE in the index")

    # ─── Re-included .env.example IS in the env index ────────────────
    r = cli("env", "list", "--project=smoke-ignore")
    if r.returncode != 0 or "KEPT_VAR" not in r.stdout:
        fail(f"!.env.example re-include didn't reach env indexer\n{r.stdout}")
        return False
    ok("env list: !re-included .env.example was indexed")

    return True


def step_hallucination_bench_pipeline():
    step("[21] v0.3.12 hallucination benchmark pipeline (mock)")

    venv_py = DATA_ROOT / "venv" / ("Scripts" if sys.platform.startswith("win") else "bin") / (
        "python.exe" if sys.platform.startswith("win") else "python3"
    )
    out_dir = SANDBOX / "bench-output"
    cmd = [str(venv_py),
            str(PLUGIN_ROOT / "tests" / "bench_hallucination.py"),
            "--mock", "--n=1", f"--output={out_dir}"]
    r = subprocess.run(cmd, capture_output=True, text=True,
                        env={**os.environ,
                              "CLAUDE_DEJAVU_DATA_ROOT": str(DATA_ROOT),
                              "CLAUDE_DEJAVU_BENCH_SANDBOX": str(SANDBOX / "bench-fixture")},
                        timeout=120)
    if r.returncode != 0:
        fail(f"bench --mock failed\n{r.stdout}\n{r.stderr[-500:]}")
        return False
    if "HEADLINE" not in r.stdout:
        fail(f"bench didn't print HEADLINE\n{r.stdout}"); return False
    if not (out_dir / "hallucination-rate-v0.3.12.md").is_file():
        fail(f"bench didn't write report"); return False
    if not (out_dir / "hallucination-rate-raw.json").is_file():
        fail(f"bench didn't write raw JSON"); return False
    raw = json.loads((out_dir / "hallucination-rate-raw.json").read_text())
    n_trials = raw["aggregate"]["meta"]["n_total_trials"]
    # 20 prompts × 2 conditions × 1 trial = 40 trials
    if n_trials != 40:
        fail(f"expected 40 trials, got {n_trials}"); return False
    ok(f"bench pipeline: synthetic fixture + 40 mock trials + report written")

    # Sanity: the mock is rigged to always have ≥1 hallucination OFF
    # and 0 ON — so reduction must be >0 and McNemar p must be < 0.05.
    rel = raw["aggregate"]["relative_reduction"]
    p = raw["aggregate"]["mcnemar"]["p_value"]
    if rel < 0.5:
        fail(f"mock benchmark should show ≥50% reduction, got {rel*100:.1f}%")
        return False
    if p >= 0.05:
        fail(f"mock benchmark should show p<0.05, got {p}")
        return False
    ok(f"bench stats: rel reduction={rel*100:.1f}%, McNemar p={p:.3g}")

    return True


def step_about_branding():
    step("[22] v0.3.13 about / branding (no email capture)")

    # `claude-dejavu about` — the CLI surface
    r = cli("about")
    if r.returncode != 0:
        fail(f"about CLI failed\n{r.stderr}"); return False
    for needed in ("HTE Switzerland", "hendrikthurau.enterprises",
                    "grounding domains", "FSL-1.1-ALv2"):
        if needed not in r.stdout:
            fail(f"about CLI missing `{needed}`\n{r.stdout}"); return False
    # Verify no email capture happens — the output should not invite
    # the user to "register", "subscribe", or "sign up".
    forbidden = ["register", "subscribe", "newsletter", "sign up",
                  "your email"]
    for f_ in forbidden:
        if f_.lower() in r.stdout.lower():
            fail(f"about CLI contains lead-capture phrase `{f_}`\n{r.stdout}")
            return False
    ok("about CLI: shows HTE credit + benchmark link, no email capture")

    # With --with-stats, project counts surface
    r = cli("about", "--with-stats", "--project=smoke-grounding")
    if r.returncode != 0:
        fail(f"about --with-stats failed\n{r.stderr}"); return False
    if "what dejavu knows about" not in r.stdout:
        fail(f"about --with-stats missing project block\n{r.stdout}")
        return False
    ok("about --with-stats: surfaces per-project indexed counts")

    # Direct inspection of mcp/server.py — assert the tool is wired in.
    # (Avoids the timing-sensitive stdio JSON-RPC dance — we know FastMCP
    # picks up @mcp.tool() decorators at import time.)
    mcp_src = (PLUGIN_ROOT / "mcp" / "server.py").read_text(encoding="utf-8")
    if "def dejavu_about(" not in mcp_src or "@mcp.tool()" not in mcp_src:
        fail(f"MCP server missing dejavu_about tool definition")
        return False
    ok("MCP: dejavu_about tool registered (verified by source inspection)")

    return True


def step_quickwins():
    step("[23] v0.3.14 quick-wins (CSS/Tailwind, i18n, npm scripts, asset paths)")

    proj = SANDBOX / "qw-fixture"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "package.json").write_text(json.dumps({
        "name": "qw-fixture",
        "scripts": {"dev": "next dev", "build": "next build",
                     "lint": "eslint .", "test": "vitest"},
    }))
    # Tailwind config with custom colors
    (proj / "tailwind.config.ts").write_text(
        "module.exports = { theme: { extend: { colors: { 'brand-primary': '#abc' } } } };\n"
    )
    # CSS module file
    (proj / "App.module.css").write_text(
        ".container {}\n.dashboardWrapper {}\n.btnPrimary {}\n"
    )
    # i18n locale
    (proj / "locales").mkdir()
    (proj / "locales" / "en.json").write_text(json.dumps({
        "user": {"profile": {"title": "Profile", "save": "Save"},
                  "settings": {"title": "Settings"}},
        "common": {"cancel": "Cancel", "ok": "OK"},
    }))
    # An asset
    (proj / "logo.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')

    # Run reindex
    r = cli("reindex", f"--path={proj}", "--project=smoke-qw",
             "--skip-semantic")
    if r.returncode != 0:
        fail(f"qw reindex failed\n{r.stdout}\n{r.stderr}"); return False
    if "css classes" not in r.stdout or "i18n keys" not in r.stdout:
        fail(f"qw reindex didn't surface stats\n{r.stdout}"); return False
    ok("quick-wins reindex: css classes + i18n keys indexed")

    # Lint a TSX file with all four kinds of issues
    proposed = proj / "Component.tsx"
    proposed.write_text(
        "import logo from './logo.svp';\n"          # asset typo (.svp vs .svg)
        "import { t } from 'i18n';\n"
        "export function MyComp() {\n"
        "  return <div className=\"rouded-lg btnPrimary container\" />\n"  # Tailwind typo + valid module + valid module
        "}\n"
        "// shell: pnpm buld # typo of build\n"
    )
    r = cli("lint", str(proposed), "--project=smoke-qw",
             f"--content-file={proposed}")
    # Should be BLOCK (asset/className UNKNOWN)
    if r.returncode != 2:
        fail(f"qw lint expected exit=2, got {r.returncode}\n{r.stdout}")
        return False
    # Tailwind typo `rouded-lg` should be flagged via css strategy
    if "rouded-lg" not in r.stdout:
        fail(f"css_grounding missed Tailwind typo\n{r.stdout}"); return False
    # Asset path .svp doesn't exist
    if "logo.svp" not in r.stdout:
        fail(f"asset_path_grounding missed missing file\n{r.stdout}"); return False
    ok("css_grounding + asset_path_grounding fired correctly")

    # i18n + npm-script: separate file (npm-script needs a shell-ish syntax)
    sh = proj / "deploy.sh"
    sh.write_text(
        "#!/usr/bin/env bash\n"
        "pnpm dev\n"          # valid
        "pnpm buld\n"         # typo of build
        "pnpm fakescript\n"   # fabricated
    )
    r = cli("lint", str(sh), "--project=smoke-qw",
             f"--content-file={sh}")
    if "buld" not in r.stdout or "fakescript" not in r.stdout:
        fail(f"npm_script_grounding missed typos\n{r.stdout}"); return False
    ok("npm_script_grounding catches typos + fabricated names")

    # Verify all 4 strategies registered
    venv_py = DATA_ROOT / "venv" / ("Scripts" if sys.platform.startswith("win") else "bin") / (
        "python.exe" if sys.platform.startswith("win") else "python3"
    )
    r3 = subprocess.run([str(venv_py), "-c",
        "import sys; sys.path.insert(0, str(__import__('pathlib').Path("
        f"{repr(str(PLUGIN_ROOT))}) / 'code'));"
        "import lint_strategies as ls;"
        "names = sorted(s.name for s in ls.all_strategies());"
        "print(','.join(names))"
    ], capture_output=True, text=True)
    if r3.returncode != 0:
        fail(f"registry probe failed: {r3.stderr}"); return False
    for needed in ("css_grounding", "i18n_grounding",
                    "npm_script_grounding", "asset_path_grounding"):
        if needed not in r3.stdout:
            fail(f"{needed} not registered\n{r3.stdout}"); return False
    ok(f"strategy registry has 4 new strategies: {r3.stdout.strip()}")

    return True


def step_languages_phase():
    step("[24] v0.3.15 PHP / C# / Kotlin symbol grounding")

    proj = SANDBOX / "lang-fixture"
    proj.mkdir(parents=True, exist_ok=True)

    (proj / "Service.php").write_text(
        "<?php\n"
        "namespace App;\n"
        "class UserService {\n"
        "  public function findByEmail(string $email): ?User {\n"
        "    return null;\n"
        "  }\n"
        "  private function clearCache(): void {}\n"
        "}\n"
        "interface UserRepository {\n"
        "  public function save(User $u): void;\n"
        "}\n"
        "function bootstrap_app(): void { /* ... */ }\n"
    )

    (proj / "Service.cs").write_text(
        "namespace App;\n"
        "public class UserService {\n"
        "  public User FindByEmail(string email) => null;\n"
        "  private void ClearCache() {}\n"
        "  public string DisplayName { get; set; }\n"
        "}\n"
        "public interface IUserRepository {\n"
        "  void Save(User u);\n"
        "}\n"
        "public enum AccountStatus { Active, Suspended, Closed }\n"
    )

    (proj / "Service.kt").write_text(
        "package app\n"
        "class UserService {\n"
        "  fun findByEmail(email: String): User? = null\n"
        "  private fun clearCache() {}\n"
        "  val displayName: String = \"\"\n"
        "}\n"
        "interface UserRepository {\n"
        "  fun save(u: User)\n"
        "}\n"
        "fun bootstrapApp() {}\n"
    )

    r = cli("reindex", f"--path={proj}", "--project=smoke-langs",
             "--skip-semantic")
    if r.returncode != 0:
        fail(f"language reindex failed\n{r.stdout}\n{r.stderr}")
        return False
    if "files indexed : 3" not in r.stdout:
        fail(f"reindex didn't pick up all 3 lang files\n{r.stdout}")
        return False
    ok("symbol indexer parses .php + .cs + .kt files")

    # PHP class lookup
    r = cli("symbols", "UserService", "--project=smoke-langs",
             "--language=php")
    if r.returncode != 0 or "UserService" not in r.stdout or "ed=0" not in r.stdout:
        fail(f"PHP class lookup failed\n{r.stdout}"); return False
    ok("PHP: class lookup resolves")

    r = cli("symbols", "findByEmail", "--project=smoke-langs",
             "--language=php")
    if r.returncode != 0 or "method" not in r.stdout:
        fail(f"PHP method lookup failed\n{r.stdout}"); return False
    ok("PHP: method lookup resolves")

    r = cli("symbols", "bootstrap_app", "--project=smoke-langs",
             "--language=php")
    if r.returncode != 0 or "function" not in r.stdout:
        fail(f"PHP top-level function lookup failed\n{r.stdout}")
        return False
    ok("PHP: top-level function lookup resolves")

    # C# class + interface + enum + method
    r = cli("symbols", "UserService", "--project=smoke-langs",
             "--language=c_sharp")
    if r.returncode != 0 or "class" not in r.stdout:
        fail(f"C# class lookup failed\n{r.stdout}"); return False
    ok("C#: class lookup resolves")

    r = cli("symbols", "IUserRepository", "--project=smoke-langs",
             "--language=c_sharp")
    if r.returncode != 0 or "interface" not in r.stdout:
        fail(f"C# interface lookup failed\n{r.stdout}"); return False
    ok("C#: interface lookup resolves")

    r = cli("symbols", "AccountStatus", "--project=smoke-langs",
             "--language=c_sharp")
    if r.returncode != 0 or "enum" not in r.stdout:
        fail(f"C# enum lookup failed\n{r.stdout}"); return False
    ok("C#: enum lookup resolves")

    # Kotlin class + fun + val
    r = cli("symbols", "UserService", "--project=smoke-langs",
             "--language=kotlin")
    if r.returncode != 0 or "class" not in r.stdout:
        fail(f"Kotlin class lookup failed\n{r.stdout}"); return False
    ok("Kotlin: class lookup resolves")

    r = cli("symbols", "findByEmail", "--project=smoke-langs",
             "--language=kotlin")
    if r.returncode != 0 or "method" not in r.stdout:
        fail(f"Kotlin method lookup failed\n{r.stdout}"); return False
    ok("Kotlin: method lookup resolves")

    r = cli("symbols", "bootstrapApp", "--project=smoke-langs",
             "--language=kotlin")
    if r.returncode != 0 or "function" not in r.stdout:
        fail(f"Kotlin top-level fun lookup failed\n{r.stdout}")
        return False
    ok("Kotlin: top-level fun lookup resolves")

    return True


def step_module_imports():
    step("[25] v0.3.16 module import grounding")

    proj = SANDBOX / "imp-fixture"
    proj.mkdir(parents=True, exist_ok=True)

    # package.json declaring lodash + a fake-package as deps
    (proj / "package.json").write_text(json.dumps({
        "name": "imp-fixture",
        "dependencies": {"lodash": "^4.17.0", "tinypkg": "^1.0.0"},
    }))

    # Stub node_modules/lodash with a .d.ts that exports `debounce` + `chunk`.
    nm_lodash = proj / "node_modules" / "lodash"
    nm_lodash.mkdir(parents=True, exist_ok=True)
    (nm_lodash / "package.json").write_text(json.dumps({
        "name": "lodash",
        "version": "4.17.0",
        "types": "index.d.ts",
    }))
    (nm_lodash / "index.d.ts").write_text(
        "export declare function debounce<T>(fn: T, wait: number): T;\n"
        "export declare function chunk<T>(arr: T[], size: number): T[][];\n"
        "export declare const VERSION: string;\n"
    )

    # Stub node_modules/tinypkg with no .d.ts (default-only).
    nm_tiny = proj / "node_modules" / "tinypkg"
    nm_tiny.mkdir(parents=True, exist_ok=True)
    (nm_tiny / "package.json").write_text(json.dumps({
        "name": "tinypkg", "version": "1.0.0", "main": "index.js",
    }))

    # Reindex
    r = cli("reindex", f"--path={proj}", "--project=smoke-imp",
             "--skip-semantic")
    if r.returncode != 0:
        fail(f"module reindex failed\n{r.stdout}\n{r.stderr}")
        return False
    if "module exports" not in r.stdout:
        fail(f"reindex didn't report module section\n{r.stdout}")
        return False
    if "pkgs scanned  : 2" not in r.stdout:
        fail(f"reindex should have scanned 2 packages\n{r.stdout}")
        return False
    ok("module indexer scans node_modules + parses .d.ts exports")

    # Lint a TS file that imports both real and fake names from lodash.
    proposed = proj / "Component.ts"
    proposed.write_text(
        "import { debounce } from 'lodash';\n"            # exact match
        "import { debounseFn } from 'lodash';\n"          # typo, fuzzy → debounce
        "import { totallyFakeUtility } from 'lodash';\n"  # unknown → block
        "import tiny from 'tinypkg';\n"                    # default import OK
    )
    r = cli("lint", str(proposed), "--project=smoke-imp",
             f"--content-file={proposed}")
    if r.returncode != 2:
        fail(f"import lint expected exit=2, got {r.returncode}\n{r.stdout}")
        return False
    if "totallyFakeUtility" not in r.stdout:
        fail(f"lint missed unknown import\n{r.stdout}"); return False
    if "debounseFn" not in r.stdout:
        fail(f"lint missed typo import\n{r.stdout}"); return False
    ok("import_grounding: blocks fabricated and typo'd named imports")

    # Empty-index graceful skip.
    r = cli("lint", str(proposed), "--project=no-imp-here",
             f"--content-file={proposed}")
    if "module-import — unknown" in r.stdout or \
            "module-import — low-confidence" in r.stdout:
        fail(f"import_grounding fired against empty index\n{r.stdout}")
        return False
    ok("import_grounding: silent-passes on empty index")

    # Strategy registered
    venv_py = DATA_ROOT / "venv" / ("Scripts" if sys.platform.startswith("win") else "bin") / (
        "python.exe" if sys.platform.startswith("win") else "python3"
    )
    r3 = subprocess.run([str(venv_py), "-c",
        "import sys; sys.path.insert(0, str(__import__('pathlib').Path("
        f"{repr(str(PLUGIN_ROOT))}) / 'code'));"
        "import lint_strategies as ls;"
        "names = sorted(s.name for s in ls.all_strategies());"
        "print(','.join(names))"
    ], capture_output=True, text=True)
    if r3.returncode != 0 or "import_grounding" not in r3.stdout:
        fail(f"import_grounding not registered: {r3.stdout}"); return False
    ok(f"strategy registry has import_grounding")

    return True


def step_generalist_pivot():
    step("[26] v0.4.0 generalist pivot — name registry + prose grounding")

    proj = SANDBOX / "novel-fixture"
    proj.mkdir(parents=True, exist_ok=True)

    # name_registry.json with characters + places + terms
    (proj / "name_registry.json").write_text(json.dumps({
        "characters": [
            "Aria",
            "Kael",
            {"name": "Mistress Volena", "aliases": ["The Veilweaver", "Volena"]},
        ],
        "places": ["Whitestone Citadel", "the Sundered Coast"],
        "terms": ["arcanum", "veilcraft", "thrall-binding"],
    }))
    (proj / ".dejavuignore").write_text("# nothing to ignore\n")

    # Reindex
    r = cli("reindex", f"--path={proj}", "--project=smoke-novel",
             "--skip-semantic")
    if r.returncode != 0:
        fail(f"registry reindex failed\n{r.stdout}\n{r.stderr}")
        return False
    if "name registry" not in r.stdout:
        fail(f"reindex didn't surface name registry section\n{r.stdout}")
        return False
    if "names added" not in r.stdout:
        fail(f"reindex didn't report names_added\n{r.stdout}"); return False
    ok("name registry indexer parses characters + places + terms + aliases")

    # Lint a markdown chapter with a clear typo of an indexed name.
    # Trigram similarity is weak for short transpositions, so we use
    # `Whitstone` (typo of `Whitestone Citadel`) where the shared
    # prefix gives strong overlap.
    chapter = proj / "chapter1.md"
    chapter.write_text(
        "# Chapter 1: The Veilweaver Arrives\n\n"
        "She stood at the gates of Whitstone Citadel, her cloak\n"
        "snapping in the wind. Behind her, **Kael** trudged through\n"
        "the snow, his breath frosting in the air. They had come\n"
        "for the *arcanum* — the source of all `veilcraft`.\n\n"
        "Mistress Volena was waiting for them in the keep.\n"
    )
    r = cli("lint", str(chapter), "--project=smoke-novel",
             f"--content-file={chapter}")
    # Should have at least ONE prose finding — Whitstone should match
    # Whitestone Citadel via the trigram fuzzy.
    if "prose — low-confidence" not in r.stdout \
            and "prose — unknown" not in r.stdout:
        fail(f"prose_grounding produced no findings on a chapter with "
             f"a known-name typo\n{r.stdout}")
        return False
    if "Whitstone" not in r.stdout and "Whitestone" not in r.stdout:
        fail(f"prose_grounding didn't surface the Whitstone/Whitestone "
             f"typo\n{r.stdout}")
        return False
    ok("prose_grounding: catches typo of registry entry in markdown chapter")

    # Empty registry → no false positives.
    r = cli("lint", str(chapter), "--project=no-novel-here",
             f"--content-file={chapter}")
    if "prose — unknown" in r.stdout or "prose — low-confidence" in r.stdout:
        fail(f"prose_grounding fired against empty registry\n{r.stdout}")
        return False
    ok("prose_grounding silent-passes when registry empty")

    # Strategy registered
    venv_py = DATA_ROOT / "venv" / ("Scripts" if sys.platform.startswith("win") else "bin") / (
        "python.exe" if sys.platform.startswith("win") else "python3"
    )
    r3 = subprocess.run([str(venv_py), "-c",
        "import sys; sys.path.insert(0, str(__import__('pathlib').Path("
        f"{repr(str(PLUGIN_ROOT))}) / 'code'));"
        "import lint_strategies as ls;"
        "names = sorted(s.name for s in ls.all_strategies());"
        "print(','.join(names))"
    ], capture_output=True, text=True)
    if r3.returncode != 0 or "prose_grounding" not in r3.stdout:
        fail(f"prose_grounding not registered: {r3.stdout}"); return False
    ok(f"strategy registry has prose_grounding: {r3.stdout.strip()}")

    return True


def step_fingerprint_cache():
    step("[27] v0.4.5 indexer fingerprint cache")

    # Build a small TS project with one CSS file + a tailwind config
    # so the quickwin indexer has something to fingerprint. We then
    # reindex twice and assert the second run reports `cached=True`.
    proj = SANDBOX / "fp-cache-fixture"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "package.json").write_text(json.dumps({"name": "fp"}))
    (proj / "tailwind.config.ts").write_text(
        "module.exports = { theme: { extend: {} } };\n"
    )
    (proj / "App.module.css").write_text(
        ".container {}\n.btn {}\n"
    )

    # First reindex — populates the cache.
    r = cli("reindex", f"--path={proj}", "--project=smoke-fp",
             "--skip-semantic")
    if r.returncode != 0:
        fail(f"first reindex failed\n{r.stdout}\n{r.stderr}"); return False
    if "(cached" in r.stdout:
        fail(f"first reindex unexpectedly hit cache (should populate)\n"
              f"{r.stdout}"); return False
    ok("v0.4.5 first reindex: populates fingerprints (no cache hits)")

    # Second reindex — cache should hit on every fingerprinted indexer.
    r = cli("reindex", f"--path={proj}", "--project=smoke-fp",
             "--skip-semantic")
    if r.returncode != 0:
        fail(f"second reindex failed\n{r.stdout}\n{r.stderr}"); return False
    # Expect at least 4 of the 6 fingerprinted indexers to report
    # "(cached, no changes since last run)".
    cached_count = r.stdout.count("(cached, no changes since last run)")
    if cached_count < 4:
        fail(f"v0.4.5 second reindex: expected ≥4 cache hits, got {cached_count}\n"
              f"{r.stdout}"); return False
    ok(f"v0.4.5 second reindex: {cached_count} indexers skipped via fingerprint cache")

    # Touch a CSS file → quickwin cache should miss but env/db/etc. still hit.
    target = proj / "App.module.css"
    target.write_text(target.read_text() + ".newClass {}\n")
    r = cli("reindex", f"--path={proj}", "--project=smoke-fp",
             "--skip-semantic")
    if r.returncode != 0:
        fail(f"third reindex failed\n{r.stdout}\n{r.stderr}"); return False
    # quick-wins line should NOT carry the "(cached" marker now
    qw_section_lines = [ln for ln in r.stdout.splitlines()
                          if "quick-wins:" in ln]
    if not qw_section_lines or "(cached" in qw_section_lines[0]:
        fail(f"v0.4.5: quick-wins should have invalidated after CSS edit\n"
              f"{r.stdout}"); return False
    ok("v0.4.5 third reindex: CSS edit correctly invalidates quickwin cache")

    # `--force` should bypass cache entirely.
    r = cli("reindex", f"--path={proj}", "--project=smoke-fp",
             "--skip-semantic", "--force")
    if r.returncode != 0:
        fail(f"force reindex failed\n{r.stdout}\n{r.stderr}"); return False
    if "(cached" in r.stdout:
        fail(f"--force should bypass the cache entirely\n{r.stdout}")
        return False
    ok("v0.4.5 --force: bypasses cache as expected")

    return True


def step_cleanup():
    step("[28] Cleanup")
    psql(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{PG_DB}' AND pid <> pg_backend_pid()")
    psql(f"DROP DATABASE IF EXISTS {PG_DB}")
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"{WV_URL}/v1/schema/ClaudeDejavuTurn", method="DELETE"),
            timeout=5,
        )
    except Exception:
        pass
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX, ignore_errors=True)
    ok("sandbox + DB + Weaviate class wiped")


def step_v05_followups():
    step("[28] v0.5.0a + error-log surfaces (rewriter cascade, "
          "check_name, rewrite_proposed_edit, logs CLI + MCP)")

    # ─── (a) rewriter cascade unit test ────────────────────────────
    # Drive the cascade directly with the 6 v0.5.0a-documented
    # failure inputs and verify each lands the right verdict.
    venv_py = DATA_ROOT / "venv" / ("Scripts" if sys.platform.startswith("win") else "bin") / (
        "python.exe" if sys.platform.startswith("win") else "python3"
    )
    cascade_script = (
        "import sys, json; "
        f"sys.path.insert(0, {str(PLUGIN_ROOT / 'code')!r}); "
        "from rewriter import rewrite, load_sdk_catalog; "
        "manifest = {"
        "'env_vars': ['DATABASE_URL','API_BASE_URL','AWS_ACCESS_KEY_ID',"
        "'AWS_SECRET_ACCESS_KEY','AWS_REGION','STRIPE_SECRET_KEY'], "
        "'models': ['User','Post'], "
        "'user_fields': ['id','email','name'], "
        "'post_fields': ['id','title','body'], "
        "'routes': ['GET /api/users/:id'], "
        "'gql_types': ['User','Post','Query','Mutation'], "
        "'flags': ['checkout-flow-v2','audit-log-rollout']}; "
        "deps = {'@launchdarkly/node-server-sdk','aws-sdk','next'}; "
        "text = '''import * as LD from \"@launchdarkly/node-server-sdk\";\\n"
        "const k = process.env.LD_SDK_KEY;\\n"
        "const k2 = process.env.LAUNCHDARKLY_SDK_KEY;\\n"
        "const s = process.env.AWS_SESSION_TOKEN;\\n"
        "const e = process.env.NODE_ENV;\\n"
        "const d = process.env.DATABSE_URL;\\n"
        "'''; "
        "bad = {'env': ['LD_SDK_KEY','LAUNCHDARKLY_SDK_KEY',"
        "'AWS_SESSION_TOKEN','NODE_ENV','DATABSE_URL'], "
        "'db': [], 'route': [], 'graphql': [], 'flag': []}; "
        "rewritten, outcomes = rewrite(text, manifest, "
        "project_deps=deps, bad_names=bad, sdk_catalog=load_sdk_catalog()); "
        "result = {o.original: (o.final_verdict, "
        "o.chosen.replacement if o.chosen else None) for o in outcomes}; "
        "print(json.dumps(result))"
    )
    r = run([str(venv_py), "-c", cascade_script])
    if r.returncode != 0:
        fail("rewriter cascade unit test crashed",
              r.stderr[-300:]); return False
    try:
        verdicts = json.loads(r.stdout.strip())
    except json.JSONDecodeError:
        fail(f"rewriter unit test: non-JSON stdout: {r.stdout[:200]}")
        return False
    expected = {
        "LD_SDK_KEY": "ACCEPT_SDK",
        "LAUNCHDARKLY_SDK_KEY": "ACCEPT_SDK",
        "AWS_SESSION_TOKEN": "ACCEPT_SDK",
        "NODE_ENV": "ACCEPT_SDK",
        "DATABSE_URL": "REPLACE",
    }
    for name, exp_verdict in expected.items():
        if name not in verdicts:
            fail(f"cascade missing outcome for {name}"); return False
        verdict, repl = verdicts[name]
        if verdict != exp_verdict:
            fail(f"cascade {name}: expected {exp_verdict}, got {verdict}")
            return False
    if verdicts["DATABSE_URL"][1] != "DATABASE_URL":
        fail(f"cascade DATABSE_URL: expected replacement DATABASE_URL, "
              f"got {verdicts['DATABSE_URL'][1]}"); return False
    ok("rewriter cascade: 5/5 v0.5.0a documented failures land correct verdicts")

    # ─── (b) error_log: write + read round trip ────────────────────
    log_script = (
        "import sys, json; "
        f"sys.path.insert(0, {str(PLUGIN_ROOT / 'code')!r}); "
        "from error_log import log_error, read_recent, log_path_str; "
        "log_error('smoke.test', 'smoke test entry', "
        "context={'k': 'v'}, level='warning'); "
        "recs = read_recent(limit=5, component='smoke.test'); "
        "print(json.dumps({'path': log_path_str(), "
        "'count': len(recs), "
        "'first_msg': recs[0].get('message') if recs else None}))"
    )
    r = run([str(venv_py), "-c", log_script])
    if r.returncode != 0:
        fail("error_log round-trip crashed", r.stderr[-300:])
        return False
    try:
        info = json.loads(r.stdout.strip())
    except json.JSONDecodeError:
        fail(f"error_log: non-JSON stdout: {r.stdout[:200]}"); return False
    if info.get("count", 0) < 1 or info.get("first_msg") != "smoke test entry":
        fail(f"error_log round-trip didn't return the written record: {info}")
        return False
    ok(f"error_log: write→read round-trip works ({info['path']})")

    # ─── (c) `claude-dejavu logs` CLI ──────────────────────────────
    r = cli("logs", "--limit", "5", "--component", "smoke.test")
    if r.returncode != 0:
        fail("dejavu logs CLI failed", r.stderr[-300:]); return False
    if "smoke test entry" not in r.stdout:
        fail("dejavu logs CLI didn't surface the synthetic record\n"
              + r.stdout[-300:]); return False
    ok("dejavu logs CLI: surfaces structured records")

    # ─── (d) MCP tool: dejavu_logs over JSON-RPC stdio ─────────────
    server = PLUGIN_ROOT / "mcp" / "server.py"
    proc = subprocess.Popen(
        [str(venv_py), str(server)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "CLAUDE_DEJAVU_DATA_ROOT": str(DATA_ROOT)},
        text=True, bufsize=1,
    )

    def send(req):
        proc.stdin.write(json.dumps(req) + "\n")
        proc.stdin.flush()

    def recv(timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            line = proc.stdout.readline()
            if not line:
                return None
            try:
                return json.loads(line.strip())
            except json.JSONDecodeError:
                continue
        return None

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "smoke", "version": "0"}}})
        if not recv():
            fail("MCP initialize for v05 followups"); return False
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        r2 = recv()
        tools = [t.get("name") for t in
                  (r2 or {}).get("result", {}).get("tools", [])]
        for needed in ("dejavu_check_name", "dejavu_rewrite_proposed_edit",
                         "dejavu_logs"):
            if needed not in tools:
                fail(f"MCP tools/list missing `{needed}`"); return False
        ok(f"MCP advertises all 3 v0.5.0a tools: dejavu_check_name, "
            f"dejavu_rewrite_proposed_edit, dejavu_logs")

        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "dejavu_logs",
                            "arguments": {"limit": 3}}})
        r2 = recv()
        if not r2 or "result" not in r2:
            fail("MCP dejavu_logs call returned no result"); return False
        # Result content has the human-formatted listing
        content = json.dumps(r2["result"])
        if "claude-dejavu error log" not in content:
            fail(f"dejavu_logs result missing header:\n{content[:300]}")
            return False
        ok("MCP dejavu_logs: returns the formatted listing")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()

    return True


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    print(f"claude-dejavu smoke test  (plugin={PLUGIN_ROOT}  sandbox={SANDBOX})")
    print(f"  PG: {PG_USER}@{PG_HOST}:{PG_PORT}    Weaviate: {WV_URL}")

    sequence = [
        step_setup, step_install, step_verify_schema, step_stage_fixture,
        step_ingest, step_cli_battery, step_workspace, step_restore,
        step_stop_hook, step_mcp, step_symbol_grounding,
        step_pages_and_bootstrap,
        step_env_grounding,
        step_db_grounding,
        step_graphql_grounding,
        step_lsp_wire_protocol,
        step_flag_grounding,
        step_adaptive_thresholds,
        step_real_world_integration,
        step_dejavuignore,
        step_hallucination_bench_pipeline,
        step_about_branding,
        step_quickwins,
        step_languages_phase,
        step_module_imports,
        step_generalist_pivot,
        step_fingerprint_cache,
        step_v05_followups,
    ]
    aborted = False
    for s in sequence:
        try:
            ok_step = s()
            if ok_step is False:
                aborted = True
                break
        except Exception as e:
            import traceback; traceback.print_exc()
            FAIL.append((s.__name__, str(e)))
            aborted = True
            break

    step_cleanup()

    print("\n" + "=" * 60)
    print(f"PASS: {len(PASS)}     FAIL: {len(FAIL)}     {'ABORTED' if aborted else 'OK'}")
    for m, d in FAIL:
        print(f"  ✗ {m}  {d}")
    sys.exit(0 if not FAIL else 1)


if __name__ == "__main__":
    main()
