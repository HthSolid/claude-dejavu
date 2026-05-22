# migrate_rsync fixture

Synthetic legacy rsync-chain for testing
`code/migrate_rsync.py` (H4 in v0.8.0 plan).

## Layout

```
snapshots/
├── 20260101-000000/   # OLDEST — contains an extra at-risk session UUID
│   └── .claude/projects/-fake-proj/
│       ├── 00000000-0000-0000-0000-000000000001.jsonl  (common)
│       ├── 00000000-0000-0000-0000-000000000002.jsonl  (common)
│       └── 42bc895b-f449-4c5a-ac78-2a1d3fb28fd5.jsonl  (at-risk — only here)
├── 20260101-000500/   # mid
│   └── .claude/projects/-fake-proj/
│       ├── 00000000-0000-0000-0000-000000000001.jsonl
│       └── 00000000-0000-0000-0000-000000000002.jsonl
└── 20260101-001000/   # NEWEST
    └── .claude/projects/-fake-proj/
        ├── 00000000-0000-0000-0000-000000000001.jsonl
        └── 00000000-0000-0000-0000-000000000002.jsonl
pg_state.json — fake DB state for the cursor mock:
    sessions row for `00000000-...-000000000001` with 5 turns (safe, has PG copy)
    sessions row for `00000000-...-000000000002` with 0 turns (at-risk — zero-turns)
    NO row for `42bc895b-...` (at-risk — not in PG at all)

Net at-risk after Step 2 = {42bc895b-..., 00000000-...0002}
```
