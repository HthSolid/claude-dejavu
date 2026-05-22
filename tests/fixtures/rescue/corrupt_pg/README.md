# Synthetic PG corruption fixtures

PG corruption can't be reliably synthesized inside a sandbox (you'd
need to corrupt the actual on-disk heap files of a running Postgres
cluster). The `test_pg_rescue.py` suite therefore uses a `FakeConn`
/ `FakeCursor` mock that simulates `length()` / `substring()` raising
on TOAST corruption — the same observable error mode the rescue
session encountered on 2026-05-14.

If you need a higher-fidelity local repro, run a throwaway Docker
Postgres, insert ~10k rows with large `content` values, then
manually `dd` zeros over a known TOAST chunk on the host volume.
That's outside the unit-test scope and lives in the
`docs/superpowers/specs/2026-05-16-pg-weaviate-rescue-toolkit-design.md`
testing notes for integration runs.

The canonical bad-id list from the original incident is documented
in `/tmp/patch-pg-toast-corruption.py` (66 rows in two clusters).
