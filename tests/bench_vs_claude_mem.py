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
import select
import subprocess
import sys
import time
from statistics import median

# ─── Server invocations ──────────────────────────────────────────────────────

CLAUDE_MEM_MCP = [
    "node",
    "/home/hendrik/.claude/plugins/cache/thedotmack/claude-mem/10.6.2/scripts/mcp-server.cjs",
]
DEJAVU_MCP = [
    "/home/hendrik/.claude-backups/code/venv/bin/python3",
    "/home/hendrik/.claude-backups/dist/claude-dejavu/mcp/server.py",
]

DEJAVU_ENV_DEFAULT = {
    **os.environ,
    "CLAUDE_DEJAVU_DATA_ROOT": "/tmp/dejavu-bench-cli",
    "CLAUDE_DEJAVU_WV_CLASS": "ClaudeTurn",  # existing test corpus is in this class
}
# Make sure dejavu has a config pointing at the live claude_audit DB
os.makedirs("/tmp/dejavu-bench-cli", exist_ok=True)
with open("/tmp/dejavu-bench-cli/config.env", "w") as f:
    f.write(
        "PG_HOST=localhost\nPG_PORT=5450\nPG_USER=postgres\nPG_PASS=postgres\n"
        "PG_DB=claude_audit\nWV_URL=http://localhost:8888\n"
        "WV_CLASS=ClaudeTurn\n"
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

REPEATS = 5  # 1 warmup + 4 measurement rounds (we use median of all 5)
WARMUP = 1   # discard this many initial calls per query (per system)

# ─── MCP helper ──────────────────────────────────────────────────────────────

class MCPClient:
    def __init__(self, cmd, env=None, name="mcp"):
        self.name = name
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, bufsize=1,
        )
        self._id = 0
        self._init()

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

def main():
    print("Spawning MCP servers …")
    mem = MCPClient(CLAUDE_MEM_MCP, name="claude-mem")
    dejavu = MCPClient(DEJAVU_MCP, env=DEJAVU_ENV_DEFAULT, name="dejavu")

    mem_tools = mem.list_tools()
    dejavu_tools = dejavu.list_tools()
    print(f"  claude-mem tools: {mem_tools}")
    print(f"  dejavu tools:     {dejavu_tools}")
    print()

    # Pick the search-equivalent in each
    mem_tool = "search" if "search" in mem_tools else mem_tools[0]
    dejavu_tool = "dejavu_search"

    print(f"Comparing tool calls: claude-mem.{mem_tool}  vs  dejavu.{dejavu_tool}")
    print()

    rows = []
    for spec in QUERIES:
        q, markers = spec["q"], spec["marker"]
        print(f"\n{'─'*78}\nQ: {q!r}  markers={markers}")
        # claude-mem
        mem_lats, mem_text, mem_hit = [], "", False
        for i in range(REPEATS):
            dt, text = mem.call(mem_tool, {"query": q, "limit": 5})
            if i >= WARMUP:
                mem_lats.append(dt)
            if i == REPEATS - 1:  # use last (warmest) response for content/hit check
                mem_text, mem_hit = text, has_marker(text, markers)
            time.sleep(DELAY_BETWEEN_CALLS_S)
        # dejavu
        dej_lats, dej_text, dej_hit = [], "", False
        for i in range(REPEATS):
            dt, text = dejavu.call(dejavu_tool, {"query": q, "scope": "global", "limit": 5})
            if i >= WARMUP:
                dej_lats.append(dt)
            if i == REPEATS - 1:
                dej_text, dej_hit = text, has_marker(text, markers)

        print(f"  claude-mem: p50={median(mem_lats):.0f}ms  chars={len(mem_text):>6}  hit={mem_hit}")
        print(f"  dejavu:     p50={median(dej_lats):.0f}ms  chars={len(dej_text):>6}  hit={dej_hit}")
        rows.append({
            "query": q,
            "markers": markers,
            "mem_p50_ms": median(mem_lats), "mem_chars": len(mem_text), "mem_hit": mem_hit,
            "dej_p50_ms": median(dej_lats), "dej_chars": len(dej_text), "dej_hit": dej_hit,
        })

    mem.close(); dejavu.close()

    # Aggregate
    print("\n" + "="*78)
    print("AGGREGATE")
    print(f"  claude-mem:  p50 latency {median([r['mem_p50_ms'] for r in rows]):.0f}ms   "
          f"avg chars {sum(r['mem_chars'] for r in rows)//len(rows)}   "
          f"recall {sum(r['mem_hit'] for r in rows)}/{len(rows)}")
    print(f"  dejavu:      p50 latency {median([r['dej_p50_ms'] for r in rows]):.0f}ms   "
          f"avg chars {sum(r['dej_chars'] for r in rows)//len(rows)}   "
          f"recall {sum(r['dej_hit'] for r in rows)}/{len(rows)}")

    with open("/tmp/dejavu-vs-mem-fair.json", "w") as f:
        json.dump(rows, f, indent=2)
    print("\nFull JSON: /tmp/dejavu-vs-mem-fair.json")


if __name__ == "__main__":
    main()
