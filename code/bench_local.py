"""Local-LLM benchmark — classifies the user's machine as fast/slow.

The free tier defaults to local Ollama for session summaries (privacy +
cost). On weak machines (4-core CPU, no GPU, slow disk) this can take
30+ seconds per gist, which makes session-end feel sluggish. Rather
than have users discover this themselves, we benchmark once at install
time + on demand, persist the result in config.env, and let
`code/summarize.py` reorder its provider cascade accordingly.

Usage:
  python code/bench_local.py [--apply] [--quick]

  --apply: write PERF_CLASS=fast|slow to config.env (otherwise prints).
  --quick: 1 sample instead of 3 (faster but noisier).

Classification:
  - fast  : median gist call < 5.0s
  - slow  : median ≥ 5.0s, OR Ollama unreachable (no LLM available)
  - The threshold is tunable via CLAUDE_DEJAVU_PERF_THRESHOLD_S.

Output is also a JSON record (one line) for programmatic consumers like
the doctor and the SessionStart preamble, which read the result to
decide whether to surface a Pro-tier nudge.

Designed to never block more than ~30s total even on the slowest
machines: 3 samples × 10s timeout = 30s ceiling.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Reuse summarize.py's Ollama client config so this is one source of truth.
sys.path.insert(0, str(Path(__file__).resolve().parent))

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("CLAUDE_DEJAVU_GIST_MODEL", "qwen2.5-coder:3b")
PERF_THRESHOLD_S = float(os.environ.get("CLAUDE_DEJAVU_PERF_THRESHOLD_S", "5.0"))
SAMPLE_TIMEOUT_S = float(os.environ.get("CLAUDE_DEJAVU_BENCH_SAMPLE_TIMEOUT_S", "10.0"))


# 1KB representative sample — long enough to actually exercise the model,
# short enough that even slow machines don't time out on the first call.
SAMPLE_TEXT = (
    "I refactored the auth module to use refresh tokens with rotation. "
    "Key changes: stored hash + single-use semantics, 5-min grace window "
    "for clock skew, and Redis as the revocation list. Tests cover the "
    "happy path plus three edge cases: stale token, replay attack, and "
    "out-of-band revocation. Found one subtle bug where the grace window "
    "wasn't applied to the issued-at timestamp — fixed by passing the "
    "iat through validate_window(). Next steps: wire the revocation list "
    "into the gateway middleware, then update the API docs."
) * 2  # ~1KB


def _data_root() -> Path:
    override = os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "claude-dejavu"
    if sys.platform.startswith("win"):
        base = (
            os.environ.get("LOCALAPPDATA")
            or os.environ.get("APPDATA")
            or str(Path.home() / "AppData" / "Local")
        )
        return Path(base) / "claude-dejavu"
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg) / "claude-dejavu" if xdg else Path.home() / ".local" / "share" / "claude-dejavu"


def _ollama_reachable() -> bool:
    """Return True if the Ollama HTTP endpoint responds at all."""
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=2.0):
            return True
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def _bench_one_call() -> float | None:
    """Run a single Ollama gist call. Returns elapsed seconds, or None
    on failure / timeout."""
    body = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": (
            "Summarize the following Claude Code turn in under 80 characters. "
            "Return ONLY the summary, no preamble.\n\n"
            f"Turn:\n{SAMPLE_TEXT}\n\nSummary:"
        ),
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 50},
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=SAMPLE_TIMEOUT_S) as r:
            r.read()
        return time.time() - t0
    except Exception:
        return None


def benchmark(samples: int = 3) -> dict:
    """Run N samples and classify the machine. Returns a dict suitable
    for jsonl logging + config persistence."""
    result: dict = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": OLLAMA_MODEL,
        "ollama_url": OLLAMA_URL,
        "ollama_reachable": False,
        "samples": [],
        "median_s": None,
        "perf_class": "untested",
        "threshold_s": PERF_THRESHOLD_S,
    }

    if not _ollama_reachable():
        # Ollama not even reachable. Treat as 'slow' so summarize falls
        # straight through to cloud providers (or rule-based digest).
        result["perf_class"] = "slow"
        result["reason"] = "ollama unreachable"
        return result

    result["ollama_reachable"] = True

    timings: list[float] = []
    for i in range(samples):
        t = _bench_one_call()
        if t is None:
            # Treat a single timeout as definitive: model couldn't
            # complete a 1KB summary in 10s — this machine is slow.
            result["samples"].append(None)
            result["perf_class"] = "slow"
            result["reason"] = f"sample {i+1} exceeded {SAMPLE_TIMEOUT_S}s"
            result["median_s"] = SAMPLE_TIMEOUT_S
            return result
        result["samples"].append(round(t, 2))
        timings.append(t)

    median = round(statistics.median(timings), 2)
    result["median_s"] = median
    result["perf_class"] = "fast" if median < PERF_THRESHOLD_S else "slow"
    return result


def _write_perf_class(perf_class: str) -> Path:
    """Persist PERF_CLASS=... to config.env. Preserves any other lines."""
    cfg = _data_root() / "config.env"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    existing = cfg.read_text() if cfg.exists() else ""
    lines = [ln for ln in existing.splitlines()
             if not ln.startswith("PERF_CLASS=")]
    lines.append(f"PERF_CLASS={perf_class}")
    lines.append(f"PERF_BENCHED_AT={time.strftime('%Y-%m-%d')}")
    cfg.write_text("\n".join(lines) + "\n")
    return cfg


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="Write PERF_CLASS=... to config.env "
                        "(default: print only, don't persist).")
    p.add_argument("--quick", action="store_true",
                   help="Run 1 sample instead of 3 (faster but noisier).")
    p.add_argument("--json", action="store_true",
                   help="Emit JSON instead of human text.")
    args = p.parse_args()

    samples = 1 if args.quick else 3
    print(f"claude-dejavu bench  (model={OLLAMA_MODEL}  url={OLLAMA_URL}  "
          f"samples={samples}  threshold={PERF_THRESHOLD_S}s)",
          file=sys.stderr)

    result = benchmark(samples=samples)

    if args.json:
        print(json.dumps(result))
    else:
        print(f"  ollama reachable: {result['ollama_reachable']}")
        if result["samples"]:
            print(f"  samples: {result['samples']}")
        if result["median_s"] is not None:
            print(f"  median:  {result['median_s']}s  (threshold {PERF_THRESHOLD_S}s)")
        print(f"  perf_class: {result['perf_class']}")
        if result.get("reason"):
            print(f"  reason: {result['reason']}")

    if args.apply:
        cfg = _write_perf_class(result["perf_class"])
        print(f"\n  wrote PERF_CLASS={result['perf_class']} to {cfg}",
              file=sys.stderr)
        if result["perf_class"] == "slow":
            print(
                "\n  ⚠ This machine is classified as slow for local LLM "
                "summarization. Session-end summaries will fall back to "
                "rule-based digest unless you have a cloud API key "
                "configured (Anthropic / Gemini / OpenRouter), in which "
                "case the cascade now tries cloud first.",
                file=sys.stderr,
            )
            print(
                "\n  → claude-dejavu Pro: managed cloud"
                "endpoint with sub-100ms gists, no key setup. Coming v0.6.",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
