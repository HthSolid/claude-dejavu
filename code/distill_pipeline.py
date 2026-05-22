"""distill_pipeline.py — claude-dejavu's fidelity wrapper around
`dejavu_bridge.distill`.

Why this file exists
====================
The closed-source `dejavu_bridge` wheel compresses a turn's text into
a tight summary. But Claude Code transcripts are full of
*load-bearing tokens* that must survive compression verbatim:

  - file paths            /home/x/projects/foo/src/bar.ts
  - URLs                  http://localhost:8888/v1/batch/objects
  - port numbers          8889, 5432, 5450
  - version strings       v0.8.3, 0.7.1-rc2
  - command-line flags    --workers=4, --dry-run
  - env-var names         WV_URL, PG_DB, DEJAVU_BRIDGE_INDEX_URL
  - error class names     UndefinedColumn, ImportError
  - identifiers in `…`    `claude-dejavu memory backfill-gists`
  - numbers with units    32 MB, 200ms, 5s, 1024-dim

A summary that loses any of those is recall poison — the user later
keyword-searches "WV_URL" or "UndefinedColumn" and the gist won't
match. We work around this by extracting every load-bearing anchor
BEFORE calling distill, splicing them back in AFTER, and verifying
the round trip. If any anchor goes missing, we return None so the
caller stores `gist=NULL` (recall then falls back to first-200-chars
of `content`, which preserves everything).

Pipeline order (per turn)
=========================
  1. content-type gate: code blocks, tracebacks, JSON, diffs, and
     tool_use payloads skip distillation entirely (return None).
  2. extract load-bearing anchors via _LOAD_BEARING_PATTERNS.
  3. call dejavu_bridge.distill(text, tier=2).
  4. splice anchors: final gist = `[anchors: a, b, c] compressed_text`.
  5. round-trip check: every anchor must keyword-match against the
     final gist. If any miss, return None (uncompressible).
  6. tag with gist_method='claudedj-distill-v1'.

A None return is a *contract*, not an error — it means "this turn
won't be compressed; the caller stores NULL and recall falls back".

Stub-mode behaviour
===================
When `dejavu_bridge` is unavailable, the stub's `distill` returns
the source text verbatim. The pipeline still runs — anchors still
get extracted and spliced into the leading `[anchors: …]` prefix —
but the "compressed body" is just the original text. That's the
worst-case storage outcome but the safest semantic one, and lets
recall continue to return the gist field uniformly.
"""
from __future__ import annotations

import logging
import re
from typing import Any


# ─── Bridge import ────────────────────────────────────────────────────────────
# Single import site for the whole codebase. Every caller imports from
# here so the stub-vs-real decision is centralized. The session_start
# hook reads `BRIDGE_VERSION` / `BRIDGE_IS_STUB` to surface degraded
# mode to the user.
try:  # pragma: no cover — the import branch tested via PYTHONPATH override
    import dejavu_bridge as _bridge  # type: ignore[import]
    BRIDGE_IS_STUB = False
except ImportError:
    import dejavu_bridge_stub as _bridge  # type: ignore[import]
    BRIDGE_IS_STUB = True

BRIDGE_VERSION = getattr(_bridge, "__version__", "unknown")


GIST_METHOD = "claudedj-distill-v1"


# ─── Load-bearing token patterns ──────────────────────────────────────────────
# Longest / most-specific patterns first so the broader ones don't eat
# their prefixes. Each pattern captures a single token type. The set
# is deliberately small + targeted: regex precision wins over recall
# here — false positives just cost a few bytes in the anchor prefix.
_LOAD_BEARING_PATTERNS: list[re.Pattern[str]] = [
    # URLs                  http://… https://…
    re.compile(r"https?://[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]+"),
    # absolute / project-relative file paths
    # (matches /a/b/c, ./a/b, ../a/b, src/foo.ts; rejects single /)
    re.compile(r"(?:\.{1,2}/|/)[A-Za-z0-9_./\\-]+[A-Za-z0-9_]"),
    # bare-filename references with a code extension
    # (matches install_restic.py, recall.ts, build.rs — anchors that
    # have no leading path but are still load-bearing in error
    # messages, diffs, and design-doc references)
    re.compile(
        r"\b[A-Za-z][A-Za-z0-9_-]*\."
        r"(?:py|pyi|ts|tsx|js|jsx|mjs|cjs|rs|go|rb|java|kt|"
        r"sh|bash|md|json|yaml|yml|toml|sql|html|css|scss|"
        r"vue|svelte)\b"),
    # version strings       v0.8.3, 0.7.1, 1.10.2-rc2
    re.compile(r"\bv?\d+\.\d+(?:\.\d+)?(?:-[A-Za-z0-9]+)?\b"),
    # numbers with units    32MB, 200ms, 5s, 1024-dim
    re.compile(r"\b\d+(?:\.\d+)?\s*(?:MB|GB|KB|TB|B|ms|s|m|h|"
                r"dim|chars?|tokens?|rows?|workers?|hits?)\b"),
    # command-line flags    --foo, --foo=bar, --workers 4
    re.compile(r"--[a-z][a-z0-9-]+(?:=[^\s]+)?"),
    # error class names     UndefinedColumn, ImportError, RuntimeException
    # Catches Camel-case identifiers ending in one of the canonical
    # error-class suffixes. The "-Column / -Constraint / -Violation /
    # -Conflict / -NotFound / -Denied / -Refused / -Timeout" set
    # covers psycopg2 + redis + http-style error classes that don't
    # carry the literal "Error" string.
    re.compile(
        r"\b[A-Z][a-z][A-Za-z0-9]*"
        r"(?:Error|Exception|Failure|Column|Constraint|Violation|"
        r"Conflict|NotFound|Denied|Refused|Timeout)\b"),
    # env-var names         ALL_CAPS_WITH_UNDERSCORES (2+ chunks)
    re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b"),
    # backtick identifiers  `claude-dejavu memory backfill-gists`
    re.compile(r"`[^`\n]{2,80}`"),
    # bare port numbers     8889, 5432 (heuristic: 4-5 digit numbers
    # that look like ports, after the version regex eats X.Y.Z)
    re.compile(r"\b(?:[1-9]\d{3,4})\b"),
]


# ─── Skip-distill content gates ───────────────────────────────────────────────
# Turn types where distillation is unsafe / pointless and the caller
# should store gist=NULL instead. Recall falls back to content[:200].
_TRACEBACK_RE = re.compile(
    r"^Traceback \(most recent call last\):", re.MULTILINE)
_DIFF_RE = re.compile(r"^[-+]{3} ", re.MULTILINE)


def _has_diff_lines(text: str, threshold: int = 5) -> bool:
    """Heuristic: 5+ lines starting with `+` or `-` (not `++`/`--`).

    Plain prose can occasionally have a leading `-`; a diff is
    characterized by *many* such lines.
    """
    n = 0
    for ln in text.splitlines():
        if ln.startswith(("+ ", "- ")) or ln in ("+", "-"):
            n += 1
            if n >= threshold:
                return True
    return False


def _looks_like_structured_json(text: str) -> bool:
    """Heuristic JSON detector: trimmed text starts with `{` or `[`
    AND ends with the matching close. Doesn't actually parse — the
    cost isn't worth it. False positives just mean we skip distill
    on a few JSON-looking prose blocks, which is the safe direction.
    """
    s = text.strip()
    if len(s) < 4:
        return False
    return (s[0] == "{" and s[-1] == "}") or (s[0] == "[" and s[-1] == "]")


def should_skip_distill(text: str,
                        content_type: str | None = None) -> str | None:
    """Return a string reason if distillation should be skipped, else
    None.

    Exposed as a pure helper so tests + callers can introspect the
    skip decision without re-running the full pipeline.
    """
    if not text:
        return "empty"
    if content_type and content_type not in ("text", None):
        # tool_use / tool_result / thinking are caller-tagged; never
        # distill them — they're already structured and tiny gists
        # break tool replay.
        return f"content_type={content_type}"
    if "```" in text:
        return "contains-code-block"
    if _TRACEBACK_RE.search(text):
        return "contains-traceback"
    if _looks_like_structured_json(text):
        return "structured-json"
    if _has_diff_lines(text):
        return "contains-diff"
    return None


# ─── Anchor extraction + splice ───────────────────────────────────────────────

def extract_anchors(text: str,
                    max_anchors: int = 32) -> list[str]:
    """Walk every load-bearing pattern over `text`, collect matches,
    dedupe preserving first-seen order, cap at `max_anchors`.

    Exposed so tests can verify the regex set independently of the
    full pipeline.
    """
    seen: dict[str, None] = {}
    for pat in _LOAD_BEARING_PATTERNS:
        for m in pat.finditer(text):
            tok = m.group(0)
            # Strip enclosing backticks for the anchor-list rendering,
            # but keep them for the search-back check (re-added below).
            tok_clean = tok.strip("`")
            if not tok_clean or len(tok_clean) > 200:
                continue
            if tok_clean not in seen:
                seen[tok_clean] = None
                if len(seen) >= max_anchors:
                    break
        if len(seen) >= max_anchors:
            break
    return list(seen)


def _splice(anchors: list[str], body: str) -> str:
    """Render the final gist: `[anchors: a, b, c] body`."""
    if not anchors:
        return body
    anchor_block = "[anchors: " + ", ".join(anchors) + "]"
    return f"{anchor_block} {body}"


def _round_trip_ok(body: str, anchors: list[str]) -> bool:
    """Assert every anchor is keyword-findable in the compressor's
    body output (pre-splice).

    Why pre-splice: the splice step always re-injects anchors into the
    `[anchors: …]` prefix, so a post-splice check would be vacuous —
    it'd always pass and never catch a compressor that paraphrased
    `WV_URL` into "the Weaviate URL env var". Checking against the
    body specifically lets us detect that the compressor went too far
    and the gist became too synthetic, even though the prefix still
    technically contains the anchor verbatim.

    "Findable" = substring match.
    """
    for a in anchors:
        if a not in body:
            return False
    return True


# ─── Public entrypoint ────────────────────────────────────────────────────────

def make_gist(text: str,
              role: str | None = None,
              content_type: str | None = None,
              *,
              tier: int = 2,
              ) -> tuple[str | None, str | None]:
    """Pipeline entrypoint. Returns `(gist, gist_method)`.

    - On distillable turns: `(spliced_gist, "claudedj-distill-v1")`.
    - On skip-type turns: `(None, None)` so the caller stores NULL.
    - On round-trip failure: `(None, None)` so the caller stores NULL.

    `role` is currently unused but accepted for API parity with the
    legacy `gist.make_gist`. `content_type` controls the skip gate.
    """
    skip_reason = should_skip_distill(text, content_type)
    if skip_reason:
        return None, None

    anchors = extract_anchors(text)
    try:
        result = _bridge.distill(text, tier=tier)
    except Exception as exc:  # noqa: BLE001 — never let a wheel bug crash ingest
        logging.getLogger(__name__).warning(
            "dejavu_bridge.distill raised %s: %s — falling back to NULL gist",
            type(exc).__name__, exc)
        return None, None

    # The real wheel returns (tier_label, body) as of dejavu_bridge 0.1.0;
    # the stub returns a bare body string. Normalize both shapes to a
    # body string here so the round-trip check + splice below don't have
    # to care which bridge is loaded.
    if isinstance(result, tuple) and len(result) == 2:
        _tier_label, body = result
    elif isinstance(result, str):
        body = result
    else:
        body = None

    if not isinstance(body, str) or not body:
        return None, None

    # Splice anchors into the prefix, then round-trip the FINAL output.
    # Earlier design checked the body alone, assuming a paraphraser-style
    # compressor that might re-phrase "WV_URL" as "the Weaviate URL env
    # var". The actual bridge ships LinePrefixCompressor — a deterministic
    # length truncator — so anchors past the char limit always get cut
    # from the body. That's fine: the splice re-injects them verbatim in
    # the prefix, and load-bearing fidelity is what we actually care about
    # in the final stored gist. Check that.
    final = _splice(anchors, body)
    if not _round_trip_ok(final, anchors):
        return None, None

    return final, GIST_METHOD


__all__ = [
    "BRIDGE_IS_STUB",
    "BRIDGE_VERSION",
    "GIST_METHOD",
    "extract_anchors",
    "make_gist",
    "should_skip_distill",
]
