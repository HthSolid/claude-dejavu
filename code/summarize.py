"""
summarize.py — LLM session summarizer (v0.2, claude-mem parity).

Provider cascade (first one that succeeds wins):

  1. `claude -p` (subprocess)         — uses Claude Code's existing auth, no API key needed
  2. Anthropic Python SDK             — if ANTHROPIC_API_KEY is set
  3. Gemini API                        — if CLAUDE_DEJAVU_GEMINI_API_KEY is set
  4. OpenRouter API                    — if CLAUDE_DEJAVU_OPENROUTER_API_KEY is set
  5. Ollama (opt-in)                   — if CLAUDE_DEJAVU_OLLAMA=true
  6. Rule-based digest                 — last resort, always succeeds

Why this order:
  - claude -p is the same UX as claude-mem's @anthropic-ai/claude-agent-sdk:
    inherits the user's existing subscription / API key auth from Claude Code,
    no extra setup, sub-second to a few seconds, Sonnet quality.
  - The SDKs are listed for users who don't run Claude Code locally (rare).
  - Ollama is opt-in only because most CPUs are too slow for usable quality.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime

# ─── Configuration ───────────────────────────────────────────────────────────

MAX_INPUT_CHARS    = int(os.environ.get("CLAUDE_DEJAVU_SUMMARIZER_INPUT_MAX", "12000"))
# Lowered from 60s → 20s in v0.5.0+. The cascade has 5 providers, so worst
# case used to be 5×60s = 5 min before falling back to the rule-based
# digest. A single Claude Code SessionEnd hanging for 5 min was
# unacceptable; 20s × 5 = 100s worst-case is the new ceiling, and in
# practice providers either respond fast or we want to skip them.
# Override via CLAUDE_DEJAVU_SUMMARIZER_TIMEOUT_S if you genuinely need
# to wait longer for a slow remote provider.
PROVIDER_TIMEOUT_S = int(os.environ.get("CLAUDE_DEJAVU_SUMMARIZER_TIMEOUT_S", "20"))
# Hard ceiling on the entire cascade across all providers — a circuit
# breaker. After this many seconds, skip remaining providers and return
# the rule-based digest. Prevents the worst-case 5×PROVIDER_TIMEOUT_S
# stack-up when multiple providers are slow on the same SessionEnd.
CASCADE_TIMEOUT_S = int(os.environ.get("CLAUDE_DEJAVU_SUMMARIZER_CASCADE_TIMEOUT_S", "60"))

ANTHROPIC_MODEL = os.environ.get("CLAUDE_DEJAVU_ANTHROPIC_MODEL", "claude-sonnet-4-5")
GEMINI_MODEL    = os.environ.get("CLAUDE_DEJAVU_GEMINI_MODEL",    "gemini-2.5-flash-lite")
OPENROUTER_MODEL = os.environ.get("CLAUDE_DEJAVU_OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet")
OLLAMA_MODEL    = os.environ.get("CLAUDE_DEJAVU_OLLAMA_MODEL",   "qwen2.5-coder:3b")
OLLAMA_URL      = os.environ.get("OLLAMA_URL",                   "http://localhost:11434")

# ─── Prompt ──────────────────────────────────────────────────────────────────

PROMPT_TEMPLATE = """You are summarizing one slice of a Claude Code coding session.
Output STRICTLY in the JSON format below — nothing else, no preamble, no markdown fences.

{
  "request": "1-sentence: what the user was trying to do",
  "investigated": "what was looked at or explored (paths, files, services)",
  "learned": "non-obvious findings or facts surfaced (with specifics)",
  "completed": "what got done in this slice (concrete artifacts, edits)",
  "next_steps": "what is queued or pending"
}

Conversation slice (most recent last):
{conversation}

Now produce the JSON. No commentary, no markdown."""


# ─── Conversation slice builder (unchanged) ──────────────────────────────────

def _select_recent_text_turns(conn, session_id: str, max_chars: int = MAX_INPUT_CHARS) -> str:
    cur = conn.cursor()
    cur.execute("""
        SELECT role, content, ts
        FROM turns
        WHERE session_id = %s::uuid
          AND role IN ('user','assistant')
          AND content_type = 'text'
        ORDER BY ts DESC NULLS LAST
        LIMIT 200
    """, (session_id,))
    rows = cur.fetchall()
    rows.reverse()
    out = []
    used = 0
    for role, content, _ts in rows:
        snippet = (content or "").strip()
        if not snippet:
            continue
        if len(snippet) > 2500:
            snippet = snippet[:2500] + "…"
        line = f"[{role}] {snippet}\n"
        if used + len(line) > max_chars and out:
            break
        out.append(line)
        used += len(line)
    return "".join(out)


# ─── Provider implementations ────────────────────────────────────────────────

def _try_claude_cli(prompt: str) -> tuple[str | None, str]:
    """Invoke `claude -p` in a subprocess. Inherits the user's existing
    Claude Code authentication (OAuth / keychain / API key).

    Notes:
      - We do NOT pass `--bare`: that flag explicitly disables OAuth and
        keychain reads, which would break for users who auth via the Claude
        Code subscription rather than a raw API key.
      - We DO restrict tools to nothing — this is a pure text generation
        call, no file system or shell access needed.
      - We mark this invocation with CLAUDE_DEJAVU_NESTED=1 so our own hooks
        can short-circuit and avoid recursive summary loops.
    """
    if not shutil.which("claude"):
        return None, ""
    if os.environ.get("CLAUDE_DEJAVU_NESTED") == "1":
        # Already inside a claude-dejavu-spawned claude run — don't recurse.
        return None, ""
    env = {**os.environ, "CLAUDE_DEJAVU_NESTED": "1"}
    try:
        r = subprocess.run(
            ["claude", "-p", "--allowedTools", "", "--disable-slash-commands"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=PROVIDER_TIMEOUT_S,
            env=env,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None, ""
        return r.stdout.strip(), "claude-cli"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None, ""


def _try_anthropic_sdk(prompt: str) -> tuple[str | None, str]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None, ""
    try:
        import anthropic  # type: ignore
    except ImportError:
        return None, ""
    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if hasattr(b, "text"))
        return text.strip() if text else None, ANTHROPIC_MODEL
    except Exception:
        return None, ""


def _try_gemini(prompt: str) -> tuple[str | None, str]:
    api_key = os.environ.get("CLAUDE_DEJAVU_GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None, ""
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 1500},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=PROVIDER_TIMEOUT_S) as r:
            d = json.loads(r.read())
        cands = d.get("candidates") or []
        if not cands:
            return None, ""
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        return (text or None), GEMINI_MODEL
    except Exception:
        return None, ""


def _try_openrouter(prompt: str) -> tuple[str | None, str]:
    api_key = os.environ.get("CLAUDE_DEJAVU_OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return None, ""
    body = {
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1500,
        "temperature": 0.0,
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/HthSolid/claude-dejavu",
            "X-Title": "claude-dejavu",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=PROVIDER_TIMEOUT_S) as r:
            d = json.loads(r.read())
        text = (d.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        return (text or None), OPENROUTER_MODEL
    except Exception:
        return None, ""


def _try_ollama(prompt: str) -> tuple[str | None, str]:
    if os.environ.get("CLAUDE_DEJAVU_OLLAMA", "").lower() not in ("1", "true", "yes", "on"):
        return None, ""
    body = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 700},
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=PROVIDER_TIMEOUT_S) as r:
            text = json.loads(r.read()).get("response", "").strip()
        return (text or None), f"ollama:{OLLAMA_MODEL}"
    except Exception:
        return None, ""


# ─── JSON parsing + rule-based fallback ──────────────────────────────────────

def _parse_json_loose(text: str) -> dict | None:
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        repaired = re.sub(r",\s*([}\]])", r"\1", m.group(0))
        try:
            return json.loads(repaired)
        except Exception:
            return None


def _rule_based_digest(text: str) -> dict:
    lines = [ln for ln in text.split("\n") if ln.strip()]
    user_lines = [ln for ln in lines if ln.startswith("[user]")]
    asst_lines = [ln for ln in lines if ln.startswith("[assistant]")]
    request = (user_lines[0][7:] if user_lines else "")[:200]
    learned = ""
    for ln in asst_lines:
        s = ln[12:]
        if any(k in s.lower() for k in ("learned", "found", "discovered", "fact:", "note:")):
            learned = s[:300]; break
    return {"request": request, "investigated": "", "learned": learned, "completed": "", "next_steps": ""}


# ─── Cascade ─────────────────────────────────────────────────────────────────

# Cascade order: local-first by design (privacy + cost + offline-friendly).
# A user's data never leaves their machine if Ollama can answer, and it
# only walks the cloud cascade when local fails or is unavailable.
#
# Ordering rationale:
#   1. ollama     — local, free, private, no auth setup needed
#   2. claude-cli — uses the user's existing Claude Code subscription
#                   (no API key juggling), still cloud but no extra
#                   cost beyond what they already pay Anthropic
#   3. anthropic  — explicit API key
#   4. gemini     — explicit API key
#   5. openrouter — explicit API key
#
# Slow-machine override: if `claude-dejavu bench` has classified the
# machine as `perf_class=slow`, this list is reordered at runtime to
# put cloud providers first (see `_get_providers()` below). That way
# users with weak hardware aren't penalized by Ollama's 30s timeout
# every session-end.
PROVIDERS = [
    ("ollama",       _try_ollama),
    ("claude-cli",   _try_claude_cli),
    ("anthropic",    _try_anthropic_sdk),
    ("gemini",       _try_gemini),
    ("openrouter",   _try_openrouter),
]


def _get_providers() -> list[tuple[str, callable]]:
    """Return PROVIDERS in the right order for THIS machine.

    Reads `perf_class` from config.env (set by `claude-dejavu bench`).
    On 'slow' machines: cloud-first ordering. Otherwise: local-first.
    """
    try:
        cfg_path = (
            os.environ.get("CLAUDE_DEJAVU_DATA_ROOT") or
            (os.environ.get("XDG_DATA_HOME", "") or "")
        )
        if not cfg_path:
            from pathlib import Path as _P
            cfg_path = str(_P.home() / ".local" / "share" / "claude-dejavu")
        cfg_file = os.path.join(cfg_path, "config.env")
        if os.path.exists(cfg_file):
            for line in open(cfg_file):
                if line.strip().startswith("PERF_CLASS="):
                    perf = line.split("=", 1)[1].strip().strip('"\'')
                    if perf == "slow":
                        # Cloud-first reordering: claude-cli → anthropic →
                        # gemini → openrouter → ollama (last).
                        return [PROVIDERS[i] for i in (1, 2, 3, 4, 0)]
                    break
    except Exception:
        pass
    return PROVIDERS


def generate_summary(prompt: str) -> tuple[dict, str]:
    """Walk the provider cascade. Returns (parsed_dict, model_label).
    Falls back to rule-based digest on full failure or cascade timeout.

    Honors CASCADE_TIMEOUT_S as a circuit breaker — if the total time
    walking providers exceeds it, we stop trying remote providers and
    return the rule-based digest. Prevents one slow SessionEnd from
    stacking up across multiple slow providers."""
    import time as _time
    cascade_start = _time.time()
    for _name, fn in _get_providers():
        if (_time.time() - cascade_start) >= CASCADE_TIMEOUT_S:
            print(f"[summarize] cascade timeout ({CASCADE_TIMEOUT_S}s), "
                  f"falling back to rule digest", file=sys.stderr)
            break
        text, model = fn(prompt)
        if not text:
            continue
        parsed = _parse_json_loose(text)
        if parsed:
            return parsed, model
    # Rule-based fallback. The "prompt" already contains the conversation slice;
    # extract it back out for digest purposes.
    convo_marker = "Conversation slice (most recent last):\n"
    convo = prompt.split(convo_marker, 1)[-1].rsplit("\n\nNow produce", 1)[0]
    return _rule_based_digest(convo), "rule"


# ─── Public entry point (used by hooks/session_end.py) ───────────────────────

def summarize_session(conn, session_id: str) -> dict:
    """Generate a session summary and persist it to PG. Returns the dict that was written."""
    convo = _select_recent_text_turns(conn, session_id)
    if not convo.strip():
        return {}

    prompt = PROMPT_TEMPLATE.replace("{conversation}", convo)
    parsed, model_used = generate_summary(prompt)

    fields = {
        "request":      (parsed.get("request") or "")[:2000],
        "investigated": (parsed.get("investigated") or "")[:4000],
        "learned":      (parsed.get("learned") or "")[:4000],
        "completed":    (parsed.get("completed") or "")[:4000],
        "next_steps":   (parsed.get("next_steps") or "")[:2000],
    }

    cur = conn.cursor()
    cur.execute("""
        SELECT count(DISTINCT idx) FROM turns
        WHERE session_id = %s::uuid AND role = 'user' AND content_type = 'text'
    """, (session_id,))
    prompt_n = (cur.fetchone() or [0])[0]

    cur.execute("""
        INSERT INTO session_summaries (session_id, prompt_number, request, investigated,
                                        learned, completed, next_steps, model, generated_at)
        VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (
        session_id, prompt_n,
        fields["request"], fields["investigated"], fields["learned"],
        fields["completed"], fields["next_steps"], model_used, datetime.utcnow(),
    ))
    sid = cur.fetchone()[0]
    fields["id"] = sid
    fields["model"] = model_used
    return fields


if __name__ == "__main__":
    import sys, psycopg2
    if len(sys.argv) < 2:
        print("Usage: summarize.py <session-uuid> [pg_dsn]"); sys.exit(1)
    dsn = sys.argv[2] if len(sys.argv) > 2 else os.environ.get(
        "CLAUDE_DEJAVU_DSN",
        "host=localhost port=5450 user=postgres password=postgres dbname=claude_audit",
    )
    with psycopg2.connect(dsn) as conn:
        result = summarize_session(conn, sys.argv[1])
        conn.commit()
    print(json.dumps(result, indent=2))
