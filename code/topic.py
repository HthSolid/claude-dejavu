"""claude-dejavu — pure-Python topic extraction.

Used by v0.8.9 Context Economy phase 6 to derive a coarse topic label
from a free-form string (the user's search query OR a session
`ai_title` field). The label is fed to `dejavu_bridge.topic_reweight`
which multiplicatively rescales hit scores by topic match.

Design goals
------------
- Deterministic: identical input → identical output, every time.
- No dependencies on the bridge wheel (the bridge wheel may not be
  loaded yet when we extract topics from PG-side metadata).
- Reuses `_STOPWORDS` from `hooks.user_prompt_submit` so we have a
  single source of truth for "what counts as a content word".
- Cheap: ~O(len(text)) regex scan + dict update. Hot path: called
  once per CLI search invocation + N+1 times per JIT recall hook
  (1 for the query + N for ai_titles).

Output shape
------------
Top-2 content tokens joined with '_'. Returns "" for inputs with
fewer than 2 distinct content tokens — caller must check the empty
case before passing to topic_reweight (an empty topic_id with all
hit_topics also empty would make every hit "same topic" with the
demote factor, which is the opposite of what an unknown-topic query
should do).

Examples
--------
>>> extract_topic("Plan rsync and restic backup strategy")
'rsync_restic'   # or 'restic_rsync' on tie — first-seen wins
>>> extract_topic("")
''
>>> extract_topic("ok")
''
>>> extract_topic("a the and is for")
''
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


# Reuse the JIT hook's stopword set — single source of truth.
# The hook lives at `hooks/user_prompt_submit.py` next to this file's
# parent (`code/`), so we walk up + over to import it. We do this at
# module load time to fail fast if the hook file moves.
_HERE = Path(__file__).resolve().parent
_HOOKS = _HERE.parent / "hooks"
if str(_HOOKS) not in sys.path:
    sys.path.insert(0, str(_HOOKS))
try:
    from user_prompt_submit import _STOPWORDS as _HOOK_STOPWORDS  # type: ignore
    _STOPWORDS: frozenset[str] = _HOOK_STOPWORDS
except Exception:
    # Hook unimportable for any reason → use the same baseline set
    # the hook ships with. Better than crashing topic extraction.
    _STOPWORDS = frozenset({
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "and", "or", "but", "if", "then", "else", "of", "to", "in", "on",
        "for", "with", "from", "by", "at", "as", "it", "this", "that",
        "these", "those", "i", "we", "you", "they", "he", "she", "them",
    })


# Match the hook's tokenizer exactly: ASCII letter start + 2+ word
# chars. Non-ASCII (e.g. Cyrillic, CJK) returns 0 tokens, which means
# `extract_topic` cleanly returns "" — the caller's safe-skip path.
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")


def tokenize_query(query: str, min_len: int = 3) -> list[str]:
    """Tokenize a free-text query into lowercase content tokens.

    Same rules as `extract_topic` (lowercase, ASCII-letter start, ≥`min_len`
    chars, drop `_STOPWORDS`), but returns the FULL deduplicated list in
    first-seen order — not the top-2 frequency-ranked join.

    Used by v0.8.10 `dejavu_observations` / `dejavu_summary` so a multi-word
    query like "off-tree backup mirror systemd alternative" matches any cache
    row that contains ANY of the content tokens (`backup`, `mirror`,
    `systemd`, `alternative` — `off-tree` is split by the non-`[A-Za-z0-9_]`
    hyphen, yielding `off` (dropped <3) and `tree`).

    Returns [] for inputs with no content tokens — caller must treat that
    as "no query filter" (i.e. all rows match), matching the pre-v0.8.10
    behavior when `query` was None / "".

    Pure, deterministic, no I/O.
    """
    if not query:
        return []
    seen: dict[str, int] = {}
    out: list[str] = []
    for m in _WORD_RE.finditer(query.lower()):
        tok = m.group(0)
        if len(tok) < min_len:
            continue
        if tok in _STOPWORDS:
            continue
        if tok in seen:
            continue
        seen[tok] = 1
        out.append(tok)
    return out


def extract_topic(title_or_text: str) -> str:
    """Tokenize → lowercase → drop stopwords → keep tokens ≥3 chars
    → take top-2 by frequency (stable: first-seen on ties) → join
    with '_'.

    Returns "" for input that has fewer than 2 content tokens.

    Pure, deterministic, no I/O. Safe to call thousands of times per
    hook invocation.
    """
    if not title_or_text:
        return ""
    counts: dict[str, int] = {}
    order: dict[str, int] = {}
    for i, m in enumerate(_WORD_RE.finditer(title_or_text.lower())):
        tok = m.group(0)
        if tok in _STOPWORDS:
            continue
        if tok not in counts:
            order[tok] = i
        counts[tok] = counts.get(tok, 0) + 1
    if len(counts) < 2:
        # Single content token is too vague to define a topic. We
        # could fall back to the lone token, but that creates noisy
        # 1-word topics ("rsync") that match too easily. Force "" so
        # the caller knows to skip reweighting for this input.
        return ""
    # Sort by count desc, then by first-seen order (stable).
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], order[kv[0]]))
    return f"{ranked[0][0]}_{ranked[1][0]}"


__all__ = ["extract_topic", "tokenize_query"]
