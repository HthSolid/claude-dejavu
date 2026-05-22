"""Unit tests for the markdown chunker."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

PASS, FAIL = [], []
def _ok(m): PASS.append(m); print(f"  \033[32m✓\033[0m {m}", flush=True)
def _f(m, d=""): FAIL.append((m, d)); print(f"  \033[31m✗\033[0m {m}"); d and print(f"      {d}")
def _step(n): print(f"\n\033[36m── {n} ──\033[0m", flush=True)
def _aT(n, c, d=""): _ok(n) if c else _f(n, d)


FIXTURES = ROOT / "tests" / "fixtures" / "docs"


def test_walk_headings_single_h1():
    _step("walk_headings: single h1 produces one section")
    from doc_indexer import walk_headings
    text = (FIXTURES / "single_h1.md").read_text()
    sections = list(walk_headings(text))
    _aT("one section", len(sections) == 1, str(sections))
    _aT("heading_path == 'Title'",
         sections[0].heading_path == "Title", sections[0].heading_path)


def test_walk_headings_nested():
    _step("walk_headings: nested headings build hierarchical paths")
    from doc_indexer import walk_headings
    text = (FIXTURES / "nested.md").read_text()
    paths = [s.heading_path for s in walk_headings(text)]
    _aT("four sections",
         len(paths) == 4, str(paths))
    _aT("paths include 'Top > Section A > Subsection A.1'",
         "Top > Section A > Subsection A.1" in paths, str(paths))


def test_walk_headings_no_heading():
    _step("walk_headings: file without headings yields one synthetic section")
    from doc_indexer import walk_headings
    text = (FIXTURES / "no_heading.md").read_text()
    sections = list(walk_headings(text))
    _aT("one synthetic section", len(sections) == 1)
    _aT("heading_path is '(no heading)'",
         sections[0].heading_path == "(no heading)", sections[0].heading_path)


def test_walk_headings_empty():
    _step("walk_headings: empty file yields no sections")
    from doc_indexer import walk_headings
    text = (FIXTURES / "empty.md").read_text()
    _aT("zero sections", list(walk_headings(text)) == [])


def test_walk_headings_keeps_code_fences_in_section():
    _step("walk_headings: fenced code inside a section stays in that section")
    from doc_indexer import walk_headings
    text = (FIXTURES / "code_fence.md").read_text()
    sections = list(walk_headings(text))
    _aT("one section", len(sections) == 1)
    _aT("section body contains def foo()",
         "def foo()" in sections[0].body, sections[0].body[:200])


def test_chunk_file_yields_chunks_with_summary_and_hash():
    _step("chunk_file emits Chunk objects with one_line_summary + content_hash")
    from doc_indexer import chunk_file
    chunks = list(chunk_file(FIXTURES / "nested.md"))
    _aT("got 4 chunks", len(chunks) == 4, str([c.heading_path for c in chunks]))
    first = chunks[0]
    _aT("first chunk has heading_path",
         first.heading_path == "Top", first.heading_path)
    _aT("one_line_summary starts with heading + ' — '",
         first.one_line_summary.startswith("Top — "),
         first.one_line_summary)
    _aT("one_line_summary contains first non-blank line of body",
         "Intro paragraph." in first.one_line_summary,
         first.one_line_summary)
    _aT("content_hash is a hex string",
         len(first.content_hash) == 64 and all(c in "0123456789abcdef"
                                                  for c in first.content_hash))
    _aT("chunk_idx is sequential 0..N-1",
         [c.chunk_idx for c in chunks] == [0, 1, 2, 3])


def test_chunk_file_sub_splits_huge_section():
    _step("chunk_file sub-splits a section larger than 1500 tokens (~6000 chars)")
    import tempfile
    from doc_indexer import chunk_file
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "huge.md"
        body_para = "Lorem ipsum dolor sit amet. " * 50  # ~1500 chars per para
        # 6 paragraphs = ~9000 chars ≈ 2250 tokens → must sub-split.
        text = "# Huge\n\n" + "\n\n".join(body_para for _ in range(6))
        path.write_text(text)
        chunks = list(chunk_file(path))
        _aT("got 2+ chunks for the huge section", len(chunks) >= 2,
             f"got {len(chunks)} chunks")
        suffixes = [c.heading_path for c in chunks]
        _aT("sub-split chunks share a heading_path",
             all(p.startswith("Huge") for p in suffixes), str(suffixes))


def test_chunk_file_empty_yields_nothing():
    _step("chunk_file on empty file yields no chunks")
    from doc_indexer import chunk_file
    _aT("zero chunks", list(chunk_file(FIXTURES / "empty.md")) == [])


def test_walk_repo_excludes_dejavuignore_paths():
    _step("walk_repo_for_md respects .dejavuignore + built-in defaults")
    import tempfile
    from doc_indexer import walk_repo_for_md
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "docs").mkdir()
        (root / "docs" / "good.md").write_text("# good\n")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "bad.md").write_text("# bad\n")
        (root / ".dejavu").mkdir()
        (root / ".dejavu" / "cache.md").write_text("# also bad\n")
        (root / "custom_skip").mkdir()
        (root / "custom_skip" / "ignored.md").write_text("# user-ignored\n")
        (root / ".dejavuignore").write_text("custom_skip/\n")

        paths = sorted(str(p.relative_to(root)) for p in walk_repo_for_md(root))
        _aT("only docs/good.md found", paths == ["docs/good.md"],
             str(paths))


def main() -> int:
    tests = [test_walk_headings_single_h1,
              test_walk_headings_nested,
              test_walk_headings_no_heading,
              test_walk_headings_empty,
              test_walk_headings_keeps_code_fences_in_section,
              test_chunk_file_yields_chunks_with_summary_and_hash,
              test_chunk_file_sub_splits_huge_section,
              test_chunk_file_empty_yields_nothing,
              test_walk_repo_excludes_dejavuignore_paths]
    for t in tests:
        try: t()
        except Exception as e:
            import traceback; traceback.print_exc()
            FAIL.append((t.__name__, str(e)))
    print(f"\n{'='*60}\nPASS: {len(PASS)}  FAIL: {len(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
