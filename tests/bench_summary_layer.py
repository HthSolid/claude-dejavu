#!/usr/bin/env python3
"""
Summary-layer head-to-head: the COMPARABLE retrieval operation.

Both systems store distilled memory:
  - claude-mem:  observations + session_summaries (in SQLite + chroma)
  - dejavu:      observations + session_summaries (in PG, v0.2)

Both invoked through their MCP servers with the same queries.
This is the apples-to-apples comparison — both compressing the same
conversation activity into typed knowledge records.

Operation under test:
  - claude-mem.search(query)         → ranked observations/summaries
  - dejavu.dejavu_observations(query) → matching observations

Measures latency, response richness, marker-based recall.
"""
import json
import os
import select
import subprocess
import time
from statistics import median

CLAUDE_MEM_MCP = [
    "node",
    "/home/hendrik/.claude/plugins/cache/thedotmack/claude-mem/10.6.2/scripts/mcp-server.cjs",
]
DEJAVU_MCP = [
    "/home/hendrik/.claude-backups/code/venv/bin/python3",
    "/home/hendrik/.claude-backups/dist/claude-dejavu/mcp/server.py",
]

os.makedirs("/tmp/dejavu-bench-cli", exist_ok=True)
with open("/tmp/dejavu-bench-cli/config.env", "w") as f:
    f.write(
        "PG_HOST=localhost\nPG_PORT=5450\nPG_USER=postgres\nPG_PASS=postgres\n"
        "PG_DB=claude_audit\nWV_URL=http://localhost:8888\n"
        "WV_CLASS=ClaudeTurn\nDATA_ROOT=/tmp/dejavu-bench-cli\nOS_USER=bench\nSHARED=0\n"
    )

DEJAVU_ENV = {**os.environ, "CLAUDE_DEJAVU_DATA_ROOT": "/tmp/dejavu-bench-cli",
              "CLAUDE_DEJAVU_WV_CLASS": "ClaudeTurn"}

# Queries chosen to hit both systems' actual content. All three of:
#   summary-style "what was the decision/learning"
#   observation-style "find a specific fact"
#   project-status "what was completed"
QUERIES = [
    {"q": "registrar pricing comparison",   "marker": ["registrar"]},
    {"q": "K3s bootstrap retry",             "marker": ["k3s"]},
    {"q": "session migrate Claude environment",  "marker": ["migrate"]},
    {"q": "off-tree backup mirror",          "marker": ["backup"]},
    {"q": "embedding model benchmark",       "marker": ["embedding"]},
    {"q": "MCP server search recall",        "marker": ["mcp"]},
    {"q": "claude-dejavu plugin scaffolding", "marker": ["dejavu"]},
    {"q": "summary cascade claude provider", "marker": ["summar"]},
]

REPEATS = 5
WARMUP = 1


class MCPClient:
    def __init__(self, cmd, env=None):
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, text=True, bufsize=1,
        )
        self._id = 0
        self._init()

    def _send(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n"); self.proc.stdin.flush()

    def _recv(self, timeout=12):
        end = time.time() + timeout
        while time.time() < end:
            rl, _, _ = select.select([self.proc.stdout], [], [], 0.1)
            if rl:
                line = self.proc.stdout.readline()
                if not line: return None
                try: return json.loads(line.strip())
                except json.JSONDecodeError: continue
        return None

    def _init(self):
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "summary-bench", "version": "0"}}})
        self._recv(15)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, tool, args):
        self._id += 1
        t0 = time.perf_counter()
        self._send({"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                    "params": {"name": tool, "arguments": args}})
        r = self._recv(15)
        dt = (time.perf_counter() - t0) * 1000
        text = ""
        if r and "result" in r:
            for blk in r["result"].get("content", []) or []:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    text += blk.get("text", "")
        return dt, text

    def close(self):
        try: self.proc.terminate(); self.proc.wait(timeout=2)
        except Exception: self.proc.kill()


def has_marker(text, markers):
    t = (text or "").lower()
    return all(m.lower() in t for m in markers)


def main():
    mem = MCPClient(CLAUDE_MEM_MCP)
    dejavu = MCPClient(DEJAVU_MCP, env=DEJAVU_ENV)

    print(f"\n{'─'*84}")
    print(f"  Summary-layer comparison")
    print(f"  claude-mem.search   →  observations + session_summaries  (chroma + SQLite)")
    print(f"  dejavu_observations →  observations table                 (PG trigram)")
    print(f"  dejavu_summary      →  session_summaries table            (PG)")
    print(f"{'─'*84}")

    rows = []
    for spec in QUERIES:
        q, markers = spec["q"], spec["marker"]
        # claude-mem search
        mem_lats, mem_text, mem_hit = [], "", False
        for i in range(REPEATS):
            dt, text = mem.call("search", {"query": q, "limit": 5})
            if i >= WARMUP: mem_lats.append(dt)
            if i == REPEATS - 1: mem_text, mem_hit = text, has_marker(text, markers)
            time.sleep(0.4)
        # dejavu_observations
        dej_obs_lats, dej_obs_text, dej_obs_hit = [], "", False
        for i in range(REPEATS):
            dt, text = dejavu.call("dejavu_observations",
                                   {"query": q, "project": "sofi-base-security", "limit": 5})
            if i >= WARMUP: dej_obs_lats.append(dt)
            if i == REPEATS - 1: dej_obs_text, dej_obs_hit = text, has_marker(text, markers)
        # dejavu_summary (per project)
        dej_sum_lats, dej_sum_text, dej_sum_hit = [], "", False
        for i in range(REPEATS):
            dt, text = dejavu.call("dejavu_summary",
                                   {"project": "sofi-base-security", "limit": 1})
            if i >= WARMUP: dej_sum_lats.append(dt)
            if i == REPEATS - 1: dej_sum_text, dej_sum_hit = text, has_marker(text, markers)

        print(f"\n{q!r:60}  markers={markers}")
        print(f"  claude-mem.search       p50={median(mem_lats):>5.0f}ms  chars={len(mem_text):>5}  hit={mem_hit}")
        print(f"  dejavu_observations     p50={median(dej_obs_lats):>5.0f}ms  chars={len(dej_obs_text):>5}  hit={dej_obs_hit}")
        print(f"  dejavu_summary          p50={median(dej_sum_lats):>5.0f}ms  chars={len(dej_sum_text):>5}  hit={dej_sum_hit}")
        rows.append({
            "query": q, "markers": markers,
            "mem":     {"p50": median(mem_lats),     "chars": len(mem_text),     "hit": mem_hit,    "preview": mem_text[:400]},
            "dej_obs": {"p50": median(dej_obs_lats), "chars": len(dej_obs_text), "hit": dej_obs_hit, "preview": dej_obs_text[:400]},
            "dej_sum": {"p50": median(dej_sum_lats), "chars": len(dej_sum_text), "hit": dej_sum_hit, "preview": dej_sum_text[:400]},
        })

    mem.close(); dejavu.close()

    print(f"\n{'═'*84}")
    print("AGGREGATE — all queries (medians include error responses)")
    for k, label in [("mem", "claude-mem.search       "),
                     ("dej_obs", "dejavu_observations     "),
                     ("dej_sum", "dejavu_summary          ")]:
        p50 = median([r[k]["p50"] for r in rows])
        chars = sum(r[k]["chars"] for r in rows) // len(rows)
        recall = sum(r[k]["hit"] for r in rows)
        print(f"  {label}  p50={p50:>5.0f}ms   avg chars={chars:>5}   recall={recall}/{len(rows)}")

    # Useful-response-only stats: filter out error templates (chars < 200 = error/empty)
    print(f"\nAGGREGATE — successful responses only (chars >= 200, real retrieval)")
    for k, label in [("mem", "claude-mem.search       "),
                     ("dej_obs", "dejavu_observations     "),
                     ("dej_sum", "dejavu_summary          ")]:
        useful = [r[k] for r in rows if r[k]["chars"] >= 200]
        if not useful:
            print(f"  {label}  (zero successful responses)")
            continue
        p50 = median([r["p50"] for r in useful])
        chars = sum(r["chars"] for r in useful) // len(useful)
        recall = sum(r["hit"] for r in useful)
        print(f"  {label}  p50={p50:>5.0f}ms   avg chars={chars:>5}   recall={recall}/{len(useful)}   ({len(useful)}/{len(rows)} successful)")

    with open("/tmp/dejavu-vs-mem-summary.json", "w") as f:
        json.dump(rows, f, indent=2)
    print(f"\nFull JSON: /tmp/dejavu-vs-mem-summary.json")


if __name__ == "__main__":
    main()
