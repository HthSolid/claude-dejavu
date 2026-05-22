#!/usr/bin/env python3
"""
Fair head-to-head: claude-mem MCP vs claude-dejavu MCP.

Both servers spawned identically as subprocesses.
Both sent identical queries via JSON-RPC over stdio.
Both timed the same way: from request-write to response-read.

Measures:
  - Latency (warm, p50 over N repeats)
  - Recall: did a known marker word appear in the response?
  - Response size in characters
  - Whether the search succeeded at all

Eight queries of varying difficulty. Both systems have ingested overlapping
content (this same conversation has been running for both since the start).
"""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
import time
from statistics import median

# ─── Server invocations ──────────────────────────────────────────────────────

# Maintainer-local benchmark — paths resolve from env so the script
# stays portable across machines. Override on your machine via:
#   BENCH_CLAUDE_MEM_PATH=/path/to/claude-mem/scripts/mcp-server.cjs
#   BENCH_DEJAVU_PYTHON=/path/to/dejavu/venv/bin/python3
#   BENCH_DEJAVU_MCP=/path/to/dejavu/mcp/server.py
CLAUDE_MEM_MCP = [
    "node",
    os.environ.get(
        "BENCH_CLAUDE_MEM_PATH",
        str(Path.home() / ".claude" / "plugins" / "cache"
            / "thedotmack" / "claude-mem" / "10.6.2"
            / "scripts" / "mcp-server.cjs")),
]
DEJAVU_MCP = [
    os.environ.get(
        "BENCH_DEJAVU_PYTHON",
        str(Path.home() / ".local" / "share" / "claude-dejavu"
            / "venv" / "bin" / "python3")),
    os.environ.get(
        "BENCH_DEJAVU_MCP",
        str(Path(__file__).resolve().parents[1]
            / "mcp" / "server.py")),
]

DEJAVU_ENV_DEFAULT = {
    **os.environ,
    "CLAUDE_DEJAVU_DATA_ROOT": "/tmp/dejavu-bench-cli",
    # v0.8.12: bench reads from the isolated ClaudeDejavuTurn_bench class.
    # The live ClaudeDejavuTurn class is untouched by bench operations.
    "CLAUDE_DEJAVU_WV_CLASS": "ClaudeDejavuTurn_bench",
}
# Point dejavu at the legacy claude_audit DB — that's the original bench
# corpus. v0.8.12: 745 sessions / 96k turns re-ingested from canonical
# ~/.claude/projects/<your-claude-projects-encoded-path>/*.jsonl
# source files into the pinned `claude-mem-observer-sessions-bench` slug.
# Weaviate moved 8888 → 8889 and the turn class was renamed
# ClaudeTurn → ClaudeDejavuTurn during the v0.7-v0.8 series; v0.8.12
# adds the `_bench` suffix to isolate the bench class from live traffic.
os.makedirs("/tmp/dejavu-bench-cli", exist_ok=True)
with open("/tmp/dejavu-bench-cli/config.env", "w") as f:
    f.write(
        "PG_HOST=localhost\nPG_PORT=5450\nPG_USER=postgres\nPG_PASS=postgres\n"
        "PG_DB=claude_audit\nWV_URL=http://localhost:8889\n"
        "WV_CLASS=ClaudeDejavuTurn_bench\n"
        "DATA_ROOT=/tmp/dejavu-bench-cli\nOS_USER=bench\nSHARED=0\n"
    )

DELAY_BETWEEN_CALLS_S = 0.4   # let chroma cool down between claude-mem calls

# ─── Queries with marker keywords ────────────────────────────────────────────

QUERIES = [
    {"q": "domain registrar wholesale pricing comparison",            "marker": ["openprovider", "rrpproxy"]},
    {"q": "K3s bootstrap retry exponential backoff",                  "marker": ["bootstrap", "k3s"]},
    {"q": "session migrate Claude environment to a new computer",     "marker": ["migrate"]},
    {"q": "claude-dejavu plugin scaffolding",                          "marker": ["dejavu", "plugin"]},
    {"q": "MCP server stdio JSONRPC tool list",                        "marker": ["mcp"]},
    {"q": "off-tree backup mirror systemd alternative",                "marker": ["off-tree", "backup"]},
    {"q": "embedding model benchmark recall MRR",                      "marker": ["recall", "embedding"]},
    {"q": "SessionEnd hook summarizer claude -p cascade",              "marker": ["summariz", "claude"]},
]

REPEATS = 25  # warmup + measurement rounds. Doc methodology = 25/query.
WARMUP = 5    # discard this many initial calls per query (per tool)

# ─── MCP helper ──────────────────────────────────────────────────────────────

class MCPClient:
    def __init__(self, cmd, env=None, name="mcp"):
        self.name = name
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, bufsize=1,
        )
        # v0.8.10 — drain the child's stderr in a daemon thread.
        # Without this, any chatty server (recall._hybrid prints diagnostics
        # to stderr on each Weaviate hiccup) fills the 64KB pipe buffer
        # after a few hundred calls and the child blocks on its next
        # write(), which looks identical to "MCP server hung". Discovered
        # 2026-05-19 while re-running this bench against v0.8.10. Pre-
        # existing harness bug — surfaced now because v0.8.10's dejavu_search
        # delegates to the chattier recall._hybrid path.
        self._stderr_drain = threading.Thread(
            target=self._drain_stderr, daemon=True)
        self._stderr_drain.start()
        self._id = 0
        self._init()

    def _drain_stderr(self) -> None:
        try:
            for _ in iter(self.proc.stderr.readline, ""):
                pass
        except Exception:
            pass

    def _next_id(self):
        self._id += 1
        return self._id

    def _send(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _recv(self, timeout=12):
        end = time.time() + timeout
        while time.time() < end:
            rl, _, _ = select.select([self.proc.stdout], [], [], 0.1)
            if rl:
                line = self.proc.stdout.readline()
                if not line:
                    return None
                try:
                    return json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
        return None

    def _init(self):
        self._send({
            "jsonrpc": "2.0", "id": self._next_id(),
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "fair-bench", "version": "0"}},
        })
        self._recv(15)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def list_tools(self):
        self._send({"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list"})
        r = self._recv()
        return [t["name"] for t in (r or {}).get("result", {}).get("tools", [])]

    def call(self, tool, args):
        t0 = time.perf_counter()
        self._send({"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/call",
                    "params": {"name": tool, "arguments": args}})
        r = self._recv(15)
        dt_ms = (time.perf_counter() - t0) * 1000
        text = ""
        if r and "result" in r:
            content = r["result"].get("content") or []
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    text += blk.get("text", "")
        return dt_ms, text

    def close(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except Exception:
            self.proc.kill()


# ─── Recall scorer ───────────────────────────────────────────────────────────

def has_marker(text: str, markers) -> bool:
    t = (text or "").lower()
    return all(m.lower() in t for m in markers)


# ─── Driver ──────────────────────────────────────────────────────────────────

def _corpus_marker_sanity(project: str = "claude-mem-observer-sessions-bench") -> None:
    """v0.8.10 — informational diagnostic.

    For each query's markers, count how many observations in the
    `claude-mem-observer-sessions-bench` project contain each marker
    as a substring of `title || ' ' || subtitle`. Print a WARNING line for
    any marker with 0 hits — recall ceiling for that query is mechanically
    0, regardless of any tool improvements.

    The bench itself runs UNCHANGED. The markers are deliberately NOT
    rewritten — that would be picking goalposts after seeing the corpus
    and would invalidate the head-to-head. The diagnostic just makes
    "this query can never satisfy recall" visible at bench startup so
    interpreting low recall doesn't require digging through PG by hand.
    """
    try:
        import psycopg2  # local import — keep bench runnable without psycopg2
    except ImportError:
        print("(corpus-sanity skipped — psycopg2 unavailable)")
        return
    try:
        c = psycopg2.connect(
            host="localhost", port=5450, user="postgres",
            password="postgres", dbname="claude_audit",
            connect_timeout=2,
        )
        c.autocommit = True
        cur = c.cursor()
        warnings = 0
        for spec in QUERIES:
            for marker in spec["marker"]:
                cur.execute("""
                    SELECT count(*)
                    FROM observations o JOIN sessions s ON s.id = o.session_id
                    WHERE s.project_slug = %s
                      AND (lower(coalesce(o.title, '')) LIKE %s
                           OR lower(coalesce(o.subtitle, '')) LIKE %s)
                """, (project, f"%{marker.lower()}%", f"%{marker.lower()}%"))
                n = cur.fetchone()[0]
                if n == 0:
                    print(f"  ⚠ marker {marker!r} has 0 hits in "
                          f"{project} — recall ceiling for "
                          f"query={spec['q']!r} is 0")
                    warnings += 1
        c.close()
        if warnings:
            print(f"  ({warnings} markers with 0 corpus hits — recall "
                  f"upper-bound capped accordingly. Bench runs unchanged.)")
        else:
            print("  (all markers present in corpus — recall upper bound is 8/8 per tool)")
    except Exception as e:  # noqa: BLE001
        print(f"(corpus-sanity skipped: {type(e).__name__}: {e})")


def main():
    print("Spawning MCP servers …")
    print("\nCorpus-sanity diagnostic (informational — bench is UNCHANGED):")
    _corpus_marker_sanity()
    mem = MCPClient(CLAUDE_MEM_MCP, name="claude-mem")
    dejavu = MCPClient(DEJAVU_MCP, env=DEJAVU_ENV_DEFAULT, name="dejavu")

    mem_tools = mem.list_tools()
    dejavu_tools = dejavu.list_tools()
    print(f"  claude-mem tools: {mem_tools}")
    print(f"  dejavu tools:     {dejavu_tools}")
    print()

    # Pick the search-equivalent in each. We measure THREE dejavu tools to
    # refresh all three rows of the docs/VS_CLAUDE_MEM.md parity table:
    #   - claude-mem.search vs
    #   - dejavu_search       (turn-level lexical hybrid)
    #   - dejavu_observations (typed observations cache, project-scoped)
    #   - dejavu_summary      (LLM-distilled summary cache, project-scoped)
    mem_tool = "search" if "search" in mem_tools else mem_tools[0]
    OBS_PROJECT = "claude-mem-observer-sessions-bench"  # v0.8.11: pinned fixture (renamed from claude-mem-observer-sessions to prevent ingest drift)

    print(f"Comparing tools: claude-mem.{mem_tool}  vs  dejavu.dejavu_{{search,observations,summary}}")
    print()

    rows = []
    for spec in QUERIES:
        q, markers = spec["q"], spec["marker"]
        print(f"\n{'─'*78}\nQ: {q!r}  markers={markers}")

        def _bench(client, tool, args, name):
            lats, last_text = [], ""
            for i in range(REPEATS):
                dt, text = client.call(tool, args)
                if i >= WARMUP:
                    lats.append(dt)
                if i == REPEATS - 1:
                    last_text = text
                if client is mem:
                    time.sleep(DELAY_BETWEEN_CALLS_S)
            hit = has_marker(last_text, markers)
            print(f"  {name:<24} p50={median(lats):>5.1f}ms  chars={len(last_text):>6}  hit={hit}")
            return median(lats), len(last_text), hit

        mem_p50, mem_chars, mem_hit = _bench(mem, mem_tool, {"query": q, "limit": 5}, "claude-mem.search")
        ds_p50, ds_chars, ds_hit = _bench(dejavu, "dejavu_search",
                                          {"query": q, "scope": "global", "limit": 5},
                                          "dejavu_search")
        do_p50, do_chars, do_hit = _bench(dejavu, "dejavu_observations",
                                          {"query": q, "project": OBS_PROJECT, "limit": 5},
                                          "dejavu_observations")
        dsm_p50, dsm_chars, dsm_hit = _bench(dejavu, "dejavu_summary",
                                             {"project": OBS_PROJECT, "limit": 1},
                                             "dejavu_summary")

        rows.append({
            "query": q, "markers": markers,
            "mem_p50_ms": mem_p50, "mem_chars": mem_chars, "mem_hit": mem_hit,
            "dej_search_p50_ms": ds_p50, "dej_search_chars": ds_chars, "dej_search_hit": ds_hit,
            "dej_obs_p50_ms": do_p50, "dej_obs_chars": do_chars, "dej_obs_hit": do_hit,
            "dej_sum_p50_ms": dsm_p50, "dej_sum_chars": dsm_chars, "dej_sum_hit": dsm_hit,
        })

    mem.close(); dejavu.close()

    # Aggregate
    def _agg(label, p50k, chk, hk):
        print(f"  {label:<24} p50 {median([r[p50k] for r in rows]):>5.1f}ms  "
              f"avg chars {sum(r[chk] for r in rows)//len(rows):>5}  "
              f"recall {sum(r[hk] for r in rows)}/{len(rows)}")

    print("\n" + "=" * 78 + "\nAGGREGATE")
    _agg("claude-mem.search",    "mem_p50_ms",         "mem_chars",        "mem_hit")
    _agg("dejavu_search",        "dej_search_p50_ms",  "dej_search_chars", "dej_search_hit")
    _agg("dejavu_observations",  "dej_obs_p50_ms",     "dej_obs_chars",    "dej_obs_hit")
    _agg("dejavu_summary",       "dej_sum_p50_ms",     "dej_sum_chars",    "dej_sum_hit")

    with open("/tmp/dejavu-vs-mem-fair.json", "w") as f:
        json.dump(rows, f, indent=2)
    print("\nFull JSON: /tmp/dejavu-vs-mem-fair.json")


if __name__ == "__main__":
    main()
