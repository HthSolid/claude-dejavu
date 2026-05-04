"""
observations.py — extract typed observations from assistant turns (v0.2).

Two extraction strategies, in order of confidence:

1. **Structured tags** (high confidence): <observation>, <summary>, <discovery>
   tags emitted by claude-mem-style minions or templates.
2. **Heuristic markers** (medium confidence): "Decision:", "Learned:", "Bug fix:",
   "Discovery:", "Refactor:", "Question:" prefixes.

Each extracted observation is written to the `observations` table with a type
classification. Used at SessionEnd by hooks/session_end.py and from ingester.py
during normal incremental ingest.
"""
from __future__ import annotations

import re
from typing import Iterator

# ─── Regex patterns ──────────────────────────────────────────────────────────

# Structured XML-like tag patterns (high signal). Captures the inner text and
# any type attribute.
TAG_RE = re.compile(
    r"<(?P<tag>observation|summary|discovery|decision)\b(?:[^>]*?\btype\s*=\s*[\"'](?P<type>[a-z]+)[\"'])?[^>]*>"
    r"(?P<body>.*?)</(?P=tag)>",
    re.IGNORECASE | re.DOTALL,
)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
SUBTITLE_RE = re.compile(r"<subtitle>(.*?)</subtitle>", re.IGNORECASE | re.DOTALL)
FACT_RE = re.compile(r"<fact>(.*?)</fact>", re.IGNORECASE | re.DOTALL)
FILES_RE = re.compile(r"<files?(?:_modified|_read)?>(.*?)</files?(?:_modified|_read)?>", re.IGNORECASE | re.DOTALL)

# Inline-marker patterns (medium signal). Each line that matches becomes an
# observation. Pattern → type mapping.
MARKER_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^\s*\*?\*?(?:Decision|DECIDED)\s*:\s*\*?\*?\s*(.+)$", re.M | re.I), "decision"),
    (re.compile(r"^\s*\*?\*?Bug\s*fix\s*:\s*\*?\*?\s*(.+)$",                re.M | re.I), "bugfix"),
    (re.compile(r"^\s*\*?\*?Discovery\s*:\s*\*?\*?\s*(.+)$",                 re.M | re.I), "discovery"),
    (re.compile(r"^\s*\*?\*?Learned\s*:\s*\*?\*?\s*(.+)$",                   re.M | re.I), "discovery"),
    (re.compile(r"^\s*\*?\*?Refactor(?:ed)?\s*:\s*\*?\*?\s*(.+)$",           re.M | re.I), "refactor"),
    (re.compile(r"^\s*\*?\*?Question\s*:\s*\*?\*?\s*(.+)$",                  re.M | re.I), "question"),
    (re.compile(r"^\s*\*?\*?Feature\s*:\s*\*?\*?\s*(.+)$",                   re.M | re.I), "feature"),
    (re.compile(r"^\s*\*?\*?Change\s*:\s*\*?\*?\s*(.+)$",                    re.M | re.I), "change"),
]

# Type aliases for tag extractions
TAG_TO_TYPE = {
    "observation": None,   # determined by `type` attribute or default to discovery
    "summary":     "discovery",
    "discovery":   "discovery",
    "decision":    "decision",
}

VALID_TYPES = {"bugfix", "feature", "decision", "discovery", "change", "refactor", "question"}


_TAG_NOISE = re.compile(r"</?(?:request|investigated|completed|next_steps|notes|fact|file_(?:read|modified)|files?(?:_modified|_read)?|title|subtitle)>", re.I)


def _strip(s: str | None) -> str:
    if not s:
        return ""
    # Drop stray closing/opening tags from copied claude-mem-style content
    return _TAG_NOISE.sub("", s).strip()


def extract(content: str) -> Iterator[dict]:
    """Yield observation dicts from an assistant text turn.

    Each dict has: type, title, subtitle, facts (list[str]), files_touched (list[str]).
    """
    if not content:
        return

    # 1. Structured tags
    for m in TAG_RE.finditer(content):
        body = m.group("body") or ""
        tag = (m.group("tag") or "").lower()
        type_attr = (m.group("type") or "").lower()

        # Resolve type
        obs_type = type_attr if type_attr in VALID_TYPES else (TAG_TO_TYPE.get(tag) or "discovery")

        title = _strip(TITLE_RE.search(body).group(1)) if TITLE_RE.search(body) else ""
        subtitle = _strip(SUBTITLE_RE.search(body).group(1)) if SUBTITLE_RE.search(body) else ""
        facts = [_strip(f) for f in FACT_RE.findall(body)]
        files_raw = [_strip(f) for f in FILES_RE.findall(body)]
        files = []
        for chunk in files_raw:
            files.extend([p.strip() for p in re.split(r"[,\n]", chunk) if p.strip()])

        # Fall back to first line if no <title>
        if not title:
            first_line = body.strip().split("\n", 1)[0]
            title = first_line[:200]

        if not title:
            continue

        yield {
            "type": obs_type,
            "title": title[:500],
            "subtitle": subtitle[:1000] or None,
            "facts": facts[:20] or None,
            "files_touched": files[:20] or None,
        }

    # 2. Inline markers (skip if structured tags already covered the same content)
    used_spans = [(m.start(), m.end()) for m in TAG_RE.finditer(content)]
    for pat, obs_type in MARKER_PATTERNS:
        for m in pat.finditer(content):
            # Skip if inside a structured tag span
            if any(start <= m.start() < end for start, end in used_spans):
                continue
            line = m.group(1).strip()
            if len(line) < 10:  # skip trivial markers
                continue
            yield {
                "type": obs_type,
                "title": line[:500],
                "subtitle": None,
                "facts": None,
                "files_touched": None,
            }


def insert(conn, session_id: str, turn_id: int | None, obs: dict) -> int | None:
    """Insert an observation. Returns the new id, or None on failure."""
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO observations (session_id, turn_id, type, title, subtitle, facts, files_touched, ts)
            VALUES (%s::uuid, %s, %s::observation_type, %s, %s, %s, %s, now())
            RETURNING id
        """, (session_id, turn_id, obs["type"], obs["title"], obs.get("subtitle"),
              obs.get("facts"), obs.get("files_touched")))
        row = cur.fetchone()
        return row[0] if row else None
    except Exception:
        conn.rollback()
        return None


if __name__ == "__main__":
    samples = [
        """<observation type="decision">
          <title>Default scope is workspace, not project</title>
          <subtitle>For tightly-coupled multi-repo stacks</subtitle>
          <fact>Frontend and backend belong together</fact>
          <fact>Privacy still enforced via per-user DB</fact>
          <files_modified>code/scope.py, README.md</files_modified>
        </observation>""",
        "Decision: switched to Postgres + Weaviate hybrid for the fair-source licensing wedge.",
        "Bug fix: install.py was hardcoding Linux path; refactored to platform.system() switch.",
        "Random text that has nothing to extract.",
    ]
    for s in samples:
        print(f"\n--- input ({len(s)} chars) ---")
        print(s[:120], "…" if len(s) > 120 else "")
        for obs in extract(s):
            print(f"  [{obs['type']:9}] {obs['title'][:80]}")
            if obs.get("subtitle"): print(f"             ↳ {obs['subtitle'][:80]}")
            if obs.get("facts"): print(f"             facts={len(obs['facts'])}")
            if obs.get("files_touched"): print(f"             files={obs['files_touched']}")
