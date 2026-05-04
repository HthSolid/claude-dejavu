"""
gist.py — produce a tight ~80-char summary of a turn for cheap MCP returns.

Two strategies:
  - rule: filler-strip + first-sentence + truncate          (fast, free, ~70% quality)
  - llm:  qwen2.5-coder:3b via Ollama at /api/generate      (slower, free, ~90% quality)

Hybrid policy: rule by default; bump to llm only when content > 1000 chars
AND role='assistant' AND content_type='text' (where the savings matter most).
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("CLAUDE_DEJAVU_GIST_MODEL", "qwen2.5-coder:3b")
LLM_THRESHOLD_CHARS = int(os.environ.get("CLAUDE_DEJAVU_LLM_GIST_MIN", "1000"))
GIST_MAX_CHARS = 100

# Filler patterns to strip from a sentence start — keep meaning, remove noise.
FILLERS_PREFIX = re.compile(
    r"^("
    r"Based on (the )?(memory|context|conversation|prior|recent commits)[^,.]*,?\s*|"
    r"Let me\s+|Let's\s+|I(?:'ll|'d| will| would)\s+|First,?\s*|Now,?\s*|So,?\s*|"
    r"Right[.,]?\s*|Actually[.,]?\s*|Okay[.,]?\s*|OK[.,]?\s*|Alright[.,]?\s*|"
    r"Great[.!]?\s*|Perfect[.!]?\s*|Sure[.,]?\s*|Of course[.,]?\s*|"
    r"Here(?:'s| is) (?:what|the|how|where|why)[^,.]*[,.]?\s*|"
    r"Looking at[^,.]*[,.]?\s*|"
    r"That('s| is) (a )?(great|good|interesting) (point|question|catch)[.,!]?\s*|"
    r"You(?:'re| are)? (right|correct)[.,]?\s*|"
    r"Confirmed[.,]?\s*"
    r")",
    re.IGNORECASE,
)

# Filler "first sentences" that should be skipped entirely if they appear as sentence 1.
FILLER_SENTENCES = re.compile(
    r"^("
    r"let me think (about (this|that))?[.,]?|"
    r"that('s| is) (a )?(great|good|fair|valid) (point|question|catch)[.,!]?|"
    r"you('re| are)? (right|correct)[.,]?|"
    r"interesting[.,]?|"
    r"hmm[.,]?|"
    r"got it[.,]?"
    r")\s*$",
    re.IGNORECASE,
)

REPLACEMENTS = [
    (re.compile(r"\bdue to the fact that\b", re.I), "because"),
    (re.compile(r"\bin order to\b", re.I), "to"),
    (re.compile(r"\bat this point in time\b", re.I), "now"),
    (re.compile(r"\ba large number of\b", re.I), "many"),
    (re.compile(r"\bin the event that\b", re.I), "if"),
    (re.compile(r"\bwith respect to\b", re.I), "re"),
    (re.compile(r"\bas a result of\b", re.I), "from"),
]

# Drop markdown decorations
MD_HEADER = re.compile(r"^#+\s*", re.MULTILINE)
MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
MD_CODE_FENCE = re.compile(r"```[a-zA-Z]*\n.*?\n```", re.DOTALL)
MD_INLINE_CODE = re.compile(r"`([^`]+)`")
MD_BULLET = re.compile(r"^\s*[-*]\s+", re.MULTILINE)
URL = re.compile(r"https?://\S+")
WHITESPACE = re.compile(r"\s+")


def rule_gist(text: str, max_chars: int = GIST_MAX_CHARS) -> str:
    """Pure-Python rule-based grunting. No I/O, no LLM."""
    if not text:
        return ""
    s = text
    # Strip code blocks (replace with [code])
    s = MD_CODE_FENCE.sub("[code]", s)
    # Drop markdown decorations
    s = MD_HEADER.sub("", s)
    s = MD_BOLD.sub(r"\1", s)
    s = MD_INLINE_CODE.sub(r"\1", s)
    s = MD_BULLET.sub("", s)
    # Strip URLs (often huge)
    s = URL.sub("[url]", s)
    # Filler replacements
    for pat, repl in REPLACEMENTS:
        s = pat.sub(repl, s)
    # Strip leading filler iteratively (handle chains like "OK. So, Actually,")
    s = s.lstrip()
    for _ in range(5):
        new = FILLERS_PREFIX.sub("", s.lstrip())
        if new == s:
            break
        s = new
    # Walk sentences; skip filler-only first sentences.
    sentences = re.split(r"(?<=[.!?])\s+", s, maxsplit=4)
    pick = []
    for sent in sentences:
        if FILLER_SENTENCES.match(sent.strip()):
            continue
        pick.append(sent)
        if sum(len(x) for x in pick) >= max_chars:
            break
    s = " ".join(pick)
    # Collapse whitespace
    s = WHITESPACE.sub(" ", s).strip()
    # Hard truncate
    if len(s) > max_chars:
        s = s[: max_chars - 1].rstrip() + "…"
    return s


def llm_gist(text: str, max_chars: int = GIST_MAX_CHARS, timeout_s: int = 30) -> str | None:
    """Try Ollama qwen2.5-coder:3b. Returns None on failure (caller falls back to rule)."""
    prompt = (
        f"Summarize this Claude Code turn in under {max_chars} characters. "
        f"Return ONLY the summary, no preamble. Keep technical specifics (filenames, errors, names).\n\n"
        f"Turn:\n{text[:3000]}\n\nSummary:"
    )
    body = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 50},
    }).encode()
    req = urllib.request.Request(f"{OLLAMA_URL}/api/generate", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            d = json.loads(r.read())
        out = (d.get("response") or "").strip().strip('"').splitlines()[0].strip()
        if not out:
            return None
        if len(out) > max_chars:
            out = out[: max_chars - 1].rstrip() + "…"
        return out
    except Exception:
        return None


def make_gist(text: str, role: str | None = None, content_type: str | None = None,
              prefer_llm: bool = False) -> tuple[str, str]:
    """Return (gist, method_used). Hybrid policy:
    - Always start with rule_gist (free, fast)
    - For long assistant text turns, try llm_gist; fall back to rule on any failure
    """
    rule = rule_gist(text)
    use_llm = prefer_llm or (
        len(text) >= LLM_THRESHOLD_CHARS
        and role == "assistant"
        and content_type == "text"
    )
    if use_llm:
        llm = llm_gist(text)
        if llm:
            return llm, "llm"
    return rule, "rule"


if __name__ == "__main__":
    # Smoke test
    samples = [
        "Based on memory and recent commits, here's where things stand: ## Where we left off **Sprint 1 is complete and merged to `main`.** Recent commits added the bootstrap service, the capability discovery probe, and the cluster join orchestrator...",
        "Actually, let me think about this. The networking integration was failing because the secondary CNI plugin did not propagate the host network namespace correctly to the second container interface.",
        "ok",
        "The benchmark produced these results: MiniLM-L6 R@1=0.44, bge-small R@5=0.78, nomic R@5=0.89.",
    ]
    for s in samples:
        g, m = make_gist(s, role="assistant", content_type="text")
        print(f"  [{m}] ({len(g)}c) {g}")
        print(f"     ← from ({len(s)}c) {s[:80]}…\n")
