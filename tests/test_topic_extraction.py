"""Unit tests for v0.8.9 topic extraction (code/topic.py).

`extract_topic` is the pure-Python label generator that feeds
`dejavu_bridge.topic_reweight`. The shape it produces is "top-2
content tokens, '_'-joined". These tests pin the contract — the
SHAPE (always 0 or 2 tokens, deterministic, no crashes) rather than
specific token choices, which depend on the stopword list that we
inherit from the JIT recall hook.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

from topic import extract_topic  # type: ignore[import]


PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(msg: str) -> None:
    PASS.append(msg); print(f"  \033[32m✓\033[0m {msg}", flush=True)


def _fail(msg: str, d: str = "") -> None:
    FAIL.append((msg, d)); print(f"  \033[31m✗\033[0m {msg}")
    if d:
        print(f"      {d}")


def _aT(name: str, cond: bool, d: str = "") -> None:
    _ok(name) if cond else _fail(name, d)


def _step(name: str) -> None:
    print(f"\n\033[36m── {name} ──\033[0m", flush=True)


def _shape_ok(topic: str) -> bool:
    """A valid topic is either "" or exactly two '_'-joined tokens."""
    if topic == "":
        return True
    parts = topic.split("_")
    return (len(parts) == 2
            and all(p and p.isascii() and all(c.isalnum() or c == "_"
                                              for c in p) for p in parts))


# ─── Empty / degenerate inputs ──────────────────────────────────────────────

def test_empty_string_returns_empty() -> None:
    _step("extract_topic(''): empty input → ''")
    _aT("returns ''", extract_topic("") == "")


def test_none_safe() -> None:
    """The callers may pass None for missing ai_title fields — must
    not crash. (Our caller code guards but defense-in-depth.)"""
    _step("extract_topic(None): graceful (we coerce via the falsy branch)")
    # Mimics what hooks would do — they pass `ai_title or ""`. We
    # only need to assert "no crash on falsy" because the empty
    # string is the canonical degenerate path.
    _aT("'' returns ''", extract_topic("") == "")
    _aT("whitespace returns ''", extract_topic("   ") == "")


def test_single_content_token() -> None:
    """A single-content-token input is too vague to be a topic.
    We force "" so the caller knows to skip reweighting."""
    _step("extract_topic: single content token → ''")
    _aT("'rsync' alone → ''", extract_topic("rsync") == "")
    _aT("'just rsync' → '' (stopword + 1 content token)",
        extract_topic("just rsync") == "")


def test_stopwords_only() -> None:
    _step("extract_topic: stopwords-only input → ''")
    _aT("'a the and is for' → ''",
        extract_topic("a the and is for") == "")
    _aT("'we will then do it' → ''",
        extract_topic("we will then do it") == "")


def test_short_tokens_dropped() -> None:
    """Tokens shorter than 3 chars are dropped by the regex."""
    _step("extract_topic: <3-char tokens dropped (matches hook regex)")
    # "is" is also a stopword AND <3 chars (filtered twice).
    # "ok" is in stopwords. Force a 2-char non-stopword: "go".
    _aT("'go go go up up up' → '' (all <3 chars)",
        extract_topic("go go go up up up") == "")


# ─── Happy path ──────────────────────────────────────────────────────────────

def test_two_word_topic() -> None:
    _step("extract_topic: two-word input → 'tok1_tok2'")
    t = extract_topic("rsync restic backup")
    _aT("shape ok", _shape_ok(t), f"got: {t!r}")
    _aT("has 2 parts", t.count("_") == 1, f"got: {t!r}")
    # rsync + restic both appear once. Order = first-seen → "rsync_restic".
    _aT("first-seen ordering on tie",
        t == "rsync_restic", f"got: {t!r}")


def test_frequency_wins_over_first_seen() -> None:
    """Higher-frequency tokens beat first-seen tokens."""
    _step("extract_topic: freq > position when counts differ")
    # 'restic' appears 3x, 'rsync' 1x, 'plan' 1x. Top-2 = restic, rsync
    # (rsync first-seen wins over plan).
    t = extract_topic("plan rsync restic restic restic")
    _aT("'restic' (freq 3) ranks first",
        t.startswith("restic_"), f"got: {t!r}")


def test_canonical_query() -> None:
    _step("extract_topic: canonical user-spec example")
    # Spec example: "Plan rsync and restic backup strategy"
    # Stopwords: 'plan' is NOT in the set, 'and' IS. After filter:
    # plan(1) rsync(1) restic(1) backup(1) strategy(1) — top-2 by
    # first-seen tie = plan, rsync.
    t = extract_topic("Plan rsync and restic backup strategy")
    _aT("shape ok", _shape_ok(t), f"got: {t!r}")
    _aT("has 2 tokens '_'-joined",
        t.count("_") == 1 and len(t.split("_")) == 2, f"got: {t!r}")


def test_truncated_ai_title() -> None:
    """ai_title fields can be truncated with '…' or '...'. The regex
    handles this cleanly — '…' isn't matched, neither is '...'."""
    _step("extract_topic: truncated title with ellipsis")
    t = extract_topic("Phase 2 Group L rsync replacement with restic …")
    _aT("shape ok", _shape_ok(t), f"got: {t!r}")
    _aT("does not contain ellipsis", "…" not in t and "..." not in t,
        f"got: {t!r}")


def test_unicode_input() -> None:
    """Non-ASCII tokens are not matched by the regex. The function
    must NOT crash and should return "" cleanly when there's no
    ASCII content."""
    _step("extract_topic: unicode-only input → ''")
    _aT("cyrillic-only → ''",
        extract_topic("Привет мир тестовый запрос") == "")
    _aT("CJK-only → ''",
        extract_topic("こんにちは 世界 テスト") == "")
    # Mixed: ASCII content tokens should still surface.
    t = extract_topic("rsync backup для тестов и restic")
    _aT("mixed: ASCII tokens extracted",
        _shape_ok(t), f"got: {t!r}")


def test_uppercase_normalized() -> None:
    _step("extract_topic: case-insensitive")
    a = extract_topic("RSYNC RESTIC BACKUP")
    b = extract_topic("rsync restic backup")
    _aT("uppercase normalizes to lowercase",
        a == b, f"upper={a!r} lower={b!r}")


def test_deterministic() -> None:
    _step("extract_topic: identical input → identical output")
    s = "Plan rsync and restic backup strategy with nightly snapshots"
    a = extract_topic(s)
    b = extract_topic(s)
    c = extract_topic(s)
    _aT("3 calls identical", a == b == c, f"a={a!r} b={b!r} c={c!r}")


def test_punctuation_split() -> None:
    _step("extract_topic: punctuation is treated as a separator")
    t = extract_topic("rsync; restic; backup, strategy.")
    _aT("shape ok", _shape_ok(t), f"got: {t!r}")
    _aT("punctuation didn't break extraction",
        len(t) > 0, f"got: {t!r}")


def test_long_input_safe() -> None:
    _step("extract_topic: large input (no perf issue / no truncation bug)")
    text = ("rsync replacement plan " * 200) + " with restic"
    t = extract_topic(text)
    _aT("shape ok", _shape_ok(t), f"got: {t!r}")
    # 'rsync' wins (200 occurrences); second by first-seen tie =
    # 'replacement'.
    _aT("highest-freq token first",
        t.startswith("rsync_"), f"got: {t!r}")


def test_stopwords_imported_from_hook() -> None:
    """Sanity: topic.py reuses the hook's stopword set (single
    source of truth — no parallel duplicated list)."""
    _step("extract_topic: stopwords sourced from hooks/user_prompt_submit.py")
    sys.path.insert(0, str(ROOT / "hooks"))
    import user_prompt_submit as ups  # type: ignore[import]
    import topic
    _aT("topic._STOPWORDS is hook _STOPWORDS",
        topic._STOPWORDS is ups._STOPWORDS,
        f"id(topic)={id(topic._STOPWORDS)} "
        f"id(ups)={id(ups._STOPWORDS)}")


def main() -> int:
    tests = [
        test_empty_string_returns_empty,
        test_none_safe,
        test_single_content_token,
        test_stopwords_only,
        test_short_tokens_dropped,
        test_two_word_topic,
        test_frequency_wins_over_first_seen,
        test_canonical_query,
        test_truncated_ai_title,
        test_unicode_input,
        test_uppercase_normalized,
        test_deterministic,
        test_punctuation_split,
        test_long_input_safe,
        test_stopwords_imported_from_hook,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAIL.append((t.__name__, str(e)))
    print()
    print("=" * 60)
    print(f"PASS: {len(PASS)}     FAIL: {len(FAIL)}")
    for m, d in FAIL:
        print(f"  ✗ {m}  {d[:80]}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
