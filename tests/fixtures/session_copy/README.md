# session_copy fixture

Throwaway-source fixtures for `tests/test_session_copy.py`. The tests
build their sources programmatically in tmpdirs (a fake `.claude/projects/`
shaped dir and a tiny `claude-mem.db`) so committed fixtures here are
minimal.

Files in this dir:

- `projects/-fake-proj/<uuid>.jsonl` — example session JSONL shape.

The tests do not depend on the on-disk fixture content; this dir exists
to keep the layout discoverable.
